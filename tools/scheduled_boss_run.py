#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one scheduled BOSS screening + greeting cycle.

This script is intended to be launched by Windows Task Scheduler. It performs
one bounded run, then exits:
  sync-jobs（拉取 BOSS 最新岗位名/ID 并写回 .env）-> start -> confirm -> automate
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "shared"
for path in (ROOT, SHARED):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from env_loader import load_project_dotenv  # noqa: E402

load_project_dotenv()

from boss_hr.application.automation_service import run_automation  # noqa: E402
from boss_hr.application.confirm_service import confirm_run  # noqa: E402
from boss_hr.application.notification_service import (  # noqa: E402
    notification_results_to_dict,
    notify_scheduler_event,
)
from boss_hr.application.start_service import start_new_run  # noqa: E402
from boss_hr.config_manager import get_config  # noqa: E402
from boss_hr.job_id_resolver import env_key_for_job, resolve_encrypt_job_id  # noqa: E402


def _setting(config, name: str, default):
    return config.get(f"app.scheduler.{name}", default)


def _default_job_name(config) -> str:
    configured = config.default_job_profile_name()
    return str(configured or config.get("app.default_job_name", "") or "").strip()


def _resolve_encrypt_job_id_for_job(
    job_name: str,
    cli_value: str | None,
    *,
    multi_job: bool = False,
) -> str:
    """解析当前岗位 ID。

    多岗位模式必须使用各岗位独立的 ``BOSS_JOB_ID_*``，禁止回退到全局
    ``BOSS_HR_ENCRYPT_JOB_ID``，否则岗位轮换时多个岗位会复用同一 ID。
    """
    return str(
        resolve_encrypt_job_id(
            cli_value=cli_value,
            job_name=job_name,
            allow_legacy_default=not multi_job,
        ) or ""
    ).strip()


def _claim_job_id(
    owners: dict[str, str], job_name: str, encrypt_job_id: str
) -> tuple[bool, str]:
    """在一次多岗位任务内声明岗位 ID，阻止两个岗位复用同一 ID。"""
    owner = owners.get(encrypt_job_id)
    if owner and owner != job_name:
        return False, owner
    owners[encrypt_job_id] = job_name
    return True, job_name


# ============ 重试池管理（跨 run 保留未招呼成功的通过者）============

def _retry_pool_path(encrypt_job_id: str, job_name: str) -> str:
    """获取岗位的重试池文件路径：state/retry_pool.json"""
    from job_registry import resolve_job_dir
    job_dir = resolve_job_dir(encrypt_job_id, name=job_name)
    return os.path.join(job_dir, "state", "retry_pool.json")


def _uses_live_card_flow(payload: dict) -> bool:
    """识别新的一卡一闭环结果，禁止再进入旧批量重试招呼。"""
    try:
        automation = payload.get("automation") or {}
        workflow_data = automation.get("data") or payload.get("data") or {}
        fetch_wrapper = workflow_data.get("fetch") or {}
        fetch_result = fetch_wrapper.get("fetch") or fetch_wrapper
        fetch_data = fetch_result.get("data") or {}
        return bool(fetch_data.get("live_card_flow"))
    except (AttributeError, TypeError):
        return False


def _terminal_card_count(payload: dict, fallback: int = 0) -> int:
    """读取一次 fetch 真正取得终态的 DOM 卡片数，禁止用请求数冒充。"""
    try:
        automation = payload.get("automation") or {}
        workflow_data = automation.get("data") or payload.get("data") or {}
        fetch_wrapper = workflow_data.get("fetch") or {}
        fetch_result = fetch_wrapper.get("fetch") or fetch_wrapper
        fetch_data = fetch_result.get("data") or {}
        value = fetch_data.get("terminal_card_count")
        if value is None:
            value = fetch_data.get("listed_count")
        return max(0, int(value if value is not None else fallback))
    except (AttributeError, TypeError, ValueError):
        return max(0, int(fallback or 0))


