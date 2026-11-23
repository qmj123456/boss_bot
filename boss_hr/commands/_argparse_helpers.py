# -*- coding: utf-8 -*-
"""boss_hr.commands._argparse_helpers — argparse 必填参数辅助。

统一 CLI 行为（v1.2.1 起）：
  缺 --run-id         → argparse rc=2（run_id 是数据边界，永远必填）
  缺 --job-name       → 自动兜底：HR_AGENT_DEFAULT_JOB_NAME / app.default_job_name
                        / 首个 config/jobs/*.yaml；都没有才 rc=2
  缺 --encrypt-job-id → 自动兜底：BOSS_JOB_ID_<岗位>（--job-name 传本地名或
                        .env BOSS_JOB_NAME_* 显示名均可）> BOSS_HR_ENCRYPT_JOB_ID；
                        全缺 → argparse rc=2

--job-name 传 BOSS 显示名（如"高级主播（往直播运营发展）"）会自动映射回本地
岗位名（如"全职主播"），保证 BOSS_JOB_ID_* env 与 config/jobs/<名>.yaml 都能命中。
"""
from __future__ import annotations
import argparse

from boss_hr.job_id_resolver import (
    env_key_for_job,
    local_name_for,
    resolve_encrypt_job_id as resolve_job_encrypt_id,
)


def add_required_arguments(parser: argparse.ArgumentParser) -> None:
    """注册 status / report / confirm / automate 等命令的公共参数。

    不直接 parser.add_argument(required=True)，因为 --job-name / --encrypt-job-id
    都有 env / config 兜底；强制校验在 require_encrypt_job_id() 里做
    （args parse 完之后）。
    """
    parser.add_argument(
        "--job-name", default=None,
        help="岗位名（可省略：默认 app.default_job_name；传 BOSS 显示名会自动映射回本地岗位名）",
    )
    parser.add_argument(
        "--encrypt-job-id", default=None,
        help="BOSS encryptJobId（可省略：默认按岗位读 env BOSS_JOB_ID_* / BOSS_HR_ENCRYPT_JOB_ID）",
    )
    parser.add_argument("--run-id", required=True, help="【必填】run_id 是数据边界")


def resolve_encrypt_job_id(cli_value: str | None) -> str | None:
    """CLI 透传 > 环境变量 > None。"""
    return resolve_job_encrypt_id(cli_value=cli_value)


def _default_job_name() -> str:
    """默认岗位名：HR_AGENT_DEFAULT_JOB_NAME > app.default_job_name > 首个岗位 profile。"""
    try:
        from boss_hr.config_manager import get_config
        cfg = get_config()
        return str(cfg.default_job_profile_name() or "").strip()
    except Exception:
        return ""


def _resolve_job_name(parser: argparse.ArgumentParser, ns: argparse.Namespace) -> str:
    """解析并写回 ns.job_name：缺省 → config 默认岗位；显示名 → 本地名。"""
    job_name = str(getattr(ns, "job_name", "") or "").strip()
    if not job_name:
        job_name = _default_job_name()
        if not job_name:
            parser.error(
                "缺少 --job-name（且未配置 app.default_job_name / config/jobs/*.yaml）"
            )
    ns.job_name = local_name_for(job_name)
    return ns.job_name


def require_encrypt_job_id(parser: argparse.ArgumentParser, ns: argparse.Namespace) -> str:
    """解析 job_name + encrypt_job_id（顺带把结果写回 ns.job_name）。

    job_name 解析顺序：--job-name > app.default_job_name（含
    HR_AGENT_DEFAULT_JOB_NAME env）；BOSS 显示名自动映射回本地岗位名。

    eid 解析顺序：--encrypt-job-id > env BOSS_JOB_ID_<岗位> > BOSS_HR_ENCRYPT_JOB_ID。
    全缺 → argparse.error（rc=2）。返回解析后的 eid（非空）。
    """
    _resolve_job_name(parser, ns)
    eid = resolve_job_encrypt_id(
        cli_value=getattr(ns, "encrypt_job_id", None),
        job_name=ns.job_name,
    )
    if not eid:
        env_key = env_key_for_job(ns.job_name)
        hint = f"、{env_key}（岗位：{ns.job_name}）" if env_key else ""
        parser.error(
            f"缺少 --encrypt-job-id（亦可通过环境变量 BOSS_HR_ENCRYPT_JOB_ID{hint} 传入）"
        )
    return eid


__all__ = [
    "add_required_arguments",
    "resolve_encrypt_job_id",
    "require_encrypt_job_id",
]
