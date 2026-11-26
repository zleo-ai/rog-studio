# 计划任务 RyuoStudio-<名字> 实际跑的东西。PowerShell 7。
# 计划任务用 conhost --headless 拉起它，不弹黑窗口；它再用 uv 的环境跑 tools/run_service.py。
#
#   pwsh -NoProfile -File desktop\windows\Run-RyuoService.ps1 -Name pusher
#   pwsh -NoProfile -File desktop\windows\Run-RyuoService.ps1 -Name pusher -FailureHook C:\path\alert.ps1
#
# 日志：%USERPROFILE%\.local\share\ryuo-studio\logs\<名字>-YYYY-MM-DD.log（这一层的启动、退出码）；
#       程序自己的输出在同目录上一级的 pusher.log / app.log。
# 退出码：0 正常停下；3 连续 5 次一分钟内就退出、已放弃重启；2 这层自己出错；其他照 Python 的退出码。
# 非 0 时若给了 -FailureHook，就调用一次（管理器让停的不算，它会先留下 <名字>.stop 记号）：& $FailureHook -Source ryuo-studio-<名字> -Reason <一句中文> -LogPath <日志>。
# 是否真的连屏幕只看 ~/.config/ryuo-studio/config.json 的 device_enabled，这里不改它。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('pusher', 'manager')]
    [string]$Name,
    # 失败时调用的脚本（可选）。
    [string]$FailureHook = '',
    # 调失败脚本时带上 -DryRun（只演练，不真的发提醒）。测试用。
    [switch]$HookDryRun,
    # adb.exe 的完整路径（可选）；不给就看环境变量 RYUO_ADB，再看 PATH。
    [string]$AdbPath = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Out = Join-Path $env:USERPROFILE '.local\share\ryuo-studio'
$LogDir = Join-Path $Out 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogPath = Join-Path $LogDir ('{0}-{1}.log' -f $Name, (Get-Date -Format 'yyyy-MM-dd'))
$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Write-Log([string]$Text) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    [IO.File]::AppendAllText($LogPath, $line + "`r`n", $utf8)
}

function Send-Failure([string]$Reason) {
    if (-not $FailureHook) { return }
    if (-not (Test-Path -LiteralPath $FailureHook)) { Write-Log "failure hook not found: $FailureHook"; return }
    $hookArgs = @{ Source = "ryuo-studio-$Name"; Reason = $Reason; LogPath = $LogPath }
    if ($HookDryRun) { $hookArgs.DryRun = $true }
    try { & $FailureHook @hookArgs } catch { Write-Log ('failure hook error: ' + $_.Exception.Message) }
}

$Label = @{ pusher = '龙屏推送'; manager = '龙屏管理器' }[$Name]
$exitCode = 0
try {
    $uv = (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if (-not $uv) { $uv = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe' }
    if (-not (Test-Path -LiteralPath $uv)) { throw "uv not found: $uv" }
    if (-not (Test-Path -LiteralPath (Join-Path $Root '.venv'))) { throw "no .venv under $Root; run desktop\install_windows.ps1 first" }
    if ($AdbPath) {
        if (-not (Test-Path -LiteralPath $AdbPath)) { throw "adb not found: $AdbPath" }
        $env:RYUO_ADB = $AdbPath
    }
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONDONTWRITEBYTECODE = '1'

    $live = $false
    $conf = Join-Path $env:USERPROFILE '.config\ryuo-studio\config.json'
    if (Test-Path -LiteralPath $conf) {
        try { $live = [bool]((Get-Content -LiteralPath $conf -Raw -Encoding utf8 | ConvertFrom-Json).device_enabled) } catch { $live = $false }
    }
    Write-Log ('start {0} root={1} device_enabled={2} adb={3}' -f $Name, $Root, $live, $(if ($env:RYUO_ADB) { $env:RYUO_ADB } else { '(PATH)' }))

    # 上次「有人让停」留下的记号，开跑前清掉
    $StopMark = Join-Path $Out "$Name.stop"
    Remove-Item -LiteralPath $StopMark -ErrorAction SilentlyContinue

    # --no-sync：任务启动时绝不改环境、不联网；环境由安装脚本按 uv.lock 建好
    $pyArgs = @('run', '--project', $Root, '--frozen', '--no-sync', 'python', '-X', 'utf8', '-I',
        (Join-Path $Root 'tools\run_service.py'), $Name)
    $ErrorActionPreference = 'Continue'
    & $uv @pyArgs 2>&1 | ForEach-Object { Write-Log ('  ' + [string]$_) }
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    Write-Log "service exit=$exitCode"

    $asked = $false
    if (Test-Path -LiteralPath $StopMark) {
        $asked = ((Get-Date) - (Get-Item -LiteralPath $StopMark).LastWriteTime).TotalMinutes -lt 5
        Remove-Item -LiteralPath $StopMark -ErrorAction SilentlyContinue
    }
    if ($asked) {
        Write-Log 'stopped on request (manager or hostsys.service); no alert'
        $exitCode = 0
    } elseif ($exitCode -eq 3) {
        Send-Failure ("${Label}连续 5 次一分钟内就退出，已经停下不再重启。需要有人看一下：" + $LogPath)
    } elseif ($exitCode -ne 0) {
        Send-Failure ("${Label}意外退出了（退出码 $exitCode）。需要有人看一下：" + $LogPath)
    }
} catch {
    Write-Log ('wrapper error: ' + $_.Exception.Message)
    Send-Failure ("${Label}的计划任务自己出错了。需要有人看一下：" + $LogPath)
    $exitCode = 2
}
Write-Log "done exit=$exitCode"
exit $exitCode
