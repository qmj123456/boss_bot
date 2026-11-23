"""Resume analysis capability."""

from __future__ import annotations

import json
import re
import base64
import mimetypes
from datetime import date
from pathlib import Path
from typing import Any

from ai.client import AIClient, OpenAICompatibleClient
from ai.errors import ResumeAnalysisUnavailableError
from ai.resume_rules import (
    evidence_contract_errors,
    enforce_evidence,
    evidence_rules,
    normalize_selected_work_index,
    normalize_work_evidence,
    work_evidence_schema,
)
from boss_hr.config_manager import ConfigManager, get_config


class ResumeAnalyzer:
    """Analyze one candidate resume against one job requirement."""

    def __init__(
        self,
        *,
        config: ConfigManager | None = None,
        client: AIClient | None = None,
    ) -> None:
        self.config = config or get_config()
        self.client = client or OpenAICompatibleClient(self.config)

    def analyze(
        self,
        *,
        job_requirement: dict[str, Any] | str,
        resume: dict[str, Any] | str,
        max_tokens_override: int | None = None,
    ) -> dict[str, Any]:
        prompt_name = self.config.get("ai.prompts.resume_analysis", "review_resume.md")
        system_prompt = self.config.load_prompt(str(prompt_name))
        employment_rules = _employment_status_rules(job_requirement)
        salary_rules = _expected_salary_rules(job_requirement)
        user_prompt = self._build_user_prompt(job_requirement, resume)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_user_content(user_prompt, resume)},
        ]
        audit: dict[str, Any] = {"local_checks": [], "repair": {"attempted": False}}
        try:
            if max_tokens_override is None:
                raw = self.client.complete_json(messages)
            else:
                raw = self.client.complete_json(
                    messages, max_tokens_override=max_tokens_override
                )
        except Exception as exc:
            audit["completion"] = getattr(exc, "audit", {}) or self._completion_audit()
            audit["completion_error_code"] = getattr(exc, "code", type(exc).__name__)
            raise ResumeAnalysisUnavailableError(
                f"AI分析响应不可用：{exc}", code="completion_unavailable", audit=audit,
            ) from exc
        audit["raw_result"] = raw
        audit["completion"] = self._completion_audit()
        try:
            result = self._normalize_result(
                raw, employment_rules=employment_rules, salary_rules=salary_rules,
            )
        except ResumeAnalysisUnavailableError as exc:
            audit["validation_errors"] = exc.audit.get("validation_errors", [str(exc)])
            raise ResumeAnalysisUnavailableError(str(exc), code=exc.code, audit=audit) from exc
        if result["reason"] != raw["reason"]:
            audit["local_checks"].append({
                "stage": "employment_and_salary", "passed": result["passed"],
                "reason": result["reason"],
            })
        rules = evidence_rules(job_requirement)
        if rules:
            evidence_payload = dict(raw)
            allow_empty = _coerce_bool(raw["passed"]) is False and bool(re.search(
                r"(?:无|没有|未填写|未展示)(?:任何)?(?:工作|实习)(?:[/／及和、]实习)?经历",
                raw["reason"],
            ))
            contract_errors = evidence_contract_errors(evidence_payload, rules, allow_empty=allow_empty)
            audit["evidence_contract_errors"] = list(contract_errors)
            # Retry extraction once against the original source. Business-rule
            # failures (e.g. a valid [] outcomes array) never trigger this repair.
            if (
                contract_errors
                and isinstance(resume, dict)
                and resume.get("resume_screenshots")
            ):
                audit["repair"]["attempted"] = True
                try:
                    repaired = self._repair_structured_evidence(
                        messages=messages, raw=raw, errors=contract_errors, rules=rules,
                        max_tokens_override=max_tokens_override,
                    )
                    audit["repair"].update({"raw_result": repaired, "completion": self._completion_audit()})
                    candidate_payload = dict(raw)
                    # Replace required fields as a unit; a partially repaired
                    # object must not accidentally inherit stale tail fields.
                    candidate_payload["work_evidence"] = repaired.get("work_evidence")
                    if rules.get("platform_experience"):
                        candidate_payload["selected_work_index"] = repaired.get("selected_work_index")
                    contract_errors = evidence_contract_errors(candidate_payload, rules, allow_empty=allow_empty)
                    audit["repair"]["validation_errors"] = list(contract_errors)
                    if not contract_errors:
                        evidence_payload = candidate_payload
                except Exception as exc:
                    audit["repair"].update({
                        "error": str(exc), "error_code": getattr(exc, "code", type(exc).__name__),
                        "completion": getattr(exc, "audit", {}) or self._completion_audit(),
                    })
            if contract_errors:
                audit["validation_errors"] = contract_errors
                raise ResumeAnalysisUnavailableError(
                    "工作经历提取不完整：" + "；".join(contract_errors),
                    code="incomplete_evidence", audit=audit,
                )
            evidence_payload["work_evidence"] = normalize_work_evidence(evidence_payload.get("work_evidence"))
            errors = enforce_evidence(evidence_payload, rules)
            audit["local_checks"].append({"stage": "work_evidence", "errors": errors})
            result["work_evidence"] = evidence_payload["work_evidence"]
            if rules.get("platform_experience") and "selected_work_index" in evidence_payload:
                selected_index = normalize_selected_work_index(
                    evidence_payload["selected_work_index"]
                )
                result["selected_work_index"] = selected_index
            if errors:
                result["passed"] = False
                result["reason"] += "\n工作经历规则：" + "；".join(errors) + "。最终结论：不通过。"
        result["analysis_status"] = "passed" if result["passed"] else "rejected"
        audit["final_result"] = dict(result)
        result["analysis_audit"] = audit
        return result

    def _completion_audit(self) -> dict[str, Any]:
        getter = getattr(self.client, "get_completion_audit", None)
        return getter() if callable(getter) else {}

    def _repair_structured_evidence(
        self,
        *,
        messages: list[dict[str, Any]],
        raw: dict[str, Any],
        errors: list[str],
        rules: dict[str, Any],
        max_tokens_override: int | None = None,
    ) -> dict[str, Any]:
        """Retry only incomplete evidence fields against the same resume images."""
        repair_request = {
            "task": "修复上一次输出中不完整的结构化工作经历证据",
            "validation_errors": errors,
            "requirements": [
                "重新阅读同一组完整简历截图，输出全部工作/实习经历，按时间由近到远",
                "每段必须包含required_output中该岗位要求的全部字段，不得省略字段",
                "content仅复制核验平台、市场、品类、职责和数字成果所需的连续原句，不复制无关长段落",
                "只修复结构化证据，不改变上一次passed、reason或employment_status结论",
            ],
            "required_output": {
                "work_evidence": work_evidence_schema(rules),
            },
        }
        if rules.get("platform_experience"):
            repair_request["required_output"]["selected_work_index"] = (
                "integer，从0开始，指向任职最长的平台相关经历；没有相关经历时为null"
            )
        repair_messages = list(messages)
        repair_messages.extend([
            {
                "role": "assistant",
                "content": json.dumps(raw, ensure_ascii=False),
            },
            {
                "role": "user",
                "content": json.dumps(repair_request, ensure_ascii=False),
            },
        ])
        if max_tokens_override is None:
            repaired = self.client.complete_json(repair_messages)
        else:
            repaired = self.client.complete_json(
                repair_messages, max_tokens_override=max_tokens_override
            )
        if not isinstance(repaired, dict):
            raise ValueError("证据补提取响应不是JSON对象")
        return repaired

    @staticmethod
    def _build_user_content(prompt: str, resume: dict[str, Any] | str) -> Any:
        """Build a text or multimodal OpenAI-compatible user message.

        Screenshot paths are local implementation details.  They are converted to
        data URLs here so the configured vision-capable DeepSeek-compatible model
        receives the actual online-resume images rather than filesystem paths.
        """
        if not isinstance(resume, dict):
            return prompt
        paths = resume.get("resume_screenshots") or []
        if not isinstance(paths, list) or not paths:
            return prompt

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for raw_path in paths:
            image_path = Path(str(raw_path))
            if not image_path.is_file():
                raise FileNotFoundError(f"Resume screenshot not found: {image_path}")
            mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            })
        return content

    @staticmethod
    def _build_user_prompt(
        job_requirement: dict[str, Any] | str,
        resume: dict[str, Any] | str,
    ) -> str:
        # 过滤 job_requirement，只保留与简历硬性条件判断相关的字段，
        # 去掉 card_prescreen / daily_send_limit / work_time / risk_control
        # 等流程配置，避免干扰 LLM 判断、节省 token。
        if isinstance(job_requirement, dict):
            keep_fields = (
                "job_name",
                "experience_required",
                "degree_required",
                "skill_requirements",
                "additional_requirements",
                "stem_major_categories",
                "resume_analysis",
            )
            filtered = {k: v for k, v in job_requirement.items() if k in keep_fields}
            # 竞品名单虽存放在卡片配置中，也是完整简历硬性条件的依据。
            # 只提取竞品核验字段，避免把活跃状态、年龄、城市等卡片条件带入。
            competitor_fields = (
                "competitor_companies",
                "competitor_position_keywords",
                "competitor_position_exclude_keywords",
                "competitor_min_months",
                "competitor_recent_years",
            )
            card_rules = job_requirement.get("card_prescreen") or {}
            for field in competitor_fields:
                if field in job_requirement:
                    filtered[field] = job_requirement[field]
                elif field in card_rules:
                    filtered[field] = card_rules[field]
            # 已在卡片阶段核验专业的岗位，不再把专业类别交给简历 AI 重判。
            if (job_requirement.get("card_prescreen") or {}).get("require_stem_major"):
                filtered.pop("stem_major_categories", None)
            # 兼容尚未迁移的岗位配置：历史上求职状态名单放在 card_prescreen
            # 下，但该字段实际只存在于完整简历。仅提取这两个名单交给 AI，
            # 不把其他卡片条件带入简历分析。
            employment_rules = _employment_status_rules(job_requirement)
            if employment_rules:
                resume_analysis = dict(filtered.get("resume_analysis") or {})
                resume_analysis.update(employment_rules)
                filtered["resume_analysis"] = resume_analysis
            # 兜底：即使 yaml 的 additional_requirements 里误混入了卡片预筛/
            # BOSS 推荐池前置选择段落（这些条件已由 card_prescreen 代码硬筛
            # + boss_filter 面板过滤，AI 不应再据此判断简历），也剥掉这些段落，
            # 避免 AI 把卡片字段缺失当作简历不通过 → 误杀真实通过者。
            add_req = filtered.get("additional_requirements")
            if isinstance(add_req, str):
                stripped = _strip_card_prescreen_sections(add_req)
                if stripped != add_req:
                    filtered["additional_requirements"] = stripped
        else:
            filtered = job_requirement
        resume_payload = resume
        if isinstance(resume, dict) and resume.get("resume_screenshots"):
            # 图片本体由多模态 content 携带；Prompt 只保留页数，避免把本地
            # 绝对路径发给模型并浪费 token。
            resume_payload = dict(resume)
            resume_payload["resume_screenshots"] = {
                "count": len(resume.get("resume_screenshots") or []),
                "instruction": "请按顺序阅读本消息所附的全部在线简历截图",
            }
        payload = {
            "current_date": date.today().isoformat(),
            "job_requirement": filtered,
            "resume": resume_payload,
            "required_output_json_schema": {
                "passed": "boolean, 是否满足岗位全部硬性条件",
                "reason": "string, 逐条对照要求的判断依据",
                "employment_status": (
                    "string, 必须从完整简历截图的求职状态/求职意向区域逐字读取；"
                    "看不清或未展示时返回空字符串"
                ),
            },
        }
        if _expected_salary_rules(job_requirement):
            payload["required_output_json_schema"]["expected_salary"] = (
                "string, 必须从完整简历截图的求职意向区域逐字读取期望薪资；"
                "例如5-7K。面议、看不清或未展示时返回空字符串"
            )
        rules = evidence_rules(job_requirement)
        if rules:
            payload["required_output_json_schema"]["work_evidence"] = work_evidence_schema(rules)
            if rules.get("platform_experience"):
                payload["required_output_json_schema"]["selected_work_index"] = (
                    "integer，work_evidence中任职最长的平台相关经历下标，从0开始；后续全部条件仅核验此段；没有相关经历时为null"
                )
                if (rules["platform_experience"].get("fallback") or {}).get("self_intro"):
                    payload["required_output_json_schema"]["self_intro"] = (
                        "string，完整简历顶部“自我介绍/个人优势/个人简介”区域原文；"
                        "用于平台关键词与市场信号的兜底判断；未展示时返回空字符串"
                    )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _normalize_result(
        data: dict[str, Any],
        *,
        employment_rules: dict[str, list[str]] | None = None,
        salary_rules: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ResumeAnalysisUnavailableError("AI分析结果不是JSON对象")
        passed = _coerce_bool(data.get("passed"))
        reason = data.get("reason")
        errors = []
        if passed is None:
            errors.append("passed缺失或不是可识别的布尔值")
        if not isinstance(reason, str) or not reason.strip():
            errors.append("reason缺失或不是非空字符串")
        for field, required in (("employment_status", employment_rules), ("expected_salary", salary_rules)):
            if required and not isinstance(data.get(field), str):
                errors.append(field + "缺失或不是字符串")
        if errors:
            raise ResumeAnalysisUnavailableError(
                "AI分析字段不完整：" + "；".join(errors),
                audit={"validation_errors": errors},
            )
        conclusion = _reason_conclusion(reason)
        if conclusion is not None and conclusion != passed:
            raise ResumeAnalysisUnavailableError(
                "AI的passed与reason最终结论矛盾，需重新分析", code="conflicting_conclusion",
            )
        employment_status = str(data.get("employment_status", "") or "").strip()
        passed, reason = _enforce_employment_status_rules(
            passed,
            reason,
            employment_status,
            employment_rules or {},
        )
        expected_salary = str(data.get("expected_salary", "") or "").strip()
        passed, reason = _enforce_expected_salary_rules(
            passed,
            reason,
            expected_salary,
            salary_rules or {},
        )
        result = {
            "passed": passed,
            "reason": reason,
        }
        if employment_rules or "employment_status" in data:
            result["employment_status"] = employment_status
        if salary_rules or "expected_salary" in data:
            result["expected_salary"] = expected_salary
        return result


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1", "通过", "pass", "passed", "符合", "合格", "满足全部", "ok"}:
            return True
        if normalized in {"false", "no", "0", "不通过", "fail", "failed", "不符合", "不合格", "未通过", "不满足", "未满足"}:
            return False
    return None


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]


