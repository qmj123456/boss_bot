from pathlib import Path

import yaml

from boss_hr.domain.card_prescreen import evaluate_card_prescreen


ROOT = Path(__file__).resolve().parents[1]


def _recruitment():
    return {
        "card_prescreen": {
            "enabled": True,
            "active_status_keywords": ["刚刚活跃", "在线"],
            "max_age": 30,
            "min_degree": "高中",
            "city_keyword": "深圳",
            "role_keyword": "主播",
        }
    }


def _employment_recruitment():
    recruitment = _recruitment()
    recruitment["card_prescreen"]["employment_status_whitelist"] = ["离职-随时到岗"]
    return recruitment


def _complete_candidate(**status_fields):
    card = {
        "geekName": "测试候选人",
        "activeTimeDesc": "刚刚活跃",
        "ageDesc": "26岁",
        "degreeCategory": "大专",
        "expectCityName": "深圳",
        "expectPosition": "带货主播",
        "geekWorks": [_work("测试公司", "主播", "2025.01", "2026.08")],
    }
    card.update(status_fields)
    return {"encryptGeekId": "gid-employment", "geekCard": card}


def _job_recruitment(job_name):
    source = ROOT / "config" / "jobs" / f"{job_name}.yaml"
    return yaml.safe_load(source.read_text(encoding="utf-8"))["recruitment"]


def test_all_active_job_profiles_allow_just_active_and_online():
    for source in (ROOT / "config" / "jobs").glob("*.yaml"):
        payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        recruitment = payload.get("recruitment") or {}
        if not recruitment.get("job_name"):
            continue
        assert recruitment["card_prescreen"]["active_status_keywords"][:2] == [
            "刚刚活跃",
            "在线",
        ], source.name
        assert recruitment["boss_filter"]["active_status"] == [
            "刚刚活跃",
            "在线",
        ], source.name


def _competitor_candidate(expect_position, works):
    return {
        "encryptGeekId": "gid-competitor",
        "geekCard": {
            "geekName": "竞品候选人",
            "activeTimeDesc": "刚刚活跃",
            "ageDesc": "28岁",
            "expectLocationName": "深圳",
            "expectPositionName": expect_position,
            "applyStatusDesc": "离职-随时到岗",
            "geekWorks": works,
        },
    }


def _work(company, position, start, end):
    return {
        "company": company,
        "positionName": position,
        "startDate": start,
        "endDate": end,
    }


def test_anchor_card_prescreen_passes_when_expectation_matches():
    item = {
        "encryptGeekId": "gid-1",
        "geekCard": {
            "geekName": "张三",
            "geekWorks": [_work("测试公司", "主播", "2025.01", "2026.08")],
            "activeTimeDesc": "刚刚活跃",
            "ageDesc": "26岁",
            "degreeCategory": "大专",
            "expectCityName": "深圳",
            "expectPosition": "带货主播",
        },
    }

    result = evaluate_card_prescreen(item, _recruitment())

    assert result.passed is True
    assert result.status == "passed"
    assert result.reasons == []
    assert result.extracted["age"] == 26


def test_anchor_card_prescreen_passes_when_recent_focus_matches():
    item = {
        "encryptGeekId": "gid-2",
        "geekCard": {
            "geekName": "李四",
            "geekWorks": [_work("测试公司", "主播", "2025.01", "2026.08")],
            "statusDesc": "在线",
            "ageDesc": "30岁",
            "degreeName": "高中",
            "expectPosition": "直播运营",
            "recentFocus": "深圳 电商主播",
        },
    }

    result = evaluate_card_prescreen(item, _recruitment())

    assert result.passed is True


def test_online_icon_status_is_accepted_even_when_yaml_omits_online_keyword():
    item = _complete_candidate(
        activeTimeDesc="",
        activeStatus=0,
        activeIcon=(
            "https://img.bosszhipin.com/beijin/icon/"
            "60dd182bd83b38c354f85155090ba05140e1b2f2809c3065eadc31b1b40a8633.png"
        ),
    )
    recruitment = _recruitment()
    recruitment["card_prescreen"]["active_status_keywords"] = ["刚刚活跃"]

    result = evaluate_card_prescreen(item, recruitment)

    assert result.passed is True
    assert not any("活跃状态不满足" in reason for reason in result.reasons)


def test_parttime_anchor_requires_city_and_role_in_the_same_card_field():
    item = {
        "encryptGeekId": "gid-parttime-split",
        "geekCard": {
            "geekName": "兼职候选人",
            "geekWorks": [_work("测试公司", "主播", "2025.01", "2026.08")],
            "activeTimeDesc": "刚刚活跃",
            "ageDesc": "26岁",
            "degreeCategory": "大专",
            "expectCityName": "深圳",
            "expectPosition": "直播运营",
            "recentFocus": "电商主播",
        },
    }

    result = evaluate_card_prescreen(item, _job_recruitment("兼职主播"))

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("任一字段同时包含" in reason for reason in result.reasons)


