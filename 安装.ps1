#Requires -Version 5.1
<#
    抖音续火花 · 一键安装脚本（Windows）

    做四件事：
      1. 检查 Python 3.11 或更高版本
      2. 创建虚拟环境，安装依赖和 Chromium
      3. 引导扫码登录、配置要续火花的好友
      4. 注册每天自动运行的 Windows 计划任务

    可以重复运行：已经完成的步骤会自动跳过。
    卸载方法见同目录下的「分享说明.md」。
#>

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { chcp 65001 > $null } catch { }

$Root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName   = 'Douyin Auto Fire'
$VenvDir    = Join-Path $Root '.venv'
$VenvPy     = Join-Path $VenvDir 'Scripts\python.exe'
$ConfigPath = Join-Path $Root 'config.json'
$EnvPath    = Join-Path $Root '.env'

function Say  { param([string]$m) Write-Host $m }
function Ok   { param([string]$m) Write-Host $m -ForegroundColor Green }
function Warn { param([string]$m) Write-Host $m -ForegroundColor Yellow }
function Bad  { param([string]$m) Write-Host $m -ForegroundColor Red }
function Head { param([string]$m) Write-Host ''; Write-Host "==== $m ====" -ForegroundColor Cyan }
function Pause-Any { param([string]$m = '按回车继续...') [void](Read-Host $m) }

# 写不带 BOM 的 UTF-8：config.json / .env 被 Python 以 utf-8 读取，
# 带 BOM 会让 json.loads 失败、让 .env 的第一个键名多出 \ufeff。
function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

Say ''
Say '=============================================='
Say '   抖音续火花 · 一键安装'
Say '=============================================='
Say "安装目录：$Root"
Warn '这个脚本只会动本目录，以及注册一个计划任务；不会修改系统设置。'

# ---------------------------------------------------------------- 1. Python
Head '第 1 步 / 5：检查 Python'

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]

    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($ver in @('3.13', '3.12', '3.11')) {
            try {
                $out = & py "-$ver" -c "import sys;print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $out) { $candidates.Add($out.Trim()) }
            } catch { }
        }
    }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $candidates.Add($cmd.Source) }
    }

    foreach ($path in ($candidates | Select-Object -Unique)) {
        try {
            $ver = & $path -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }
            $parts = $ver.Trim().Split('.')
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) { return $path }
        } catch { }
    }
    return $null
}

$PythonExe = Find-Python
if (-not $PythonExe) {
    Bad '没有找到 Python 3.11 或更高版本。'
    Say ''
    Say '请先安装 Python：'
    Say '    https://www.python.org/downloads/'
    Say ''
    Warn '安装时务必勾选最下方的「Add python.exe to PATH」，否则本脚本找不到它。'
    Say ''
    $answer = Read-Host '现在打开下载页面吗？(Y/n)'
    if ($answer -notmatch '^[Nn]') { Start-Process 'https://www.python.org/downloads/' }
    Bad '请安装完 Python 后重新运行本脚本。'
    Pause-Any
    exit 1
}
$PythonVersion = (& $PythonExe -c "import sys;print('%d.%d.%d' % sys.version_info[:3])").Trim()
Ok "找到 Python $PythonVersion"
Say "  路径：$PythonExe"

# ------------------------------------------------------------ 2. 依赖安装
Head '第 2 步 / 5：安装运行环境（第一次会比较慢）'

if (-not (Test-Path $VenvPy)) {
    Say '创建虚拟环境 .venv ...'
    & $PythonExe -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) {
        Bad '创建虚拟环境失败。'
        Pause-Any
        exit 1
    }
} else {
    Say '虚拟环境已存在，跳过创建'
}

Say '安装 Python 依赖 ...'
& $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VenvPy -m pip install -r (Join-Path $Root 'requirements.txt') --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Bad '依赖安装失败，请把上面的报错发给分享给你的人。'; Pause-Any; exit 1 }

Say '安装 Chromium 浏览器（约 130MB，只需一次）...'
& $VenvPy -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Bad 'Chromium 安装失败。'; Pause-Any; exit 1 }
Ok '运行环境就绪'