def _employment_status_rules(
    job_requirement: dict[str, Any] | str,
) -> dict[str, list[str]]:
    """读取完整简历阶段的求职状态名单，并兼容历史配置位置。"""
    if not isinstance(job_requirement, dict):
        return {}
    resume_analysis = job_requirement.get("resume_analysis") or {}
    card_prescreen = job_requirement.get("card_prescreen") or {}
    if not isinstance(resume_analysis, dict):
        resume_analysis = {}
    if not isinstance(card_prescreen, dict):
        card_prescreen = {}

    whitelist = _as_string_list(
        resume_analysis.get("employment_status_whitelist")
        or job_requirement.get("employment_status_whitelist")
        or card_prescreen.get("employment_status_whitelist")
    )
    blacklist = _as_string_list(
        resume_analysis.get("employment_status_blacklist")
        or job_requirement.get("employment_status_blacklist")
        or card_prescreen.get("employment_status_blacklist")
    )
    rules: dict[str, list[str]] = {}
    if whitelist:
        rules["employment_status_whitelist"] = whitelist
    if blacklist:
        rules["employment_status_blacklist"] = blacklist
    return rules


def _expected_salary_rules(
    job_requirement: dict[str, Any] | str,
) -> dict[str, Any]:
    """读取完整简历阶段的期望薪资硬筛配置，金额单位为元/月。"""
    if not isinstance(job_requirement, dict):
        return {}
    resume_analysis = job_requirement.get("resume_analysis") or {}
    if not isinstance(resume_analysis, dict):
        return {}
    minimum = resume_analysis.get("expected_salary_min")
    maximum = resume_analysis.get("expected_salary_max")
    if minimum is None and maximum is None:
        return {}
    return {
        "minimum": int(minimum) if minimum is not None else None,
        "maximum": int(maximum) if maximum is not None else None,
        "basis": str(resume_analysis.get("expected_salary_basis") or "full_range"),
        "accept_negotiable": bool(
            resume_analysis.get("expected_salary_accept_negotiable", False)
        ),
    }


