#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 BOSS 自动化产生的过期缓存文件。

清理范围：
  1. runs/<run_id>/ 目录（每次任务的中间产物：简历 JSON、筛选结果、报告 HTML）
     —— 保留最近 N 天，默认 7 天
  2. state/ 目录下的 resumes_master.json（累计简历缓存）
     —— 超过 N 天的条目按 _meta.downloaded_at 清理

不清理：
  - data/hr_agent.db（业务数据库，独立保留）
  - logs/（日志独立保留策略）
  - state/candidate_pool.json、download_state.json（跨 run 去重状态，体积小）

用法：
  # 默认清理 7 天前的 run 目录和简历缓存
  python tools/cleanup_cache.py

  # 指定保留天数
  python tools/cleanup_cache.py --days 14

  # 只看不删（dry-run）
  python tools/cleanup_cache.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 让脚本能直接运行：把项目根目录加到 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared"))

try:
    from env_loader import load_project_dotenv
    load_project_dotenv()
except Exception:
    pass

# 输出根目录（与 output_manager.py 保持一致）
OUTPUT_ROOT = os.environ.get("BOSS_HR_OUTPUT_DIR") or str(ROOT / "data" / "runs")


def _log(msg: str, *, dry_run: bool = False) -> None:
    prefix = "[cleanup]" + (" [DRY-RUN]" if dry_run else "")
    print(f"{prefix} {msg}", flush=True)


def cleanup_runs(output_root: str, days: int, *, dry_run: bool = False) -> tuple[int, int]:
    """清理 runs/<run_id>/ 目录，返回 (删除数, 跳过数)。

    run_id 格式：2026-09-08_104715 —— 直接从目录名解析日期。
    """
    cutoff = datetime.now() - timedelta(days=days)
    deleted = 0
    skipped = 0

    if not os.path.isdir(output_root):
        _log(f"输出目录不存在：{output_root}")
        return 0, 0

    for job_dir_name in os.listdir(output_root):
        job_dir = os.path.join(output_root, job_dir_name)
        if not os.path.isdir(job_dir):
            continue
        runs_dir = os.path.join(job_dir, "runs")
        if not os.path.isdir(runs_dir):
            continue

        for run_name in os.listdir(runs_dir):
            run_dir = os.path.join(runs_dir, run_name)
            if not os.path.isdir(run_dir):
                continue

            # 从 run_id 解析日期：2026-09-08_104715 → 2026-09-08
            date_str = run_name.split("_")[0] if "_" in run_name else run_name[:10]
            try:
                run_date = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                # 非标准 run_id，用目录修改时间兜底
                try:
                    mtime = os.path.getmtime(run_dir)
                    run_date = datetime.fromtimestamp(mtime)
                except OSError:
                    skipped += 1
                    continue

            if run_date < cutoff:
                size = _dir_size(run_dir)
                _log(f"删除 run: {job_dir_name}/{run_name} ({_fmt_size(size)})", dry_run=dry_run)
                if not dry_run:
                    try:
                        shutil.rmtree(run_dir)
                        deleted += 1
                    except OSError as exc:
                        _log(f"  删除失败: {exc}")
                else:
                    deleted += 1
            else:
                skipped += 1

    return deleted, skipped


def cleanup_resumes_master(output_root: str, days: int, *, dry_run: bool = False) -> int:
    """清理 state/resumes_master.json 中超过 N 天的简历条目。

    每条简历有 _meta.downloaded_at（格式 2026-09-08_104715），
    超过 cutoff 的条目从 JSON 删除，保留剩余条目。
    """
    cutoff = datetime.now() - timedelta(days=days)
    cleaned = 0

    if not os.path.isdir(output_root):
        return 0

    for job_dir_name in os.listdir(output_root):
        job_dir = os.path.join(output_root, job_dir_name)
        if not os.path.isdir(job_dir):
            continue
        state_dir = os.path.join(job_dir, "state")
        if not os.path.isdir(state_dir):
            continue

        master_path = os.path.join(state_dir, "resumes_master.json")
        if not os.path.isfile(master_path):
            continue

        try:
            with open(master_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            _log(f"读取失败 {master_path}: {exc}")
            continue

        if not isinstance(data, dict):
            continue

        items = data.get("items") or {}
        if not isinstance(items, dict) or not items:
            continue

        kept: dict = {}
        removed_count = 0
        for key, item in items.items():
            meta = item.get("_meta") or {}
            downloaded_at = str(meta.get("downloaded_at") or "")
            try:
                dl_date = datetime.strptime(
                    downloaded_at.split("_")[0] if "_" in downloaded_at else downloaded_at[:10],
                    "%Y-%m-%d",
                )
            except (ValueError, IndexError):
                # 无法解析日期，保守保留
                kept[key] = item
                continue

            if dl_date < cutoff:
                removed_count += 1
            else:
                kept[key] = item

        if removed_count == 0:
            continue

        _log(
            f"清理简历缓存: {job_dir_name}/state/resumes_master.json "
            f"({removed_count} 条 > {days} 天)",
            dry_run=dry_run,
        )
        cleaned += removed_count

        if not dry_run:
            data["items"] = kept
            tmp_path = master_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, master_path)
            except OSError as exc:
                _log(f"  写回失败: {exc}")

    return cleaned


def _dir_size(path: str) -> int:
    """计算目录总大小（字节）。"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                total += os.path.getsize(fpath)
            except OSError:
                pass
    return total


def _fmt_size(size: int) -> str:
    """字节 → 人类可读。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 BOSS 自动化过期缓存文件。")
    parser.add_argument("--days", type=int, default=7, help="保留天数，默认 7 天")
    parser.add_argument("--dry-run", action="store_true", help="只统计不删除")
    parser.add_argument("--output-dir", default=OUTPUT_ROOT, help=f"输出根目录（默认 {OUTPUT_ROOT}）")
    args = parser.parse_args(argv)

    _log(f"输出目录: {args.output_dir}")
    _log(f"保留天数: {args.days}")
    _log(f"模式: {'DRY-RUN（只看不删）' if args.dry_run else '实际清理'}")
    _log("")

    # 1. 清理过期 run 目录
    _log("=== 清理 runs 目录 ===")
    runs_deleted, runs_skipped = cleanup_runs(args.output_dir, args.days, dry_run=args.dry_run)
    _log(f"runs 目录：删除 {runs_deleted} 个，保留 {runs_skipped} 个")
    _log("")

    # 2. 清理 resumes_master.json 中的过期简历
    _log("=== 清理 resumes_master.json ===")
    resumes_cleaned = cleanup_resumes_master(args.output_dir, args.days, dry_run=args.dry_run)
    _log(f"resumes_master：清理 {resumes_cleaned} 条过期简历")
    _log("")

    _log(
        f"完成：共删除 {runs_deleted} 个 run 目录，清理 {resumes_cleaned} 条过期简历",
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
