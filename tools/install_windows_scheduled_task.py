#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Install Windows Task Scheduler entries for scheduled BOSS automation.

支持多时段注册：根据 app.yaml 的 time_window 配置，自动为每个时段
注册一个 Windows 计划任务。例如：
  - 早班 09:00 → BossHrDaemon_Morning
  - 晚班 18:00 → BossHrDaemon_Evening

用法：
  # 自动读取 app.yaml 的 time_window，为每个时段注册一个计划任务
  python tools\install_windows_scheduled_task.py --auto-windows --allow-auto-confirm

  # 手动指定时间（兼容旧用法）
  python tools\install_windows_scheduled_task.py --time 09:00 --allow-auto-confirm --daemon
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read_time_windows_from_config() -> list[tuple[str, str]]:
    """从 app.yaml 读取所有 time_window，返回 [(start, end), ...] 去重列表。"""
    import yaml

    config_path = ROOT / "config" / "app.yaml"
    if not config_path.exists():
        return []
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    jobs = (
        (data.get("app") or {}).get("scheduler", {}).get("jobs", [])
    )
    windows: list[tuple[str, str]] = []
    for job in jobs:
        tw = job.get("time_window")
        if tw and isinstance(tw, list) and len(tw) >= 2:
            pair = (str(tw[0]), str(tw[1]))
            if pair not in windows:
                windows.append(pair)
    # 按开始时间排序
    windows.sort(key=lambda x: x[0])
    return windows


def _register_task(
    name: str,
    start_time: str,
    command: str,
    arguments: str,
    cwd: str,
) -> bool:
    """用 XML 方式注册 Windows 计划任务。返回 True/False。

    相比 schtasks /Create 命令行方式，XML 方式可以精细控制：
    - WorkingDirectory（解决 Start In=N/A 导致的 cwd 问题）
    - DisallowStartIfOnBatteries / StopIfGoingOnBatteries（笔记本电池不中断）
    - MultipleInstancesPolicy
    """
    import os
    import tempfile
    from datetime import date

    user = os.environ.get("USERNAME") or ""
    today = date.today().isoformat()
    start_boundary = f"{today}T{start_time}:00"

    xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>BOSS HR Daemon</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start_boundary}</StartBoundary>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <ExecutionTimeLimit>PT72H</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <Enabled>true</Enabled>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{cwd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>'''

    xml_path = os.path.join(tempfile.gettempdir(), f"boss_hr_task_{name}.xml")
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(xml)

    proc = subprocess.run(
        ["schtasks", "/Create", "/XML", xml_path, "/TN", name, "/F"],
        capture_output=True,
        encoding="gbk",
        errors="replace",
    )

    try:
        os.remove(xml_path)
    except OSError:
        pass

    if proc.returncode != 0:
        sys.stderr.write(f"[{name}] 注册失败：{proc.stderr or proc.stdout}\n")
        return False
    if proc.stdout:
        print(proc.stdout.strip())
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 Windows 定时任务。")
    parser.add_argument("--time", help="每天运行时间，格式 HH:MM，例如 09:30（单时段）")
    parser.add_argument("--name", default="BossHrScheduledRun", help="任务计划名称（单时段模式）")
    parser.add_argument("--dry-run", action="store_true", help="定时任务只演练，不真实打招呼")
    parser.add_argument("--allow-auto-confirm", action="store_true", help="允许定时任务自动确认并操作 BOSS")
    parser.add_argument("--daemon", action="store_true", help="注册 daemon 常驻轮询模式")
    parser.add_argument(
        "--auto-windows",
        action="store_true",
        help="自动从 app.yaml 读取 time_window，为每个时段注册一个计划任务",
    )
    args = parser.parse_args(argv)

    # 构建 Task 命令
    # 优先通过 run_daemon.bat 运行：复用 cwd (cd /d %~dp0) + stderr 捕获 (2>&1 | Tee-Object)
    bat_path = ROOT / "run_daemon.bat"
    if bat_path.exists() and not args.dry_run:
        # run_daemon.bat 硬编码 --allow-auto-confirm --daemon，只需 --no-pause 跳过 pause
        task_command = str(bat_path)
        task_arguments = "--no-pause"
    else:
        # fallback：直接 python 命令（dry-run 或 bat 不存在时）
        task_command = f'"{sys.executable}"'
        parts = ["-X", "utf8", f'"{ROOT / "tools" / "scheduled_boss_run.py"}"']
        if args.dry_run:
            parts.append("--dry-run")
        if args.allow_auto_confirm:
            parts.append("--allow-auto-confirm")
        if args.daemon or args.auto_windows:
            parts.append("--daemon")
        task_arguments = " ".join(parts)

    # 模式1：auto-windows，为每个唯一开始时间注册独立的 daemon 任务
    # daemon 内部按各岗位 time_window 过滤，但 daemon 会在所有窗口内岗位达标后退出。
    # 如果 app.yaml 有多个不连续时段（如 07:00-18:00 和 20:55-24:00），
    # 需要为每个时段的开始时间注册独立任务，否则 daemon 在早班结束后就退出，
    # 晚班不会被触发。
    if args.auto_windows:
        windows = _read_time_windows_from_config()
        if not windows:
            print("app.yaml 未配置 time_window，无法自动注册。请先配置 time_window 或使用 --time 手动指定。")
            return 1

        print(f"从 app.yaml 读取到 {len(windows)} 个唯一时段：")
        for start, end in windows:
            print(f"  {start} - {end}")

        # 为每个唯一开始时间注册一个 daemon 任务
        # 任务名按开始时间区分：BossHrDaemon（最早）、BossHrDaemon_2、BossHrDaemon_3...
        all_ok = True
        for idx, (start, end) in enumerate(windows):
            if idx == 0:
                task_name = "BossHrDaemon"
            else:
                task_name = f"BossHrDaemon_{idx + 1}"
            print(f"\n注册 daemon 任务：{task_name} @ {start}（窗口 {start}-{end}）")
            print(f"  daemon 将处理 app.yaml 中所有配置的岗位，按各自 time_window 过滤")
            if _register_task(task_name, start, task_command, task_arguments, str(ROOT)):
                print(f"  ✅ {task_name} 已安装，每天 {start} 运行")
                print(f"     查看：schtasks /Query /TN {task_name} /V /FO LIST")
                print(f"     触发：schtasks /Run /TN {task_name}")
                print(f"     删除：schtasks /Delete /TN {task_name} /F")
            else:
                print(f"  ❌ {task_name} 注册失败")
                all_ok = False
        return 0 if all_ok else 1

    # 模式2：单时段（兼容旧用法）
    if not args.time:
        parser.error("请使用 --auto-windows 或 --time 指定运行时间")

    if _register_task(args.name, args.time, task_command, task_arguments, str(ROOT)):
        print(f"任务已安装：{args.name}，每天 {args.time} 运行")
        print(f"查看：schtasks /Query /TN {args.name} /V /FO LIST")
        print(f"触发：schtasks /Run /TN {args.name}")
        print(f"删除：schtasks /Delete /TN {args.name} /F")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