def _parse_expected_salary_range(value: str) -> tuple[int, int] | None:
    """解析月薪文本；无法确定完整上下界时返回 None。"""
    text = str(value or "").strip().replace(",", "").replace("，", "")
    if not text or any(token in text for token in ("面议", "可谈", "不限")):
        return None
    if any(token in text for token in ("以上", "以下", "起", "+")):
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if not numbers or len(numbers) > 2:
        return None
    multiplier = 1000 if re.search(r"[kK千]", text) else 1
    values = [int(float(number) * multiplier) for number in numbers]
    if len(values) == 1:
        values.append(values[0])
    low, high = sorted(values)
    return low, high


def _enforce_expected_salary_rules(
    passed: bool,
    reason: str,
    expected_salary: str,
    rules: dict[str, Any],
) -> tuple[bool, str]:
    """本地执行期望薪资范围，避免模型漏判或误判。"""
    if not rules:
        return passed, reason
    if (
        rules.get("accept_negotiable")
        and any(token in expected_salary for token in ("面议", "可谈"))
    ):
        return passed, reason
    parsed = _parse_expected_salary_range(expected_salary)
    if parsed is None:
        detail = "期望薪资未展示、为面议或无法确认完整范围"
        return False, _append_salary_rule_reason(reason, detail)
    low, high = parsed
    minimum = rules.get("minimum")
    maximum = rules.get("maximum")
    basis = rules.get("basis") or "full_range"
    actual_min = low
    actual_max = low if basis == "lower_bound" else high
    if (minimum is not None and actual_min < minimum) or (
        maximum is not None and actual_max > maximum
    ):
        min_label = f"{int(minimum) // 1000}K" if minimum is not None else "不限"
        max_label = f"{int(maximum) // 1000}K" if maximum is not None else "不限"
        if basis == "lower_bound":
            detail = (
                f"期望薪资“{expected_salary}”的下限未落在"
                f"{min_label}-{max_label}范围内"
            )
        else:
            detail = (
                f"期望薪资“{expected_salary}”未完整落在"
                f"{min_label}-{max_label}范围内"
            )
        return False, _append_salary_rule_reason(reason, detail)
    return passed, reason


