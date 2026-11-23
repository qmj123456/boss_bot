# -*- coding: utf-8 -*-
"""boss_hr.application.doctor_service — `boss-hr doctor` 业务逻辑。

复用 boss_hr.adapters.browser_preflight（不再复制 CDP / Cookie 检查）。
doctor 不连接业务岗位 API、不创建 run、不写业务输出。
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent

from boss_hr.contracts.results import CommandResult, ok, error
from boss_hr.contracts.errors import ExitCode, ErrorCode, UnifiedError
from boss_hr.adapters.browser_preflight import (
    BrowserProbe,
    check_python_version,
    check_patchright_installed,
    check_edge_executable,
    check_cdp_port_listening,
    check_cdp_connectable,
    check_boss_logged_in,
    CDP_HOST,
    CDP_PORT,
    CDP_URL_DEFAULT,
)


# ============================================================
# 启动自动化专用 Edge（--launch-edge）
# ============================================================

def launch_edge(*, wait_seconds: int = 15) -> dict:
    """带 CDP 调试端口启动**自动化专用 Edge**（独立 user-data-dir）并等待端口监听。

    Edge 136+（含本机 Edge 152）安全限制：默认 profile 上
    --remote-debugging-port 被静默忽略，"日常 Edge + CDP"不可行，CDP
    必须配非默认 --user-data-dir（%LOCALAPPDATA%\\boss-hr-edge-profile）。
    专用目录与日常 Edge 互不冲突（Chromium 单例按 profile 目录隔离），
    日常 Edge 可以照常开着。统一委托
    boss_hr.adapters.browser_environment.launch_edge_with_cdp()。

    Returns:
        dict: {"ok": bool, "edge_path": str, "profile_dir": str,
               "pid": int | None, "error": str | None}
    """
    from boss_hr.adapters.browser_environment import launch_edge_with_cdp

    launch = launch_edge_with_cdp(wait_seconds=wait_seconds)
    return {
        "ok": launch.ok,
        "error": launch.error_code,
        "message": launch.message,
        "edge_path": launch.edge_path,
        "profile_dir": launch.profile_dir,
        "pid": launch.pid,
    }


# ============================================================
# 主入口：run_doctor
# ============================================================

def run_doctor(*, launch_edge_flag: bool = False,
               skip_browser: bool = False) -> CommandResult:
    """boss-hr doctor 主入口。

    skip_browser=True 用于不需要 CDP 的"纯本地"检查（pytest 用）。
    """
    # 0. Python 版本
    py_ok, py_ver = check_python_version()

    # 1. patchright 安装
    pr_ok = check_patchright_installed()

    probe = BrowserProbe(
        python_ok=py_ok,
        python_version=py_ver,
        patchright_installed=pr_ok,
    )

    if skip_browser:
        return _ok_or_diagnose_only(probe)

    # 2. --launch-edge 流程
    if launch_edge_flag:
        result = launch_edge()
        if not result["ok"]:
            return error(
                error_obj=UnifiedError(
                    code=ErrorCode(result["error"]),
                    message=result["message"],
                    recoverable=True,
                ),
                exit_code=ExitCode.GENERIC,
                next_action="retry_doctor_after_login",
                remediation={
                    "command": "boss-hr doctor",
                    "instructions": [
                        "退出自动化专用 Edge 窗口后重新运行 boss-hr doctor --launch-edge"
                        "（日常 Edge 不用关）",
                        "在打开的专用 Edge 窗口中登录 BOSS 招聘者后台",
                        "登录后保持该窗口打开",
                        "重新运行 boss-hr doctor 验证",
                    ],
                },
            )

    # 3. Edge 可执行
    edge_path = check_edge_executable()
    probe.edge_path = edge_path

    # 4. CDP 端口
    probe.cdp_port_listening = check_cdp_port_listening()
    if not probe.cdp_port_listening:
        return _diagnose(probe,
            ErrorCode.CDP_NOT_RUNNING,
            f"未检测到 Edge 远程调试端口 {CDP_PORT}",
            "launch_edge",
            "boss-hr doctor --launch-edge",
            [
                "运行 boss-hr doctor --launch-edge 启动自动化专用 Edge"
                "（独立 profile，与日常 Edge 互不冲突）",
                "在该专用 Edge 窗口中登录 BOSS 招聘者后台",
                "登录后重新运行 boss-hr doctor 验证",
            ],
        )

    # 5. CDP 连接
    cdp_ok, cdp_err = check_cdp_connectable()
    probe.cdp_connected = cdp_ok
    probe.cdp_connected_error = cdp_err
    if not cdp_ok:
        return _diagnose(probe,
            ErrorCode.CDP_CONNECT_FAILED,
            f"Edge CDP 端口已开但连不上：{cdp_err}",
            "restart_edge",
            "boss-hr doctor --launch-edge",
            [
                "完全关闭该 Edge 后重新以 --remote-debugging-port=9222 启动",
                "或在另一个浏览器窗口登录后再次运行",
            ],
        )

    # 6. BOSS 登录态
    logged_in, login_info = check_boss_logged_in()
    probe.boss_logged_in = logged_in
    probe.page_url = login_info.get("current_url") or ""
    probe.page_kind = login_info.get("page_kind") or ""
    if not logged_in:
        return _diagnose(probe,
            ErrorCode.BOSS_LOGIN_REQUIRED,
            "Edge 已启动但 BOSS 招聘者未登录。",
            "login_boss",
            "boss-hr doctor",
            [
                "在专用 Edge 窗口中打开 https://www.zhipin.com 并扫码登录招聘者后台",
                "登录后保持 Edge 窗口打开",
                "重新运行 boss-hr doctor 验证",
            ],
        )

    # 7. BOSS 页面类型
    if probe.page_kind in ("unknown",) and not probe.page_url:
        return _diagnose(probe,
            ErrorCode.BOSS_PAGE_REQUIRED,
            "Edge 已登录但当前不在 BOSS 招聘者页面。",
            "open_boss_page",
            "boss-hr doctor",
            [
                "在专用 Edge 中打开 https://www.zhipin.com/web/chat/recommend "
                "或 https://www.zhipin.com/web/chat/index",
                "重新运行 boss-hr doctor 验证",
            ],
        )

    probe.boss_page_ok = True

    return ok(
        status="ready",
        data={
            "edge_found": probe.edge_path is not None,
            "edge_path": probe.edge_path,
            "cdp_port": CDP_PORT,
            "cdp_connected": probe.cdp_connected,
            "boss_logged_in": probe.boss_logged_in,
            "page_url": probe.page_url,
            "page_kind": probe.page_kind,
            "python_version": probe.python_version,
            "patchright_installed": probe.patchright_installed,
        },
        next_action="start",
    )


def _diagnose(probe: BrowserProbe, code: ErrorCode, message: str,
              next_action: str, command: Optional[str],
              instructions: list[str]) -> CommandResult:
    rem = {"instructions": instructions}
    if command:
        rem["command"] = command
    return error(
        error_obj=UnifiedError(code=code, message=message, recoverable=True),
        exit_code=ExitCode.GENERIC,
        next_action=next_action,
        remediation=rem,
    )


def _ok_or_diagnose_only(probe: BrowserProbe) -> CommandResult:
    """无浏览器路径：只返回本地检查结果（pytest / 无 CDP 环境用）。"""
    return ok(
        status="local_only",
        data={
            "python_version": probe.python_version,
            "patchright_installed": probe.patchright_installed,
            "browser_check_skipped": True,
        },
        next_action="run_with_browser",
    )


__all__ = ["run_doctor", "launch_edge"]