def test_parttime_anchor_passes_when_recent_focus_contains_city_and_role():
    item = {
        "encryptGeekId": "gid-parttime-focus",
        "geekCard": {
            "geekName": "兼职候选人",
            "geekWorks": [_work("测试公司", "主播", "2025.01", "2026.08")],
            "activeTimeDesc": "在线",
            "ageDesc": "30岁",
            "degreeName": "高中",
            "expectPosition": "直播运营",
            "recentFocus": "深圳 电商主播",
        },
    }

    result = evaluate_card_prescreen(item, _job_recruitment("兼职主播"))

    assert result.passed is True


def test_fulltime_anchor_salary_uses_expected_lower_bound_only():
    item = _complete_candidate(lowSalary=13, highSalary=20)

    result = evaluate_card_prescreen(item, _job_recruitment("全职主播"))

    assert result.passed is True
    assert result.extracted["expected_salary_low"] == 13000
    assert result.extracted["expected_salary_high"] == 20000


def test_fulltime_anchor_rejects_expected_salary_lower_bound_above_13k():
    item = _complete_candidate(lowSalary=14, highSalary=15)

    result = evaluate_card_prescreen(item, _job_recruitment("全职主播"))

    assert result.passed is False
    assert "期望薪资下限超过13K：14K" in result.reasons


def test_anchor_card_prescreen_rejects_over_age_before_resume_download():
    item = {
        "encryptGeekId": "gid-3",
        "geekCard": {
            "geekWorks": [_work("测试公司", "主播", "2025.01", "2026.08")],
            "activeTimeDesc": "刚刚活跃",
            "ageDesc": "31岁",
            "degreeCategory": "本科",
            "expectCityName": "深圳",
            "expectPosition": "主播",
        },
    }

    result = evaluate_card_prescreen(item, _recruitment())

    assert result.passed is False
    assert result.status == "rejected"
    assert "年龄超过30岁：31岁" in result.reasons


def test_anchor_card_prescreen_marks_missing_fields_pending_confirmation():
    item = {
        "encryptGeekId": "gid-4",
        "geekCard": {
            "geekWorks": [_work("测试公司", "主播", "2025.01", "2026.08")],
            "activeTimeDesc": "刚刚活跃",
            "expectCityName": "深圳",
            "expectPosition": "主播",
        },
    }

    result = evaluate_card_prescreen(item, _recruitment())

    assert result.passed is False
    assert result.status == "pending_confirmation"
    assert result.missing_fields == ["年龄", "学历"]
    assert "待确认：年龄、学历" in result.reasons


def test_anchor_card_prescreen_ignores_zero_from_age_light():
    item = {
        "encryptGeekId": "gid-5",
        "geekCard": {
            "geekWorks": [_work("测试公司", "主播", "2025.01", "2026.08")],
            "activeTime": 0,
            "activeTimeDesc": "刚刚活跃",
            "ageDesc": "27岁",
            "ageLight": {"content": "27岁", "highlight": False},
            "geekDegree": "大专",
            "expectLocationName": "深圳",
            "expectPositionName": "主播",
        },
    }

    result = evaluate_card_prescreen(item, _recruitment())

    assert result.passed is True
    assert result.extracted["age"] == 27


def test_missing_apply_status_desc_is_not_a_card_prescreen_field():
    result = evaluate_card_prescreen(_complete_candidate(), _employment_recruitment())

    assert result.passed is True
    assert result.status == "passed"
    assert result.allow_full_resume is True
    assert result.missing_fields == []
    assert result.to_dict()["allow_full_resume"] is True


def test_explicit_non_immediate_employment_status_is_deferred_to_resume_analysis():
    result = evaluate_card_prescreen(
        _complete_candidate(applyStatusDesc="在职-月内到岗"),
        _employment_recruitment(),
    )

    assert result.passed is True
    assert result.status == "passed"
    assert result.allow_full_resume is True
    assert result.reasons == []


def test_immediate_employment_status_passes():
    result = evaluate_card_prescreen(
        _complete_candidate(applyStatusDesc="离职-随时到岗"),
        _employment_recruitment(),
    )

    assert result.passed is True
    assert result.status == "passed"
    assert result.allow_full_resume is True


def test_alternative_employment_status_desc_is_supported_without_mapping_numeric_code():
    result = evaluate_card_prescreen(
        _complete_candidate(employmentStatusDesc="离职-随时到岗", applyStatus=3),
        _employment_recruitment(),
    )

    assert result.passed is True
    assert result.extracted["employment_status"] == "离职-随时到岗"


