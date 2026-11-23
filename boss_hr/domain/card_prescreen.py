# -*- coding: utf-8 -*-
"""BOSS 推荐卡片预筛规则。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_AGE_PATTERN = re.compile(r"(\d{1,3})")
_DEGREE_RANK = {
    "初中": 1,
    "高中": 2,
    "中专": 2,
    "职高": 2,
    "技校": 2,
    "大专": 3,
    "专科": 3,
    "本科": 4,
    "硕士": 5,
    "研究生": 5,
    "博士": 6,
}

# 理学 / 工学学士专业目录（教育部《普通高等学校本科专业目录》）。
# require_stem_major=true 的岗位按这份目录判断：专业名命中任一专业即通过，
# 不再依赖岗位 yaml 里零散的专业类别配置。
_STEM_MAJOR_CATALOG = {
    # ---------------- 理学 12 类 ----------------
    "数学类": ("数学与应用数学", "信息与计算科学", "数理基础科学", "数据计算及应用"),
    "物理学类": ("物理学", "应用物理学", "核物理", "声学", "系统科学与工程"),
    "化学类": ("化学", "应用化学", "化学生物学", "分子科学与工程", "能源化学"),
    "天文学类": ("天文学",),
    "地理科学类": ("地理科学", "自然地理与资源环境", "人文地理与城乡规划", "地理信息科学"),
    "大气科学类": ("大气科学", "应用气象学"),
    "海洋科学类": ("海洋科学", "海洋技术", "海洋资源与环境", "军事海洋学"),
    "地球物理学类": ("地球物理学", "空间科学与技术"),
    "地质学类": ("地质学", "地球化学", "地球信息科学与技术", "古生物学"),
    "生物科学类": ("生物科学", "生物技术", "生物信息学", "生态学"),
    "心理学类": ("心理学", "应用心理学"),
    "统计学类": ("统计学", "应用统计学"),
    # ---------------- 工学 31 类 ----------------
    "力学类": ("理论与应用力学", "工程力学"),
    "机械类": (
        "机械工程", "机械设计制造及其自动化", "材料成型及控制工程", "机械电子工程",
        "工业设计", "过程装备与控制工程", "车辆工程", "汽车服务工程", "智能制造工程",
        "应急装备技术与工程", "智能交互设计",
    ),
    "仪器类": ("测控技术与仪器", "智能感知工程"),
    "材料类": (
        "材料科学与工程", "材料物理", "材料化学", "冶金工程", "金属材料工程",
        "无机非金属材料工程", "高分子材料与工程", "复合材料与工程", "粉体材料科学与工程",
        "新能源材料与器件", "材料设计科学与工程", "电子信息材料",
    ),
    "能源动力类": ("能源与动力工程", "能源与环境系统工程", "新能源科学与工程", "储能科学与工程", "氢能科学与工程"),
    "电气类": (
        "电气工程及其自动化", "智能电网信息工程", "光源与照明", "电气工程与智能控制",
        "电机电器智能化", "电缆工程",
    ),
    "电子信息类": (
        "电子信息工程", "电子科学与技术", "通信工程", "微电子科学与工程",
        "光电信息科学与工程", "信息工程", "广播电视工程", "水声工程", "电子封装技术",
        "集成电路设计与集成系统", "医学信息工程", "电磁场与无线技术", "电波传播与天线",
        "电子信息科学与技术", "应用电子技术教育", "人工智能", "海洋信息工程",
        "柔性电子学", "智能测控工程",
    ),
    "自动化类": ("自动化", "轨道交通信号与控制", "机器人工程", "邮政工程", "核电技术与控制工程", "智能装备与系统", "工业智能"),
    "计算机类": (
        "计算机科学与技术", "软件工程", "网络工程", "信息安全", "物联网工程",
        "数字媒体技术", "智能科学与技术", "空间信息与数字技术", "电子与计算机工程",
        "数据科学与大数据技术", "网络空间安全", "新媒体技术", "保密技术",
        "服务科学与工程", "虚拟现实技术", "区块链工程", "密码科学与技术",
    ),
    "土木类": (
        "土木工程", "建筑环境与能源应用工程", "给排水科学与工程", "建筑电气与智能化",
        "城市地下空间工程", "道路桥梁与渡河工程", "铁道工程", "智能建造",
        "土木、水利与海洋工程", "土木、水利与交通工程", "城市水系统工程", "智能建造与智慧交通",
    ),
    "水利类": ("水利水电工程", "水文与水资源工程", "港口航道与海岸工程", "水务工程", "水利科学与工程", "智慧水利"),
    "测绘类": ("测绘工程", "遥感科学与技术", "导航工程", "地理国情监测", "地理空间信息工程"),
    "化工与制药类": ("化学工程与工艺", "制药工程", "资源循环科学与工程", "能源化学工程", "化学工程与工业生物工程", "化工安全工程", "涂料工程", "精细化工"),
    "地质类": ("地质工程", "勘查技术与工程", "资源勘查工程", "地下水科学与工程", "旅游地学与规划工程", "智能地球探测", "资源环境大数据工程"),
    "矿业类": ("采矿工程", "石油工程", "矿物加工工程", "油气储运工程", "矿物资源工程", "海洋油气工程", "智能采矿工程"),
    "纺织类": ("纺织工程", "服装设计与工程", "非织造材料与工程", "服装设计与工艺教育", "丝绸设计与工程"),
    "轻工类": ("轻化工程", "包装工程", "印刷工程", "香料香精技术与工程", "化妆品技术与工程", "生物质能源与材料"),
    "交通运输类": (
        "交通运输", "交通工程", "航海技术", "轮机工程", "飞行技术",
        "交通设备与控制工程", "救助与打捞工程", "船舶电子电气工程",
        "轨道交通电气与控制", "邮轮工程与管理", "智慧交通", "智能运输工程",
    ),
    "海洋工程类": ("船舶与海洋工程", "海洋工程与技术", "海洋资源开发技术", "海洋机器人", "智慧海洋技术"),
    "航空航天类": (
        "航空航天工程", "飞行器设计与工程", "飞行器制造工程", "飞行器动力工程",
        "飞行器环境与生命保障工程", "飞行器质量与可靠性", "飞行器适航技术",
        "飞行器控制与信息工程", "无人驾驶航空器系统工程", "智能飞行器技术",
        "空天智能电推进技术",
    ),
    "兵器类": ("武器系统与工程", "武器发射工程", "探测制导与控制技术", "弹药工程与爆炸技术", "特种能源技术与工程", "装甲车辆工程", "信息对抗技术", "智能无人系统技术"),
    "核工程类": ("核工程与核技术", "辐射防护与核安全", "工程物理", "核化工与核燃料工程"),
    "农业工程类": ("农业工程", "农业机械化及其自动化", "农业电气化", "农业建筑环境与能源工程", "农业水利工程", "土地整治工程", "农业智能装备工程"),
    "林业工程类": ("森林工程", "木材科学与工程", "林产化工", "家具设计与工程", "木结构建筑与材料"),
    "环境科学与工程类": ("环境科学", "环境工程", "环境生态工程", "环保设备工程", "资源环境科学", "水质科学与技术"),
    "生物医学工程类": ("生物医学工程", "假肢矫形工程", "临床工程技术", "康复工程", "智能医学工程"),
    "食品科学与工程类": (
        "食品科学与工程", "食品质量与安全", "粮食工程", "乳品工程", "酿酒工程",
        "葡萄与葡萄酒工程", "食品营养与检验教育", "烹饪与营养教育",
        "食品安全与检测", "食品营养与健康",
    ),
    "建筑类": ("建筑学", "城乡规划", "风景园林", "历史建筑保护工程", "人居环境科学与技术", "城市设计", "智慧建筑与建造"),
    "安全科学与工程类": ("安全工程", "应急技术与管理", "职业卫生工程", "安全生产监管"),
    "生物工程类": ("生物工程", "生物制药", "合成生物学", "生物育种技术"),
    "公安技术类": (
        "刑事科学技术", "消防工程", "交通管理工程", "安全防范工程", "公安视听技术",
        "抢险救援指挥与技术", "火灾勘查", "网络安全与执法", "核生化消防",
        "海警舰艇指挥与技术", "数据警务技术", "食品药品环境犯罪侦查技术",
    ),
}
_NON_STEM_MAJOR_MARKERS = (
    "电子商务", "工商管理", "工程管理", "金融", "经济", "会计",
    "财务", "市场营销", "行政管理", "教育", "文学", "语言",
)

# 预计算：长专业名优先匹配，避免「自动化」抢走「机械设计制造及其自动化」。
_STEM_MAJOR_LOOKUP = tuple(
    sorted(
        (
            (re.sub(r"[\s·•（）()、,，/]+", "", name).casefold(), category)
            for category, names in _STEM_MAJOR_CATALOG.items()
            for name in names
        ),
        key=lambda item: -len(item[0]),
    )
)


@dataclass
class CardPrescreenResult:
    passed: bool
    allow_full_resume: bool = False
    reasons: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    extracted: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.passed:
            return "passed"
        # 只有“信息待确认”时才标记 pending；一旦存在明确不满足的硬条件，
        # 即使还伴随字段缺失，也应保持 rejected，避免审计含义混淆。
        hard_reasons = [reason for reason in self.reasons if not reason.startswith("待确认：")]
        if self.missing_fields and not hard_reasons:
            return "pending_confirmation"
        return "rejected"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "allow_full_resume": self.allow_full_resume,
            "status": self.status,
            "reasons": self.reasons,
            "missing_fields": self.missing_fields,
            "extracted": self.extracted,
        }


def evaluate_card_prescreen(item: dict[str, Any], recruitment: dict[str, Any]) -> CardPrescreenResult:
    """按配置的硬性条件检查 BOSS 推荐卡片。

    ``passed`` 表示卡片字段已完整确认；``allow_full_resume`` 表示是否允许进入
    完整简历阶段。求职状态只存在于完整简历，不在卡片阶段判断；配置中的
    求职状态白名单/黑名单统一交给 ``ResumeAnalyzer`` 读取简历截图后执行。
    """
    config = recruitment.get("card_prescreen") or {}
    enabled = config.get("enabled", True) is not False
    # 显式配置的工作经历要求是独立硬条件；关闭其他卡片预筛不能取消它。
    # 未配置时保持已启用岗位的历史默认，但允许显式 false 关闭该项。
    require_work_experience = config.get("require_work_experience", enabled)
    card = item.get("geekCard") or (item.get("card") or {}).get("geekCard") or item
    work_experiences = _extract_work_experiences(card)
    no_work_marker = any(
        text.strip() in {"未填写工作经历", "未填写工作经验", "无工作经历", "无工作经验", "暂无工作经历"}
        for text in _string_values(card)
    )
    if require_work_experience and (no_work_marker or not work_experiences):
        return CardPrescreenResult(
            passed=False, allow_full_resume=False,
            reasons=["未填写工作经历或无可核验工作经历"],
            extracted={"work_experiences": work_experiences},
        )
    if not enabled:
        return CardPrescreenResult(
            passed=True, allow_full_resume=True, extracted={"disabled": True}
        )

    # 所有岗位统一接受“刚刚活跃”和“在线”。岗位配置可以额外放行其他状态，
    # 但不能通过漏配“在线”把当前在线候选人排除。
    active_keywords = list(dict.fromkeys(
        _as_list(config.get("active_status_keywords")) + ["刚刚活跃", "在线"]
    ))
    # ``null`` / 空值表示该岗位不在卡片阶段筛年龄；未配置时保留历史默认 30。
    max_age_raw = config.get("max_age", 30)
    max_age = int(max_age_raw) if max_age_raw not in (None, "") else None
    min_degree_raw = config.get("min_degree")
    min_degree = str(min_degree_raw) if min_degree_raw not in (None, "") else ""
    # 精确学历白名单（如只收「本科」）：硕士/博士等更高学历也会被拦。
    degree_exact_raw = config.get("degree_exact")
    degree_exact = _as_list(degree_exact_raw) if degree_exact_raw not in (None, "") else []
    city_keyword = str(config.get("city_keyword") or "深圳")
    role_keywords = _as_list(config.get("role_keywords") or config.get("role_keyword") or "主播")
    role_exclude_keywords = _as_list(config.get("role_exclude_keywords") or [])
    # 竞品公司名单：卡片工作经历中的公司名需命中其一
    competitor_companies = _as_list(config.get("competitor_companies") or [])
    # 毕业年份硬筛：教育经历最近一段的 end 年份需匹配
    require_graduation_year = config.get("require_graduation_year")
    # 薪资范围筛选
    salary_min = config.get("salary_min")
    salary_max = config.get("salary_max")
    # 独立规则：只限制候选人期望薪资下限，不限制其区间上限。
    # 仅配置该字段的岗位启用，避免改变其他岗位原有的区间相交逻辑。
    salary_low_max = config.get("salary_low_max")
    # 期望薪资下限门槛：候选人自报的期望下限必须 >= 该值（如 9000）。
    salary_low_min = config.get("salary_low_min")
    # 专业豁免：工作经历中任意一段是电商运营/电商平台运营时，不判理学/工学专业。
    # 只放专业，不放学历：硕士/博士仍然一律淘汰。
    stem_exempt_position_keywords = _as_list(
        config.get("stem_exempt_position_keywords") or []
    )
    stem_exempt_work_limit = int(config.get("stem_exempt_work_limit") or 0)
    salary_accept_negotiable = bool(config.get("salary_accept_negotiable", True))
    active_text = _join_texts(_find_values(item, _active_key))
    age_text = _join_texts(_find_values(item, _age_key))
    # 某些历史接口响应可能带状态描述，仅保留审计；卡片预筛绝不据此判断。
    employment_text = _join_texts(_find_values(item, _employment_key))
    # 学历文本只保留文字；BOSS 卡片里会有 degree=0 这类数值 code，拼进来会变成
    # 「硕士 0 本科」，既影响可读性，也容易让人误以为 0 是学历。
    degree_text = _join_texts([
        value for value in _find_values(item, _degree_key)
        if not str(value).strip().isdigit()
    ])
    expectation_text = _join_texts(_find_values(item, _expectation_key))
    recent_focus_text = _join_texts(_find_values(item, _recent_focus_key))

    reasons: list[str] = []
    missing: list[str] = []

    if not active_text:
        missing.append("活跃状态")
    elif not _active_status_matches(active_text, active_keywords):
        reasons.append(f"活跃状态不满足：{active_text}")

    age = _parse_age(age_text)
    if max_age is not None:
        if age is None:
            missing.append("年龄")
        elif age > max_age:
            reasons.append(f"年龄超过{max_age}岁：{age}岁")

    if min_degree:
        if not degree_text:
            missing.append("学历")
        elif not _degree_at_least(degree_text, min_degree):
            reasons.append(f"学历低于{min_degree}：{degree_text}")

    if degree_exact:
        if not degree_text:
            missing.append("学历")
        else:
            allowed_ranks = {_degree_rank(name) for name in degree_exact}
            actual_rank = _degree_rank(degree_text)
            if actual_rank is None or actual_rank not in allowed_ranks:
                reasons.append(
                    "学历不符合（仅限{}）：{}".format("/".join(degree_exact), degree_text)
                )

    # 城市与岗位默认分别判断。部分岗位可要求“期望”或“最近关注”中的
    # 任一字段独立同时命中城市和岗位，避免跨字段拼接条件后误放行。
    if not expectation_text and not recent_focus_text:
        missing.append("期望/最近关注")
    else:
        require_same_field = bool(config.get("require_city_and_role_in_same_field", False))
        if require_same_field:
            fields = [expectation_text, recent_focus_text]
            if not any(
                (not city_keyword or city_keyword in text)
                and (not role_keywords or _contains_any(text, role_keywords))
                for text in fields
                if text
            ):
                reasons.append(
                    "期望/最近关注需任一字段同时包含"
                    f"“{city_keyword}”和岗位关键词：{_join_texts(role_keywords)}"
                )
        else:
            combined_expectation = _join_texts([expectation_text, recent_focus_text])
            if city_keyword and city_keyword not in combined_expectation:
                reasons.append(f"期望城市不包含“{city_keyword}”：{combined_expectation}")
            if role_keywords and not (
                _contains_any(expectation_text, role_keywords)
                or _contains_any(recent_focus_text, role_keywords)
            ):
                reasons.append(
                    f"期望岗位/最近关注未包含岗位关键词：{_join_texts(role_keywords)}"
                )
        if role_exclude_keywords and _contains_any(expectation_text, role_exclude_keywords):
            reasons.append(
                f"期望岗位包含排除关键词：{_join_texts(role_exclude_keywords)}"
            )

    # 竞品公司卡片预筛：检查 geekWorks 中的 company 字段
    # 数据结构可能嵌套在 item.geekCard 或 item.card.geekCard
    work_companies = [w["company"] for w in work_experiences if w.get("company")]

    # 最近一份工作结束时间下限；只有明确写明“至今”，或结束年月达到
    # 配置下限时才通过。结束时间缺失属于无法确认，必须在卡片阶段拦截。
    latest_work_end_not_before = config.get("latest_work_end_not_before")
    if latest_work_end_not_before:
        if not work_experiences:
            missing.append("最近一份工作结束时间")
        else:
            latest_end = str(work_experiences[0].get("end_date") or "").strip()
            if not latest_end:
                missing.append("最近一份工作结束时间")
            elif not _is_current_end_date(latest_end):
                parsed_latest_end = _parse_date(latest_end)
                parsed_cutoff = _parse_date(latest_work_end_not_before)
                if parsed_latest_end is None:
                    missing.append("最近一份工作结束时间")
                elif parsed_cutoff is not None and parsed_latest_end < parsed_cutoff:
                    reasons.append(
                        f"最近一份工作结束时间早于{latest_work_end_not_before}：{latest_end}"
                    )

    # 近 N 份工作中，至少一段必须命中岗位关键词；如配置最低月数，
    # 则岗位关键词与最低月数必须在同一段经历命中。
    recent_work_position_keywords = _as_list(
        config.get("recent_work_position_keywords") or []
    )
    recent_work_min_months = config.get("recent_work_min_months")
    recent_work_limit = int(config.get("recent_work_limit") or 2)
    qualified_recent_work_experiences: list[dict[str, Any]] = []
    if recent_work_position_keywords or recent_work_min_months:
        recent_works = work_experiences[:recent_work_limit]
        if not recent_works:
            missing.append(f"近{recent_work_limit}份工作经历")
        else:
            position_matched = [
                exp for exp in recent_works
                if not recent_work_position_keywords
                or _contains_any(exp.get("position", ""), recent_work_position_keywords)
            ]
            if not position_matched:
                reasons.append(
                    f"近{recent_work_limit}份工作岗位未包含关键词："
                    f"{_join_texts(recent_work_position_keywords)}"
                )
            elif not recent_work_min_months:
                qualified_recent_work_experiences = position_matched
            else:
                min_months = int(recent_work_min_months)
                qualified_recent_work_experiences = [
                    exp for exp in position_matched
                    if exp.get("months") is not None and int(exp["months"]) >= min_months
                ]
                if not qualified_recent_work_experiences:
                    unknown_duration = [
                        exp for exp in position_matched if exp.get("months") is None
                    ]
                    if unknown_duration:
                        missing.append("相关岗位在职时长")
                    else:
                        actual = "、".join(str(exp.get("months")) for exp in position_matched)
                        reasons.append(
                            f"近{recent_work_limit}份相关岗位经历未满{min_months}个月：{actual}个月"
                        )
    # 竞品公司岗位名白名单：命中竞品公司的那段经历的岗位名需包含其一
    competitor_position_keywords = _as_list(config.get("competitor_position_keywords") or [])
    competitor_position_exclude_keywords = _as_list(
        config.get("competitor_position_exclude_keywords") or []
    )
    # 竞品公司最低工作月数：命中竞品公司的那段经历需满足最低月数
    competitor_min_months = config.get("competitor_min_months")
    competitor_recent_years = config.get("competitor_recent_years")
    qualified_competitor_experiences: list[dict[str, Any]] = []
    # competitor_card_check=false 时，竞品名单只留给简历阶段（如豁免数据）使用，
    # 卡片阶段不再把「未命中竞品」当成淘汰理由。
    if competitor_companies and bool(config.get("competitor_card_check", True)):
        if not work_companies:
            missing.append("工作经历公司名")
        else:
            matched_exps = _match_competitor_experiences(work_experiences, competitor_companies)
            if not matched_exps:
                reasons.append(f"工作经历公司名未命中竞品名单：{', '.join(work_companies[:3])}")
            else:
                # 必须是同一段经历依次满足：竞品公司 → 岗位包含词且不含排除词
                # → 工作月数 →（若配置）近 N 年。禁止把不同经历的条件拼接放行。
                position_matched = [
                    exp
                    for exp in matched_exps
                    if (
                        not competitor_position_keywords
                        or _contains_any(exp.get("position", ""), competitor_position_keywords)
                    )
                    and not _contains_any(
                        exp.get("position", ""), competitor_position_exclude_keywords
                    )
                ]
                if not position_matched:
                    positions = [exp.get("position", "") for exp in matched_exps]
                    reasons.append(
                        f"竞品公司岗位名不符合要求：需包含{competitor_position_keywords}"
                        f"且不包含{competitor_position_exclude_keywords}；"
                        f"实际{', '.join(positions[:3])}"
                    )
                else:
                    duration_matched = position_matched
                    if competitor_min_months:
                        min_months = int(competitor_min_months)
                        duration_matched = [
                            exp
                            for exp in position_matched
                            if exp.get("months") is not None
                            and int(exp["months"]) >= min_months
                        ]
                        if not duration_matched:
                            months_list = [
                                str(exp.get("months"))
                                if exp.get("months") is not None
                                else "无法确认"
                                for exp in position_matched
                            ]
                            reasons.append(
                                f"符合公司和岗位的同一段经历未满{min_months}个月："
                                f"{', '.join(months_list)}"
                            )

                    recent_matched = duration_matched
                    if duration_matched and competitor_recent_years:
                        recent_years = int(competitor_recent_years)
                        recent_matched = [
                            exp
                            for exp in duration_matched
                            if _experience_within_recent_years(exp, recent_years)
                        ]
                        if not recent_matched:
                            reasons.append(
                                f"符合公司、岗位和时长的同一段经历不在近{recent_years}年内"
                            )
                    qualified_competitor_experiences = recent_matched

    # 毕业年份卡片预筛：检查 geekEdus 最近一段的 end 年份
    if require_graduation_year:
        grad_year = _extract_graduation_year(card)
        if grad_year is None:
            missing.append(f"毕业年份({require_graduation_year}届)")
        elif str(grad_year) != str(require_graduation_year):
            reasons.append(f"毕业年份不匹配：需{require_graduation_year}届，实际{grad_year}届")

    # 仅运营助理启用：在打开在线简历之前，从卡片最近一段教育读取专业。
    stem_major_required = bool(config.get("require_stem_major", False))
    stem_exempt = False
    if stem_exempt_position_keywords and work_experiences:
        scope = (
            work_experiences
            if stem_exempt_work_limit <= 0
            else work_experiences[:stem_exempt_work_limit]
        )
        stem_exempt = any(
            _contains_any(str(exp.get("position") or ""), stem_exempt_position_keywords)
            for exp in scope
        )
    latest_major = _extract_latest_major(card) if stem_major_required else ""
    stem_major_category = None
    if stem_major_required and not stem_exempt:
        if not latest_major:
            missing.append("最近一段教育专业")
        else:
            stem_major_category = _stem_major_category(
                latest_major, recruitment.get("stem_major_categories")
            )
            if stem_major_category is None:
                reasons.append(f"最近一段教育专业不属于理学/工学学士专业：{latest_major}")

    # 薪资卡片预筛：salary_low_max 只校验候选人薪资下限；历史
    # salary_min/salary_max 仍按薪资区间是否相交判断。
    if (
        salary_min is not None
        or salary_max is not None
        or salary_low_max is not None
        or salary_low_min is not None
    ):
        low_sal, high_sal = _extract_salary_range(item)
        # BOSS 返回 0 或 None 表示"面议"，不是有效薪资数据
        has_salary = (low_sal is not None and low_sal > 0) or (high_sal is not None and high_sal > 0)
        if not has_salary:
            # 薪资面议：按配置决定是否放行
            if not salary_accept_negotiable:
                reasons.append("薪资面议，不接受面议")
            else:
                pass  # 面议且配置接受，放行
        else:
            if salary_low_max is not None:
                if low_sal is None or low_sal <= 0:
                    missing.append("期望薪资下限")
                elif low_sal > int(salary_low_max):
                    reasons.append(
                        f"期望薪资下限超过{int(salary_low_max)//1000}K："
                        f"{low_sal//1000}K"
                    )
            if salary_low_min is not None:
                if low_sal is None or low_sal <= 0:
                    missing.append("期望薪资下限")
                elif low_sal < int(salary_low_min):
                    reasons.append(
                        f"期望薪资下限低于{int(salary_low_min)//1000}K："
                        f"{low_sal//1000}K"
                    )
            if salary_min is not None or salary_max is not None:
                sal_low = int(salary_min) if salary_min is not None else 0
                sal_high = int(salary_max) if salary_max is not None else 999999
                # 要求候选人薪资范围与岗位薪资范围有交集
                cand_low = low_sal if low_sal and low_sal > 0 else 0
                cand_high = high_sal if high_sal and high_sal > 0 else 999999
                if cand_low > sal_high or cand_high < sal_low:
                    reasons.append(
                        f"薪资范围不匹配：岗位{sal_low//1000}-{sal_high//1000}K，"
                        f"候选人{cand_low//1000 if cand_low else '?'}-{cand_high//1000 if cand_high != 999999 else '?'}K"
                    )

    # 所有卡片阶段真实可见字段的缺失都保留审计标记。求职状态不属于卡片字段，
    # 不进入 missing，也不会阻止打开完整简历。
    if missing:
        reasons.append("待确认：" + "、".join(missing))

    hard_reasons = [reason for reason in reasons if not reason.startswith("待确认：")]
    allow_full_resume = not hard_reasons and not missing

    return CardPrescreenResult(
        passed=not reasons and not missing,
        allow_full_resume=allow_full_resume,
        reasons=reasons,
        missing_fields=missing,
        extracted={
            "active_status": active_text,
            "employment_status": employment_text,
            "age": age,
            "age_text": age_text,
            "degree": degree_text,
            "stem_exempt": stem_exempt,
            "expectation": expectation_text,
            "recent_focus": recent_focus_text,
            "work_companies": work_companies,
            "work_experiences": [
                {
                    "company": w.get("company", ""),
                    "position": w.get("position", ""),
                    "months": w.get("months"),
                    "start_date": w.get("start_date", ""),
                    "end_date": w.get("end_date", ""),
                }
                for w in work_experiences
            ],
            "qualified_competitor_experiences": qualified_competitor_experiences,
            "qualified_recent_work_experiences": qualified_recent_work_experiences,
            "graduation_year": _extract_graduation_year(card),
            **({
                "latest_major": latest_major,
                "stem_major_category": stem_major_category,
            } if stem_major_required else {}),
            "expected_salary_low": _extract_salary_range(item)[0],
            "expected_salary_high": _extract_salary_range(item)[1],
        },
    )


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if value in (None, ""):
        return []
    return [str(value)]


def _find_values(value: Any, key_matcher) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key_matcher(str(key)):
                found.extend(_string_values(child))
            found.extend(_find_values(child, key_matcher))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_values(child, key_matcher))
    return found


def _string_values(value: Any) -> list[str]:
    if isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, dict):
        values: list[str] = []
        for child in value.values():
            values.extend(_string_values(child))
        return values
    if isinstance(value, list):
        values: list[str] = []
        for child in value:
            values.extend(_string_values(child))
        return values
    return []


def _join_texts(values: list[str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return " ".join(out)


def _active_key(key: str) -> bool:
    lowered = key.lower()
    # applyStatusDesc 属于求职状态，不是活跃状态，排除
    if "applystatus" in lowered:
        return False
    return (
        lowered == "statusdesc"
        or lowered == "status"
        or "activetime" in lowered
        or "active" in lowered
        or "online" in lowered
        or "活跃" in key
        or "在线" in key
    )


def _active_status_matches(active_text: str, active_keywords: list[str]) -> bool:
    """匹配活跃状态，并兼容 BOSS 用固定图标表达“在线”的卡片数据。"""
    if any(keyword in active_text for keyword in active_keywords):
        return True
    online_icon_markers = (
        "60dd182bd83b38c354f85155090ba05140e1b2f2809c3065eadc31b1b40a8633",
    )
    return "在线" in active_keywords and any(
        marker in active_text for marker in online_icon_markers
    )


def _employment_key(key: str) -> bool:
    """匹配求职状态文本字段，排除尚未确认含义的数字状态码。"""
    lowered = key.lower()
    return (
        "applystatusdesc" in lowered
        or "employmentstatusdesc" in lowered
        or "jobseekingstatusdesc" in lowered
        or "求职状态" in key
    )


def _age_key(key: str) -> bool:
    lowered = key.lower()
    return "age" in lowered or "年龄" in key


def _degree_key(key: str) -> bool:
    lowered = key.lower()
    return "degree" in lowered or "学历" in key or "education" in lowered


def _expectation_key(key: str) -> bool:
    lowered = key.lower()
    return "expect" in lowered or "期望" in key


def _recent_focus_key(key: str) -> bool:
    lowered = key.lower()
    return (
        "recent" in lowered
        or "focus" in lowered
        or "attention" in lowered
        or "关注" in key
        or "最近" in key
    )


def _parse_age(text: str) -> int | None:
    # BOSS 的 ageLight 等结构里可能混入 0/False，不能只取第一个数字。
    for match in _AGE_PATTERN.finditer(text or ""):
        age = int(match.group(1))
        if 14 <= age <= 100:
            return age
    return None


def _degree_at_least(text: str, minimum: str) -> bool:
    actual = _degree_rank(text)
    required = _degree_rank(minimum)
    if actual is None or required is None:
        return False
    return actual >= required


def _degree_rank(text: str) -> int | None:
    # 卡片学历字段可能是「硕士 本科 硕士」这类多段拼接，必须取最高学历，
    # 否则「本科」先命中会把硕士误判成本科。
    ranks = [rank for name, rank in _DEGREE_RANK.items() if name in (text or "")]
    return max(ranks) if ranks else None


def _contains_all(text: str, keywords: list[str]) -> bool:
    return bool(text) and all(keyword in text for keyword in keywords)


def _contains_any(text: str, keywords: list[str]) -> bool:
    folded = (text or "").casefold()
    return bool(folded) and any(str(keyword).casefold() in folded for keyword in keywords)


def _extract_work_experiences(card: dict[str, Any]) -> list[dict[str, Any]]:
    """从卡片的 geekWorks 提取工作经历列表。

    返回 [{company, position, months, work_time}]，months 为解析后的工作月数。
    """
    experiences: list[dict[str, Any]] = []
    works = card.get("geekWorks") or []
    if isinstance(works, list):
        for w in works:
            if not isinstance(w, dict):
                continue
            company = str(w.get("company") or "").strip()
            if company in {"未填写工作经历", "未填写工作经验", "无工作经历", "无工作经验"}:
                continue
            position = str(w.get("positionName") or w.get("position") or "").strip()
            if not company and not position:
                continue
            work_time = str(w.get("workTime") or "").strip()
            months = _parse_work_months(work_time, w.get("startDate"), w.get("endDate"))
            experiences.append({
                "company": company,
                "position": position,
                "months": months,
                "work_time": work_time,
                "start_date": w.get("startDate") or "",
                "end_date": w.get("endDate") or "",
            })
    return experiences


def _parse_work_months(work_time: str, start_date: Any, end_date: Any) -> int | None:
    """从 workTime 或 startDate/endDate 解析工作月数。

    优先解析带单位的 workTime，其次从 startDate/endDate 计算。
    不把日期、区间或无单位数字误当成月数；无法确认时返回 None。
    """
    text = re.sub(r"\s+", "", str(work_time or ""))
    if text == "半年":
        return 6
    match = re.fullmatch(r"(\d+)年半", text)
    if match:
        return int(match.group(1)) * 12 + 6
    match = re.fullmatch(r"(?:(\d+)年)?(\d+)(?:个)?月", text)
    if match:
        return int(match.group(1) or 0) * 12 + int(match.group(2))
    match = re.fullmatch(r"(\d+(?:\.\d+)?)年", text)
    if match:
        from decimal import Decimal
        return int(Decimal(match.group(1)) * 12)
    # 从 startDate/endDate 计算
    start = _parse_date(start_date)
    if start is None:
        return None
    end_str = str(end_date or "").strip()
    if not end_str or _is_current_end_date(end_str):
        # endDate 为空表示在职，用当前日期
        from datetime import datetime
        end = datetime.now()
    else:
        end = _parse_date(end_date)
        if end is None:
            return None
    # 计算月份差
    months = (end.year - start.year) * 12 + (end.month - start.month)
    return months if end >= start else None


def _parse_date(date_str: Any) -> Any:
    """解析日期字符串，如 '2025.12' 或 '20251201'，返回 datetime 对象。"""
    if not date_str:
        return None
    text = str(date_str).strip()
    if not text:
        return None
    from datetime import datetime
    # 格式 "2025.12" 或 "2025.12.01"
    parts = text.replace("-", ".").split(".")
    try:
        if len(parts) >= 2:
            year = int(parts[0])
            month = int(parts[1])
            day = int(parts[2]) if len(parts) >= 3 and parts[2] else 1
            return datetime(year, month, day)
    except (ValueError, IndexError):
        pass
    # 格式 "202512" 或 "20251201"
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})?", text)
    if match:
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3) or 1),
            )
        except ValueError:
            pass
    return None


def _is_current_end_date(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"至今", "现在", "今", "present", "current", "在职"}


def _experience_within_recent_years(
    experience: dict[str, Any], recent_years: int
) -> bool:
    """判断经历结束时间是否位于近 N 年；结束时间为空表示仍在职。"""
    if recent_years <= 0:
        return True
    from datetime import datetime

    end_text = str(experience.get("end_date") or "").strip()
    if not end_text:
        return _parse_date(experience.get("start_date")) is not None
    if _is_current_end_date(end_text):
        return True
    end = _parse_date(end_text)
    if end is None:
        return False
    now = datetime.now()
    threshold_year = now.year - recent_years
    try:
        threshold = now.replace(year=threshold_year)
    except ValueError:
        threshold = now.replace(year=threshold_year, day=28)
    return end >= threshold


def _match_competitor_experiences(
    work_experiences: list[dict[str, Any]], competitor_list: list[str]
) -> list[dict[str, Any]]:
    """返回命中竞品名单的工作经历列表（双向子串匹配）。"""
    matched: list[dict[str, Any]] = []
    if not work_experiences or not competitor_list:
        return matched
    for exp in work_experiences:
        company = exp.get("company", "").strip()
        if not company:
            continue
        for competitor in competitor_list:
            competitor = competitor.strip()
            if not competitor:
                continue
            if competitor in company or company in competitor:
                matched.append(exp)
                break
    return matched


def _extract_salary_range(item: dict[str, Any]) -> tuple[int | None, int | None]:
    """从卡片或 expect 字段提取薪资范围 (low, high)。

    BOSS 卡片中薪资字段为 lowSalary / highSalary。
    BOSS 返回的数值单位是 K（千元），如 10 表示 10K=10000 元。
    需乘以 1000 转换为元，与 YAML 配置的 salary_min/max（元）比较。
    """
    # 数据结构可能嵌套在 item.geekCard 或 item.card.geekCard
    card = item.get("geekCard") or {}
    if not card:
        outer = item.get("card")
        if isinstance(outer, dict):
            card = outer.get("geekCard") or {}
    low = card.get("lowSalary")
    high = card.get("highSalary")
    # 如果卡片层没有，尝试从顶层或其他嵌套结构中查找
    if low is None or high is None:
        low_vals = _find_values(item, _salary_low_key)
        high_vals = _find_values(item, _salary_high_key)
        if low is None and low_vals:
            low = _parse_salary_number(low_vals[0])
        if high is None and high_vals:
            high = _parse_salary_number(high_vals[0])
    low_n = _parse_salary_number(low) if low is not None else None
    high_n = _parse_salary_number(high) if high is not None else None
    # BOSS 返回的数值单位是 K（千元），乘以 1000 转为元
    if low_n is not None and low_n < 1000:
        low_n = low_n * 1000
    if high_n is not None and high_n < 1000:
        high_n = high_n * 1000
    return low_n, high_n


def _salary_low_key(key: str) -> bool:
    return key.lower() == "lowsalary"


def _salary_high_key(key: str) -> bool:
    return key.lower() == "highsalary"


def _parse_salary_number(value: Any) -> int | None:
    """解析薪资数值，接受 int/float/str（如 11000, '11K', '1.1万'）。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    # 处理 "11K" / "11k" / "1.1万" 等格式
    if "万" in text:
        try:
            return int(float(text.replace("万", "")) * 10000)
        except ValueError:
            return None
    if "k" in text.lower():
        try:
            return int(float(text.lower().replace("k", "")) * 1000)
        except ValueError:
            return None
    # 纯数字
    match = re.search(r"\d+", text)
    if match:
        return int(match.group())
    return None