def _normalize_employment_status(value: str) -> str:
    """统一截图 OCR/模型输出中常见的空格和连接符差异。"""
    return re.sub(r"[\s\-—–·•_/]+", "", str(value or "")).strip()


def _status_matches(actual: str, configured: str) -> bool:
    actual_normalized = _normalize_employment_status(actual)
    configured_normalized = _normalize_employment_status(configured)
    return bool(
        actual_normalized
        and configured_normalized
        and actual_normalized == configured_normalized
    )


def _enforce_employment_status_rules(
    passed: bool,
    reason: str,
    employment_status: str,
    rules: dict[str, list[str]],
) -> tuple[bool, str]:
    """在 DeepSeek 读取截图后本地执行名单，防止模型结论与名单矛盾。"""
    whitelist = rules.get("employment_status_whitelist") or []
    blacklist = rules.get("employment_status_blacklist") or []
    if not whitelist and not blacklist:
        return passed, reason

    if not employment_status:
        detail = "求职状态无法从完整简历截图确认，无法执行求职状态名单校验"
        return False, _append_rule_reason(reason, detail)

    matched_blacklist = next(
        (item for item in blacklist if _status_matches(employment_status, item)),
        None,
    )
    if matched_blacklist:
        detail = f"求职状态“{employment_status}”命中黑名单“{matched_blacklist}”"
        return False, _append_rule_reason(reason, detail)

    if whitelist and not any(
        _status_matches(employment_status, item) for item in whitelist
    ):
        detail = (
            f"求职状态“{employment_status}”不在白名单"
            f"（{', '.join(whitelist)}）"
        )
        return False, _append_rule_reason(reason, detail)

    return passed, reason