def test_missing_employment_status_does_not_override_another_hard_rejection():
    result = evaluate_card_prescreen(
        _complete_candidate(ageDesc="31岁"),
        _employment_recruitment(),
    )

    assert result.status == "rejected"
    assert result.allow_full_resume is False
    assert "求职状态" not in result.missing_fields
    assert "年龄超过30岁：31岁" in result.reasons


def test_operation_card_requires_company_position_and_months_on_same_experience():
    result = evaluate_card_prescreen(
        _competitor_candidate(
            "京东运营",
            [_work("机乐堂", "电商运营", "2025.07", "2026.05")],
        ),
        _job_recruitment("运营"),
    )

    assert result.passed is True
    qualified = result.extracted["qualified_competitor_experiences"]
    assert len(qualified) == 1
    assert qualified[0]["months"] == 10


def test_operation_card_rejects_each_configured_expected_role_exclusion():
    for excluded in ("跨境", "亚马逊", "用户", "产品"):
        result = evaluate_card_prescreen(
            _competitor_candidate(
                f"{excluded}运营",
                [_work("机乐堂", "京东运营", "2025.07", "2026.09")],
            ),
            _job_recruitment("运营"),
        )

        assert result.passed is False
        assert result.allow_full_resume is False
        assert any("期望岗位包含排除关键词" in reason for reason in result.reasons)


def test_operation_card_exclusion_only_applies_to_expected_role():
    candidate = _competitor_candidate(
        "京东运营",
        [_work("机乐堂", "京东运营", "2025.07", "2026.09")],
    )
    candidate["geekCard"]["recentFocus"] = "亚马逊运营"

    result = evaluate_card_prescreen(candidate, _job_recruitment("运营"))

    assert result.passed is True


def _operation_assistant_candidate(major, older_major=""):
    edus = [{"major": major, "endDate": "2027.06"}]
    if older_major:
        edus.append({"major": older_major, "endDate": "2023.06"})
    return {
        "encryptGeekId": "gid-operation-assistant-major",
        "geekCard": {
            "activeTimeDesc": "刚刚活跃",
            "ageDesc": "24岁",
            "expectLocationName": "深圳",
            "expectPositionName": "运营助理",
            "degreeName": "本科",
            "geekWorks": [_work("测试公司", "运营实习生", "2026.01", "2026.08")],
            "geekEdus": edus,
        },
    }


def test_operation_assistant_card_accepts_configured_stem_major_examples():
    majors = (
        "软件工程", "大数据管理与应用", "统计学", "计算机科学与技术",
        "数学与应用数学", "机械设计制造及其自动化",
        # 以下是旧别名表会漏掉、新目录应放行的理学/工学专业
        "电子科学与技术", "数理基础科学", "环境生态工程", "地球物理学",
        "生物医学工程", "智能科学与技术", "非织造材料与工程", "安全工程",
        "土木工程", "食品科学与工程", "化学工程与工艺", "人工智能",
    )
    for major in majors:
        result = evaluate_card_prescreen(
            _operation_assistant_candidate(major),
            _job_recruitment("运营助理"),
        )

        assert result.passed is True, major
        assert result.allow_full_resume is True, major
        assert result.extracted["stem_major_category"], major


def test_operation_assistant_card_accepts_major_missing_trailing_xue():
    # 卡片上的专业名常少写末尾的「学」：应用统计学 → 应用统计。不能因此判成非理学。
    for major in ("应用统计", "统计学", "统计", "数据科学与大数据技术"):
        result = evaluate_card_prescreen(
            _operation_assistant_candidate(major),
            _job_recruitment("运营助理"),
        )

        assert result.passed is True, major
        assert result.extracted["stem_major_category"], major


def _render_candidate(works=None, salary=(9, 12)):
    return {
        "encryptGeekId": "gid-render",
        "geekCard": {
            "geekName": "渲染候选人",
            "activeTimeDesc": "刚刚活跃",
            "ageDesc": "28岁",
            "expectLocationName": "深圳",
            "expectPositionName": "3D设计师",
            "lowSalary": salary[0],
            "highSalary": salary[1],
            "geekWorks": works or [_work("设计公司", "3D设计师", "2025.07", "2026.09")],
        },
    }


def test_render_rejects_latest_job_ended_before_202608():
    result = evaluate_card_prescreen(
        _render_candidate([_work("设计公司", "3D设计师", "2025.01", "2026.07")]),
        _job_recruitment("渲染"),
    )

    assert result.passed is False
    assert any("结束时间早于2026.08" in reason for reason in result.reasons)


