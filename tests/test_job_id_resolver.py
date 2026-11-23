# -*- coding: utf-8 -*-
"""job_id_resolver + _argparse_helpers 的岗位名/eid 自动解析测试。

覆盖 v1.2.1 行为：
  - --job-name 传 BOSS 显示名（.env BOSS_JOB_NAME_* 的值）→ 映射回本地岗位名
  - --job-name 缺省 → app.default_job_name 兜底
  - eid 按本地名或显示名从 BOSS_JOB_ID_* 反查
"""
import argparse

import pytest

from boss_hr.commands import _argparse_helpers as helpers
from boss_hr.job_id_resolver import (
    JOB_BINDINGS,
    local_name_for,
    resolve_encrypt_job_id,
)

_FULLTIME_NAME_ENV = "BOSS_JOB_NAME_FULLTIME_ANCHOR"
_FULLTIME_ID_ENV = "BOSS_JOB_ID_FULLTIME_ANCHOR"


def _clean_env(monkeypatch):
    for key in (_FULLTIME_NAME_ENV, _FULLTIME_ID_ENV, "BOSS_HR_ENCRYPT_JOB_ID",
                "BOSS_JOB_NAME_OPERATION", "BOSS_JOB_ID_OPERATION"):
        monkeypatch.delenv(key, raising=False)


# ============================================================
# local_name_for
# ============================================================

def test_local_name_exact_local_name(monkeypatch):
    _clean_env(monkeypatch)
    assert local_name_for("全职主播") == "全职主播"


def test_local_name_from_boss_display_name(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv(_FULLTIME_NAME_ENV, "高级主播（往直播运营发展）")
    assert local_name_for("高级主播（往直播运营发展）") == "全职主播"


def test_local_name_normalized_match(monkeypatch):
    """>.env 值括号打错（"（("）时，归一化（去括号/斜杠）仍能对齐。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("BOSS_JOB_NAME_SENIOR_SHOOT_EDIT", "短视频剪辑（(成熟账号/晋升明确/3C")
    assert local_name_for("短视频剪辑（成熟账号/晋升明确/3C）") == "高级剪拍"


def test_local_name_unknown_passthrough(monkeypatch):
    _clean_env(monkeypatch)
    assert local_name_for("自定义岗位") == "自定义岗位"
    assert local_name_for("") == ""
    assert local_name_for(None) == ""


def test_cross_border_operation_jobs_have_independent_greet_bindings():
    assert JOB_BINDINGS["亚马逊运营"]["boss_id_env"] == "BOSS_JOB_ID_AMAZON_OPERATION"
    assert JOB_BINDINGS["tiktok运营"]["boss_id_env"] == "BOSS_JOB_ID_TIKTOK_OPERATION"


def test_customer_service_has_independent_storage_bindings():
    binding = JOB_BINDINGS["客服"]

    assert binding == {
        "boss_id_env": "BOSS_JOB_ID_CUSTOMER_SERVICE",
        "boss_name_env": "BOSS_JOB_NAME_CUSTOMER_SERVICE",
        "feishu_table_env": "FEISHU_TABLE_CUSTOMER_SERVICE",
    }
    assert len({item["boss_id_env"] for item in JOB_BINDINGS.values()}) == len(JOB_BINDINGS)
    assert len({item["feishu_table_env"] for item in JOB_BINDINGS.values()}) == len(JOB_BINDINGS)


# ============================================================
# resolve_encrypt_job_id（显示名反查）
# ============================================================

def test_resolve_eid_by_local_name(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv(_FULLTIME_ID_ENV, "eid-1")
    assert resolve_encrypt_job_id(job_name="全职主播") == "eid-1"


def test_resolve_eid_by_display_name(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv(_FULLTIME_NAME_ENV, "高级主播（往直播运营发展）")
    monkeypatch.setenv(_FULLTIME_ID_ENV, "eid-1")
    assert (
        resolve_encrypt_job_id(job_name="高级主播（往直播运营发展）") == "eid-1"
    )


def test_resolve_eid_cli_wins(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv(_FULLTIME_ID_ENV, "eid-env")
    assert (
        resolve_encrypt_job_id(cli_value="eid-cli", job_name="全职主播")
        == "eid-cli"
    )


# ============================================================
# require_encrypt_job_id（job_name 缺省 + 翻译 + 报错）
# ============================================================

def _make_parser_and_ns(job_name=None, encrypt_job_id=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-name", default=None)
    parser.add_argument("--encrypt-job-id", default=None)
    ns = argparse.Namespace(job_name=job_name, encrypt_job_id=encrypt_job_id)
    return parser, ns


def test_require_resolves_default_job_name(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setattr(helpers, "_default_job_name", lambda: "全职主播")
    monkeypatch.setenv(_FULLTIME_ID_ENV, "eid-1")
    parser, ns = _make_parser_and_ns()
    eid = helpers.require_encrypt_job_id(parser, ns)
    assert eid == "eid-1"
    assert ns.job_name == "全职主播"


def test_require_translates_display_name(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv(_FULLTIME_NAME_ENV, "高级主播（往直播运营发展）")
    monkeypatch.setenv(_FULLTIME_ID_ENV, "eid-1")
    parser, ns = _make_parser_and_ns(job_name="高级主播（往直播运营发展）")
    eid = helpers.require_encrypt_job_id(parser, ns)
    assert eid == "eid-1"
    assert ns.job_name == "全职主播"


def test_require_errors_when_all_missing(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setattr(helpers, "_default_job_name", lambda: "全职主播")
    parser, ns = _make_parser_and_ns()
    with pytest.raises(SystemExit) as excinfo:
        helpers.require_encrypt_job_id(parser, ns)
    assert excinfo.value.code == 2


def test_require_errors_when_no_default_job(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setattr(helpers, "_default_job_name", lambda: "")
    parser, ns = _make_parser_and_ns()
    with pytest.raises(SystemExit) as excinfo:
        helpers.require_encrypt_job_id(parser, ns)
    assert excinfo.value.code == 2