def _append_rule_reason(reason: str, detail: str) -> str:
    base = str(reason or "").strip()
    suffix = f"求职状态规则：{detail}。最终结论：不通过。"
    return f"{base}\n{suffix}" if base else suffix


def _append_salary_rule_reason(reason: str, detail: str) -> str:
    base = str(reason or "").strip()
    suffix = f"期望薪资规则：{detail}。最终结论：不通过。"
    return f"{base}\n{suffix}" if base else suffix


def _reason_conclusion(reason: str) -> bool | None:
    """Read a final conclusion conservatively; never match partial pass clauses."""
    if not reason:
        return None
    text = re.sub(r"\s+", "", reason)
    markers = list(re.finditer(r"最终结论[：:]|结论[：:]", text))
    if not markers:
        markers = list(re.finditer(r"综上(?:所述)?[，,:：]?|因此[，,:：]?", text))
    tail = text[markers[-1].end():] if markers else text
    # Without a conclusion marker only a standalone decision is unambiguous.
    if not markers and not re.fullmatch(
        r"(?:通过(?:筛选)?|不通过|未通过|合格|不合格|符合|不符合)[。.!！]*", tail,
    ):
        return None
    negative = re.search(
        r"(?:不|未|不能|无法)(?:予以)?通过|(?:不|未|尚未|不能|无法)(?:全部|完全)?(?:满足|符合)"
        r"|不合格|不达标|拒绝|淘汰", tail,
    )
    if negative:
        return False
    # A trailing qualification or counterexample makes the conclusion unclear.
    if re.search(r"但是|但|然而|缺少|缺乏|缺失|缺[少乏失]?[^。]*条件|尚缺", tail):
        return None
    if re.search(r"(?:^|[，,。；;：:]|判定(?:为)?|结论(?:为)?)(?:通过(?:筛选)?|合格|符合)[。.!！]*$", tail):
        return True
    if re.search(
        r"(?:所有|全部)(?:硬性)?条件(?:均|皆|全部)?(?:满足|符合)"
        r"|(?:满足|符合)(?:全部|所有)(?:硬性)?条件", tail,
    ):
        return True
    return None