def _extract_graduation_year(card: dict[str, Any]) -> int | None:
    """从卡片的 geekEdus 提取最近一段教育的毕业年份。

    BOSS 返回的 endDate 格式为 "2027.01" 或 "20270101"。
    取最近一段教育（geekEdus[0]）的 end 年份。
    """
    edus = card.get("geekEdus") or []
    if not isinstance(edus, list) or not edus:
        return None
    # 取第一段（最近）教育
    edu = edus[0] if isinstance(edus[0], dict) else {}
    end_date = str(edu.get("endDate") or "")
    # 从 "2027.01" 或 "20270101" 中提取年份
    import re
    match = re.search(r"(\d{4})", end_date)
    if match:
        return int(match.group(1))
    return None


def _extract_latest_major(card: dict[str, Any]) -> str:
    edus = card.get("geekEdus") or []
    if not isinstance(edus, list) or not edus or not isinstance(edus[0], dict):
        return ""
    return str(edus[0].get("major") or "").strip()


def _stem_major_category(major: str, configured: Any = None) -> str | None:
    """按「理学/工学学士专业目录」判断；configured 仅为兼容旧调用保留。"""
    normalized = re.sub(r"[\s·•（）()、,，/]+", "", major).casefold()
    if not normalized:
        return None
    # 历史特例：大数据管理与应用（管理学）一直按统计学类放行。
    if "大数据管理与应用" in normalized:
        return "统计学类"
    if any(marker.casefold() in normalized for marker in _NON_STEM_MAJOR_MARKERS):
        return None
    # 卡片上的专业名常少写末尾的「学」：应用统计 / 统计学。只补这一个字，
    # 不做事后截断，避免「生物科」「数学与应用数」这类片段去误命中其他专业。
    candidates = (normalized, normalized + "学") if not normalized.endswith("学") else (normalized,)
    for candidate in candidates:
        for key, category in _STEM_MAJOR_LOOKUP:
            if key and key in candidate:
                return category
    return None


__all__ = ["CardPrescreenResult", "evaluate_card_prescreen"]
