"""Resolve BOSS encryptJobId values from local job names and environment."""
from __future__ import annotations

import os
import re


JOB_BINDINGS = {
    "全职主播": {
        "boss_id_env": "BOSS_JOB_ID_FULLTIME_ANCHOR",
        "boss_name_env": "BOSS_JOB_NAME_FULLTIME_ANCHOR",
        "feishu_table_env": "FEISHU_TABLE_FULLTIME_ANCHOR",
    },
    "兼职主播": {
        "boss_id_env": "BOSS_JOB_ID_PARTTIME_ANCHOR",
        "boss_name_env": "BOSS_JOB_NAME_PARTTIME_ANCHOR",
        "feishu_table_env": "FEISHU_TABLE_PARTTIME_ANCHOR",
    },
    "运营": {
        "boss_id_env": "BOSS_JOB_ID_OPERATION",
        "boss_name_env": "BOSS_JOB_NAME_OPERATION",
        "feishu_table_env": "FEISHU_TABLE_OPERATION",
    },
    "亚马逊运营": {
        "boss_id_env": "BOSS_JOB_ID_AMAZON_OPERATION",
        "boss_name_env": "BOSS_JOB_NAME_AMAZON_OPERATION",
        "feishu_table_env": "FEISHU_TABLE_AMAZON_OPERATION",
    },
    "tiktok运营": {
        "boss_id_env": "BOSS_JOB_ID_TIKTOK_OPERATION",
        "boss_name_env": "BOSS_JOB_NAME_TIKTOK_OPERATION",
        "feishu_table_env": "FEISHU_TABLE_TIKTOK_OPERATION",
    },
    "初级剪拍": {
        "boss_id_env": "BOSS_JOB_ID_JUNIOR_SHOOT_EDIT",
        "boss_name_env": "BOSS_JOB_NAME_JUNIOR_SHOOT_EDIT",
        "feishu_table_env": "FEISHU_TABLE_JUNIOR_SHOOT_EDIT",
    },
    "运营助理": {
        "boss_id_env": "BOSS_JOB_ID_OPERATION_ASSISTANT",
        "boss_name_env": "BOSS_JOB_NAME_OPERATION_ASSISTANT",
        "feishu_table_env": "FEISHU_TABLE_OPERATION_ASSISTANT",
    },
    "运营助理（前期需要在中控岗位轮岗）": {
        "boss_id_env": "BOSS_JOB_ID_OPERATION_ASSISTANT_ROTATION",
        "boss_name_env": "BOSS_JOB_NAME_OPERATION_ASSISTANT_ROTATION",
        "feishu_table_env": "FEISHU_TABLE_OPERATION_ASSISTANT_ROTATION",
    },
    "高级剪拍": {
        "boss_id_env": "BOSS_JOB_ID_SENIOR_SHOOT_EDIT",
        "boss_name_env": "BOSS_JOB_NAME_SENIOR_SHOOT_EDIT",
        "feishu_table_env": "FEISHU_TABLE_SENIOR_SHOOT_EDIT",
    },
    "客服": {
        "boss_id_env": "BOSS_JOB_ID_CUSTOMER_SERVICE",
        "boss_name_env": "BOSS_JOB_NAME_CUSTOMER_SERVICE",
        "feishu_table_env": "FEISHU_TABLE_CUSTOMER_SERVICE",
    },
    "编导": {
        "boss_id_env": "BOSS_JOB_ID_BIANDAO",
        "boss_name_env": "BOSS_JOB_NAME_BIANDAO",
        "feishu_table_env": "FEISHU_TABLE_BIANDAO",
    },
    "渲染": {
        "boss_id_env": "BOSS_JOB_ID_RENDER",
        "boss_name_env": "BOSS_JOB_NAME_RENDER",
        "feishu_table_env": "FEISHU_TABLE_RENDER",
    },
}

JOB_ID_ENV_KEYS = {
    job_name: binding["boss_id_env"] for job_name, binding in JOB_BINDINGS.items()
}
JOB_NAME_ENV_KEYS = {
    job_name: binding["boss_name_env"] for job_name, binding in JOB_BINDINGS.items()
}
FEISHU_TABLE_ENV_KEYS = {
    job_name: binding["feishu_table_env"] for job_name, binding in JOB_BINDINGS.items()
}


def env_key_for_job(job_name: str | None) -> str | None:
    """Return the configured BOSS_JOB_ID_* env key for a local job name."""
    return JOB_ID_ENV_KEYS.get(str(job_name or "").strip())


# 岗位名归一化：去空白 + 中英文括号/分隔符。BOSS 显示名常带
# "（精品/3C品类）" 之类的修饰（甚至有括号打错的情况），归一化后对齐。
_NAME_NOISE_PATTERN = re.compile(r"[\s（）()【】\[\]·、，,。.．/\\\-—_―]+")


def _normalize_job_name(name: str) -> str:
    return _NAME_NOISE_PATTERN.sub("", str(name or ""))


def local_name_for(job_name: str | None) -> str:
    """把 BOSS 岗位显示名映射回本地岗位名（config/jobs/<本地名>.yaml）。

    匹配顺序：
      1. 本地名精确命中（JOB_BINDINGS 的 key，如"全职主播"）；
      2. .env 里 BOSS_JOB_NAME_* 的值精确 / 归一化命中
         （如"高级主播（往直播运营发展）" → 全职主播）；
      3. 都不命中 → 原样返回（自定义岗位名不强制绑定）。
    """
    name = str(job_name or "").strip()
    if not name:
        return name
    if name in JOB_BINDINGS:
        return name
    normalized = _normalize_job_name(name)
    for local_name, env_key in JOB_NAME_ENV_KEYS.items():
        value = str(os.environ.get(env_key) or "").strip()
        if not value:
            continue
        if value == name or _normalize_job_name(value) == normalized:
            return local_name
    return name


def resolve_encrypt_job_id(
    *,
    cli_value: str | None = None,
    job_name: str | None = None,
    allow_legacy_default: bool = True,
) -> str | None:
    """Resolve encryptJobId using CLI > per-job env > legacy global env.

    per-job env 同时支持本地岗位名（"全职主播"）和 BOSS 显示名
    （"高级主播（往直播运营发展）"，按 .env BOSS_JOB_NAME_* 反查）。
    """
    if cli_value:
        return cli_value
    env_key = env_key_for_job(job_name)
    if env_key:
        value = os.environ.get(env_key)
        if value:
            return value
    # BOSS 显示名兜底：job_name 是 .env BOSS_JOB_NAME_* 的值时反查对应 ID。
    local_name = local_name_for(job_name)
    if local_name and local_name != str(job_name or "").strip():
        value = os.environ.get(JOB_ID_ENV_KEYS.get(local_name) or "")
        if value:
            return value
    if allow_legacy_default:
        return os.environ.get("BOSS_HR_ENCRYPT_JOB_ID")
    return None


__all__ = [
    "FEISHU_TABLE_ENV_KEYS",
    "JOB_BINDINGS",
    "JOB_ID_ENV_KEYS",
    "JOB_NAME_ENV_KEYS",
    "env_key_for_job",
    "local_name_for",
    "resolve_encrypt_job_id",
]
