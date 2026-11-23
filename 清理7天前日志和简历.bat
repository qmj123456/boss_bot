@echo off
setlocal
chcp 65001 >nul
set "CLEANUP_SCRIPT=%~f0"
set "CLEANUP_MODE=%~1"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$r=[IO.File]::ReadAllText($env:CLEANUP_SCRIPT); iex $r.Substring($r.LastIndexOf([char]35+' POWERSHELL_PAYLOAD')+20)"
set "CLEANUP_EXIT=%ERRORLEVEL%"

if "%CLEANUP_MODE%"=="" pause
goto :EOF

# POWERSHELL_PAYLOAD
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()

$RetentionDays = 7
$Cutoff = (Get-Date).AddDays(-$RetentionDays)
$AppRoot = [IO.Path]::GetFullPath((Split-Path -Parent $env:CLEANUP_SCRIPT))
$Mode = [string]$env:CLEANUP_MODE
$Preview = $Mode -in @('/PREVIEW', '--preview')
$AutoConfirm = $Mode -in @('/Y', '--yes')

if ($Mode -and -not ($Preview -or $AutoConfirm)) {
    Write-Host '参数错误。可用参数：' -ForegroundColor Red
    Write-Host '  不带参数       列出目标并询问是否删除'
    Write-Host '  /PREVIEW       只预览，不删除'
    Write-Host '  /Y              无需确认，直接删除（适合计划任务）'
    exit 2
}