def test_render_accepts_latest_job_202608_or_current():
    for end in ("2026.08", "2026.09", "至今"):
        result = evaluate_card_prescreen(
            _render_candidate([_work("设计公司", "3D设计师", "2025.07", end)]),
            _job_recruitment("渲染"),
        )

        assert result.passed is True, end


def test_render_rejects_expected_salary_below_9k():
    result = evaluate_card_prescreen(
        _render_candidate(salary=(8, 13)),
        _job_recruitment("渲染"),
    )

    assert result.passed is False
    assert any("期望薪资下限低于9K" in reason for reason in result.reasons)


def test_render_accepts_expected_salary_9k_or_more():
    for low, high in ((9, 12), (10, 15), (12, 18)):
        result = evaluate_card_prescreen(
            _render_candidate(salary=(low, high)),
            _job_recruitment("渲染"),
        )

        assert result.passed is True, (low, high)


def test_latest_end_and_salary_rules_configured_for_render_editor_senior():
    for job in ("渲染", "编导", "高级剪拍"):
        card = _job_recruitment(job)["card_prescreen"]
        assert card.get("latest_work_end_not_before") == "2026.08", job
        assert card.get("salary_low_min") == 9000, job


def test_operation_assistant_card_rejects_non_stem_major_before_resume():
    for major in ("跨境电子商务", "德语", "金融工程", "工程管理"):
        result = evaluate_card_prescreen(
            _operation_assistant_candidate(major, older_major="软件工程"),
            _job_recruitment("运营助理"),
        )

        assert result.status == "rejected", major
        assert result.allow_full_resume is False, major
        assert any("专业不属于理学/工学学士专业" in reason for reason in result.reasons)


def test_operation_assistant_card_rejects_master_degree():
    candidate = _operation_assistant_candidate("软件工程")
    candidate["geekCard"]["degreeName"] = "硕士"

    result = evaluate_card_prescreen(candidate, _job_recruitment("运营助理"))

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("仅限本科" in reason for reason in result.reasons)


def test_operation_assistant_major_exempt_for_ecommerce_operation():
    # 任意一段电商平台运营经历 → 不判专业，管理学/文科都可以。
    for position in ("天猫运营", "京东运营", "亚马逊运营", "国内电商运营", "跨境电商运营"):
        candidate = _operation_assistant_candidate("电子商务")
        candidate["geekCard"]["geekWorks"] = [
            _work("电商公司", position, "2025.01", "2026.09")
        ]

        result = evaluate_card_prescreen(candidate, _job_recruitment("运营助理"))

        assert result.passed is True, position
        assert result.extracted["stem_exempt"] is True, position


def test_operation_assistant_major_exempt_for_any_segment_not_just_latest():
    candidate = _operation_assistant_candidate("工商管理")
    candidate["geekCard"]["geekWorks"] = [
        _work("某公司", "行政专员", "2026.01", "2026.09"),
        _work("电商公司", "天猫运营", "2025.01", "2025.12"),
    ]

    result = evaluate_card_prescreen(candidate, _job_recruitment("运营助理"))

    assert result.passed is True
    assert result.extracted["stem_exempt"] is True


def test_operation_assistant_rejects_master_even_with_ecommerce_operation():
    # 坚决不要硕士：即使有真实电商运营经历，学历仍然卡本科。
    candidate = _operation_assistant_candidate("软件工程")
    candidate["geekCard"]["degreeName"] = "硕士"
    candidate["geekCard"]["geekWorks"] = [
        _work("电商公司", "天猫运营", "2025.01", "2026.09")
    ]

    result = evaluate_card_prescreen(candidate, _job_recruitment("运营助理"))

    assert result.passed is False
    assert any("仅限本科" in reason for reason in result.reasons)


def test_operation_assistant_without_ecommerce_operation_still_requires_stem_major():
    candidate = _operation_assistant_candidate("电子商务")
    candidate["geekCard"]["geekWorks"] = [
        _work("某公司", "行政专员", "2025.01", "2026.09")
    ]

    result = evaluate_card_prescreen(candidate, _job_recruitment("运营助理"))

    assert result.passed is False
    assert result.extracted["stem_exempt"] is False
    assert any("专业不属于理学/工学学士专业" in reason for reason in result.reasons)


def test_degree_text_ignores_numeric_degree_code():
    # BOSS 卡片可能带 degree=0 这类数值 code，不能拼进学历文本。
    candidate = _operation_assistant_candidate("软件工程")
    candidate["geekCard"]["degree"] = 0

    result = evaluate_card_prescreen(candidate, _job_recruitment("运营助理"))

    assert result.passed is True
    assert "0" not in result.extracted["degree"]


def test_operation_assistant_card_blocks_missing_major_before_resume():
    result = evaluate_card_prescreen(
        _operation_assistant_candidate(""),
        _job_recruitment("运营助理"),
    )

    assert result.status == "pending_confirmation"
    assert result.allow_full_resume is False
    assert "最近一段教育专业" in result.missing_fields