def _read_retry_pool(encrypt_job_id: str, job_name: str) -> list[dict]:
    """读取重试池候选人列表。"""
    path = _retry_pool_path(encrypt_job_id, job_name)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("candidates", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _write_retry_pool(encrypt_job_id: str, job_name: str, candidates: list[dict]) -> None:
    """写入重试池。"""
    path = _retry_pool_path(encrypt_job_id, job_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidates": candidates,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _extract_and_save_retry_candidates(
    payload: dict, encrypt_job_id: str, job_name: str, run_id: str,
) -> int:
    """从自动化结果中提取需要重试的候选人，合并写入重试池。

    提取规则：
      1) screening_results.json 中 passed=True 但 greet_log.json 中 status != greeted
      2) 配额截断：passed 但未出现在 greet_log 中（max_count 截断）

    返回重试池当前总人数。
    """
    from output_manager import JobOutputManager

    out = JobOutputManager(job_name, encrypt_job_id=encrypt_job_id, run_id=run_id, lazy=True)
    screen_path = os.path.join(out.process_dir, "screening_results.json")
    greet_log_path = os.path.join(out.process_dir, "greet_log.json")

    # 读取 screening_results 中的通过者
    passed = []  # [{name, gid}, ...]
    if os.path.exists(screen_path):
        try:
            with open(screen_path, encoding="utf-8") as f:
                sr = json.load(f)
            for c in sr.get("candidates", []):
                if c.get("passed") or c.get("recommended"):
                    passed.append({
                        "name": (c.get("name") or "").strip(),
                        "encrypt_geek_id": c.get("encrypt_geek_id") or c.get("geek_id") or "",
                    })
        except Exception:
            pass

    if not passed:
        return len(_read_retry_pool(encrypt_job_id, job_name))

    # 读取 greet_log 中已打招呼的 gid 集合
    greeted_gids: set[str] = set()
    if os.path.exists(greet_log_path):
        try:
            with open(greet_log_path, encoding="utf-8") as f:
                gl = json.load(f)
            for r in gl.get("results", []):
                if ((r.get("status") == "greeted" and r.get("message_sent"))
                        or r.get("status") == "already_contacted"):
                    gid = r.get("encrypt_geek_id") or r.get("target_encrypt_geek_id") or ""
                    if gid:
                        greeted_gids.add(gid)
        except Exception:
            pass

    # 提取未打招呼的通过者
    new_retry = []
    for p in passed:
        gid = p["encrypt_geek_id"]
        if gid and gid in greeted_gids:
            continue  # 已成功打招呼
        new_retry.append({
            "name": p["name"],
            "encrypt_geek_id": gid,
            "source_run_id": run_id,
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fail_count": 0,
        })

    if not new_retry:
        return len(_read_retry_pool(encrypt_job_id, job_name))

    # 合并到现有重试池（按 encrypt_geek_id 去重）
    existing = _read_retry_pool(encrypt_job_id, job_name)
    existing_gids = {c.get("encrypt_geek_id") for c in existing if c.get("encrypt_geek_id")}
    for c in new_retry:
        gid = c.get("encrypt_geek_id")
        if gid and gid in existing_gids:
            continue  # 已在重试池中
        if not gid and c["name"] in {e["name"] for e in existing}:
            continue  # 同名也在池中（掩码名可能重复，但无 gid 无法区分）
        existing.append(c)

    _write_retry_pool(encrypt_job_id, job_name, existing)
    return len(existing)


def _remove_greeted_from_retry(
    encrypt_job_id: str, job_name: str, greeted_names: list[str],
    greeted_gids: list[str] | None = None,
) -> int:
    """从重试池中移除已打招呼的候选人。优先按 gid 移除，无 gid 时按名字。"""
    pool = _read_retry_pool(encrypt_job_id, job_name)
    if not pool:
        return 0
    greeted_set = {n.strip() for n in greeted_names if n}
    greeted_gid_set = {g.strip() for g in greeted_gids if g} if greeted_gids else set()
    remaining = []
    for c in pool:
        gid = (c.get("encrypt_geek_id") or "").strip()
        name = (c.get("name") or "").strip()
        if gid and gid in greeted_gid_set:
            continue
        if not gid and name and name in greeted_set:
            continue
        remaining.append(c)
    _write_retry_pool(encrypt_job_id, job_name, remaining)
    return len(remaining)


# 连续 not_found 达到此值后自动移除
_RETRY_FAIL_THRESHOLD = 3


def _increment_retry_fail_counts(
    encrypt_job_id: str, job_name: str, failed_names: list[str],
) -> tuple[int, int]:
    """对重试池中招呼失败的候选人递增 fail_count，达到阈值后移除。

    返回 (剩余人数, 移除人数)。
    """
    pool = _read_retry_pool(encrypt_job_id, job_name)
    if not pool:
        return 0, 0
    failed_set = {n.strip() for n in failed_names if n}
    remaining = []
    removed = 0
    changed = False
    for c in pool:
        name = (c.get("name") or "").strip()
        if name in failed_set:
            c["fail_count"] = c.get("fail_count", 0) + 1
            changed = True
            if c["fail_count"] >= _RETRY_FAIL_THRESHOLD:
                removed += 1
                continue
        remaining.append(c)
    if changed:
        _write_retry_pool(encrypt_job_id, job_name, remaining)
    return len(remaining), removed


def _scheduled_job_names(config) -> list[str]:
    values = config.get("app.scheduler.job_names", []) or []
    if not isinstance(values, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _scheduled_jobs(config) -> list[dict[str, Any]]:
    values = config.get("app.scheduler.jobs", []) or []
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str):
                item = {"name": value}
            elif isinstance(value, dict):
                item = dict(value)
            else:
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            item["name"] = name
            jobs.append(item)
    if jobs:
        return jobs
    return [{"name": name} for name in _scheduled_job_names(config)]


def _int_setting(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    return int(value)


def _emit(payload: dict, exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)


def _scheduled_status_paths() -> tuple[Path, Path, Path]:
    started = datetime.now()
    run_key = started.strftime("%Y%m%d_%H%M%S")
    log_dir = ROOT / "logs" / "scheduled"
    status_dir = ROOT / "data" / "scheduler"
    log_path = log_dir / f"{run_key}.log"
    history_path = status_dir / f"{run_key}.json"
    latest_path = status_dir / "latest_run.json"
    return log_path, history_path, latest_path


def _append_run_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{_now_text()}] {message}\n")


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _finish_scheduler_run(
    *,
    payload: dict,
    exit_code: int,
    started_at: str,
    log_path: Path,
    history_path: Path,
    latest_path: Path,
    config,
    start_notifications: list[dict[str, Any]] | None = None,
) -> int:
    finished_at = _now_text()
    stage = payload.get("stage") or payload.get("status") or ""
    end_message = _format_end_message(payload, exit_code, started_at, finished_at)
    is_daemon = stage == "daemon_complete"
    end_title = f"BOSS 自动化结束（{'daemon' if is_daemon else '单轮'}）：{'成功' if payload.get('ok') else '异常'}"
    end_notifications = notify_scheduler_event(
        "end",
        end_title,
        end_message,
        config=config,
    )
    for item in end_notifications:
        status = "SKIP" if item.skipped else ("OK" if item.ok else "FAIL")
        _append_run_log(log_path, f"notify end {item.channel} {status} {item.reason}")
    status_payload = {
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "ok": bool(payload.get("ok")),
        "stage": stage,
        "summary": payload,
        "log_file": str(log_path),
        "history_file": str(history_path),
        "notifications": {
            "start": start_notifications or [],
            "end": notification_results_to_dict(end_notifications),
        },
    }
    payload["scheduler_started_at"] = started_at
    payload["scheduler_finished_at"] = finished_at
    payload["scheduler_log_file"] = str(log_path)
    payload["scheduler_latest_file"] = str(latest_path)
    payload["scheduler_history_file"] = str(history_path)
    payload["notifications"] = status_payload["notifications"]

    _write_status(history_path, status_payload)
    _write_status(latest_path, status_payload)
    _append_run_log(
        log_path,
        f"END ok={status_payload['ok']} exit_code={exit_code} "
        f"stage={stage}",
    )
    return _emit(payload, exit_code)


def _format_job_specs(job_specs: list[dict[str, Any]], default_count: int, default_max: int) -> str:
    parts = []
    for spec in job_specs:
        name = str(spec.get("name") or "")
        count = _int_setting(spec.get("count"), default_count)
        max_greetings = _int_setting(spec.get("max_greetings"), default_max)
        if count <= 0 or max_greetings <= 0:
            parts.append(f"{name}: 跳过")
        else:
            parts.append(f"{name}: 抓取{count}/招呼{max_greetings}")
    return "；".join(parts)


def _automation_metrics(payload: dict) -> tuple[str, str, str, str]:
    automation_data = (
        ((payload.get("automation") or {}).get("data") or {})
        if isinstance(payload.get("automation"), dict)
        else {}
    )
    fetch_data = (((automation_data.get("fetch") or {}).get("fetch") or {}).get("data") or {})
    analysis_data = automation_data.get("analysis") or {}
    greet_data = (((automation_data.get("greet") or {}).get("greet") or {}).get("data") or {})
    feishu_data = automation_data.get("feishu") or {}
    return (
        str(fetch_data.get("downloaded_count", "")),
        str(analysis_data.get("analyzed", "")),
        str(greet_data.get("greeted", "")),
        str(feishu_data.get("total", "")),
    )


def _format_end_message(payload: dict, exit_code: int, started_at: str, finished_at: str) -> str:
    lines = [
        f"开始：{started_at}",
        f"结束：{finished_at}",
        f"退出码：{exit_code}",
    ]
    # daemon 模式专用汇总：显示总轮数、总招呼数和各岗位明细
    if payload.get("stage") == "daemon_complete":
        summary = payload.get("daemon_summary") or {}
        boss_acct = summary.get("boss_account_name") or ""
        lines.append(f"BOSS账号：{boss_acct}" if boss_acct else "BOSS账号：未获取")
        lines.append(f"总轮数：{payload.get('rounds', 0)}")
        lines.append(f"总招呼数：{summary.get('total_greeted', 0)}")
        per_job = summary.get("per_job_greeted") or {}
        for jn, cnt in per_job.items():
            lines.append(f"  {jn}: {cnt} 人")
        completed = summary.get("completed_jobs") or []
        if completed:
            lines.append(f"已达标岗位：{'、'.join(completed)}")
        lines.append(f"结果文件：{ROOT / 'data' / 'scheduler' / 'latest_run.json'}")
        return "\n".join(lines)
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if item.get("stage") == "skipped":
                lines.append(f"{item.get('job_name')}: 已跳过")
                continue
            fetched, analyzed, greeted, feishu_total = _automation_metrics(item)
            lines.append(
                f"{item.get('job_name')}: run_id={item.get('run_id', '')} "
                f"抓取={fetched} 分析={analyzed} 招呼={greeted} 飞书={feishu_total}"
            )
    else:
        fetched, analyzed, greeted, feishu_total = _automation_metrics(payload)
        lines.append(
            f"{payload.get('job_name', '')}: run_id={payload.get('run_id', '')} "
            f"抓取={fetched} 分析={analyzed} 招呼={greeted} 飞书={feishu_total}"
        )
    lines.append(f"结果文件：{ROOT / 'data' / 'scheduler' / 'latest_run.json'}")
    return "\n".join(lines)


def _sync_jobs(cdp_url: str = "http://localhost:9222") -> Any:
    """同步 BOSS 岗位信息（独立包装便于测试 monkeypatch）。

    成功时把 .env 的 BOSS_JOB_NAME_* / BOSS_JOB_ID_* 更新同步到当前进程
    os.environ，后续 resolve_encrypt_job_id / start 直接用最新值。
    """
    from boss_hr.application.sync_jobs_service import run_sync_jobs

    return run_sync_jobs(cdp_url=cdp_url)


def _extract_run_id(payload) -> str:
    if not payload.ok:
        return ""
    return str(payload.run_id or "")


def _run_one_job(
    *,
    job_name: str,
    encrypt_job_id: str,
    count: int,
    max_greetings: int,
    dry_run: bool,
    sync_feishu: bool,
    skip_fetch: bool = False,
    only_names: str | None = None,
    greet_only: bool = False,
    existing_run_id: str | None = None,
    refresh_page: bool = False,
) -> tuple[dict, int]:
    # greet_only 模式：复用已有 run_id，跳过 start/confirm/fetch/analysis
    if greet_only:
        run_id = existing_run_id or ""
        if not run_id:
            return {
                "ok": False,
                "stage": "greet_only",
                "job_name": job_name,
                "encrypt_job_id": encrypt_job_id,
                "message": "greet_only 模式需要 existing_run_id",
            }, 1
        automation = run_automation(
            job_name=job_name,
            encrypt_job_id=encrypt_job_id,
            run_id=run_id,
            count=count,
            max_greetings=max_greetings,
            dry_run=dry_run,
            sync_feishu=sync_feishu,
            only_names=only_names,
            greet_only=True,
            refresh_page=refresh_page,
        )
        return {
            "ok": automation.ok,
            "stage": "greet_only_complete" if automation.ok else "greet_only",
            "run_id": run_id,
            "job_name": job_name,
            "encrypt_job_id": encrypt_job_id,
            "dry_run": dry_run,
            "sync_feishu": sync_feishu,
            "automation": automation.to_dict("automate"),
        }, int(automation.exit_code)

    start = start_new_run(
        query=job_name,
        job_name=job_name,
        encrypt_job_id=encrypt_job_id,
        login_wait_seconds=0,
    )
    run_id = _extract_run_id(start)
    if not start.ok or not run_id:
        return {
            "ok": False,
            "stage": "start",
            "job_name": job_name,
            "encrypt_job_id": encrypt_job_id,
            "start": start.to_dict("start"),
            "message": "定时任务未能创建 run；通常是 BOSS 未登录或浏览器环境未就绪。",
        }, int(start.exit_code) or 1

    confirm = confirm_run(
        job_name=job_name,
        encrypt_job_id=encrypt_job_id,
        run_id=run_id,
    )
    if not confirm.ok:
        return {
            "ok": False,
            "stage": "confirm",
            "job_name": job_name,
            "encrypt_job_id": encrypt_job_id,
            "run_id": run_id,
            "confirm": confirm.to_dict("confirm"),
        }, int(confirm.exit_code) or 1

    automation = run_automation(
        job_name=job_name,
        encrypt_job_id=encrypt_job_id,
        run_id=run_id,
        count=count,
        max_greetings=max_greetings,
        dry_run=dry_run,
        sync_feishu=sync_feishu,
        skip_fetch=skip_fetch,
        only_names=only_names,
        refresh_page=refresh_page,
    )
    return {
        "ok": automation.ok,
        "stage": "complete" if automation.ok else "automation",
        "run_id": run_id,
        "job_name": job_name,
        "encrypt_job_id": encrypt_job_id,
        "dry_run": dry_run,
        "sync_feishu": sync_feishu,
        "automation": automation.to_dict("automate"),
    }, int(automation.exit_code)


def _to_dt_time(value: Any) -> dt_time:
    """把 YAML 解析后的值转成 dt_time。

    兼容三种 PyYAML 解析结果：
      - 字符串 ``"09:00"`` → 直接 split
      - 整数 840（六十进制，14*60）→ 14:00
      - datetime.time 对象 → 原样返回

    ``"24:00"`` 会被转成 23:59:59（当天最后一刻），因为 dt_time 的 hour 范围是 0-23。
    """
    if isinstance(value, dt_time):
        return value
    text = str(value)
    if ":" in text:
        h, m = text.split(":")
        hour = int(h)
        minute = int(m)
        # 24:00 不是合法的 time 对象，用 23:59:59 兜底
        if hour >= 24:
            return dt_time(23, 59, 59)
        return dt_time(hour, minute)
    # 无冒号 → PyYAML 六十进制整数（如 840 = 14*60）
    total = int(text)
    hour = total // 60
    if hour >= 24:
        return dt_time(23, 59, 59)
    return dt_time(hour, total % 60)


def _parse_end_time(config, job_specs: list[dict] | None = None) -> dt_time | None:
    """计算 daemon 退出时间。

    优先取 app.yaml 中所有岗位 time_window 里最晚的 end 作为退出边界
    （确保多时段场景下 daemon 能跑到最晚时段结束）。
    若 job_specs 为空或无 time_window，回退到 recruitment.work_time.end。
    都未配置时默认 18:00。
    """
    # 1. 优先从 job_specs 的 time_window 取最晚 end
    if job_specs:
        latest_end: dt_time | None = None
        for spec in job_specs:
            w = _parse_time_window(spec)
            if w and (latest_end is None or w[1] > latest_end):
                latest_end = w[1]
        if latest_end is not None:
            return latest_end
    # 2. 回退到 recruitment.work_time.end
    try:
        policy = config.section("recruitment").get("work_time", {})
        end_str = str(policy.get("end", "18:00"))
        h, m = end_str.split(":")
        return dt_time(int(h), int(m))
    except Exception:
        return dt_time(18, 0)


def _parse_time_window(spec: dict) -> tuple[dt_time, dt_time] | None:
    """读取岗位的 time_window 配置 [start, end]，未配置返回 None。

    兼容 PyYAML 的六十进制解析：``14:00`` 会被解析为整数 840（14*60），
    ``09:00`` 因前导零被解析为字符串 ``"09:00"``。两种格式都要支持。
    """
    tw = spec.get("time_window")
    if not tw or not isinstance(tw, list) or len(tw) < 2:
        return None
    try:
        return (_to_dt_time(tw[0]), _to_dt_time(tw[1]))
    except Exception:
        return None


def _is_in_time_window(now: datetime, window: tuple[dt_time, dt_time] | None) -> bool:
    """检查当前时间是否在时段内。无时段配置默认 True。"""
    if not window:
        return True
    start, end = window
    return start <= now.time() < end


def _collect_time_windows(job_specs: list[dict]) -> list[tuple[dt_time, dt_time]]:
    """收集所有岗位的时段，用于判断是否还有待跑时段。"""
    windows = []
    for spec in job_specs:
        w = _parse_time_window(spec)
        if w and w not in windows:
            windows.append(w)
    # 按开始时间排序
    windows.sort(key=lambda x: x[0])
    return windows


def _has_upcoming_window(now: datetime, windows: list[tuple[dt_time, dt_time]]) -> bool:
    """检查今天是否还有未到的时段。"""
    for start, end in windows:
        if now.time() < start:
            return True
    return False


def _count_today_greeted(job_name: str) -> int:
    """查数据库当天该岗位已成功招呼数。"""
    try:
        from logger.risk_control import RiskControl
        rc = RiskControl()
        return rc.count_today_sent(job_name=job_name)
    except Exception:
        return 0


def _successful_greeting_keys(job_name: str, encrypt_job_id: str,
                              run_id: str) -> set[str]:
    """Read successful external side effects even when the child run failed."""
    if not run_id:
        return set()
    try:
        from output_manager import JobOutputManager
        out = JobOutputManager(
            job_name,
            encrypt_job_id=encrypt_job_id,
            run_id=run_id,
            lazy=True,
        )
        path = Path(out.process_dir) / "greet_log.json"
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return set()

    keys: set[str] = set()
    for item in payload.get("results", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "greeted" or not item.get("message_sent"):
            continue
        gid = str(
            item.get("encrypt_geek_id")
            or item.get("target_encrypt_geek_id")
            or item.get("geek_id")
            or ""
        ).strip()
        name = str(item.get("name") or "").strip()
        if gid:
            keys.add(f"gid:{gid}")
        elif name:
            keys.add(f"name:{name}")
    return keys


def _run_daemon_loop(
    *,
    job_specs: list[dict],
    default_count: int,
    default_max_greetings: int,
    dry_run: bool,
    sync_feishu: bool,
    poll_interval: int,
    started_at: str,
    log_path: Path,
    history_path: Path,
    latest_path: Path,
    config,
    start_notifications: list[dict],
    encrypt_job_id_override: str | None = None,
) -> int:
    """常驻轮询：每个岗位跑完一轮后立刻检查是否达标，不达标加短节流再跑，全达标或下班退出。"""
    end_time = _parse_end_time(config, job_specs)
    round_idx = 0
    all_results: list[dict] = []
    # 每轮 fetch 之间的节流时间（秒），防止 BOSS 风控
    throttle_sec = 3
    # 大循环之间的统一节流时间（秒）
    big_loop_throttle_sec = 40
    # 每个岗位每次大循环固定跑多少轮 fetch
    fetch_rounds_per_job = 3
    # 每个岗位每次大循环卡片扫描上限（兜底防异常超扫）
    cards_limit_per_job = 2000
    # BOSS 平台所有岗位共享的每日打招呼上限。达到后 daemon 立即退出，
    # recruit_workflow 的 ensure_can_send 也会在单次 run 内兜底停。
    global_daily_greet_cap = 200
    # 连续失败后暂缓几轮，避免页面暂时异常导致岗位当天永久跳过。
    max_consecutive_failures = 3
    all_windows = _collect_time_windows(job_specs)
    # 已达标岗位集合（跨大循环保留，当天不再跑）
    completed_jobs: set[str] = set()
    # 岗位连续失败计数器（跨大循环保留，连续失败 N 次后跳过）
    fail_streaks: dict[str, int] = {}
    retry_after_round: dict[str, int] = {}
    runnable_job_names = {
        str(spec.get("name") or "")
        for spec in job_specs
        if str(spec.get("name") or "")
        and _int_setting(spec.get("count"), default_count) > 0
        and _int_setting(spec.get("max_greetings"), default_max_greetings) > 0
    }
    multi_job_mode = len(runnable_job_names) > 1
    job_id_owners: dict[str, str] = {}
    # Freeze the database baseline at daemon start.  Successful live actions
    # from every child run (including a child that later exits non-zero) are
    # then unioned by candidate id, preventing both undercount and double count.
    baseline_by_job = {
        str(spec.get("name", "")): _count_today_greeted(str(spec.get("name", "")))
        for spec in job_specs if str(spec.get("name", ""))
    }
    baseline_global = _count_today_greeted("")
    session_greeted: dict[str, set[str]] = {
        name: set() for name in baseline_by_job
    }

    def effective_sent(job_name: str) -> int:
        if job_name:
            return baseline_by_job.get(job_name, 0) + len(
                session_greeted.setdefault(job_name, set())
            )
        return baseline_global + sum(len(keys) for keys in session_greeted.values())

    _append_run_log(
        log_path,
        f"DAEMON START throttle={throttle_sec}s big_loop_throttle={big_loop_throttle_sec}s "
        f"fetch_rounds={fetch_rounds_per_job} cards_limit={cards_limit_per_job} "
        f"global_greet_cap={global_daily_greet_cap} end={end_time} windows={all_windows}",
    )

    # daemon 启动时发飞书群聊通知（start 通知）
    # 获取 BOSS 账号名（失败不阻断）
    # 先确保 Edge 启动，否则 CDP 连不上无法获取账号名
    boss_account_name = ""
    try:
        from boss_hr.adapters.browser_environment import ensure_browser_ready
        ready = ensure_browser_ready(auto_launch=True, wait_for_user_login=False)
        if ready.ok:
            from recruiter_job_catalog import fetch_boss_account_name
            boss_account_name = fetch_boss_account_name()
    except Exception:
        pass
    boss_account_label = f"BOSS账号：{boss_account_name}" if boss_account_name else "BOSS账号：未获取"
    daemon_start_msg = "\n".join([
        f"开始时间：{started_at}",
        f"运行模式：daemon 常驻轮询",
        f"下班时间：{end_time}",
        boss_account_label,
        f"岗位：{_format_job_specs(job_specs, default_count, default_max_greetings)}",
    ])
    start_notifs = notify_scheduler_event(
        "start", "BOSS 自动化开始（daemon）", daemon_start_msg, config=config,
    )
    for item in start_notifs:
        status = "SKIP" if item.skipped else ("OK" if item.ok else "FAIL")
        _append_run_log(log_path, f"notify start {item.channel} {status} {item.reason}")

    while True:
        round_idx += 1
        now = datetime.now()
        # 下班时间检查
        if end_time and now.time() >= end_time:
            _append_run_log(log_path, f"DAEMON EXIT reason=off_work now={now.strftime('%H:%M')}")
            break

        # 收集本轮大循环要跑的未达标岗位
        pending_jobs: list[dict] = []
        all_reached = True
        for spec in job_specs:
            name = str(spec.get("name", ""))
            if not name:
                continue
            # 跳过已达标岗位（跨大循环保留）
            if name in completed_jobs:
                continue
            count = _int_setting(spec.get("count"), default_count)
            max_greetings = _int_setting(spec.get("max_greetings"), default_max_greetings)
            if count <= 0 or max_greetings <= 0:
                continue
            daily_target = int(spec.get("daily_greet_target", 0) or max_greetings)
            if daily_target <= 0:
                continue
            # 时段过滤
            window = _parse_time_window(spec)
            if not _is_in_time_window(now, window):
                _append_run_log(log_path, f"DAEMON big_round={round_idx} job={name} SKIP not_in_window now={now.strftime('%H:%M')}")
                continue
            sent_today = effective_sent(name)
            remaining = max(0, daily_target - sent_today)
            if remaining <= 0:
                _append_run_log(log_path, f"DAEMON big_round={round_idx} job={name} REACHED sent={sent_today}/{daily_target}")
                completed_jobs.add(name)
                continue
            all_reached = False
            if round_idx < retry_after_round.get(name, 0):
                _append_run_log(log_path, f"DAEMON big_round={round_idx} job={name} COOLDOWN retry_round={retry_after_round[name]}")
                continue
            this_round_greetings = min(remaining, max_greetings)
            pending_jobs.append({
                "name": name,
                "count": count,
                "max_greetings": this_round_greetings,
                "daily_target": daily_target,
                "sent_today": sent_today,
                "remaining": remaining,
            })

        if all_reached:
            # 检查后续窗口里是否有启用的岗位（daily_target > 0 且未达标）
            has_active_upcoming = False
            for spec in job_specs:
                name = str(spec.get("name", ""))
                if not name or name in completed_jobs:
                    continue
                count = _int_setting(spec.get("count"), default_count)
                max_greetings = _int_setting(spec.get("max_greetings"), default_max_greetings)
                if count <= 0 or max_greetings <= 0:
                    continue
                daily_target = int(spec.get("daily_greet_target", 0) or max_greetings)
                if daily_target <= 0:
                    continue
                # 这个岗位启用了，检查它的窗口是否在后面
                window = _parse_time_window(spec)
                if window and now.time() < window[0]:
                    has_active_upcoming = True
                    break
            if has_active_upcoming:
                next_window = None
                for start, end in all_windows:
                    if now.time() < start:
                        next_window = (start, end)
                        break
                if next_window:
                    _append_run_log(log_path, f"DAEMON WAIT next_window={next_window[0]}-{next_window[1]} now={now.strftime('%H:%M')}")
                    wait_until = datetime.combine(now.date(), next_window[0])
                    wait_seconds = (wait_until - now).total_seconds()
                    if wait_seconds > 0 and wait_seconds <= 7200:
                        time.sleep(min(wait_seconds, 7200))
                        continue
                    else:
                        _append_run_log(log_path, f"DAEMON EXIT reason=wait_too_long wait_seconds={wait_seconds}")
                        break
            _append_run_log(log_path, f"DAEMON EXIT reason=all_reached round={round_idx}")
            break

        if not pending_jobs:
            if any(round_idx < retry_round for retry_round in retry_after_round.values()):
                time.sleep(big_loop_throttle_sec)
                continue
            # 检查后续窗口里是否有启用的岗位（daily_target > 0 且未达标）
            has_active_upcoming = False
            for spec in job_specs:
                name = str(spec.get("name", ""))
                if not name or name in completed_jobs:
                    continue
                count = _int_setting(spec.get("count"), default_count)
                max_greetings = _int_setting(spec.get("max_greetings"), default_max_greetings)
                if count <= 0 or max_greetings <= 0:
                    continue
                daily_target = int(spec.get("daily_greet_target", 0) or max_greetings)
                if daily_target <= 0:
                    continue
                window = _parse_time_window(spec)
                if window and now.time() < window[0]:
                    has_active_upcoming = True
                    break
            if has_active_upcoming:
                next_window = None
                for start, end in all_windows:
                    if now.time() < start:
                        next_window = (start, end)
                        break
                if next_window:
                    _append_run_log(log_path, f"DAEMON WAIT next_window={next_window[0]}-{next_window[1]} now={now.strftime('%H:%M')}")
                    wait_until = datetime.combine(now.date(), next_window[0])
                    wait_seconds = (wait_until - now).total_seconds()
                    if wait_seconds > 0 and wait_seconds <= 7200:
                        time.sleep(min(wait_seconds, 7200))
                        continue
                    else:
                        _append_run_log(log_path, f"DAEMON EXIT reason=wait_too_long wait_seconds={wait_seconds}")
                        break
            else:
                _append_run_log(log_path, f"DAEMON EXIT reason=no_pending_jobs round={round_idx}")
                break

        # 内层岗位循环：每个未达标岗位固定跑 fetch_rounds_per_job 轮 fetch
        for job in pending_jobs:
            name = job["name"]
            encrypt_job_id = _resolve_encrypt_job_id_for_job(
                name,
                encrypt_job_id_override,
                multi_job=multi_job_mode,
            )
            if not encrypt_job_id:
                _append_run_log(log_path, f"DAEMON big_round={round_idx} job={name} SKIP missing_encrypt_job_id")
                continue
            claimed, owner = _claim_job_id(job_id_owners, name, encrypt_job_id)
            if not claimed:
                _append_run_log(
                    log_path,
                    f"DAEMON big_round={round_idx} job={name} SKIP "
                    f"duplicate_encrypt_job_id owner={owner}",
                )
                completed_jobs.add(name)
                continue

            cards_scanned = 0
            last_run_id: str | None = None
            live_card_flow_used = False
            _append_run_log(
                log_path,
                f"DAEMON big_round={round_idx} job={name} START fetch_rounds={fetch_rounds_per_job} "
                f"cards_limit={cards_limit_per_job} sent_today={job['sent_today']}/{job['daily_target']}",
            )

            for fetch_round in range(1, fetch_rounds_per_job + 1):
                now2 = datetime.now()
                if end_time and now2.time() >= end_time:
                    _append_run_log(log_path, f"DAEMON job={name} EXIT off_work now={now2.strftime('%H:%M')}")
                    break

                # 全局打招呼上限检查（所有岗位共享 200）：达到后整个 daemon 退出
                global_sent = effective_sent("")
                if global_daily_greet_cap > 0 and global_sent >= global_daily_greet_cap:
                    _append_run_log(
                        log_path,
                        f"DAEMON job={name} GLOBAL_CAP_REACHED global_sent={global_sent}/{global_daily_greet_cap}",
                    )
                    completed_jobs.add(name)
                    break

                sent_now = effective_sent(name)
                remaining_now = max(0, job["daily_target"] - sent_now)
                if remaining_now <= 0:
                    _append_run_log(log_path, f"DAEMON job={name} REACHED sent={sent_now}/{job['daily_target']}")
                    completed_jobs.add(name)
                    break

                if cards_scanned >= cards_limit_per_job:
                    _append_run_log(log_path, f"DAEMON job={name} SKIP cards_limit scanned={cards_scanned}/{cards_limit_per_job}")
                    break

                this_round = min(remaining_now, job["max_greetings"])
                _append_run_log(
                    log_path,
                    f"DAEMON job={name} fetch_round={fetch_round}/{fetch_rounds_per_job} "
                    f"count={job['count']} greet={this_round} sent={sent_now}/{job['daily_target']} "
                    f"scanned={cards_scanned}/{cards_limit_per_job}",
                )
                payload, exit_code = _run_one_job(
                    job_name=name,
                    encrypt_job_id=encrypt_job_id,
                    count=job["count"],
                    max_greetings=this_round,
                    dry_run=dry_run,
                    sync_feishu=sync_feishu,
                    refresh_page=True,
                )
                _append_run_log(
                    log_path,
                    f"DAEMON job={name} fetch_round={fetch_round} END ok={payload.get('ok')} exit={exit_code}",
                )
                all_results.append(payload)
                live_card_flow_used = live_card_flow_used or _uses_live_card_flow(payload)
                run_id_from_payload = str(payload.get("run_id") or "")
                if run_id_from_payload:
                    before = len(session_greeted.setdefault(name, set()))
                    session_greeted[name].update(_successful_greeting_keys(
                        name, encrypt_job_id, run_id_from_payload,
                    ))
                    credited = len(session_greeted[name]) - before
                    if credited:
                        _append_run_log(
                            log_path,
                            f"DAEMON job={name} GREET_COMMIT run_id={run_id_from_payload} "
                            f"credited={credited} sent={effective_sent(name)}/{job['daily_target']}",
                        )

                # 提取未招呼成功的候选人到重试池
                if payload.get("ok") and payload.get("run_id"):
                    last_run_id = payload["run_id"]
                    if live_card_flow_used:
                        _append_run_log(
                            log_path,
                            f"DAEMON job={name} LIVE_CARD_FLOW enabled; skip legacy retry-pool extraction",
                        )
                    else:
                        pool_size = _extract_and_save_retry_candidates(
                            payload, encrypt_job_id, name, last_run_id,
                        )
                        if pool_size:
                            _append_run_log(log_path, f"DAEMON job={name} RETRY_POOL updated size={pool_size}")

                # 验证码检测：exit_code=30 或 error.code=CAPTCHA → 立即退出 + 飞书告警
                _err = payload.get("error") or {}
                _err_code = _err.get("code", "") if isinstance(_err, dict) else ""
                if exit_code == 30 or _err_code == "CAPTCHA":
                    _captcha_msg = _err.get("message", "") if isinstance(_err, dict) else ""
                    _captcha_url = ""
                    if isinstance(_err, dict):
                        _captcha_url = _err.get("url", "") or _err.get("detail", {}).get("url", "")
                    _append_run_log(
                        log_path,
                        f"DAEMON EXIT reason=captcha_detected job={name} url={_captcha_url}",
                    )
                    # 飞书群告警
                    notify_scheduler_event(
                        "captcha",
                        "⚠️ BOSS 图形验证码",
                        f"岗位={name}\n时间={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"页面={_captcha_url or '未知'}\n详情={_captcha_msg or '检测到验证码'}\n"
                        f"程序已自动停止，需人工处理验证码后手动重启。",
                        config=config,
                    )
                    # 标记所有岗位为已完成，让 daemon 退出
                    for j in job_specs:
                        completed_jobs.add(str(j.get("name", "")))
                    break

                # 连续失败计数：成功时重置，失败时累加
                if payload.get("ok"):
                    fail_streaks[name] = 0
                    retry_after_round.pop(name, None)
                else:
                    fail_streaks[name] = fail_streaks.get(name, 0) + 1
                    if fail_streaks[name] >= max_consecutive_failures:
                        _append_run_log(
                            log_path,
                            f"DAEMON job={name} COOLDOWN reason=consecutive_failures streak={fail_streaks[name]} retry_round={round_idx + 3}",
                        )
                        retry_after_round[name] = round_idx + 3
                        fail_streaks[name] = 0
                        break

                cards_scanned += _terminal_card_count(payload, fallback=0)

                if fetch_round < fetch_rounds_per_job:
                    _append_run_log(log_path, f"DAEMON job={name} THROTTLE {throttle_sec}s")
                    time.sleep(throttle_sec)

            _append_run_log(log_path, f"DAEMON job={name} DONE big_round={round_idx} scanned={cards_scanned}")

            # ★ 重试池：对未招呼成功的候选人重新打招呼
            retry_pool = _read_retry_pool(encrypt_job_id, name)
            if retry_pool and last_run_id and not dry_run and not live_card_flow_used:
                sent_after = effective_sent(name)
                remaining_after = max(0, job["daily_target"] - sent_after)
                if remaining_after > 0:
                    retry_names = [c["name"] for c in retry_pool if c.get("name")]
                    # 限制本轮重试人数不超过剩余配额
                    retry_max = min(len(retry_names), remaining_after)
                    retry_names = retry_names[:retry_max]
                    only_names_str = ",".join(retry_names)
                    _append_run_log(
                        log_path,
                        f"DAEMON job={name} RETRY_GREET pool={len(retry_pool)} "
                        f"targeting={len(retry_names)} remaining_quota={remaining_after}",
                    )
                    retry_payload, retry_rc = _run_one_job(
                        job_name=name,
                        encrypt_job_id=encrypt_job_id,
                        count=0,
                        max_greetings=retry_max,
                        dry_run=False,
                        sync_feishu=sync_feishu,
                        greet_only=True,
                        existing_run_id=last_run_id,
                        only_names=only_names_str,
                        refresh_page=True,
                    )
                    _append_run_log(
                        log_path,
                        f"DAEMON job={name} RETRY_GREET END ok={retry_payload.get('ok')} exit={retry_rc}",
                    )
                    all_results.append(retry_payload)
                    # 从 greet_log 提取成功/失败名单（含 gid）
                    greeted_names: list[str] = []
                    greeted_gids: list[str] = []
                    failed_names: list[str] = []
                    gl_path = ""
                    try:
                        from output_manager import JobOutputManager
                        out = JobOutputManager(name, encrypt_job_id=encrypt_job_id, run_id=last_run_id, lazy=True)
                        gl_path = os.path.join(out.process_dir, "greet_log.json")
                        if os.path.exists(gl_path):
                            with open(gl_path, encoding="utf-8") as f:
                                gl = json.load(f)
                            for r in gl.get("results", []):
                                nm = r.get("name") or ""
                                gid = r.get("encrypt_geek_id") or r.get("target_encrypt_geek_id") or ""
                                if r.get("status") == "greeted" and r.get("message_sent"):
                                    if nm:
                                        greeted_names.append(nm)
                                    if gid:
                                        greeted_gids.append(gid)
                                else:
                                    if nm:
                                        failed_names.append(nm)
                    except Exception:
                        pass
                    # 移除已成功打招呼的候选人（优先按 gid）
                    if greeted_names or greeted_gids:
                        remaining_pool = _remove_greeted_from_retry(
                            encrypt_job_id, name, greeted_names, greeted_gids,
                        )
                        _append_run_log(
                            log_path,
                            f"DAEMON job={name} RETRY_POOL removed={len(greeted_names)} remaining={remaining_pool}",
                        )
                    # 递增连续失败计数，达到阈值自动移除
                    if failed_names:
                        remaining_pool, purged = _increment_retry_fail_counts(
                            encrypt_job_id, name, failed_names,
                        )
                        if purged:
                            _append_run_log(
                                log_path,
                                f"DAEMON job={name} RETRY_POOL fail_purged={purged} remaining={remaining_pool}",
                            )
                else:
                    _append_run_log(log_path, f"DAEMON job={name} RETRY_GREET SKIP quota_exhausted sent={sent_after}/{job['daily_target']}")
            elif retry_pool and live_card_flow_used:
                _append_run_log(
                    log_path,
                    f"DAEMON job={name} RETRY_GREET SKIP live_card_flow pool={len(retry_pool)}",
                )

        # 本大循环结束检查
        now3 = datetime.now()
        if end_time and now3.time() >= end_time:
            _append_run_log(log_path, f"DAEMON EXIT reason=off_work after_big_round={round_idx}")
            break

        # 全局打招呼上限兜底：所有岗位共享 200，达到立即退出
        global_sent_now = effective_sent("")
        if global_daily_greet_cap > 0 and global_sent_now >= global_daily_greet_cap:
            _append_run_log(
                log_path,
                f"DAEMON EXIT reason=global_cap_reached global_sent={global_sent_now}/{global_daily_greet_cap} after_big_round={round_idx}",
            )
            break

        has_pending = False
        for spec in job_specs:
            name = str(spec.get("name", ""))
            if not name or name in completed_jobs:
                continue
            count = _int_setting(spec.get("count"), default_count)
            max_greetings = _int_setting(spec.get("max_greetings"), default_max_greetings)
            if count <= 0 or max_greetings <= 0:
                continue
            daily_target = int(spec.get("daily_greet_target", 0) or max_greetings)
            if daily_target <= 0:
                continue
            window = _parse_time_window(spec)
            if not _is_in_time_window(now3, window):
                continue
            sent = effective_sent(name)
            if daily_target - sent > 0:
                has_pending = True
                break

        if not has_pending:
            _append_run_log(log_path, f"DAEMON EXIT reason=all_reached after_big_round={round_idx}")
            break

        # 大循环间统一节流
        _append_run_log(log_path, f"DAEMON big_round={round_idx} DONE throttle {big_loop_throttle_sec}s before next big round")
        slept = 0
        while slept < big_loop_throttle_sec:
            sleep_chunk = min(10, big_loop_throttle_sec - slept)
            time.sleep(sleep_chunk)
            slept += sleep_chunk
            now4 = datetime.now()
            if end_time and now4.time() >= end_time:
                _append_run_log(log_path, f"DAEMON EXIT reason=off_work during_throttle now={now4.strftime('%H:%M')}")
                break

    # daemon 结束，汇总结果
    finished_at = _now_text()
    # 统计各岗位本轮 daemon 真实招呼数（从 results 提取 greeted）
    job_greet_summary: dict[str, int] = {
        name: len(keys) for name, keys in session_greeted.items() if keys
    }
    total_greeted = sum(job_greet_summary.values())
    payload = {
        "ok": all(item.get("ok") for item in all_results) if all_results else True,
        "stage": "daemon_complete",
        "rounds": round_idx,
        "results": all_results,
        "daemon_summary": {
            "finished_at": finished_at,
            "total_greeted": total_greeted,
            "per_job_greeted": job_greet_summary,
            "completed_jobs": sorted(completed_jobs),
            "boss_account_name": boss_account_name,
        },
    }
    _append_run_log(log_path, f"DAEMON END rounds={round_idx} total_greeted={total_greeted} jobs={job_greet_summary}")
    return _finish_scheduler_run(
        payload=payload, exit_code=0,
        started_at=started_at, log_path=log_path,
        history_path=history_path, latest_path=latest_path,
        config=config, start_notifications=start_notifications,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one scheduled BOSS automation cycle.",
    )
    parser.add_argument("--job-name", default=None, help="default: app.default_job_name")
    parser.add_argument("--encrypt-job-id", default=None, help="default: BOSS_HR_ENCRYPT_JOB_ID")
    parser.add_argument("--count", type=int, default=None, help="max candidates to fetch/analyze")
    parser.add_argument("--max-greetings", type=int, default=None, help="max candidates to greet")
    parser.add_argument("--dry-run", action="store_true", help="do not really greet or sync Feishu")
    parser.add_argument("--skip-feishu", action="store_true", help="skip Feishu sync")
    parser.add_argument("--skip-sync", action="store_true", help="skip BOSS job catalog sync")
    parser.add_argument("--allow-auto-confirm", action="store_true", help="allow scheduled auto confirm")
    parser.add_argument("--daemon", action="store_true", help="常驻轮询模式：每10分钟检查各岗位当天已招呼数，达标跳过，全达标或下班退出")
    parser.add_argument("--poll-interval", type=int, default=600, help="daemon 模式轮询间隔秒数（默认 600=10分钟）")
    args = parser.parse_args(argv)

    started_at = _now_text()
    log_path, history_path, latest_path = _scheduled_status_paths()
    _append_run_log(log_path, f"START argv={argv or []}")
    start_notifications: list[dict[str, Any]] = []

    config = get_config(reload=True)
    enabled = bool(_setting(config, "enabled", True))
    if not enabled:
        payload = {
            "ok": False,
            "status": "scheduler_disabled",
            "message": "app.scheduler.enabled=false",
        }
        return _finish_scheduler_run(
            payload=payload, exit_code=0, started_at=started_at,
            log_path=log_path, history_path=history_path, latest_path=latest_path,
            config=config, start_notifications=start_notifications,
        )

    default_count = int(_setting(config, "count", 10))
    default_max_greetings = int(_setting(config, "max_greetings", 3))

    if args.job_name:
        configured_job = next(
            (item for item in _scheduled_jobs(config) if item.get("name") == args.job_name),
            {},
        )
        job_specs = [{"name": args.job_name, **configured_job}]
    else:
        job_specs = _scheduled_jobs(config) or [{"name": _default_job_name(config)}]
    job_specs = [item for item in job_specs if item.get("name")]

    if len(job_specs) > 1 and args.encrypt_job_id:
        payload = {
            "ok": False,
            "error": "multi-job mode must use per-job BOSS_JOB_ID_* values instead of --encrypt-job-id",
            "job_names": [item["name"] for item in job_specs],
        }
        return _finish_scheduler_run(
            payload=payload, exit_code=2, started_at=started_at,
            log_path=log_path, history_path=history_path, latest_path=latest_path,
            config=config, start_notifications=start_notifications,
        )

    if not job_specs:
        payload = {"ok": False, "error": "missing job_name"}
        return _finish_scheduler_run(
            payload=payload, exit_code=2, started_at=started_at,
            log_path=log_path, history_path=history_path, latest_path=latest_path,
            config=config, start_notifications=start_notifications,
        )

    dry_run = bool(args.dry_run or _setting(config, "dry_run", False))
    sync_feishu = not bool(args.skip_feishu or _setting(config, "skip_feishu", False))
    allow_auto_confirm = bool(args.allow_auto_confirm or _setting(config, "allow_auto_confirm", False))

    if not allow_auto_confirm:
        payload = {
            "ok": False,
            "status": "auto_confirm_required",
            "message": (
                "Scheduled automation requires explicit opt-in. Set "
                "app.scheduler.allow_auto_confirm=true or pass --allow-auto-confirm."
            ),
            "next_action": "enable_allow_auto_confirm",
        }
        return _finish_scheduler_run(
            payload=payload, exit_code=1, started_at=started_at,
            log_path=log_path, history_path=history_path, latest_path=latest_path,
            config=config, start_notifications=start_notifications,
        )

    # daemon 模式：常驻轮询，跳过普通单轮逻辑
    if args.daemon:
        return _run_daemon_loop(
            job_specs=job_specs,
            default_count=default_count,
            default_max_greetings=default_max_greetings,
            dry_run=dry_run,
            sync_feishu=sync_feishu,
            poll_interval=args.poll_interval,
            started_at=started_at,
            log_path=log_path,
            history_path=history_path,
            latest_path=latest_path,
            config=config,
            start_notifications=start_notifications,
            encrypt_job_id_override=args.encrypt_job_id,
        )

    # 获取 BOSS 账号名（失败不阻断）
    boss_account_name_single = ""
    try:
        from boss_hr.adapters.browser_environment import ensure_browser_ready
        ready = ensure_browser_ready(auto_launch=True, wait_for_user_login=False)
        if ready.ok:
            from recruiter_job_catalog import fetch_boss_account_name
            boss_account_name_single = fetch_boss_account_name()
    except Exception:
        pass
    boss_acct_label_single = f"BOSS账号：{boss_account_name_single}" if boss_account_name_single else "BOSS账号：未获取"
    start_message = "\n".join([
        f"开始：{started_at}",
        boss_acct_label_single,
        f"岗位：{_format_job_specs(job_specs, default_count, default_max_greetings)}",
        f"dry_run={dry_run} sync_feishu={sync_feishu}",
    ])
    start_notification_results = notify_scheduler_event(
        "start",
        "BOSS 自动化开始",
        start_message,
        config=config,
    )
    start_notifications = notification_results_to_dict(start_notification_results)
    for item in start_notification_results:
        status = "SKIP" if item.skipped else ("OK" if item.ok else "FAIL")
        _append_run_log(log_path, f"notify start {item.channel} {status} {item.reason}")

    job_sync: dict | None = None
    if not args.skip_sync:
        _append_run_log(log_path, "sync_jobs START")
        sync_result = _sync_jobs()
        if not sync_result.ok:
            payload = {
                "ok": False,
                "stage": "sync_jobs",
                "message": "岗位信息同步失败，通常是 Edge 未启动或 BOSS 未登录。",
                "sync": sync_result.to_dict("sync-jobs"),
            }
            exit_code = int(sync_result.exit_code) or 1
            _append_run_log(log_path, f"sync_jobs FAIL exit_code={exit_code}")
            return _finish_scheduler_run(
                payload=payload, exit_code=exit_code, started_at=started_at,
                log_path=log_path, history_path=history_path, latest_path=latest_path,
                config=config, start_notifications=start_notifications,
            )
        sync_data = sync_result.data or {}
        job_sync = {
            "ok": True,
            "matched": sync_data.get("matched") or [],
            "unmatched": sync_data.get("unmatched") or [],
            "warnings": sync_data.get("warnings") or [],
        }
        _append_run_log(
            log_path,
            f"sync_jobs END matched={len(job_sync['matched'])} "
            f"unmatched={len(job_sync['unmatched'])}",
        )

    results = []
    final_exit_code = 0
    runnable_job_names = {
        str(spec.get("name") or "")
        for spec in job_specs
        if str(spec.get("name") or "")
        and _int_setting(
            args.count if args.count is not None else spec.get("count"),
            default_count,
        ) > 0
        and _int_setting(
            args.max_greetings
            if args.max_greetings is not None
            else spec.get("max_greetings"),
            default_max_greetings,
        ) > 0
    }
    multi_job_mode = len(runnable_job_names) > 1
    job_id_owners: dict[str, str] = {}
    for spec in job_specs:
        name = str(spec["name"])
        count = _int_setting(args.count if args.count is not None else spec.get("count"), default_count)
        max_greetings = _int_setting(
            args.max_greetings if args.max_greetings is not None else spec.get("max_greetings"),
            default_max_greetings,
        )
        if count <= 0 or max_greetings <= 0:
            _append_run_log(
                log_path,
                f"job SKIP name={name} count={count} max_greetings={max_greetings}",
            )
            results.append({
                "ok": True,
                "stage": "skipped",
                "job_name": name,
                "count": count,
                "max_greetings": max_greetings,
                "message": "count or max_greetings is 0; skipped",
            })
            continue

        encrypt_job_id = _resolve_encrypt_job_id_for_job(
            name,
            args.encrypt_job_id,
            multi_job=multi_job_mode,
        )
        if not encrypt_job_id:
            env_key = env_key_for_job(name)
            hint = f", {env_key}" if env_key else ""
            _append_run_log(log_path, f"job FAIL name={name} reason=missing_encrypt_job_id")
            payload = {
                "ok": False,
                "stage": "resolve_job_id",
                "job_name": name,
                "error": f"missing BOSS_HR_ENCRYPT_JOB_ID{hint} or --encrypt-job-id",
            }
            results.append(payload)
            final_exit_code = final_exit_code or 2
            continue
        claimed, owner = _claim_job_id(job_id_owners, name, encrypt_job_id)
        if not claimed:
            _append_run_log(
                log_path,
                f"job FAIL name={name} reason=duplicate_encrypt_job_id owner={owner}",
            )
            results.append({
                "ok": False,
                "stage": "resolve_job_id",
                "job_name": name,
                "error": f"encryptJobId is already assigned to job: {owner}",
            })
            final_exit_code = final_exit_code or 2
            continue

        _append_run_log(
            log_path,
            f"job START name={name} count={count} max_greetings={max_greetings}",
        )
        payload, exit_code = _run_one_job(
            job_name=name,
            encrypt_job_id=encrypt_job_id,
            count=count,
            max_greetings=max_greetings,
            dry_run=dry_run,
            sync_feishu=sync_feishu,
        )
        automation_data = (
            ((payload.get("automation") or {}).get("data") or {})
            if isinstance(payload.get("automation"), dict)
            else {}
        )
        greet_data = (((automation_data.get("greet") or {}).get("greet") or {}).get("data") or {})
        _append_run_log(
            log_path,
            f"job END name={name} ok={payload.get('ok')} run_id={payload.get('run_id')} "
            f"exit_code={exit_code} greeted={greet_data.get('greeted', '')} "
            f"targeted={greet_data.get('candidates_targeted', '')}",
        )
        results.append(payload)
        if exit_code:
            final_exit_code = final_exit_code or exit_code

    if len(results) == 1:
        if job_sync is not None:
            results[0]["job_sync"] = job_sync
        return _finish_scheduler_run(
            payload=results[0], exit_code=final_exit_code,
            started_at=started_at, log_path=log_path,
            history_path=history_path, latest_path=latest_path,
            config=config, start_notifications=start_notifications,
        )

    payload = {
        "ok": all(item.get("ok") for item in results),
        "stage": "multi_job_complete" if final_exit_code == 0 else "multi_job_partial",
        "job_count": len(results),
        "results": results,
    }
    if job_sync is not None:
        payload["job_sync"] = job_sync
    return _finish_scheduler_run(
        payload=payload, exit_code=final_exit_code,
        started_at=started_at, log_path=log_path,
        history_path=history_path, latest_path=latest_path,
        config=config, start_notifications=start_notifications,
    )


if __name__ == "__main__":
    raise SystemExit(main())
