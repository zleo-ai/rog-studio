# 在 Windows 上装龙屏。PowerShell 7，不需要管理员权限。
#
# 默认只做准备、不注册任何东西：按 uv.lock 建好 .venv，把两个计划任务（推送、管理器）的定义写成 XML，
# 放在 %USERPROFILE%\.local\share\ryuo-studio\tasks\，打印出来给人看。加 -Register 才真正注册。
#
#   pwsh -NoProfile -File desktop\install_windows.ps1                          只准备，生成任务定义
#   pwsh -NoProfile -File desktop\install_windows.ps1 -Register                注册任务和开始菜单「龙屏」，登录后自动开
#   pwsh -NoProfile -File desktop\install_windows.ps1 -Register -NoAuto        注册，但不设登录启动，点图标才开
#   pwsh -NoProfile -File desktop\install_windows.ps1 -Register -Services pusher   只注册推送
#   pwsh -NoProfile -File desktop\install_windows.ps1 -Uninstall               删掉任务和快捷方式（配置、日志、视频留着）
#
# 其他参数：
#   -TaskPath '\RyuoStudio\'   任务放在哪个文件夹，默认根目录 '\'
#   -AdbPath  C:\...\adb.exe    adb 不在 PATH 上时指过去
#   -FailureHook C:\...\x.ps1   任务非正常退出时调用它：-Source ryuo-studio-<名字> -Reason <一句话> -LogPath <日志>
#   -PwshPath C:\...\pwsh.exe   任务用哪个 pwsh.exe，默认是正在跑本脚本的这个。商店版 PowerShell 的真实路径带版本号，
#                               商店一升级旧目录就没了、任务起不来；有固定路径的安装（MSI 或 zip）时指过去
#
# 装好以后屏幕照样是演练模式：真正上屏只看 ~/.config/ryuo-studio/config.json 里的 device_enabled，本脚本不改它。
[CmdletBinding()]
param(
    [switch]$Register,
    [switch]$NoAuto,
    [switch]$Uninstall,
    [ValidateSet('pusher', 'manager')]
    [string[]]$Services = @('pusher', 'manager'),
    [string]$TaskPath = '\',
    [string]$AdbPath = '',
    [string]$FailureHook = '',
    [string]$PwshPath = '',
    [switch]$SkipSync
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $env:USERPROFILE '.local\share\ryuo-studio'
$TaskDir = Join-Path $Out 'tasks'
$TasksJson = Join-Path $Out 'windows-tasks.json'
$Runner = Join-Path $Root 'desktop\windows\Run-RyuoService.ps1'
$Programs = [Environment]::GetFolderPath('Programs')
$Lnk = if ($Programs) { Join-Path $Programs '龙屏.lnk' } else { '' }
$utf8 = New-Object System.Text.UTF8Encoding $false

if (-not ($TaskPath.StartsWith('\') -and $TaskPath.EndsWith('\'))) { throw "TaskPath 要以 \ 开头和结尾，比如 '\' 或 '\RyuoStudio\'。" }

# 没明说 -TaskPath 时，按上次注册时记下的文件夹来（卸载、重装都对得上）
if (-not $PSBoundParameters.ContainsKey('TaskPath') -and (Test-Path -LiteralPath $TasksJson)) {
    try {
        $saved = (Get-Content -LiteralPath $TasksJson -Raw -Encoding utf8 | ConvertFrom-Json).task_path
        if ($saved -is [string] -and $saved.StartsWith('\') -and $saved.EndsWith('\')) { $TaskPath = $saved }
    } catch {}
}

if ($Uninstall) {
    foreach ($n in @('pusher', 'manager')) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName "RyuoStudio-$n" -Confirm:$false -ErrorAction SilentlyContinue
    }
    if ($Lnk) { Remove-Item -LiteralPath $Lnk -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $TasksJson -ErrorAction SilentlyContinue
    Write-Host "已删掉 $TaskPath 下的计划任务和快捷方式。"
    exit 0
}

# 计划任务要钉到真实的 pwsh.exe（商店版的执行别名会让计划任务找不到它）
$Pwsh = if ($PwshPath) { $PwshPath } else { (Get-Process -Id $PID).Path }
if ([IO.Path]::GetFileName($Pwsh) -ne 'pwsh.exe' -or -not (Test-Path -LiteralPath $Pwsh)) {
    throw "要 PowerShell 7 的 pwsh.exe：$Pwsh（用 pwsh 运行本脚本，或用 -PwshPath 指过去）。"
}
if ($Pwsh -like '*\WindowsApps\*') {
    Write-Warning "任务会钉在商店版 PowerShell 带版本号的路径上：$Pwsh。商店升级 PowerShell 后这个目录会被删掉，任务就起不来了；有固定路径的安装时用 -PwshPath 指过去。"
}

$uv = (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $uv) { throw '没找到 uv：先装 uv（https://docs.astral.sh/uv/），Python 由它管。' }

if (-not $SkipSync) {
    # 按 uv.lock 装，锁文件对不上就报错，不偷偷升级
    & $uv sync --project $Root --frozen
    if ($LASTEXITCODE -ne 0) { throw 'uv sync 没成功，看看网络或 uv 配置。' }
}
$Pyw = Join-Path $Root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $Pyw)) { throw "没找到 $Pyw；先去掉 -SkipSync 跑一次。" }

# 依赖检查：缺了只提醒，不替你下载
if ($AdbPath) {
    if (-not (Test-Path -LiteralPath $AdbPath)) { throw "没找到 adb：$AdbPath" }
} elseif (-not (Get-Command adb -ErrorAction SilentlyContinue) -and -not $env:RYUO_ADB) {
    Write-Warning '没找到 adb：去 developer.android.com 下载 Android SDK Platform-Tools，解压后用 -AdbPath 指到 adb.exe。'
}
if ($FailureHook -and -not (Test-Path -LiteralPath $FailureHook)) { throw "没找到失败提醒脚本：$FailureHook" }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Write-Warning '没找到 ffmpeg：只有「生成视频」要用，别的不受影响。' }
try { Invoke-WebRequest -UseBasicParsing -NoProxy -TimeoutSec 2 'http://127.0.0.1:8085/data.json' | Out-Null }
catch { Write-Warning '读不到 CPU 温度：装 LibreHardwareMonitor，在 Options 里打开 Remote Web Server（端口 8085）。不装的话温度显示破折号。' }

$conf = Join-Path $env:USERPROFILE '.config\ryuo-studio\config.json'
$live = $false
if (Test-Path -LiteralPath $conf) {
    try { $live = [bool]((Get-Content -LiteralPath $conf -Raw -Encoding utf8 | ConvertFrom-Json).device_enabled) } catch {}
}

function Protect-XmlText([string]$Text) { return [Security.SecurityElement]::Escape($Text) }

function New-TaskXml([string]$Name) {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $runnerArgs = '-Name {0}' -f $Name
    if ($AdbPath) { $runnerArgs += ' -AdbPath "{0}"' -f $AdbPath }
    if ($FailureHook) { $runnerArgs += ' -FailureHook "{0}"' -f $FailureHook }
    $arguments = '--headless "{0}" -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{1}" {2}' -f $Pwsh, $Runner, $runnerArgs
    $trigger = if ($NoAuto) { '' } else {
        "    <LogonTrigger>`r`n      <Enabled>true</Enabled>`r`n      <UserId>$(Protect-XmlText $user)</UserId>`r`n    </LogonTrigger>`r`n"
    }
    $label = @{ pusher = '推送'; manager = '管理器' }[$Name]
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$(Protect-XmlText "龙屏$label（ryuo-studio）。conhost 无窗口跑 PowerShell 7，再用 uv 的环境跑 tools\run_service.py $Name。日志在 %USERPROFILE%\.local\share\ryuo-studio\logs\。是否连屏幕只看 config.json 的 device_enabled。")</Description>
    <URI>$(Protect-XmlText "$TaskPath" )RyuoStudio-$Name</URI>
  </RegistrationInfo>
  <Triggers>
$trigger  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(Protect-XmlText $user)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(Protect-XmlText "$env:SystemRoot\System32\conhost.exe")</Command>
      <Arguments>$(Protect-XmlText $arguments)</Arguments>
      <WorkingDirectory>$(Protect-XmlText $Root)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

New-Item -ItemType Directory -Force -Path $TaskDir | Out-Null
$written = @()
foreach ($n in $Services) {
    $xml = New-TaskXml $n
    [xml]$xml | Out-Null  # 先确认是合法 XML
    $file = Join-Path $TaskDir "RyuoStudio-$n.xml"
    [IO.File]::WriteAllText($file, $xml, [Text.Encoding]::Unicode)
    $written += $file
}
Write-Host "任务定义已写好（还没注册）："
$written | ForEach-Object { Write-Host "  $_" }
Write-Host ("屏幕模式：{0}" -f $(if ($live) { 'device_enabled=true，任务一跑就会连屏幕' } else { '演练（device_enabled 没打开，不连屏幕）' }))
if ($live) { Write-Warning 'config.json 里 device_enabled 已经是 true：注册并启动后推送程序会直接连屏幕。' }

if (-not $Register) {
    Write-Host '要注册时加 -Register；也可以手动：Register-ScheduledTask -TaskPath <文件夹> -TaskName <名字> -Xml (Get-Content <xml> -Raw)'
    exit 0
}

foreach ($n in $Services) {
    $xml = [IO.File]::ReadAllText((Join-Path $TaskDir "RyuoStudio-$n.xml"), [Text.Encoding]::Unicode)
    Register-ScheduledTask -TaskPath $TaskPath -TaskName "RyuoStudio-$n" -Xml $xml -Force | Out-Null
}
# 管理器和开始菜单入口按这份文件找任务
[IO.File]::WriteAllText($TasksJson, (@{ task_path = $TaskPath } | ConvertTo-Json -Compress), $utf8)

# 开始菜单快捷方式：点它确保服务在跑，再开独立窗口
if (-not $Lnk) { throw '找不到开始菜单的程序目录，快捷方式没建。任务已经注册好了。' }
$sh = (New-Object -ComObject WScript.Shell).CreateShortcut($Lnk)
$sh.TargetPath = $Pyw
$sh.Arguments = "-X utf8 -I ""$Root\tools\launch.py"""
$sh.WorkingDirectory = $Root
$sh.IconLocation = "$Root\desktop\ryuo-studio.ico"
$sh.Description = '龙屏管理器'
$sh.Save()

if (-not $NoAuto) { foreach ($n in $Services) { Start-ScheduledTask -TaskPath $TaskPath -TaskName "RyuoStudio-$n" } }
Write-Host '装好了：开始菜单里搜「龙屏」，右键可固定到任务栏。'
