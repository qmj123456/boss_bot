"""Risk controls for automated recruitment actions."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from boss_hr.config_manager import ConfigManager, get_config
from database.connection import Database
from database.models import Candidate, GreetingRecord
from database.repositories import UnitOfWork
from logger.log_manager import get_automation_logger


class RiskStop(RuntimeError):
    """Raised when automation must stop immediately."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class DailySendQuota:
    limit: int
    sent: int
    remaining: int


class RiskControl:
    """Central risk control policy loaded from external config."""

    SUCCESS_STATUSES = {"greeted", "sent", "success"}
    CAPTCHA_KEYWORDS = ("验证码", "安全验证", "滑块", "拖动", "captcha", "verify")
    LOGIN_KEYWORDS = ("扫码登录", "请登录", "登录后", "/login", "/web/user")
    PAGE_EXCEPTION_KEYWORDS = ("页面异常", "加载失败", "iframe", "network error", "timeout")

    def __init__(
        self,
        *,
        config: ConfigManager | None = None,
        database: Database | None = None,
    ) -> None:
        self.config = config or get_config()
        self.database = database or Database(self.config)
        self.automation_logger = get_automation_logger(self.config)

    def daily_quota(self) -> DailySendQuota:
        limit = int(self.config.get("recruitment.daily_send_limit", 0) or 0)
        sent = self.count_today_sent()
        # BOSS 平台所有岗位共享的每日打招呼硬上限（200）。即使岗位 yaml 里
        # daily_send_limit=0（不限制），全局兜底仍会生效，确保不超 BOSS 上限。
        global_cap = 200
        effective_limit = min(limit, global_cap) if limit > 0 else global_cap
        remaining = max(0, effective_limit - sent)
        return DailySendQuota(limit=effective_limit, sent=sent, remaining=remaining)

    def count_today_sent(self, job_name: str = "") -> int:
        """统计当天已成功发送的招呼数。

        job_name 为空时统计全部岗位；指定时通过 Candidate.job 精确过滤。
        """
        # SQLite 的 CURRENT_TIMESTAMP 是 UTC；业务“每天”按 app.timezone
        # 计算。转换为无时区 UTC 边界后再查询，确保北京时间 00:00 准时清零。
        timezone_name = str(self.config.get("app.timezone", "Asia/Shanghai") or "Asia/Shanghai")
        try:
            business_tz = ZoneInfo(timezone_name)
        except Exception:
            business_tz = ZoneInfo("Asia/Shanghai")
        local_now = datetime.now(business_tz)
        local_start = datetime.combine(local_now.date(), dt_time.min, tzinfo=business_tz)
        local_end = local_start + timedelta(days=1)
        start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
        end = local_end.astimezone(timezone.utc).replace(tzinfo=None)
        with UnitOfWork(self.database.session_factory) as uow:
            stmt = select(GreetingRecord).where(
                GreetingRecord.send_time >= start,
                GreetingRecord.send_time < end,
                GreetingRecord.status.in_(self.SUCCESS_STATUSES),
            )
            if job_name:
                stmt = stmt.join(
                    Candidate, GreetingRecord.candidate_id == Candidate.id
                ).where(Candidate.job == job_name)
            return len(list(uow.session.execute(stmt).scalars()))

    def ensure_can_send(self, requested_count: int) -> int:
        """Validate work time and daily quota, returning allowed count."""
        self.ensure_work_time()
        quota = self.daily_quota()
        if quota.remaining <= 0:
            detail = {"limit": quota.limit, "sent": quota.sent, "requested_count": requested_count}
            self._stop("daily_limit_exceeded", "每日发送限制已达到，自动化招呼已停止", detail)
        allowed = min(max(0, requested_count), quota.remaining)
        if allowed < requested_count:
            self.automation_logger.warning(
                "daily_limit_clamped | requested=%s | allowed=%s | sent=%s | limit=%s",
                requested_count,
                allowed,
                quota.sent,
                quota.limit,
            )
        return allowed

    def ensure_candidate_limit(self, requested_count: int) -> int:
        """按每日候选人上限裁剪抓取数量，避免一次请求过大触发平台风控。"""
        requested_count = max(0, int(requested_count))
        limit = int(self.config.get("recruitment.daily_candidate_limit", 0) or 0)
        if limit <= 0:
            return requested_count
        start = datetime.combine(datetime.now().date(), dt_time.min)
        end = datetime.combine(datetime.now().date(), dt_time.max)
        with UnitOfWork(self.database.session_factory) as uow:
            stmt = select(Candidate).where(
                Candidate.created_time >= start,
                Candidate.created_time <= end,
            )
            used = len(list(uow.session.execute(stmt).scalars()))
        remaining = max(0, limit - used)
        if remaining <= 0:
            self._stop(
                "daily_candidate_limit_exceeded",
                "每日候选人抓取限制已达到，自动化流程已停止",
                {"limit": limit, "used": used, "requested_count": requested_count},
            )
        allowed = min(requested_count, remaining)
        if allowed < requested_count:
            self.automation_logger.warning(
                "daily_candidate_limit_clamped | requested=%s | allowed=%s | used=%s | limit=%s",
                requested_count, allowed, used, limit,
            )
        return allowed

    def ensure_work_time(self) -> None:
        policy = self.config.section("recruitment").get("work_time", {})
        if not bool(policy.get("enabled", False)):
            return
        start = _parse_time(str(policy.get("start", "09:00")))
        end = _parse_time(str(policy.get("end", "18:00")))
        now = datetime.now().time()
        ok = start <= now <= end if start <= end else now >= start or now <= end
        if not ok:
            self._stop(
                "outside_work_time",
                f"当前不在允许自动化工作时间内：{start.strftime('%H:%M')}-{end.strftime('%H:%M')}",
                {"now": now.strftime("%H:%M:%S")},
            )

    def random_wait_seconds(self) -> float:
        policy = self.config.section("recruitment").get("risk_control", {})
        wait = policy.get("random_wait_seconds", {})
        min_seconds = float(wait.get("min", 5))
        max_seconds = float(wait.get("max", 10))
        if max_seconds < min_seconds:
            max_seconds = min_seconds
        return random.uniform(min_seconds, max_seconds)

    def wait_before_action(self, action: str) -> float:
        seconds = self.random_wait_seconds()
        self.automation_logger.info("random_wait | action=%s | seconds=%.2f", action, seconds)
        time.sleep(seconds)
        return seconds

    def detect_page_exception(
        self,
        *,
        url: str = "",
        text: str = "",
        error: str = "",
    ) -> None:
        """Detect high-risk page states and stop immediately."""
        haystack = f"{url}\n{text}\n{error}".lower()
        stop_policy = self.config.section("recruitment").get("risk_control", {}).get("stop_on", {})
        if bool(stop_policy.get("captcha", True)) and _contains_any(haystack, self.CAPTCHA_KEYWORDS):
            self._stop("captcha_detected", "检测到验证码或安全验证，自动化已停止", {"url": url})
        if bool(stop_policy.get("login_expired", True)) and _contains_any(haystack, self.LOGIN_KEYWORDS):
            self._stop("login_expired", "检测到登录失效，自动化已停止", {"url": url})
        if bool(stop_policy.get("page_exception", True)) and _contains_any(haystack, self.PAGE_EXCEPTION_KEYWORDS):
            self._stop("page_exception", "检测到页面异常，自动化已停止", {"url": url, "error": error[:300]})

    def _stop(self, code: str, message: str, detail: dict[str, Any]) -> None:
        self.automation_logger.error("risk_stop | code=%s | message=%s | detail=%s", code, message, detail)
        raise RiskStop(code, message, detail)


def _parse_time(value: str) -> dt_time:
    hour, minute = value.split(":", 1)
    return dt_time(hour=int(hour), minute=int(minute))


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.lower() in text for needle in needles)


__all__ = ["DailySendQuota", "RiskControl", "RiskStop"]
