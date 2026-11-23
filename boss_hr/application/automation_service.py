"""端到端招聘自动化应用服务。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from boss_hr.contracts.errors import ErrorCode, ExitCode, UnifiedError
from boss_hr.contracts.results import CommandResult, error, ok
from workflow.recruit_workflow import RecruitWorkflow


def run_automation(
    *,
    job_name: str,
    encrypt_job_id: str,
    run_id: str,
    count: int,
    max_greetings: int | None = None,
    dry_run: bool = False,
    sync_feishu: bool = True,
    skip_fetch: bool = False,
    requirements_file: str | None = None,
    only_names: str | None = None,
    greet_only: bool = False,
    refresh_page: bool = False,
) -> CommandResult:
    """运行已确认 run 的自动化流程并返回统一 CLI 结果。

    ``greet_only=True`` 时复用已有 ``run_id``，仅执行招呼阶段（daemon 重试池
    定向打招呼），跳过 start/confirm/fetch/analysis；``only_names`` 为逗号分隔
    的候选人姓名，仅在招呼阶段（含 greet_only）生效。
    """
    try:
        workflow = RecruitWorkflow()
        if greet_only:
            result = workflow.send_greetings_and_save_records(
                job_name=job_name,
                encrypt_job_id=encrypt_job_id,
                run_id=run_id,
                max_count=max_greetings,
                only_names=only_names,
                dry_run=dry_run,
                refresh_page=refresh_page,
            )
        else:
            requirement = _load_job_requirement(requirements_file)
            result = workflow.run_end_to_end(
                job_name=job_name,
                encrypt_job_id=encrypt_job_id,
                run_id=run_id,
                count=count,
                max_greetings=max_greetings,
                dry_run=dry_run,
                sync_feishu=sync_feishu,
                skip_fetch=skip_fetch,
                job_requirement=requirement,
                refresh_page=refresh_page,
            )
    except Exception as exc:
        return error(
            error_obj=UnifiedError(ErrorCode.INTERNAL, f"自动化初始化失败：{exc}"),
            run_id=run_id,
            encrypt_job_id=encrypt_job_id,
            job_name=job_name,
            exit_code=ExitCode.INTERNAL,
        )

    if not result.ok:
        return error(
            error_obj=UnifiedError(
                ErrorCode.INTERNAL,
                result.error or f"自动化在 {result.stage} 阶段失败",
            ),
            run_id=run_id,
            encrypt_job_id=encrypt_job_id,
            job_name=job_name,
            exit_code=ExitCode.GENERIC,
            data={"stage": result.stage, "status": result.status, **result.data},
        )
    return ok(
        status=result.status,
        run_id=run_id,
        encrypt_job_id=encrypt_job_id,
        job_name=job_name,
        data=result.data,
        next_action="done",
    )


def _load_job_requirement(path: str | None) -> dict[str, Any] | str | None:
    """读取可选的独立岗位要求文件；未传时由工作流读取 recruitment.yaml。"""
    if not path:
        return None
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"岗位要求文件不存在：{source}")
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() == ".json":
        value = json.loads(text)
    elif source.suffix.lower() in (".yaml", ".yml"):
        value = yaml.safe_load(text)
    else:
        # Markdown/文本岗位说明原样交给 AI，避免重复定义解析规则。
        return text
    if not isinstance(value, (dict, str)):
        raise ValueError("岗位要求文件必须是 JSON/YAML 对象或文本")
    return value


__all__ = ["run_automation"]