def test_other_job_card_does_not_use_operation_assistant_major_rule():
    candidate = _complete_candidate()
    candidate["geekCard"]["geekEdus"] = [{"major": "德语", "endDate": "2027.06"}]

    result = evaluate_card_prescreen(candidate, _recruitment())

    assert result.passed is True
    assert "latest_major" not in result.extracted


def test_operation_card_does_not_combine_position_and_months_from_two_experiences():
    result = evaluate_card_prescreen(
        _competitor_candidate(
            "京东运营",
            [
                _work("机乐堂", "京东运营", "2026.01", "2026.05"),
                _work("倍思", "行政专员", "2024.01", "2025.01"),
            ],
        ),
        _job_recruitment("运营"),
    )

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("同一段经历未满8个月" in reason for reason in result.reasons)


def test_operation_card_rejects_non_competitor_company():
    result = evaluate_card_prescreen(
        _competitor_candidate(
            "京东运营",
            [_work("普通电商公司", "京东运营", "2025.01", "2026.01")],
        ),
        _job_recruitment("运营"),
    )

    assert result.passed is False
    assert any("公司名未命中竞品名单" in reason for reason in result.reasons)


def test_operation_card_rejects_excluded_position_even_when_other_conditions_match():
    result = evaluate_card_prescreen(
        _competitor_candidate(
            "京东运营",
            [_work("机乐堂", "亚马逊电商运营", "2025.01", "2026.01")],
        ),
        _job_recruitment("运营"),
    )

    assert result.passed is False
    assert any("岗位名不符合要求" in reason for reason in result.reasons)


def test_operation_card_rejects_competitor_experience_older_than_three_years():
    result = evaluate_card_prescreen(
        _competitor_candidate(
            "京东运营",
            [_work("机乐堂", "京东运营", "2019.01", "2020.01")],
        ),
        _job_recruitment("运营"),
    )

    assert result.passed is False
    assert any("不在近3年内" in reason for reason in result.reasons)


def test_operation_card_accepts_current_competitor_operation_experience():
    result = evaluate_card_prescreen(
        _competitor_candidate(
            "运营",
            [_work("机乐堂", "运营", "2025.01", "至今")],
        ),
        _job_recruitment("运营"),
    )

    assert result.passed is True


def test_senior_shoot_edit_card_no_longer_gates_on_competitor_company():
    # 2026.11.3 起竞品名单只用于简历阶段豁免，卡片只看最近一份岗位关键词。
    result = evaluate_card_prescreen(
        _competitor_candidate(
            "视频剪辑",
            [_work("非竞品公司", "电商运营", "2025.07", "2026.09")],
        ),
        _job_recruitment("高级剪拍"),
    )

    assert result.passed is True


def test_senior_shoot_edit_rejects_when_latest_role_has_no_keyword():
    result = evaluate_card_prescreen(
        _competitor_candidate(
            "行政专员",
            [_work("某公司", "行政专员", "2025.07", "2026.09")],
        ),
        _job_recruitment("高级剪拍"),
    )

    assert result.passed is False
    assert any("工作岗位未包含关键词" in reason for reason in result.reasons)


def test_senior_shoot_edit_passes_same_experience_with_all_yaml_conditions():
    result = evaluate_card_prescreen(
        _competitor_candidate(
            "视频剪辑",
            [_work("机乐堂", "视频剪辑", "2025.07", "2026.09")],
        ),
        _job_recruitment("高级剪拍"),
    )

    assert result.passed is True


def test_senior_shoot_edit_rejects_age_above_32():
    candidate = _competitor_candidate(
        "视频剪辑",
        [_work("公司", "视频剪辑", "2025.07", "2026.09")],
    )
    candidate["geekCard"]["ageDesc"] = "33岁"

    result = evaluate_card_prescreen(candidate, _job_recruitment("高级剪拍"))

    assert result.passed is False
    assert any("年龄超过32岁" in reason for reason in result.reasons)


def _junior_shoot_edit_candidate(works):
    return {
        "encryptGeekId": "gid-junior-shoot-edit",
        "geekCard": {
            "geekName": "初级剪拍候选人",
            "activeTimeDesc": "刚刚活跃",
            "ageDesc": "23岁",
            "degreeCategory": "本科",
            "expectLocationName": "深圳",
            "expectPositionName": "视频剪辑",
            "geekEdus": [{"school": "测试大学", "endDate": "2027.06"}],
            "geekWorks": works,
        },
    }


