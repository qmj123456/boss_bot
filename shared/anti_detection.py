# -*- coding: utf-8 -*-
"""浏览器层防风控增强（patchright / Playwright 同步 API）。

目标：在不改变业务主流程的前提下，补齐行为层与网络层的反检测能力：

1. 降低自动化特征
   - 覆盖 ``navigator.webdriver``（原型 getter，``toString`` 伪装成原生函数）
   - 清理 Playwright / Puppeteer / ChromeDriver 留下的常见全局变量
   - 对 ``console`` 的对象参数做 O(1) 归一化，干扰「大对象打印耗时差」的
      DevTools 检测

2. 拦截风控/监控请求
   - 阻断 ``risk-detection``、``zhipin-security/web/boss`` 等检测脚本
   - 对 BOSS 的日志 / APM / warlock 上报地址直接返回 HTTP 204

3. 风控异常熔断
   - 60 秒内第 4 次跳转风控页（403 / verify / security-check / safe verify）
   - 或同一 URL 在 15 秒内加载 5 次（SPA 自刷新循环）
   - 命中后只记录状态并提示人工处理，不自动跳回、不关闭用户浏览器

所有能力都可以通过环境变量关闭，默认开启：

===============================  ==========================================
环境变量                          作用
===============================  ==========================================
BOSS_HR_ANTI_DETECT=0            关闭注入脚本 + 请求拦截（保留熔断）
BOSS_HR_RISK_GUARD=0             关闭风控页 / 刷新循环熔断
BOSS_HR_BLOCK_SECURITY_SCRIPTS=0 不阻断风控检测脚本
BOSS_HR_BLOCK_REPORTS=0          不拦截日志/监控上报
BOSS_HR_CONSOLE_SANITIZE=0       不包装 console（保留对象展开能力）
BOSS_HR_ALLOW_CONSOLE_CLEAR=1    保留页面原本的 console.clear 行为
BOSS_HR_ANTI_DETECT_DEBUG=1      把拦截/安装细节打到 stderr
===============================  ==========================================

设计约束：
- 不新增页面可见的全局标记，避免自身成为指纹。
- 所有安装步骤都是 best-effort：页面已关闭、浏览器不支持某 API 时静默跳过，
  绝不让防风控代码成为主流程失败的原因。
- 只读页面状态，不主动导航、不关闭浏览器；命中熔断后由调用方决定退出。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# URL 规则
# ---------------------------------------------------------------------------

# 风控 / 验证页（主 frame 被推到这里通常意味着平台已经判定异常）
RISK_URL_RE = re.compile(
    r"(?:"
    r"/web/common/(?:403|nonsupport)\.html"
    r"|/web/user/safe/verify"
    r"|/web/passport/(?:zp/(?:403|verify|security)\.html"
    r"|cm/(?:403|verify|security-check)\.html)"
    r")",
    re.IGNORECASE,
)

# 只保留真正会做环境检测的脚本。验证码 SDK（zhipin/geek/sdk）不在默认
# 阻断列表里，避免人工验证页加载不出来。
_SECURITY_URL_RE = re.compile(
    r"(?:zhipin-security/web/boss/|risk-detection)",
    re.IGNORECASE,
)

# BOSS 的日志 / 监控上报端点。命中后本地返回 204，请求不会真正发出。
_REPORT_URL_RE = re.compile(
    r"(?:"
    r"logapi(?:-dev)?\.(?:zhipin|weizhipin)\.com/dap/api/json"
    r"|apm-fe(?:-qa)?\.(?:zhipin|weizhipin)\.com/wapi/zpApm/"
    r"|warlock\.zhipin\.com/wapi/warlock/"
    r"|shink\.zhipin\.com/wapi/dapCommon/json"
    r")",
    re.IGNORECASE,
)

# 熔断阈值（与参考实现一致：60s 内第 4 次风控页；15s 内同一 URL 加载 5 次）
RISK_BOUNCE_WINDOW_MS = 60_000
RISK_BOUNCE_LIMIT = 4
RELOAD_WINDOW_MS = 15_000
RELOAD_LIMIT = 5


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _debug(message: str) -> None:
    if _env_flag("BOSS_HR_ANTI_DETECT_DEBUG", False):
        print(f"[anti-detect] {message}", file=sys.stderr, flush=True)


def is_risk_url(url: str) -> bool:
    """Return True when *url* is a BOSS risk-control / verification page."""
    return bool(url) and bool(RISK_URL_RE.search(url))


# ---------------------------------------------------------------------------
# 注入脚本：降低浏览器自动化特征
# ---------------------------------------------------------------------------

_STEALTH_SCRIPT = r"""
(() => {
  'use strict';

  // ---- 让包装函数看起来像原生函数 -------------------------------------
  const nativeToString = Function.prototype.toString;
  const nativeMap = new WeakMap();
  const asNative = (fn, name) => {
    try {
      Object.defineProperty(fn, 'name', {
        value: name, configurable: true, enumerable: false,
      });
    } catch (e) {}
    nativeMap.set(fn, 'function ' + name + '() { [native code] }');
    return fn;
  };
  const fakeToString = function toString() {
    try {
      const mapped = nativeMap.get(this);
      if (typeof mapped === 'string') return mapped;
    } catch (e) {}
    return nativeToString.call(this);
  };
  nativeMap.set(fakeToString, 'function toString() { [native code] }');
  try {
    Object.defineProperty(Function.prototype, 'toString', {
      value: fakeToString,
      writable: true,
      configurable: true,
      enumerable: false,
    });
  } catch (e) {}

  // ---- navigator.webdriver ---------------------------------------------
  try {
    const proto = Object.getPrototypeOf(navigator);
    const desc = Object.getOwnPropertyDescriptor(proto, 'webdriver');
    Object.defineProperty(proto, 'webdriver', {
      get: asNative(function webdriver() { return false; }, 'get webdriver'),
      configurable: true,
      enumerable: desc ? !!desc.enumerable : true,
    });
  } catch (e) {}

  // ---- 清理常见自动化全局变量 ------------------------------------------
  try {
    const names = [
      '__playwright', '__pw_manual', '__PW_inspect',
      '__puppeteer_evaluation_script__', '_selenium',
      'callSelenium', '_Selenium_IDE_Recorder',
    ];
    for (const name of names) {
      try { delete window[name]; } catch (e) {}
    }
    for (const key of Object.keys(window)) {
      if (/^cdc_/.test(key) || /^__driver_/.test(key)) {
        try { delete window[key]; } catch (e) {}
      }
    }
  } catch (e) {}

  // ---- console 时间差检测对抗 ------------------------------------------
  // 平台会连续打印大对象并比较 performance.now()，DevTools 打开时 V8
  // inspector 序列化显著变慢。这里先把对象/函数参数归一化成短字符串，
  // 让两条路径的耗时接近；字符串参数保持原样，不影响普通日志。
  if (__SANITIZE_CONSOLE__) {
    try {
      const objectTag = Object.prototype.toString;
      const sanitize = (args) => {
        const out = new Array(args.length);
        for (let i = 0; i < args.length; i += 1) {
          const value = args[i];
          if (value !== null && typeof value === 'object') {
            try { out[i] = objectTag.call(value); }
            catch (e) { out[i] = '[object Object]'; }
          } else if (typeof value === 'function') {
            out[i] = '[Function: ' + (value.name || 'anonymous') + ']';
          } else {
            out[i] = value;
          }
        }
        return out;
      };
      const methods = [
        'log', 'info', 'debug', 'warn', 'error',
        'table', 'dir', 'dirxml', 'trace',
        'group', 'groupCollapsed',
      ];
      for (const method of methods) {
        const original = console[method];
        if (typeof original !== 'function') continue;
        const wrapped = asNative(function () {
          return original.apply(console, sanitize(arguments));
        }, method);
        try {
          Object.defineProperty(console, method, {
            value: wrapped,
            writable: true,
            configurable: true,
            enumerable: false,
          });
        } catch (e) {}
      }
    } catch (e) {}
  }

  // ---- console.clear 改为无副作用空函数 ---------------------------------
  // 平台在检测命中后会周期性清屏，抹掉排查线索；替身保留原生 toString
  // 形态，避免被「非原生函数」完整性校验命中。
  if (!__ALLOW_CONSOLE_CLEAR__) {
    try {
      const noopClear = asNative(function clear() {}, 'clear');
      try {
        Object.defineProperty(console, 'clear', {
          value: noopClear,
          writable: true,
          configurable: true,
          enumerable: false,
        });
      } catch (e) {}
    } catch (e) {}
  }
})();
"""


def _stealth_script() -> str:
    sanitize = "true" if _env_flag("BOSS_HR_CONSOLE_SANITIZE", True) else "false"
    allow_clear = "true" if _env_flag("BOSS_HR_ALLOW_CONSOLE_CLEAR", False) else "false"
    return (
        _STEALTH_SCRIPT
        .replace("__SANITIZE_CONSOLE__", sanitize)
        .replace("__ALLOW_CONSOLE_CLEAR__", allow_clear)
    )


# ---------------------------------------------------------------------------
# 风控熔断：纯状态机，便于单测
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskEvent:
    kind: str
    url: str
    message: str
    count: int


@dataclass
class RiskTracker:
    """Track risk-page bounces and same-URL reload loops.

    ``observe_navigation`` 只处理主 frame 的整页跳转；``observe_load`` 处理
    文档 load 事件。两者都接受可注入的 ``now_ms``，单测不需要真实等待。
    """

    bounce_window_ms: int = RISK_BOUNCE_WINDOW_MS
    bounce_limit: int = RISK_BOUNCE_LIMIT
    reload_window_ms: int = RELOAD_WINDOW_MS
    reload_limit: int = RELOAD_LIMIT
    tripped: bool = False
    kind: str = ""
    url: str = ""
    message: str = ""
    count: int = 0
    _bounces: list[float] = field(default_factory=list, repr=False)
    _loads: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def observe_navigation(self, url: str, now_ms: Optional[float] = None) -> Optional[RiskEvent]:
        if self.tripped or not url or url.startswith("about:"):
            return None
        if not is_risk_url(url):
            return None
        now = time.monotonic() * 1000.0 if now_ms is None else now_ms
        self._bounces = [
            stamp for stamp in self._bounces
            if now - stamp <= self.bounce_window_ms
        ]
        self._bounces.append(now)
        if len(self._bounces) >= self.bounce_limit:
            return self._trip(
                "risk_navigation",
                url,
                (
                    f"检测到 BOSS 反复跳转风控/验证页（{url}），"
                    f"{self.bounce_window_ms // 1000}s 内已 {len(self._bounces)} 次。"
                    "已停止自动操作：请在浏览器中完成验证/登录后重试；"
                    "继续自动操作可能触发封号。"
                ),
                len(self._bounces),
            )
        return None

    def observe_load(self, url: str, now_ms: Optional[float] = None) -> Optional[RiskEvent]:
        if self.tripped or not url or url.startswith("about:"):
            return None
        if is_risk_url(url):
            return None
        now = time.monotonic() * 1000.0 if now_ms is None else now_ms
        stamps = [
            stamp for stamp in self._loads.get(url, [])
            if now - stamp <= self.reload_window_ms
        ]
        stamps.append(now)
        self._loads[url] = stamps
        if len(stamps) >= self.reload_limit:
            return self._trip(
                "reload_loop",
                url,
                (
                    f"页面在 {self.reload_window_ms // 1000}s 内重复加载 "
                    f"{len(stamps)} 次（{url}），疑似 BOSS 风控或安全脚本被拦截"
                    "导致自刷新。已停止自动操作，请在浏览器中确认页面状态。"
                ),
                len(stamps),
            )
        return None

    def _trip(self, kind: str, url: str, message: str, count: int) -> RiskEvent:
        self.tripped = True
        self.kind = kind
        self.url = url
        self.message = message
        self.count = count
        return RiskEvent(kind=kind, url=url, message=message, count=count)


# ---------------------------------------------------------------------------
# 页面守卫
# ---------------------------------------------------------------------------


def _risk_log_path() -> Path:
    override = os.environ.get("BOSS_HR_RISK_LOG", "")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "logs" / "risk_guard_events.jsonl"


def _append_risk_log(event: RiskEvent) -> None:
    try:
        path = _risk_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": event.kind,
            "url": event.url,
            "count": event.count,
            "message": event.message,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # 日志失败不能影响主流程
        _debug(f"risk log write failed: {exc}")


class RiskGuard:
    """Per-page navigation observer that trips on repeated risk navigation."""

    def __init__(self, page: Any, *, log: Optional[Callable[[str], None]] = None) -> None:
        self.page = page
        self.tracker = RiskTracker()
        self._log = log or (lambda message: print(message, file=sys.stderr, flush=True))

    @property
    def tripped(self) -> bool:
        return self.tracker.tripped

    @property
    def message(self) -> str:
        return self.tracker.message

    def on_framenavigated(self, frame: Any) -> None:
        try:
            if frame != self.page.main_frame:
                return
            url = frame.url
        except Exception:
            return
        event = self.tracker.observe_navigation(url)
        if event is not None:
            self._handle(event)

    def on_load(self) -> None:
        try:
            url = self.page.url
        except Exception:
            return
        event = self.tracker.observe_load(url)
        if event is not None:
            self._handle(event)

    def _handle(self, event: RiskEvent) -> None:
        _append_risk_log(event)
        try:
            self._log(f"[anti-detect] {event.message}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 安装 / 查询
# ---------------------------------------------------------------------------

# id -> (context, handler) / id -> (page, guard)。持有强引用，避免 id 复用。
_INSTALLED_CONTEXTS: dict[int, tuple[Any, Callable[..., Any]]] = {}
_INSTALLED_PAGES: dict[int, tuple[Any, Optional[RiskGuard]]] = {}
_LAST_GUARD: Optional[RiskGuard] = None


def _report_headers(request: Any) -> dict[str, str]:
    headers = {
        str(key).lower(): str(value)
        for key, value in dict(getattr(request, "headers", {}) or {}).items()
    }
    origin = headers.get("origin") or "https://www.zhipin.com"
    requested = headers.get("access-control-request-headers") or (
        "content-type,x-requested-with,traceid,zp_token,__zp_stoken__"
    )
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-credentials": "true",
        "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
        "access-control-allow-headers": requested,
        "cache-control": "no-store",
    }


def _make_route_handler() -> Callable[[Any], None]:
    def handle(route: Any) -> None:
        try:
            url = str(route.request.url)
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass
            return

        try:
            if _REPORT_URL_RE.search(url):
                route.fulfill(status=204, headers=_report_headers(route.request))
                _debug(f"report -> 204: {url}")
            else:
                route.abort("blockedbyclient")
                _debug(f"blocked: {url}")
        except Exception as exc:
            _debug(f"route handling failed for {url}: {exc}")
            try:
                route.continue_()
            except Exception:
                pass

    return handle


def _block_pattern() -> Optional[re.Pattern[str]]:
    parts: list[str] = []
    if _env_flag("BOSS_HR_BLOCK_SECURITY_SCRIPTS", True):
        parts.append(_SECURITY_URL_RE.pattern)
    if _env_flag("BOSS_HR_BLOCK_REPORTS", True):
        parts.append(_REPORT_URL_RE.pattern)
    if not parts:
        return None
    return re.compile("(?:" + "|".join(f"(?:{part})" for part in parts) + ")", re.IGNORECASE)


def _attach_page(page: Any, *, install_stealth: bool) -> Optional[RiskGuard]:
    key = id(page)
    existing = _INSTALLED_PAGES.get(key)
    if existing is not None and existing[0] is page:
        return existing[1]

    guard: Optional[RiskGuard] = None
    if install_stealth:
        try:
            page.evaluate(_stealth_script())
        except Exception as exc:
            _debug(f"stealth evaluate skipped: {exc}")

    if _env_flag("BOSS_HR_RISK_GUARD", True):
        try:
            guard = RiskGuard(page)
            page.on("framenavigated", guard.on_framenavigated)
            page.on("load", guard.on_load)
        except Exception as exc:
            _debug(f"risk guard attach skipped: {exc}")
            guard = None

    _INSTALLED_PAGES[key] = (page, guard)
    return guard


def install_guards(
    context: Any,
    page: Any = None,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[RiskGuard]:
    """Install stealth + request blocking + risk fuse on *context* (and *page*).

    Safe to call repeatedly. Returns the RiskGuard for *page* when available.
    """
    if context is None:
        return None

    anti_detect = _env_flag("BOSS_HR_ANTI_DETECT", True)
    guard_enabled = _env_flag("BOSS_HR_RISK_GUARD", True)
    if not anti_detect and not guard_enabled:
        return get_guard(page) if page is not None else None

    key = id(context)
    if key not in _INSTALLED_CONTEXTS or _INSTALLED_CONTEXTS[key][0] is not context:
        route_handler = _make_route_handler()
        if anti_detect and hasattr(context, "add_init_script"):
            try:
                context.add_init_script(_stealth_script())
            except Exception as exc:
                _debug(f"add_init_script skipped: {exc}")

        if anti_detect:
            pattern = _block_pattern()
            if pattern is not None and hasattr(context, "route"):
                try:
                    context.route(pattern, route_handler)
                except Exception as exc:
                    _debug(f"context.route skipped: {exc}")

        if guard_enabled and hasattr(context, "on"):
            try:
                context.on(
                    "page",
                    lambda new_page: _attach_page(new_page, install_stealth=anti_detect),
                )
            except Exception as exc:
                _debug(f"context page listener skipped: {exc}")

        _INSTALLED_CONTEXTS[key] = (context, route_handler)

    # 已存在的页面：init script 不会回溯，补一次 evaluate
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []
    for candidate in pages:
        _attach_page(candidate, install_stealth=anti_detect)

    if page is None:
        return None
    try:
        if page not in pages:
            _attach_page(page, install_stealth=anti_detect)
    except Exception:
        pass

    guard = get_guard(page)
    if guard is not None and log is not None:
        guard._log = log
    return guard


def get_guard(page: Any) -> Optional[RiskGuard]:
    """Return the installed RiskGuard for *page*, if any."""
    if page is None:
        return None
    entry = _INSTALLED_PAGES.get(id(page))
    if entry is not None and entry[0] is page:
        return entry[1]
    return None


def risk_tripped(page: Any) -> bool:
    guard = get_guard(page)
    return bool(guard and guard.tripped)


def exit_if_risk_page(page: Any, *, exit_code: int = 30) -> None:
    """Stop the current command when a risk page / reload loop is detected.

    Prints a JSON line understood by ``boss_hr`` (exit 30 -> CAPTCHA) and
    raises ``SystemExit``. Returns normally when nothing suspicious happened.
    """
    guard = get_guard(page)
    url = ""
    try:
        url = str(page.url or "")
    except Exception:
        url = ""

    event: Optional[RiskEvent] = None
    if guard is not None and guard.tripped:
        event = RiskEvent(
            kind=guard.tracker.kind,
            url=guard.tracker.url,
            message=guard.tracker.message,
            count=guard.tracker.count,
        )
    elif is_risk_url(url):
        event = RiskEvent(
            kind="risk_navigation",
            url=url,
            message=(
                f"当前页面停留在 BOSS 验证/风控页（{url}）。"
                "已停止自动操作：请在浏览器中完成验证或重新登录后重试。"
            ),
            count=0,
        )

    if event is None:
        return

    payload = {
        "status": "captcha",
        "exit_code": exit_code,
        "message": event.message,
        "reason": "risk_guard",
        "risk_kind": event.kind,
        "url": event.url,
        "count": event.count,
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    raise SystemExit(exit_code)


__all__ = [
    "RISK_URL_RE",
    "RiskEvent",
    "RiskGuard",
    "RiskTracker",
    "exit_if_risk_page",
    "get_guard",
    "install_guards",
    "is_risk_url",
    "risk_tripped",
]
