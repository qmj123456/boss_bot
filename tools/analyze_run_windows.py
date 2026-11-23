# -*- coding: utf-8 -*-
"""Build a per-candidate audit report for scheduled daemon log windows."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


JOB_RUN_DIRS = {
    "亚马逊运营": "a10903dc992042f60nN82d2-FFBS",
    "tiktok运营": "1ae5fae847649e620nN82d2-GVdW",
    "运营助理": "08cfaa0b97a572b00nN43d64EFpR",
    "客服": "f22420c4468a6a050nN93d-9GFZY",
}
TIME_PATTERN = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})]")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def log_bounds(path: Path) -> tuple[datetime, datetime]:
    stamps = [
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        for value in TIME_PATTERN.findall(path.read_text(encoding="utf-8"))
    ]
    if not stamps:
        raise ValueError(f"日志没有时间戳：{path}")
    return stamps[0], stamps[-1]


def run_time(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.name, "%Y-%m-%d_%H%M%S")
    except ValueError:
        return None


def candidate_id(item: dict[str, Any]) -> str:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else item
    meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
    return str(
        item.get("encrypt_geek_id")
        or item.get("encryptGeekId")
        or meta.get("encrypt_geek_id")
        or raw.get("encryptGeekId")
        or ""
    )


def candidate_name(item: dict[str, Any]) -> str:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else item
    card = raw.get("geekCard") if isinstance(raw.get("geekCard"), dict) else {}
    return str(item.get("name") or card.get("geekName") or raw.get("name") or "未显示姓名")


def reason_categories(reasons: list[str]) -> set[str]:
    text = "；".join(str(reason) for reason in reasons)
    categories: set[str] = set()
    mappings = [
        ("活跃状态", ("活跃", "在线")),
        ("年龄", ("年龄",)),
        ("城市", ("城市", "期望城市")),
        ("期望岗位/关注岗位", ("期望岗位", "最近关注")),
        ("最近工作结束时间", ("工作结束时间",)),
        ("最近工作岗位关键词", ("工作岗位未包含关键词", "相关岗位")),
        ("工作时长", ("未满", "在职时长")),
        ("学历", ("学历", "本科", "大专")),
        ("毕业年份", ("毕业年份", "27届", "2027")),
        ("专业类别", ("专业不属于", "教育专业")),
        ("薪资", ("薪资",)),
        ("竞品公司", ("竞品", "公司名未命中")),
        ("缺少卡片字段", ("待确认", "无法确认", "缺少")),
        ("无工作经历", ("无工作经历",)),
    ]
    for category, tokens in mappings:
        if any(token in text for token in tokens):
            categories.add(category)
    if not categories:
        categories.add("其他")
    return categories


def compact(text: Any) -> str:
    return " ".join(str(text or "").replace("|", "／").split())


def build_window(root: Path, label: str, start: datetime, end: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": label,
        "start": start,
        "end": end,
        "jobs": {},
    }
    for job_name, job_id in JOB_RUN_DIRS.items():
        runs_root = root / "data" / "runs" / job_id / "runs"
        selected = []
        if runs_root.exists():
            for path in runs_root.iterdir():
                stamp = run_time(path)
                if stamp is not None and start <= stamp <= end:
                    selected.append(path)
        selected.sort(key=lambda p: p.name)
        if not selected:
            continue

        job: dict[str, Any] = {
            "runs": [path.name for path in selected],
            "listed": 0,
            "card_accepted": 0,
            "card_rejected": 0,
            "pending_blocked": 0,
            "card_reject_people": {},
            "accepted_people": {},
            "resume_people": {},
            "technical_people": {},
            "greet_people": {},
            "resume_passed_count": 0,
            "resume_rejected_count": 0,
            "technical_count": 0,
            "greeted_count": 0,
            "category_counts": Counter(),
            "technical_stage_counts": Counter(),
        }
        for run_path in selected:
            process = run_path / "process"
            summary = read_json(process / "card_prescreen_summary.json", {})
            accepted_count = int(summary.get("accepted_count") or 0)
            rejected_count = int(summary.get("rejected_count") or 0)
            job["listed"] += accepted_count + rejected_count
            job["card_accepted"] += accepted_count
            job["card_rejected"] += rejected_count
            job["pending_blocked"] += int(summary.get("pending_blocked_count") or 0)

            for item in read_json(process / "card_prescreen_rejected.json", []):
                if not isinstance(item, dict):
                    continue
                cid = candidate_id(item) or f"{run_path.name}:{candidate_name(item)}"
                record = job["card_reject_people"].setdefault(
                    cid,
                    {"name": candidate_name(item), "runs": [], "reasons": []},
                )
                record["runs"].append(run_path.name)
                reasons = [compact(value) for value in (item.get("reasons") or [])]
                for reason in reasons:
                    if reason and reason not in record["reasons"]:
                        record["reasons"].append(reason)
                for category in reason_categories(reasons):
                    job["category_counts"][category] += 1

            for item in read_json(process / "recommend_geek_ids.json", []):
                if not isinstance(item, dict):
                    continue
                cid = candidate_id(item) or f"{run_path.name}:{candidate_name(item)}"
                record = job["accepted_people"].setdefault(
                    cid,
                    {"name": candidate_name(item), "runs": []},
                )
                record["runs"].append(run_path.name)

            screening = read_json(process / "screening_results.json", {})
            for item in screening.get("candidates", []) if isinstance(screening, dict) else []:
                if not isinstance(item, dict):
                    continue
                cid = candidate_id(item) or f"{run_path.name}:{candidate_name(item)}"
                job["resume_people"][cid] = {
                    "name": candidate_name(item),
                    "run": run_path.name,
                    "passed": bool(item.get("passed")),
                    "decision": compact(item.get("decision")),
                    "reason": compact(item.get("reason")),
                }
                if item.get("passed"):
                    job["resume_passed_count"] += 1
                else:
                    job["resume_rejected_count"] += 1
            failures = screening.get("failures", []) if isinstance(screening, dict) else []
            failures += read_json(process / "failed_resumes.json", [])
            seen_failures_in_run: set[str] = set()
            for item in failures:
                if not isinstance(item, dict):
                    continue
                cid = candidate_id(item) or f"{run_path.name}:{candidate_name(item)}"
                if cid not in seen_failures_in_run:
                    job["technical_count"] += 1
                    job["technical_stage_counts"][compact(item.get("stage") or "unknown")] += 1
                    seen_failures_in_run.add(cid)
                job["technical_people"][cid] = {
                    "name": candidate_name(item),
                    "run": run_path.name,
                    "stage": compact(item.get("stage")),
                    "error": compact(item.get("error") or item.get("reason")),
                    "attempts": item.get("attempts") or item.get("analysis_attempts") or item.get("screenshot_attempts"),
                }

            greet = read_json(process / "greet_log.json", {})
            for item in greet.get("results", []) if isinstance(greet, dict) else []:
                if not isinstance(item, dict):
                    continue
                cid = candidate_id(item) or f"{run_path.name}:{candidate_name(item)}"
                job["greet_people"][cid] = {
                    "name": candidate_name(item),
                    "run": run_path.name,
                    "status": compact(item.get("status")),
                    "message_sent": bool(item.get("message_sent")),
                }
                if item.get("status") == "greeted":
                    job["greeted_count"] += 1
        result["jobs"][job_name] = job
    return result


def render(window: dict[str, Any]) -> list[str]:
    lines = [
        f"## {window['label']}",
        "",
        f"时间范围：{window['start']:%Y-%m-%d %H:%M:%S} ～ {window['end']:%Y-%m-%d %H:%M:%S}",
        "",
        "| 岗位 | run数 | 卡片次数 | 卡片淘汰次数 | 卡片淘汰去重人数 | 卡片通过次数 | 卡片通过去重人数 | 待确认拦截 | 简历通过 | 简历淘汰 | 技术失败 | 打招呼 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for job_name, job in window["jobs"].items():
        lines.append(
            f"| {job_name} | {len(job['runs'])} | {job['listed']} | {job['card_rejected']} | "
            f"{len(job['card_reject_people'])} | {job['card_accepted']} | {len(job['accepted_people'])} | "
            f"{job['pending_blocked']} | {job['resume_passed_count']} | "
            f"{job['resume_rejected_count']} | {job['technical_count']} | {job['greeted_count']} |"
        )
    lines += ["", "### 卡片淘汰原因分布", ""]
    for job_name, job in window["jobs"].items():
        distribution = "；".join(
            f"{name} {count}次" for name, count in job["category_counts"].most_common()
        ) or "无"
        lines.append(f"- **{job_name}**：{distribution}")

    lines += ["", "### 卡片阶段逐人明细", ""]
    for job_name, job in window["jobs"].items():
        lines += [f"#### {job_name}", ""]
        if not job["card_reject_people"]:
            lines.append("无卡片淘汰记录。")
            lines.append("")
            continue
        lines += ["| 候选人 | 出现run | 淘汰原因 |", "|---|---|---|"]
        for record in job["card_reject_people"].values():
            lines.append(
                f"| {compact(record['name'])} | {', '.join(record['runs'])} | "
                f"{'；'.join(record['reasons']) or '审计文件未记录原因'} |"
            )
        lines.append("")

    lines += ["### 完整简历阶段逐人明细", ""]
    for job_name, job in window["jobs"].items():
        lines += [f"#### {job_name}", ""]
        processed_ids = set(job["resume_people"]) | set(job["technical_people"])
        skipped = {
            cid: value for cid, value in job["accepted_people"].items()
            if cid not in processed_ids
        }
        if job["resume_people"]:
            lines += ["| 候选人 | run | 结论 | 原因 |", "|---|---|---|---|"]
            for record in job["resume_people"].values():
                conclusion = "通过" if record["passed"] else "淘汰"
                lines.append(
                    f"| {compact(record['name'])} | {record['run']} | {conclusion} | {record['reason']} |"
                )
            lines.append("")
        if job["technical_people"]:
            lines += ["| 候选人 | run | 技术阶段 | 错误 | 尝试次数 |", "|---|---|---|---|---:|"]
            for record in job["technical_people"].values():
                lines.append(
                    f"| {compact(record['name'])} | {record['run']} | {record['stage']} | "
                    f"{record['error']} | {record['attempts'] or ''} |"
                )
            lines.append("")
        if skipped:
            names = "、".join(compact(value["name"]) for value in skipped.values())
            lines.append(
                f"卡片通过但本窗口没有新增完整简历结论（通常为历史状态去重/已经处理）：{names}"
            )
            lines.append("")
        if job["greet_people"]:
            greeted = "、".join(
                compact(value["name"]) for value in job["greet_people"].values()
            )
            lines.append(f"实际执行打招呼：{greeted}")
            lines.append("")
        if not job["resume_people"] and not job["technical_people"] and not skipped:
            lines.append("没有进入完整简历处理的候选人。")
            lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--first-log", required=True)
    parser.add_argument("--second-log", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    first_log = Path(args.first_log).resolve()
    second_log = Path(args.second_log).resolve()
    first_start, _ = log_bounds(first_log)
    second_start, second_end = log_bounds(second_log)
    first_end = second_start

    windows = [
        build_window(root, first_log.name, first_start, first_end),
        build_window(root, second_log.name, second_start, second_end),
    ]
    lines = [
        "# 两个守护运行时间段逐人审计",
        "",
        "说明：卡片阶段明细来自 `card_prescreen_rejected.json`；完整简历结论来自 "
        "`screening_results.json` 和 `failed_resumes.json`。同一候选人重复出现时合并展示run。",
        "",
    ]
    for window in windows:
        lines.extend(render(window))
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for window in windows:
        print(window["label"], window["start"], window["end"])
        for job_name, job in window["jobs"].items():
            print(json.dumps({
                "job": job_name,
                "runs": len(job["runs"]),
                "card_occurrences": job["listed"],
                "card_rejected_occurrences": job["card_rejected"],
                "card_rejected_unique": len(job["card_reject_people"]),
                "card_accepted_occurrences": job["card_accepted"],
                "card_accepted_unique": len(job["accepted_people"]),
                "pending_blocked": job["pending_blocked"],
                "resume_passed": job["resume_passed_count"],
                "resume_rejected": job["resume_rejected_count"],
                "technical_failed": job["technical_count"],
                "greeted": job["greeted_count"],
                "card_reason_categories": dict(job["category_counts"]),
                "technical_stages": dict(job["technical_stage_counts"]),
            }, ensure_ascii=False))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