def test_junior_shoot_edit_accepts_each_recent_work_position_keyword():
    for position in ("新媒体运营", "内容运营", "视频剪辑", "短视频编导", "自媒体运营", "拍剪", "拍摄剪辑"):
        result = evaluate_card_prescreen(
            _junior_shoot_edit_candidate([_work("内容公司", position, "2026.01", "至今")]),
            _job_recruitment("初级剪拍"),
        )

        assert result.passed is True, position
        assert result.extracted["qualified_recent_work_experiences"][0]["position"] == position


def test_junior_shoot_edit_card_rejects_master_degree():
    candidate = _junior_shoot_edit_candidate([_work("公司", "视频剪辑", "2026.01", "至今")])
    candidate["geekCard"]["degreeCategory"] = "硕士"

    result = evaluate_card_prescreen(candidate, _job_recruitment("初级剪拍"))

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("学历不符合（仅限本科）" in reason for reason in result.reasons)


def test_junior_shoot_edit_rejects_mixed_bachelor_master_card():
    # 真实卡片会把 degreeName/geekDegree/教育经历拼成「硕士 本科 硕士」，
    # 必须按最高学历判断，不能被其中的「本科」放行。
    candidate = _junior_shoot_edit_candidate([_work("公司", "视频剪辑", "2026.01", "至今")])
    candidate["geekCard"]["degreeName"] = "本科"
    candidate["geekCard"]["geekDegree"] = "硕士"

    result = evaluate_card_prescreen(candidate, _job_recruitment("初级剪拍"))

    assert result.passed is False
    assert any("学历不符合（仅限本科）" in reason for reason in result.reasons)


def test_degree_rank_uses_highest_degree():
    from boss_hr.domain.card_prescreen import _degree_rank

    assert _degree_rank("硕士 本科 硕士 0 本科") == 5
    assert _degree_rank("本科") == 4
    assert _degree_rank("") is None


def test_junior_shoot_edit_accepts_keyword_in_second_recent_work():
    result = evaluate_card_prescreen(
        _junior_shoot_edit_candidate([
            _work("公司A", "行政专员", "2026.06", "至今"),
            _work("公司B", "内容运营", "2025.06", "2026.05"),
        ]),
        _job_recruitment("初级剪拍"),
    )

    assert result.passed is True
    assert result.extracted["qualified_recent_work_experiences"][0]["position"] == "内容运营"


def test_junior_shoot_edit_rejects_when_two_recent_positions_have_no_keyword():
    result = evaluate_card_prescreen(
        _junior_shoot_edit_candidate([
            _work("公司A", "行政专员", "2026.06", "至今"),
            _work("公司B", "电商客服", "2025.06", "2026.05"),
        ]),
        _job_recruitment("初级剪拍"),
    )

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("近2份工作岗位未包含关键词" in reason for reason in result.reasons)


def test_junior_shoot_edit_role_match_cannot_bypass_recent_work_keywords():
    candidate = _junior_shoot_edit_candidate([
        _work("东峰贸易", "产品专员/助理", "2026.02", "2026.07"),
        _work("尝尝酒酒", "电商客服", "2025.10", "2026.01"),
    ])
    candidate["geekCard"]["geekName"] = "万阳洪"
    candidate["geekCard"]["expectPositionName"] = "新媒体运营"

    result = evaluate_card_prescreen(candidate, _job_recruitment("初级剪拍"))

    assert result.passed is False
    assert result.allow_full_resume is False
    assert result.extracted["qualified_recent_work_experiences"] == []
    assert any("近2份工作岗位未包含关键词" in reason for reason in result.reasons)


def test_junior_shoot_edit_ignores_matching_third_recent_work():
    result = evaluate_card_prescreen(
        _junior_shoot_edit_candidate([
            _work("公司A", "行政专员", "2026.06", "至今"),
            _work("公司B", "电商客服", "2025.06", "2026.05"),
            _work("公司C", "视频剪辑", "2024.06", "2025.05"),
        ]),
        _job_recruitment("初级剪拍"),
    )

    assert result.passed is False
    assert any("近2份工作岗位未包含关键词" in reason for reason in result.reasons)


def _cross_border_candidate(*, age="29岁", expect_position="亚马逊运营", works=None):
    return {
        "encryptGeekId": "gid-cross-border",
        "geekCard": {
            "geekName": "跨境候选人",
            "activeTimeDesc": "刚刚活跃",
            "ageDesc": age,
            "expectLocationName": "深圳",
            "expectPositionName": expect_position,
            "geekWorks": works or [],
        },
    }


def test_amazon_card_passes_without_age_limit_and_with_8_month_relevant_work():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            age="36岁",
            works=[_work("跨境公司", "亚马逊运营", "2025.12", "2026.09")],
        ),
        _job_recruitment("亚马逊运营"),
    )

    assert result.passed is True
    assert result.extracted["age"] == 36
    assert len(result.extracted["qualified_recent_work_experiences"]) == 1


