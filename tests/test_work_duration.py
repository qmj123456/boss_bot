from datetime import datetime

import pytest
import yaml
from pathlib import Path

from boss_hr.domain.card_prescreen import _parse_work_months, evaluate_card_prescreen


@pytest.mark.parametrize("text,months", [
    ("1年", 12), ("2年", 24), ("1年半", 18), ("7年半", 90),
    ("半年", 6), ("1年2个月", 14), ("2年3月", 27),
    ("1.5年", 18), (" 1 年 6 个月 ", 18),
    ("9个月", 9), ("10个月", 10), ("12个月", 12), ("0个月", 0),
])
def test_duration_units(text, months):
    assert _parse_work_months(text, None, None) == months


@pytest.mark.parametrize("text", [None, "", "未知", "12", "1-3年", "不足1年", "2025.08-2026.08"])
def test_unrecognized_duration_uses_dates_or_stays_unknown(text):
    assert _parse_work_months(text, "2025.08", "2026.08") == 12
    assert _parse_work_months(text, None, None) is None


@pytest.mark.parametrize("end", ["", "至今", "在职", "present"])
def test_current_job_date_fallback(end):
    now = datetime.now()
    assert _parse_work_months("", "2025.01", end) == (now.year - 2025) * 12 + now.month - 1


def test_invalid_dates_stay_unknown():
    assert _parse_work_months("", "2026.08", "2025.08") is None
    assert _parse_work_months("", "2025.13", "2026.08") is None
    assert _parse_work_months("", "2025.08", "未知") is None


@pytest.mark.parametrize("job,position,text,allowed", [
    ("亚马逊运营", "亚马逊运营", "1年", True),
    ("亚马逊运营", "跨境电商运营", "1年半", True),
    ("亚马逊运营", "亚马逊运营", "8个月", True),
    ("亚马逊运营", "亚马逊运营", "7个月", False),
    ("tiktok运营", "TikTok运营", "1年", True),
    ("tiktok运营", "新媒体运营", "10个月", True),
    ("tiktok运营", "跨境运营", "9个月", False),
    ("tiktok运营", "TikTok运营", "未知", False),
])
def test_card_gate_with_realistic_work_time(job, position, text, allowed):
    path = Path(__file__).resolve().parents[1] / "config" / "jobs" / f"{job}.yaml"
    recruitment = yaml.safe_load(path.read_text(encoding="utf-8"))["recruitment"]
    card = {"geekCard": {
        "activeTimeDesc": "刚刚活跃", "ageDesc": "28岁",
        "expectLocationName": "深圳", "expectPositionName": job,
        "geekWorks": [{"company": "测试公司", "positionName": position,
                       "workTime": text, "endDate": "2026.09"}],
    }}
    result = evaluate_card_prescreen(card, recruitment)
    assert result.passed is allowed
    assert result.allow_full_resume is allowed
    if text == "未知":
        assert result.status == "pending_confirmation"
        assert "相关岗位在职时长" in result.missing_fields
