<#
stop_boss.ps1 - 按 PID 停止 BOSS 自动化脚本（stop.cmd 的实际逻辑）

匹配范围（任一命中即视为 BOSS 脚本）：
  1. 进程命令行包含本项目根目录（例如 E:\boss_bot\...）
  2. 进程命令行包含本项目脚本名（scheduled_boss_run / boss_hr / auto_greet /
     recommend_list / recommend_download / boss_jd / run_orchestrator 等）

只结束脚本进程本身（taskkill /F，不带 /T），
不会关闭自动化专用 Edge（9222）窗口，浏览器可以继续留着登录态。

⚠️ 本文件必须保存为 UTF-8 with BOM：Windows PowerShell 5.1 在无 BOM 时
   会把中文按 GBK 解码，导致引号错乱、脚本无法解析。

用法：
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\stop_boss.ps1 -List
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\stop_boss.ps1 -Id 1234,5678
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\stop_boss.ps1 -Force
#>
[CmdletBinding()]
param(
    [int[]]$Id = @(),
    [string]$Root = "",
    [switch]$Force,
    [switch]$List
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = Split-Path -Parent $PSScriptRoot
}
$Root = $Root.TrimEnd('\', '/')
$rootPattern = [regex]::Escape($Root)
$scriptPattern = 'scheduled_boss_run|boss_hr|boss-hr|auto_greet|recommend_list|recommend_download|boss_jd|run_orchestrator|cli_runner|score_resumes|generate_html_report'

function Get-BossProcessList {
    $items = @()
    try {
        $all = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='boss-hr.exe' OR Name='boss_hr.exe'" -ErrorAction Stop
    } catch {
        Write-Host "[stop] 读取进程列表失败：$($_.Exception.Message)" -ForegroundColor Red
        return @()
    }
    foreach ($p in $all) {
        if ($p.ProcessId -eq $PID) { continue }
        $cmdline = [string]$p.CommandLine
        if ([string]::IsNullOrWhiteSpace($cmdline)) { continue }
        if ($cmdline -notmatch $rootPattern -and $cmdline -notmatch $scriptPattern) { continue }
        $items += [pscustomobject]@{
            Id       = [int]$p.ProcessId
            ParentId = [int]$p.ParentProcessId
            Name     = [string]$p.Name
            Command  = $cmdline
        }
    }
    return $items
}

function Format-Command([string]$text, [int]$width = 160) {
    $one = ($text -replace '\s+', ' ').Trim()
    if ($one.Length -gt $width) { return $one.Substring(0, $width - 3) + '...' }
    return $one
}

function Show-List($items) {
    foreach ($item in ($items | Sort-Object Id)) {
        Write-Host ("  PID {0,-6} 父进程 {1,-6} {2}" -f $item.Id, $item.ParentId, $item.Name) -ForegroundColor Cyan
        Write-Host ("       {0}" -f (Format-Command $item.Command)) -ForegroundColor DarkGray
    }
}

function Stop-ProcessId([int]$processId) {
    # 只用 /F，不用 /T：/T 会连带杀掉子进程树，把自动化专用 Edge（9222）也关掉。
    # 子脚本本身也会被匹配到并逐个结束，不需要树杀。
    $output = & taskkill.exe /PID $processId /F 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host ("  [已停止] PID {0}" -f $processId) -ForegroundColor Green
        return $true
    }
    Write-Host ("  [失败]   PID {0}：{1}" -f $processId, (Format-Command ($output -join ' ') 120)) -ForegroundColor Yellow
    return $false
}

# ---------------- 指定 PID 模式 ----------------
if ($Id.Count -gt 0) {
    Write-Host ("[stop] 指定 PID：{0}" -f ($Id -join ', '))
    foreach ($processId in $Id) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        if (-not $proc) {
            Write-Host ("  PID {0} 不存在（可能已退出）" -f $processId) -ForegroundColor Yellow
            continue
        }
        $cmdline = [string]$proc.CommandLine
        $looksBoss = ($cmdline -match $rootPattern) -or ($cmdline -match $scriptPattern)
        Write-Host ("  PID {0} {1}" -f $processId, $proc.Name) -ForegroundColor Cyan
        Write-Host ("       {0}" -f (Format-Command $cmdline)) -ForegroundColor DarkGray
        if (-not $looksBoss -and -not $Force) {
            $answer = Read-Host "  该进程不像 BOSS 脚本，仍要结束吗？(y/N)"
            if ($answer -notmatch '^(?i)y(es)?$') { Write-Host "  已跳过"; continue }
        }
        Stop-ProcessId $processId | Out-Null
    }
    exit 0
}

# ---------------- 列表模式 ----------------
$procs = @(Get-BossProcessList)
if ($List) {
    if ($procs.Count -eq 0) {
        Write-Host "[stop] 没有检测到正在运行的 BOSS 脚本。" -ForegroundColor Green
    } else {
        Write-Host ("[stop] 检测到 {0} 个 BOSS 相关进程：" -f $procs.Count)
        Show-List $procs
    }
    exit 0
}

# ---------------- 默认模式：列出 + 确认后全部停止 ----------------
if ($procs.Count -eq 0) {
    Write-Host "[stop] 没有检测到正在运行的 BOSS 脚本。" -ForegroundColor Green
    exit 0
}

Write-Host ("[stop] 检测到 {0} 个 BOSS 相关进程：" -f $procs.Count)
Show-List $procs
if (-not $Force) {
    $answer = Read-Host "确认全部停止？(y/N)"
    if ($answer -notmatch '^(?i)y(es)?$') {
        Write-Host "[stop] 已取消。"
        exit 0
    }
}

# 逐个 PID 结束（不做进程树 kill），9222 的 Edge 窗口保持不动
Write-Host "[stop] 开始按 PID 停止（浏览器窗口保持不动）..."
foreach ($item in ($procs | Sort-Object Id -Descending)) {
    Stop-ProcessId $item.Id | Out-Null
}

Start-Sleep -Milliseconds 800
$left = @(Get-BossProcessList)
if ($left.Count -gt 0) {
    Write-Host "[stop] 仍有残留进程，继续停止："
    foreach ($item in ($left | Sort-Object Id)) {
        Stop-ProcessId $item.Id | Out-Null
    }
    Start-Sleep -Milliseconds 500
    $left = @(Get-BossProcessList)
}
if ($left.Count -gt 0) {
    Write-Host ("[stop] 警告：仍有 {0} 个进程未停止（可能权限不足或属于其他用户）。" -f $left.Count) -ForegroundColor Yellow
    Show-List $left
    exit 1
}

Write-Host "[stop] 已全部停止（9222 浏览器窗口未关闭）。" -ForegroundColor Green
exit 0