def test_amazon_card_accepts_8_month_cross_border_position_in_two_recent_jobs():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            works=[
                _work("公司A", "天猫运营", "2026.01", "2026.09"),
                _work("公司B", "跨境电商运营", "2025.12", "2026.09"),
            ],
        ),
        _job_recruitment("亚马逊运营"),
    )

    assert result.passed is True
    assert result.extracted["qualified_recent_work_experiences"][0]["position"] == "跨境电商运营"


def test_amazon_card_does_not_borrow_8_month_duration_from_tmall_job():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            works=[
                _work("公司A", "亚马逊运营", "2025.12", "2026.05"),
                _work("公司B", "天猫运营", "2025.08", "2026.09"),
            ],
        ),
        _job_recruitment("亚马逊运营"),
    )

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("相关岗位经历未满8个月" in reason for reason in result.reasons)


def test_amazon_card_rejects_unrelated_12_month_job_even_when_latest_is_recent():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            works=[
                _work("公司A", "天猫运营", "2025.08", "2026.09"),
                _work("公司B", "亚马逊运营", "2025.12", "2026.05"),
            ],
        ),
        _job_recruitment("亚马逊运营"),
    )

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("相关岗位经历未满8个月" in reason for reason in result.reasons)


def test_amazon_card_rejects_stale_latest_job():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            works=[_work("跨境公司", "亚马逊运营", "2024.01", "2026.07")],
        ),
        _job_recruitment("亚马逊运营"),
    )

    assert result.passed is False
    assert any("结束时间早于2026.09" in reason for reason in result.reasons)


def test_tiktok_card_requires_12_months_in_one_of_two_recent_relevant_jobs():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            expect_position="TikTok运营",
            works=[
                _work("内容公司", "电商运营", "2026.01", "至今"),
                _work("品牌公司", "新媒体运营", "2025.08", "2026.08"),
            ],
        ),
        _job_recruitment("tiktok运营"),
    )

    assert result.passed is True
    assert result.extracted["qualified_recent_work_experiences"][0]["months"] == 12


def test_tiktok_card_accepts_each_configured_position_keyword_at_12_months():
    for position in ("TikTok运营", "新媒体运营", "跨境电商运营"):
        result = evaluate_card_prescreen(
            _cross_border_candidate(
                expect_position="TikTok运营",
                works=[_work("公司A", position, "2025.08", "2026.08")],
            ),
            _job_recruitment("tiktok运营"),
        )

        assert result.passed is True
        assert result.extracted["qualified_recent_work_experiences"][0]["months"] == 12


def test_tiktok_card_accepts_age_35_and_rejects_age_36():
    works = [_work("公司A", "TikTok运营", "2025.08", "2026.08")]

    age_35 = evaluate_card_prescreen(
        _cross_border_candidate(age="35岁", expect_position="TikTok运营", works=works),
        _job_recruitment("tiktok运营"),
    )
    age_36 = evaluate_card_prescreen(
        _cross_border_candidate(age="36岁", expect_position="TikTok运营", works=works),
        _job_recruitment("tiktok运营"),
    )

    assert age_35.passed is True
    assert age_36.passed is False
    assert "年龄超过35岁：36岁" in age_36.reasons


def test_tiktok_card_does_not_borrow_12_month_duration_from_unrelated_job():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            expect_position="TikTok运营",
            works=[
                _work("公司A", "TikTok运营", "2025.09", "2026.08"),
                _work("公司B", "天猫运营", "2025.08", "2026.08"),
            ],
        ),
        _job_recruitment("tiktok运营"),
    )

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("相关岗位经历未满12个月" in reason for reason in result.reasons)


def test_tiktok_card_marks_unknown_relevant_duration_pending_without_opening_resume():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            expect_position="TikTok运营",
            works=[_work("内容公司", "TikTok运营", "", "至今")],
        ),
        _job_recruitment("tiktok运营"),
    )

    assert result.status == "pending_confirmation"
    assert result.allow_full_resume is False
    assert "相关岗位在职时长" in result.missing_fields


def test_amazon_card_rejects_when_relevant_work_is_shorter_than_8_months():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            works=[_work("跨境公司", "亚马逊运营", "2026.02", "2026.09")],
        ),
        _job_recruitment("亚马逊运营"),
    )

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("未满8个月" in reason for reason in result.reasons)


def test_tiktok_card_only_checks_two_most_recent_jobs_for_12_month_rule():
    result = evaluate_card_prescreen(
        _cross_border_candidate(
            expect_position="TikTok运营",
            works=[
                _work("公司A", "TikTok运营", "2026.01", "2026.08"),
                _work("公司B", "跨境运营", "2026.02", "2026.08"),
                _work("公司C", "新媒体运营", "2024.01", "2026.08"),
            ],
        ),
        _job_recruitment("tiktok运营"),
    )

    assert result.passed is False
    assert result.allow_full_resume is False
    assert any("未满12个月" in reason for reason in result.reasons)