# ------------------------------------------------- 3. 检查抖音域名能否解析
Head '第 3 步 / 5：检查网络能否访问抖音'

$dnsState = 'unknown'
try {
    $answers = Resolve-DnsName 'www.douyin.com' -Type A -ErrorAction Stop
    $ip = ($answers | Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress
    if ($ip -eq '0.0.0.0') { $dnsState = 'blocked' }
    elseif ($ip) { $dnsState = 'ok' }
} catch { $dnsState = 'unknown' }

if ($dnsState -eq 'blocked') {
    Bad 'www.douyin.com 被解析成了 0.0.0.0。'
    Say ''
    Say '这说明这台电脑上有软件在屏蔽抖音域名（例如 WebLimit 这类自律/网站限制工具）。'
    Say '本安装包不包含应对该情况的组件，脚本将连不上抖音，一定发送失败。'
    Say ''
    Say '请先关闭或卸载那个屏蔽软件，然后重新运行本脚本。'
    Say ''
    $answer = Read-Host '仍然继续安装吗？(y/N)'
    if ($answer -notmatch '^[Yy]') { Pause-Any; exit 1 }
} elseif ($dnsState -eq 'ok') {
    Ok "域名解析正常（www.douyin.com -> $ip）"
} else {
    Warn '无法确认域名解析状态（可能是系统命令受限），继续安装，稍后用 Dry Run 验证。'
}

# ------------------------------------------------------- 4. 好友与登录
Head '第 4 步 / 5：配置好友并登录'

$needConfig = $true
if (Test-Path $ConfigPath) {
    try {
        $existing = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Say "已配置的好友：$($existing.friends -join '、')"
        $answer = Read-Host '要重新配置吗？(y/N)'
        if ($answer -notmatch '^[Yy]') { $needConfig = $false }
    } catch {
        Warn '已有 config.json 无法解析，将重新生成。'
    }
}

if ($needConfig) {
    Say ''
    Say '请输入要每天续火花的好友昵称。'
    Warn '必须和抖音私信列表里显示的昵称完全一致；'
    Warn '抖音的私信搜索只能搜到已经聊过的会话，从没聊过的人搜不到。'
    Say ''
    $raw = Read-Host '好友昵称（多个用英文逗号分隔）'
    $names = @($raw -split '[,，]' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($names.Count -eq 0) { Bad '至少要输入一个昵称。'; Pause-Any; exit 1 }

    $message = Read-Host '要发送的内容（直接回车用默认：续火花 ✨）'
    if (-not $message) { $message = '续火花 ✨' }

    $config = [ordered]@{
        friends               = $names
        messages              = @([ordered]@{ type = 'text'; value = $message })
        send_interval_seconds = [ordered]@{ min = 3; max = 8 }
        prevent_duplicates    = $false
        target_open_retries   = 1
        target_open_timeout_seconds = 15
    }
    Write-Utf8NoBom $ConfigPath ($config | ConvertTo-Json -Depth 6)
    Ok "已写入 config.json：$($names -join '、')"
}

if (Test-Path (Join-Path $Root 'storage-state.json')) {
    Say ''
    $answer = Read-Host '已存在登录状态，要重新扫码吗？(y/N)'
    $needLogin = ($answer -match '^[Yy]')
} else {
    $needLogin = $true
}

if ($needLogin) {
    Say ''
    Say '接下来会弹出一个浏览器窗口，请用抖音 App 扫码登录。'
    Warn '登录完成后不要关闭窗口，也不要按任何键，脚本会自动检测并保存状态。'
    Say ''
    Pause-Any '准备好了按回车开始...'
    & $VenvPy (Join-Path $Root 'local-login.py')
    if ($LASTEXITCODE -ne 0) {
        Bad '登录没有成功。可以重新运行本脚本再试一次。'
        Pause-Any
        exit 1
    }
    Ok '登录状态已保存'
}

# ------------------------------------------------------------ 5. Dry Run
Head '验证：Dry Run（只检查，不发送任何消息）'

& $VenvPy (Join-Path $Root 'run-daily.py') --dry-run --force
if ($LASTEXITCODE -ne 0) {
    Bad ''
    Bad 'Dry Run 失败，说明登录或好友昵称有问题。'
    Say '常见原因：'
    Say '  · 好友昵称和抖音里显示的不完全一致（含表情符号也要一模一样）'
    Say '  · 这个好友还没聊过，抖音私信搜索里搜不到'
    Say '  · 登录状态失效，需要重新扫码'
    Pause-Any
    exit 1
}
Ok 'Dry Run 通过，登录和好友都正常'

# --------------------------------------------------------- 6. 计划任务
Head '第 5 步 / 5：设置每天自动运行'

$slot = Read-Host '每天几点自动发送？24 小时制 HH:MM，直接回车用 20:30'
if (-not $slot) { $slot = '20:30' }
if ($slot -notmatch '^([01]?\d|2[0-3]):([0-5]\d)$') {
    Warn "时间格式不对（$slot），改用默认 20:30"
    $slot = '20:30'
}
$hour = [int]$slot.Split(':')[0]
$minute = [int]$slot.Split(':')[1]

# 兜底时间 = 主时间 + 2 小时，最晚 23:30
$total = $hour * 60 + $minute + 120
if ($total -gt (23 * 60 + 30)) { $total = 23 * 60 + 30 }
$fallback = '{0:00}:{1:00}' -f [math]::Floor($total / 60), ($total % 60)

# 写入 .env（不带 BOM）。保留已有的通知配置等键。
$envLines = @()
if (Test-Path $EnvPath) {
    # 保留已有的通知配置等键；SLOT_TIME / HEADLESS 由本脚本接管，先剔除避免重复
    $envLines = @(Get-Content $EnvPath -Encoding UTF8 |
        Where-Object { $_ -notmatch '^\s*(SLOT_TIME|HEADLESS)\s*=' })
}
$envLines = @($envLines | Where-Object { $_.Trim() -ne '' })
$envLines += "SLOT_TIME=$slot"
$envLines += 'HEADLESS=false'
Write-Utf8NoBom $EnvPath (($envLines -join "`r`n") + "`r`n")
Ok "已设置发送时间：$slot（兜底 $fallback）"

try {
    $action = New-ScheduledTaskAction -Execute $VenvPy -Argument 'run-daily.py' -WorkingDirectory $Root

    $logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    try { $logon.Delay = 'PT2M' } catch { }

    $primary  = New-ScheduledTaskTrigger -Daily -At $slot
    $backup   = New-ScheduledTaskTrigger -Daily -At $fallback

    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logon, $primary, $backup `
        -Settings $settings -Principal $principal -Force | Out-Null

    Ok "计划任务已注册：$TaskName"
    Say '  触发器：开机登录后 2 分钟 / 每天 ' + $slot + ' / 每天 ' + $fallback
} catch {
    Bad "注册计划任务失败：$($_.Exception.Message)"
    Warn '可以手动创建：任务计划程序 -> 创建基本任务'
    Warn "  程序：$VenvPy"
    Warn '  参数：run-daily.py'
    Warn "  起始于：$Root"
}

# ---------------------------------------------------------------- 完成
Head '安装完成'
Say ''
Say '这个程序会：'
Say "  · 每天 $slot 自动给好友发送一条消息（间隔随机 3~8 秒）"
Say '  · 如果到点电脑关着，下次开机登录后会自动补发'
Say '  · 同一天不会重复发送'
Say ''
Warn '注意：到点时会短暂弹出一个浏览器窗口，这是正常的，发完会自动关闭。'
Say ''
$answer = Read-Host '要现在立即真实发送一次做验证吗？(y/N)'
if ($answer -match '^[Yy]') {
    & $VenvPy (Join-Path $Root 'run-daily.py') --force
    if ($LASTEXITCODE -eq 0) { Ok '发送成功' } else { Bad '发送失败，请查看 artifacts\run.log' }
}
Say ''
Ok '全部完成。日常查看日志：'
Say "  Get-Content '$Root\artifacts\run-daily.log' -Tail 30"
Say "  Get-Content '$Root\artifacts\run.log' -Tail 40"
Pause-Any
