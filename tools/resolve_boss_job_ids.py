#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discover BOSS recruiter jobs and maintain one-to-one project bindings."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "shared"
for path in (ROOT, SHARED):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from shared.env_loader import load_project_dotenv  # noqa: E402

load_project_dotenv()

from boss_hr.config_manager import ConfigManager  # noqa: E402
from boss_hr.job_id_resolver import (  # noqa: E402
    FEISHU_TABLE_ENV_KEYS,
    JOB_ID_ENV_KEYS,
    JOB_NAME_ENV_KEYS,
)
from recruiter_job_catalog import list_jobs  # noqa: E402


DISPLAY_NAME_KEYS = (
    "boss_display_name",
    "boss_job_name",
    "boss_title",
    "boss_position_name",
    "display_name",
)
_ENV_ASSIGNMENT = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=).*$")
LEGACY_JOB_ALIASES = {
    "主播": "全职主播",
    "短视频剪辑": "初级剪拍",
    "短视频拍剪师": "高级剪拍",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从已登录 Edge 的 BOSS 推荐页账号读取岗位并维护 .env。",
    )
    parser.add_argument(
        "--cdp-url", default="http://localhost:9222",
        help="已登录 BOSS 的 Edge CDP 地址，默认 http://localhost:9222",
    )
    parser.add_argument(
        "--update-env", action="store_true",
        help="将 BOSS 岗位真实名称和 encryptJobId 原子写入 .env。",
    )
    parser.add_argument(
        "--env-file", default=str(ROOT / ".env"),
        help="要读取/更新的 .env 路径，默认项目根目录下的 .env。",
    )
    parser.add_argument(
        "--keep-unmatched", action="store_true",
        help="保留未匹配岗位旧值；默认清空，防止沿用其他账号的岗位 ID。",
    )
    parser.add_argument(
        "--migrate-db-jobs", action="store_true",
        help="把数据库中的已知旧岗位名迁移为当前稳定内部岗位名。",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON。")
    parser.add_argument(
        "--show-boss-jobs", action="store_true",
        help="同时输出当前 BOSS 账号返回的全部岗位。",
    )
    return parser


def run_sync(
    *,
    cdp_url: str = "http://localhost:9222",
    env_file: str | None = None,
    update_env: bool = True,
    clear_unmatched: bool = True,
    migrate_db_jobs: bool = False,
) -> dict[str, Any]:
    """同步 BOSS 岗位信息核心逻辑（boss-hr sync-jobs / 定时任务共用）。

    返回 payload dict：
      - list_jobs 失败（CDP/登录态/解析）→ ok=False + error（list_jobs 原样）
      - 成功 → resolved（逐岗位匹配结果）、env_updates（本次写入 .env 的
        键值映射）、validation（配置校验 errors/warnings）
      - ok = 全部岗位匹配成功 且 无配置校验错误（未匹配岗位不算硬失败，
        调用方按 warning 处理；本函数不写 .env 时 env_updates 仍返回应写值）
    """
    env_path = Path(env_file or (ROOT / ".env")).expanduser().resolve()
    cfg = ConfigManager(runtime_dir=env_path.parent)
    migration = {"renamed": 0, "merged": 0}
    if migrate_db_jobs:
        migration = _migrate_legacy_database_jobs(cfg)
    result = list_jobs(cdp_url)
    if not result.get("ok"):
        result["database_migration"] = migration
        return result

    boss_jobs = list(result.get("data") or [])
    resolved = _resolve_profiles(cfg, boss_jobs)
    _reject_duplicate_bindings(resolved)

    env_updates = _env_updates(resolved, clear_unmatched=clear_unmatched)
    env_updated = False
    if update_env:
        _update_env_file(env_path, env_updates)
        env_updated = True

    validation = _validate_project_bindings(
        cfg, resolved, _read_env_values(env_path),
    )
    return {
        "ok": all(item["matched"] for item in resolved) and not validation["errors"],
        "env_updated": env_updated,
        "env_file": str(env_path) if env_updated else None,
        "env_updates": env_updates,
        "database_migration": migration,
        "resolved": resolved,
        "validation": validation,
        "boss_jobs": boss_jobs,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_sync(
        cdp_url=args.cdp_url,
        env_file=args.env_file,
        update_env=args.update_env,
        clear_unmatched=not args.keep_unmatched,
        migrate_db_jobs=args.migrate_db_jobs,
    )

    # list_jobs 硬失败：原样打印（无 resolved 等字段）。
    if payload.get("error") and not payload.get("resolved"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_result(payload, show_boss_jobs=args.show_boss_jobs)
    return 0 if payload["ok"] else 2


def _resolve_profiles(
    cfg: ConfigManager,
    boss_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for profile_name in JOB_ID_ENV_KEYS:
        profile = cfg.load_job_profile(profile_name)
        queries = _queries_for_profile(profile_name, profile)
        match, reason = _match_boss_job(queries, boss_jobs)
        resolved.append({
            "profile_name": profile_name,
            "boss_name_env": JOB_NAME_ENV_KEYS[profile_name],
            "boss_id_env": JOB_ID_ENV_KEYS[profile_name],
            "feishu_table_env": FEISHU_TABLE_ENV_KEYS[profile_name],
            "queries": queries,
            "matched": match is not None,
            "match_error": reason,
            "boss_job": match,
            "boss_job_name": str((match or {}).get("jobName") or ""),
            "encrypt_job_id": str((match or {}).get("encryptJobId") or ""),
        })
    return resolved


def _queries_for_profile(profile_name: str, profile: dict[str, Any]) -> list[str]:
    queries: list[str] = []
    for key in DISPLAY_NAME_KEYS:
        value = profile.get(key) if isinstance(profile, dict) else None
        if value:
            queries.append(str(value))
    aliases = profile.get("boss_match_names", []) if isinstance(profile, dict) else []
    if isinstance(aliases, str):
        aliases = [aliases]
    if isinstance(aliases, list):
        queries.extend(str(value) for value in aliases if value)
    job_name = profile.get("job_name") if isinstance(profile, dict) else None
    if job_name:
        queries.append(str(job_name))
    queries.append(profile_name)

    seen: set[str] = set()
    output: list[str] = []
    for query in queries:
        value = query.strip()
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _match_boss_job(
    queries: list[str], boss_jobs: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Only accept an unambiguous normalized-name match."""
    for query in queries:
        expected = _normalize_name(query)
        matches = [
            job for job in boss_jobs
            if _normalize_name(str(job.get("jobName") or job.get("job_name") or "")) == expected
        ]
        if len(matches) == 1 and matches[0].get("encryptJobId"):
            return matches[0], ""
        if len(matches) > 1:
            return None, f"岗位名存在 {len(matches)} 个同名项，拒绝自动选择"
    return None, "未找到精确匹配；为防止串岗，不使用模糊匹配"


def _reject_duplicate_bindings(resolved: list[dict[str, Any]]) -> None:
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in resolved:
        if item["matched"] and item["encrypt_job_id"]:
            by_id[item["encrypt_job_id"]].append(item)
    for items in by_id.values():
        if len(items) < 2:
            continue
        names = "、".join(item["profile_name"] for item in items)
        for item in items:
            item["matched"] = False
            item["match_error"] = f"同一 BOSS 岗位 ID 被多个内部岗位匹配：{names}"
            item["boss_job"] = None
            item["boss_job_name"] = ""
            item["encrypt_job_id"] = ""


def _env_updates(
    resolved: list[dict[str, Any]], *, clear_unmatched: bool,
) -> dict[str, str]:
    updates: dict[str, str] = {}
    for item in resolved:
        if item["matched"]:
            updates[item["boss_name_env"]] = item["boss_job_name"]
            updates[item["boss_id_env"]] = item["encrypt_job_id"]
        elif clear_unmatched:
            updates[item["boss_name_env"]] = ""
            updates[item["boss_id_env"]] = ""
    return updates


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    if not path.exists():
        raise FileNotFoundError(f".env 不存在：{path}")
    original = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in original else "\n"
    pending = dict(updates)
    output: list[str] = []
    for line in original.splitlines():
        match = _ENV_ASSIGNMENT.match(line)
        if not match or match.group(2) not in pending:
            output.append(line)
            continue
        key = match.group(2)
        output.append(f"{match.group(1)}{key}{match.group(3)}{_dotenv_value(pending.pop(key))}")
    if pending:
        if output and output[-1].strip():
            output.append("")
        output.append("# BOSS 推荐页岗位名称和 ID（由 tools/resolve_boss_job_ids.py 维护）")
        for key, value in pending.items():
            output.append(f"{key}={_dotenv_value(value)}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(newline.join(output) + newline, encoding="utf-8", newline="")
    os.replace(temporary, path)


def _dotenv_value(value: str) -> str:
    text = str(value or "")
    if "\n" in text or "\r" in text:
        raise ValueError(".env 值不能包含换行")
    return text


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ENV_ASSIGNMENT.match(line)
        if not match:
            continue
        raw = line.split("=", 1)[1].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1]
        values[match.group(2)] = raw
    return values


def _migrate_legacy_database_jobs(cfg: ConfigManager) -> dict[str, int]:
    """Rename known legacy candidate.job values without losing related rows."""
    db_path = cfg.get_path("app.database.path")
    if not db_path or not db_path.exists():
        return {"renamed": 0, "merged": 0}
    renamed = 0
    merged = 0
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for old_name, new_name in LEGACY_JOB_ALIASES.items():
            old_rows = connection.execute(
                "SELECT id, boss_id FROM candidate WHERE job = ?", (old_name,),
            ).fetchall()
            for old_id, boss_id in old_rows:
                target = connection.execute(
                    "SELECT id FROM candidate WHERE boss_id = ? AND job = ?",
                    (boss_id, new_name),
                ).fetchone()
                if target is None:
                    connection.execute(
                        "UPDATE candidate SET job = ? WHERE id = ?", (new_name, old_id),
                    )
                    renamed += 1
                    continue
                target_id = int(target[0])
                for table in ("candidate_analysis", "greeting_record", "sync_record"):
                    connection.execute(
                        f"UPDATE {table} SET candidate_id = ? WHERE candidate_id = ?",
                        (target_id, old_id),
                    )
                connection.execute("DELETE FROM candidate WHERE id = ?", (old_id,))
                merged += 1
    return {"renamed": renamed, "merged": merged}


def _validate_project_bindings(
    cfg: ConfigManager,
    resolved: list[dict[str, Any]],
    env_values: dict[str, str],
) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    canonical_jobs = set(JOB_ID_ENV_KEYS)
    job_tables = cfg.get("feishu.bitable.job_tables", {}) or {}
    if not isinstance(job_tables, dict):
        errors.append("config/feishu.yaml 的 feishu.bitable.job_tables 必须是映射")
        job_tables = {}

    for job_name in JOB_ID_ENV_KEYS:
        profile = cfg.load_job_profile(job_name)
        configured_name = str(profile.get("job_name") or "")
        if configured_name != job_name:
            errors.append(f"config/jobs/{job_name}.yaml 的 job_name 必须等于 {job_name}")
        if job_name not in job_tables:
            errors.append(f"飞书分表配置缺少内部岗位：{job_name}")

    _append_duplicate_value_errors(
        errors,
        {job: env_values.get(key, "").strip() for job, key in FEISHU_TABLE_ENV_KEYS.items()},
        label="飞书 table_id",
    )
    table_names = {
        job: str((value or {}).get("table_name") or "").strip()
        for job, value in job_tables.items()
        if job in canonical_jobs and isinstance(value, dict)
    }
    _append_duplicate_value_errors(errors, table_names, label="飞书 table_name")

    unmatched = [item["profile_name"] for item in resolved if not item["matched"]]
    if unmatched:
        warnings.append("当前 BOSS 账号未精确匹配：" + "、".join(unmatched))

    db_path = cfg.get_path("app.database.path")
    if db_path and db_path.exists():
        try:
            with sqlite3.connect(db_path) as connection:
                rows = connection.execute(
                    "SELECT DISTINCT job FROM candidate WHERE job <> ''"
                ).fetchall()
            unknown = sorted(str(row[0]) for row in rows if str(row[0]) not in canonical_jobs)
            if unknown:
                warnings.append("数据库中存在未配置的 candidate.job：" + "、".join(unknown))
        except (sqlite3.Error, OSError) as exc:
            warnings.append(f"未能检查数据库岗位：{exc}")
    return {"errors": errors, "warnings": warnings}


def _append_duplicate_value_errors(
    errors: list[str], values_by_job: dict[str, str], *, label: str,
) -> None:
    grouped: dict[str, list[str]] = defaultdict(list)
    for job_name, value in values_by_job.items():
        if value:
            grouped[value].append(job_name)
    for value, jobs in grouped.items():
        if len(jobs) > 1:
            errors.append(f"{label}={value} 被重复绑定：{'、'.join(jobs)}")


def _normalize_name(value: str) -> str:
    return re.sub(r"[\s()（）【】\[\]_\-—–,，、/]+", "", value).lower()


def _print_result(payload: dict[str, Any], *, show_boss_jobs: bool) -> None:
    print("# 本地岗位 -> BOSS 推荐页岗位 -> 环境变量")
    for item in payload["resolved"]:
        if item["matched"]:
            print(f"# {item['profile_name']} -> {item['boss_job_name']}")
            print(f"{item['boss_name_env']}={item['boss_job_name']}")
            print(f"{item['boss_id_env']}={item['encrypt_job_id']}")
        else:
            print(f"# {item['profile_name']} -> 未匹配：{item['match_error']}")
            print(f"{item['boss_name_env']}=")
            print(f"{item['boss_id_env']}=")
        print(f"# 飞书表变量：{item['feishu_table_env']}")
        print()

    if payload["env_updated"]:
        print(f"# 已更新：{payload['env_file']}")
    migration = payload.get("database_migration") or {}
    if migration.get("renamed") or migration.get("merged"):
        print(
            "# 数据库岗位迁移："
            f"重命名 {migration.get('renamed', 0)} 条，合并 {migration.get('merged', 0)} 条"
        )
    for message in payload["validation"]["errors"]:
        print(f"# 配置错误：{message}")
    for message in payload["validation"]["warnings"]:
        print(f"# 提醒：{message}")

    if show_boss_jobs and payload.get("boss_jobs"):
        print("# 当前 BOSS 账号返回的岗位列表：")
        for job in payload["boss_jobs"]:
            print(f"# - {job.get('jobName')}: {job.get('encryptJobId')}")


if __name__ == "__main__":
    raise SystemExit(main())
