<#
.SYNOPSIS
    把 live/dispatch.py 注册成 Windows 任务计划，每 5 分钟唤醒一次。

.DESCRIPTION
    为什么用「每 5 分钟唤醒 + 到点现算」而不是「每天改一次定时时刻」：
      - 美股收盘在 UTC 里每天在变（夏令时 20:00Z / 冬令时 21:00Z / 早收日 17:00Z）
      - 改系统定时任务需要权限、跨日切换有竞态、失败无感知
      - 高频唤醒后由 live/calendar.py 现算「今天是不是交易日、收盘几点、现在到没到」，
        其余 287 次唤醒直接 exit 0，单次开销约 100ms

    触发逻辑全部在 live/dispatch.py 里，本脚本只负责把「闹钟」挂上去。

.EXAMPLE
    # 注册（默认每 5 分钟，开机自启）
    powershell -ExecutionPolicy Bypass -File live\install_task.ps1

    # 只看会注册什么，不实际写入
    powershell -ExecutionPolicy Bypass -File live\install_task.ps1 -DryRun

    # 卸载
    powershell -ExecutionPolicy Bypass -File live\install_task.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [switch]$DryRun,
    [string]$TaskName = "trade-dispatch",
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $PSScriptRoot "dispatch.py"
$LogDir = Join-Path $Root "logs"

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "任务 $TaskName 不存在，无需卸载"
        return
    }
    if ($DryRun) {
        Write-Host "[dry-run] 将卸载任务 $TaskName"
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已卸载任务 $TaskName"
    return
}

# 找 python
$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) {
    throw "找不到 python.exe，请确认已加入 PATH"
}
if (-not (Test-Path $Script)) {
    throw "找不到 $Script"
}

Write-Host "Python : $Python"
Write-Host "脚本   : $Script"
Write-Host "工作目录: $Root"
Write-Host "间隔   : $IntervalMinutes 分钟"
Write-Host "日志   : $LogDir\dispatch.log"
Write-Host ""

if ($DryRun) {
    Write-Host "[dry-run] 将注册任务 $TaskName（每 $IntervalMinutes 分钟）"
    return
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# 日志由 dispatch.py 自己写入 logs/dispatch.log，这里不再用 cmd 包装
$Action = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}" --log' -f $Script) -WorkingDirectory $Root

# 每 5 分钟重复一次，持续 10 年
$StartAt = (Get-Date).Date.AddMinutes(1)
$Trigger = New-ScheduledTaskTrigger -Once -At $StartAt -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "任务已存在，先删除再重建"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "美股收盘前 30 分钟计算信号（每 $IntervalMinutes 分钟唤醒，由 dispatch.py 现算是否到点）" | Out-Null

Write-Host "已注册任务 $TaskName"
Write-Host ""
Write-Host "查看状态：Get-ScheduledTask -TaskName $TaskName | Format-List"
Write-Host "立即测试：Start-ScheduledTask -TaskName $TaskName"
Write-Host "看日志  ：Get-Content `"$LogDir\dispatch.log`" -Tail 20"
Write-Host "卸载    ：powershell -ExecutionPolicy Bypass -File live\install_task.ps1 -Uninstall"