def _customer_service_candidate(*, age="27岁", degree="大专", active="在线",
                                expect_position="电商客服", works=None):
    return {
        "encryptGeekId": "gid-customer-service",
        "geekCard": {
            "geekName": "客服候选人",
            "activeTimeDesc": active,
            "ageDesc": age,
            "degreeCategory": degree,
            "expectLocationName": "深圳",
            "expectPositionName": expect_position,
            "geekWorks": works or [],
        },
    }


def test_customer_service_card_passes_all_yaml_conditions():
    result = evaluate_card_prescreen(
        _customer_service_candidate(
            works=[_work("电商公司", "售后客服", "2025.12", "至今")],
        ),
        _job_recruitment("客服"),
    )

    assert result.passed is True
    assert result.allow_full_resume is True
    assert result.extracted["qualified_recent_work_experiences"][0]["months"] >= 8


def test_customer_service_card_passes_example_ending_in_august_2026():
    result = evaluate_card_prescreen(
        _customer_service_candidate(
            works=[_work("新款贸易", "客服专员", "2025.10", "2026.08")],
        ),
        _job_recruitment("客服"),
    )

    assert result.passed is True
    assert result.allow_full_resume is True
    assert result.extracted["work_experiences"][0]["months"] == 10


def test_customer_service_card_rejects_age_over_27():
    result = evaluate_card_prescreen(
        _customer_service_candidate(
            age="28岁",
            works=[_work("电商公司", "客服", "2025.10", "2026.08")],
        ),
        _job_recruitment("客服"),
    )

    assert result.allow_full_resume is False
    assert "年龄超过27岁：28岁" in result.reasons


def test_customer_service_card_rejects_stale_latest_job():
    result = evaluate_card_prescreen(
        _customer_service_candidate(
            works=[_work("电商公司", "客服", "2025.01", "2026.06")],
        ),
        _job_recruitment("客服"),
    )

    assert result.allow_full_resume is False
    assert any("结束时间早于2026.07" in reason for reason in result.reasons)


def test_customer_service_card_accepts_latest_job_ending_in_july_2026():
    result = evaluate_card_prescreen(
        _customer_service_candidate(
            works=[_work("电商公司", "客服", "2025.09", "2026.07")],
        ),
        _job_recruitment("客服"),
    )

    assert result.passed is True
    assert result.allow_full_resume is True


def test_customer_service_card_blocks_when_latest_job_end_is_missing():
    result = evaluate_card_prescreen(
        _customer_service_candidate(
            works=[_work("电商公司", "客服", "2025.10", "")],
        ),
        _job_recruitment("客服"),
    )

    assert result.status == "pending_confirmation"
    assert result.allow_full_resume is False
    assert "最近一份工作结束时间" in result.missing_fields


def test_customer_service_card_allows_second_recent_customer_service_job():
    result = evaluate_card_prescreen(
        _customer_service_candidate(
            works=[
                _work("公司A", "行政", "2026.01", "至今"),
                _work("公司B", "售后客服", "2025.10", "2026.08"),
            ],
        ),
        _job_recruitment("客服"),
    )

    assert result.passed is True
    assert result.allow_full_resume is True
    qualified = result.extracted["qualified_recent_work_experiences"]
    assert len(qualified) == 1
    assert qualified[0]["position"] == "售后客服"
    assert qualified[0]["months"] == 10


def test_customer_service_card_only_checks_two_latest_jobs_for_eight_month_rule():
    # 用相对日期，避免这条「未满 8 个月」用例随日历翻月而失效。
    from datetime import date, timedelta

    recent_start = (date.today() - timedelta(days=150)).strftime("%Y.%m")
    result = evaluate_card_prescreen(
        _customer_service_candidate(
            works=[
                _work("公司A", "客服", recent_start, "至今"),
                _work("公司B", "行政", "2025.01", "2026.08"),
                _work("公司C", "客服", "2024.01", "2025.01"),
            ],
        ),
        _job_recruitment("客服"),
    )

    assert result.allow_full_resume is False
    assert any("未满8个月" in reason for reason in result.reasons)


def test_customer_service_missing_duration_is_pending_and_blocks_resume():
    result = evaluate_card_prescreen(
        _customer_service_candidate(
            works=[_work("电商公司", "客服", "", "至今")],
        ),
        _job_recruitment("客服"),
    )

    assert result.status == "pending_confirmation"
    assert result.allow_full_resume is False
    assert "相关岗位在职时长" in result.missing_fields