function Resolve-OutputRoot {
    $envPath = Join-Path $AppRoot '.env'
    $configured = $null
    if (Test-Path -LiteralPath $envPath -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
            if ($line -match '^\s*BOSS_HR_OUTPUT_DIR\s*=\s*(.*?)\s*$') {
                $configured = $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    if (-not $configured) {
        $configured = [Environment]::GetEnvironmentVariable('BOSS_HR_OUTPUT_DIR')
    }
    if (-not $configured) {
        $configured = Join-Path ([Environment]::GetFolderPath('Desktop')) 'boss-hr-output'
    }
    $configured = [Environment]::ExpandEnvironmentVariables($configured)
    if (-not [IO.Path]::IsPathRooted($configured)) {
        $configured = Join-Path $AppRoot $configured
    }
    return [IO.Path]::GetFullPath($configured)
}

function Assert-SafeRoot([string]$Path, [string]$Label) {
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $driveRoot = [IO.Path]::GetPathRoot($full).TrimEnd('\')
    if (-not $full -or $full -eq $driveRoot -or $full -eq $AppRoot.TrimEnd('\')) {
        throw "$Label 路径不安全，已停止：$full"
    }
}

function Get-ItemBytes([string]$Path, [bool]$IsDirectory) {
    if (-not $IsDirectory) {
        try { return [int64](Get-Item -LiteralPath $Path -Force).Length } catch { return 0 }
    }
    try {
        return [int64]((Get-ChildItem -LiteralPath $Path -File -Force -Recurse -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum)
    } catch {
        return 0
    }
}

function Add-Target([Collections.Generic.List[object]]$List, [string]$Kind,
                    [string]$Path, [bool]$IsDirectory, [datetime]$Timestamp) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $List.Add([pscustomobject]@{
        Kind = $Kind
        Path = [IO.Path]::GetFullPath($Path)
        IsDirectory = $IsDirectory
        Timestamp = $Timestamp
        Bytes = Get-ItemBytes $Path $IsDirectory
    })
}

function Remove-EmptyFolders([string]$Root) {
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return }
    Get-ChildItem -LiteralPath $Root -Directory -Force -Recurse -ErrorAction SilentlyContinue |
        Sort-Object { $_.FullName.Length } -Descending |
        ForEach-Object {
            if (-not (Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
        }
}

$OutputRoot = Resolve-OutputRoot
$LogRoot = Join-Path $AppRoot 'logs'
$SchedulerRoot = Join-Path $AppRoot 'data\scheduler'
$DiagnosticsRoot = Join-Path $AppRoot 'data\diagnostics'

Assert-SafeRoot $OutputRoot '简历输出目录'
Assert-SafeRoot $LogRoot '日志目录'

$Targets = [Collections.Generic.List[object]]::new()

# 1. 程序日志：按文件最后修改时间清理；正在写入的日志不会命中。
if (Test-Path -LiteralPath $LogRoot -PathType Container) {
    Get-ChildItem -LiteralPath $LogRoot -File -Force -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $Cutoff } |
        ForEach-Object { Add-Target $Targets '日志' $_.FullName $false $_.LastWriteTime }
}

# 2. 调度历史：仅清理时间戳命名的历史 JSON，始终保留 latest_run.json。
if (Test-Path -LiteralPath $SchedulerRoot -PathType Container) {
    Get-ChildItem -LiteralPath $SchedulerRoot -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d{8}_\d{6}\.json$' -and $_.LastWriteTime -lt $Cutoff } |
        ForEach-Object { Add-Target $Targets '调度历史' $_.FullName $false $_.LastWriteTime }
}

# 3. 下载简历与本地截图：按 run 目录名中的创建时间删除整个历史运行产物。
#    只匹配 <输出根>\<岗位>\runs\yyyy-MM-dd_HHmmss[_序号]，不会碰 state、数据库或锁。
if (Test-Path -LiteralPath $OutputRoot -PathType Container) {
    Get-ChildItem -LiteralPath $OutputRoot -Directory -Force -ErrorAction SilentlyContinue |
        ForEach-Object {
            $runsRoot = Join-Path $_.FullName 'runs'
            if (-not (Test-Path -LiteralPath $runsRoot -PathType Container)) { return }
            Get-ChildItem -LiteralPath $runsRoot -Directory -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}_\d{6}(?:_\d+)?$' } |
                ForEach-Object {
                    $stamp = [datetime]::MinValue
                    $ok = [datetime]::TryParseExact(
                        $_.Name.Substring(0, 17), 'yyyy-MM-dd_HHmmss',
                        [Globalization.CultureInfo]::InvariantCulture,
                        [Globalization.DateTimeStyles]::None, [ref]$stamp)
                    if ($ok -and $stamp -lt $Cutoff) {
                        Add-Target $Targets '历史简历/截图' $_.FullName $true $stamp
                    }
                }
        }
}

# 4. 本地截图诊断产物。
if (Test-Path -LiteralPath $DiagnosticsRoot -PathType Container) {
    Get-ChildItem -LiteralPath $DiagnosticsRoot -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^resume_(?:capture|long)_smoke_' } |
        ForEach-Object {
            $newest = Get-ChildItem -LiteralPath $_.FullName -File -Force -Recurse -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
            $timestamp = if ($newest) { $newest.LastWriteTime } else { $_.LastWriteTime }
            if ($timestamp -lt $Cutoff) {
                Add-Target $Targets '截图诊断' $_.FullName $true $timestamp
            }
        }
}

$Targets = @($Targets | Sort-Object Path -Unique)
$TotalBytes = [int64](($Targets | Measure-Object -Property Bytes -Sum).Sum)
$TotalSize = if ($TotalBytes -ge 1GB) {
    '{0:N2} GB' -f ($TotalBytes / 1GB)
} elseif ($TotalBytes -ge 1MB) {
    '{0:N2} MB' -f ($TotalBytes / 1MB)
} else {
    '{0:N2} KB' -f ($TotalBytes / 1KB)
}

Write-Host ''
Write-Host 'BOSS HR 历史文件清理工具' -ForegroundColor Cyan
Write-Host "保留天数：$RetentionDays 天"
Write-Host "截止时间：$($Cutoff.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "程序目录：$AppRoot"
Write-Host "简历目录：$OutputRoot"
Write-Host ''

if ($Targets.Count -eq 0) {
    Write-Host '没有找到需要清理的文件。' -ForegroundColor Green
    exit 0
}

$Targets | Group-Object Kind | ForEach-Object {
    $size = [int64](($_.Group | Measure-Object -Property Bytes -Sum).Sum)
    Write-Host ("{0,-14} {1,6} 项  {2,10:N2} MB" -f $_.Name, $_.Count, ($size / 1MB))
}
Write-Host ('-' * 54)
Write-Host "合计：$($Targets.Count) 项，预计释放 $TotalSize" -ForegroundColor Yellow

if ($Preview) {
    Write-Host ''
    Write-Host '预览模式，不会删除。目标如下：' -ForegroundColor Cyan
    $Targets | ForEach-Object { Write-Host "[$($_.Kind)] $($_.Path)" }
    exit 0
}

if (-not $AutoConfirm) {
    Write-Host ''
    $answer = Read-Host '确定永久删除以上 7 天前的文件吗？输入 Y 继续'
    if ($answer -notin @('Y', 'y')) {
        Write-Host '已取消，未删除任何文件。' -ForegroundColor Yellow
        exit 0
    }
}

$Deleted = 0
$Failed = 0
foreach ($target in $Targets) {
    try {
        if ($target.IsDirectory) {
            Remove-Item -LiteralPath $target.Path -Force -Recurse -ErrorAction Stop
        } else {
            Remove-Item -LiteralPath $target.Path -Force -ErrorAction Stop
        }
        $Deleted++
    } catch {
        $Failed++
        Write-Host "删除失败：$($target.Path)；$($_.Exception.Message)" -ForegroundColor Red
    }
}

Remove-EmptyFolders $LogRoot
Write-Host ''
if ($Failed -eq 0) {
    Write-Host "清理完成：删除 $Deleted 项，释放约 $TotalSize。" -ForegroundColor Green
    exit 0
}
Write-Host "清理结束：成功 $Deleted 项，失败 $Failed 项。失败项可能正被程序占用。" -ForegroundColor Yellow
exit 1