def _reason_concludes_pass(reason: str) -> bool:
    """Compatibility helper; conclusions are checked, never used to override passed."""
    return _reason_conclusion(reason) is True


# 卡片预筛/BOSS 推荐池前置选择段落标题。这些段落里写的卡片条件（如
# "刚刚活跃""期望薪资A-B""卡片必须显示"等）属于 card_prescreen 代码
# 硬筛职责。求职状态是例外：它通过 resume_analysis 独立传入，并由 AI 从
# 完整简历截图读取。AI 不应从这些卡片规则段落推断求职状态。
# 这里按段落标题整段剥掉，只保留【完整简历必须全部满足】【直接排除】
# 【有效成果口径】等真正属于简历判断阶段的段落。
_CARD_PRESCREEN_SECTION_TITLES = (
    "【BOSS岗位】",
    "【BOSS推荐池前置选择】",
    "【推荐池与卡片预筛】",
    "【卡片预筛规则】",
    "【卡片预筛】",
    "【推荐池前置选择】",
    "【BOSS 推荐池前置选择】",
)


def _strip_card_prescreen_sections(text: str) -> str:
    """剥掉 additional_requirements 文本里属于卡片预筛/推荐池前置的整段内容。

    段落以【XXX】标题开头，到下一个【XXX】标题前结束。匹配标题在
    _CARD_PRESCREEN_SECTION_TITLES 中的段落整段删除。
    """
    if not text:
        return text
    lines = text.split("\n")
    keep_lines: list[str] = []
    skip_mode = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("【") and "】" in stripped:
            title = stripped[:stripped.index("】") + 1]
            if title in _CARD_PRESCREEN_SECTION_TITLES or title.startswith("【卡片预筛"):
                skip_mode = True
                continue
            skip_mode = False
        if skip_mode:
            continue
        keep_lines.append(line)
    # 清理尾部多余空行
    result = "\n".join(keep_lines)
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result.rstrip() + "\n" if result else result


__all__ = ["ResumeAnalyzer"]
