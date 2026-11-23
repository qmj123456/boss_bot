# -*- coding: utf-8 -*-
"""Tests for shared/anti_detection.py and the human typing helper.

No real browser / CDP connection is used: fake page/context objects record the
Playwright calls so the guards can be tested deterministically.
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

import anti_detection as ad  # noqa: E402
import human_interaction as hi  # noqa: E402


# ---------------------------------------------------------------------------
# risk URL / tracker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.zhipin.com/web/common/403.html?x=1",
        "https://www.zhipin.com/web/user/safe/verify?callback=cb",
        "https://www.zhipin.com/web/passport/zp/verify.html",
        "https://www.zhipin.com/web/passport/cm/security-check.html",
    ],
)
def test_is_risk_url_matches_boss_verification_pages(url):
    assert ad.is_risk_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "about:blank",
        "https://www.zhipin.com/web/chat/recommend",
        "https://www.zhipin.com/web/chat/index",
    ],
)
def test_is_risk_url_ignores_normal_pages(url):
    assert ad.is_risk_url(url) is False


def test_fourth_risk_navigation_within_window_trips():
    tracker = ad.RiskTracker()
    url = "https://www.zhipin.com/web/common/403.html"
    assert tracker.observe_navigation(url, now_ms=0) is None
    assert tracker.observe_navigation(url, now_ms=1_000) is None
    assert tracker.observe_navigation(url, now_ms=2_000) is None
    event = tracker.observe_navigation(url, now_ms=3_000)
    assert event is not None
    assert event.kind == "risk_navigation"
    assert tracker.tripped is True
    # 已熔断后不再重复触发
    assert tracker.observe_navigation(url, now_ms=4_000) is None


def test_risk_navigation_older_than_window_is_dropped():
    tracker = ad.RiskTracker()
    url = "https://www.zhipin.com/web/common/403.html"
    for stamp in (0, 1_000, 2_000):
        assert tracker.observe_navigation(url, now_ms=stamp) is None
    # 第 4 次距第 1 次已超过 60s，窗口内只剩 3 次（含本次），不熔断
    assert tracker.observe_navigation(url, now_ms=61_500) is None
    assert tracker.tripped is False


def test_same_url_loaded_five_times_in_15s_trips():
    tracker = ad.RiskTracker()
    url = "https://www.zhipin.com/web/chat/recommend"
    for index in range(4):
        assert tracker.observe_load(url, now_ms=index * 1_000) is None
    event = tracker.observe_load(url, now_ms=4_000)
    assert event is not None
    assert event.kind == "reload_loop"
    assert event.count == 5


def test_reload_loop_is_per_url_and_ignores_about_blank():
    tracker = ad.RiskTracker()
    for index in range(4):
        tracker.observe_load(f"https://www.zhipin.com/page-{index}", now_ms=index)
    assert tracker.tripped is False
    for index in range(5):
        tracker.observe_load("about:blank", now_ms=index)
    assert tracker.tripped is False


# ---------------------------------------------------------------------------
# install / route interception
# ---------------------------------------------------------------------------


class FakeRequest:
    def __init__(self, url, headers=None):
        self.url = url
        self.headers = headers or {}


class FakeRoute:
    def __init__(self, url):
        self.request = FakeRequest(url)
        self.actions = []

    def fulfill(self, **kwargs):
        self.actions.append(("fulfill", kwargs))

    def abort(self, reason=None):
        self.actions.append(("abort", reason))

    def continue_(self):
        self.actions.append(("continue", None))


class FakePage:
    def __init__(self, url="https://www.zhipin.com/web/chat/recommend"):
        self.url = url
        self.main_frame = SimpleNamespace(url=url)
        self.handlers = {}
        self.evaluated = []

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def evaluate(self, _script, *_args):
        self.evaluated.append(_script)
        return None


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.init_scripts = []
        self.routes = []
        self.handlers = {}

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)


def _install(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_HR_RISK_LOG", str(tmp_path / "risk.jsonl"))
    page = FakePage()
    context = FakeContext([page])
    guard = ad.install_guards(context, page)
    return page, context, guard


def test_install_guards_registers_once_and_is_idempotent(monkeypatch, tmp_path):
    page, context, guard = _install(monkeypatch, tmp_path)
    assert guard is not None
    assert len(context.init_scripts) == 1
    assert len(context.routes) == 1
    script = context.init_scripts[0]
    assert "webdriver" in script
    assert "console.clear" in script
    assert "__SANITIZE_CONSOLE__" not in script
    assert "__ALLOW_CONSOLE_CLEAR__" not in script
    assert page.evaluated  # 已存在页面补注入一次

    # 再次安装不应重复注册
    ad.install_guards(context, page)
    assert len(context.init_scripts) == 1
    assert len(context.routes) == 1


def test_report_request_fulfilled_with_204(monkeypatch, tmp_path):
    _page, context, _guard = _install(monkeypatch, tmp_path)
    _pattern, handler = context.routes[0]
    route = FakeRoute("https://logapi.zhipin.com/dap/api/json?foo=1")
    handler(route)
    assert route.actions[0][0] == "fulfill"
    assert route.actions[0][1]["status"] == 204
    assert route.actions[0][1]["headers"]["access-control-allow-origin"] == "https://www.zhipin.com"


def test_security_script_request_is_aborted(monkeypatch, tmp_path):
    _page, context, _guard = _install(monkeypatch, tmp_path)
    _pattern, handler = context.routes[0]
    route = FakeRoute("https://www.zhipin.com/static/risk-detection.js")
    handler(route)
    assert route.actions == [("abort", "blockedbyclient")]


def test_guard_trips_via_framenavigated_and_exit_30(monkeypatch, tmp_path, capsys):
    page, _context, guard = _install(monkeypatch, tmp_path)
    frame = page.main_frame
    risk_url = "https://www.zhipin.com/web/common/403.html"
    frame.url = risk_url
    page.url = risk_url
    handler = page.handlers["framenavigated"][0]
    for _ in range(4):
        handler(frame)

    assert guard.tripped is True
    with pytest.raises(SystemExit) as excinfo:
        ad.exit_if_risk_page(page)
    assert excinfo.value.code == 30
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "captcha"
    assert payload["reason"] == "risk_guard"
    assert payload["risk_kind"] == "risk_navigation"


def test_exit_if_risk_page_returns_normally_for_recommend(monkeypatch, tmp_path):
    page, _context, _guard = _install(monkeypatch, tmp_path)
    assert ad.exit_if_risk_page(page) is None


def test_get_or_create_recommend_page_installs_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_HR_RISK_LOG", str(tmp_path / "risk.jsonl"))
    import boss_job_switcher

    class Context(FakeContext):
        def new_page(self):
            new_page = FakePage(url="about:blank")
            new_page.goto = lambda *args, **kwargs: None
            self.pages.append(new_page)
            return new_page

    context = Context([])
    page = boss_job_switcher.get_or_create_recommend_page(context)
    assert page.url == "about:blank"
    assert len(context.init_scripts) == 1
    assert len(context.routes) == 1
    assert ad.get_guard(page) is not None
    assert page.evaluated


# ---------------------------------------------------------------------------
# human typing
# ---------------------------------------------------------------------------


class FakeElement:
    def __init__(self):
        self.actions = []

    def focus(self):
        self.actions.append(("focus",))

    def type(self, char, delay=0):
        self.actions.append(("type", char, delay))

    def press(self, key):
        self.actions.append(("press", key))


def test_human_type_text_types_char_by_char_and_shift_enter(monkeypatch):
    monkeypatch.setattr(hi.time, "sleep", lambda _seconds: None)
    element = FakeElement()
    assert hi.human_type_text(element, "ab\nc") is True
    assert element.actions == [
        ("focus",),
        ("type", "a", 0),
        ("type", "b", 0),
        ("press", "Shift+Enter"),
        ("type", "c", 0),
    ]


def test_human_type_text_normalizes_crlf_and_reports_failure(monkeypatch):
    monkeypatch.setattr(hi.time, "sleep", lambda _seconds: None)
    element = FakeElement()
    assert hi.human_type_text(element, "a\r\nb") is True
    assert ("press", "Shift+Enter") in element.actions

    class Broken(FakeElement):
        def type(self, char, delay=0):
            raise RuntimeError("detached")

    assert hi.human_type_text(Broken(), "abc") is False


def test_typing_gap_env_is_clamped(monkeypatch):
    monkeypatch.setenv("BOSS_HR_TYPING_MIN_MS", "10")
    monkeypatch.setenv("BOSS_HR_TYPING_MAX_MS", "5")
    assert hi._typing_gap_ms() == (10, 10)
