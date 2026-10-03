#Requires -Version 5.1
<#
    生成可以发给朋友的分享包。

    关键约束：包里**绝对不能**出现登录凭证和个人配置。
    最后一步会按「文件名 + JSON 结构 + 会话值特征」三重校验，
    任何一项没通过就直接失败退出，不产出 ZIP。

    注意：本文件必须以「UTF-8 带 BOM」保存。
    Windows PowerShell 5.1 读取无 BOM 的 UTF-8 脚本时会按 GBK 解码，中文注释会导致语法错误。

    用法（本机执行策略禁止直接跑 .ps1，用下面这行）：
      powershell -NoProfile -ExecutionPolicy Bypass -File .\打包分享包.ps1
#>

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# 支持两种摆放位置：
#   A) 本脚本放在项目根目录内（仓库形态）    -> 源目录 = 脚本所在目录
#   B) 本脚本放在项目的上一层目录（开发形态）-> 源目录 = ./douyin-auto-fire
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $Here 'run.py')) {
    $Source = $Here
    $OutDir = Join-Path (Split-Path $Here -Parent) 'dist'
} else {
    $Source = Join-Path $Here 'douyin-auto-fire'
    $OutDir = Join-Path $Here 'dist'
}
$StageName = '抖音续火花'
$Stage     = Join-Path $OutDir $StageName
$ZipPath   = Join-Path $OutDir '抖音续火花-分享版.zip'

if (-not (Test-Path $Source)) { throw "源目录不存在：$Source" }

# ── 绝不进包 ────────────────────────────────────────────────
# 目录名（出现在路径任意一层就整棵排除）
$ExcludeDirs = @(
    '.venv', 'artifacts', '.git', '.github', '__pycache__',
    'storage_state', '.pytest_cache', '.mypy_cache'
)
# 文件名（不含路径）
$ExcludeFiles = @(
    # 登录凭证 / 个人配置
    'storage-state.json', 'storage-state.json.tmp',
    '.env', '.env.account1', '.env.account2', '.env.account3',
    '.env.account4', '.env.account5',
    'config.json', 'accounts.json',
    # 只服务于本机 WebLimit 环境的组件
    'doh_proxy.py', 'test-proxy.py',
    # 含本机信息的说明
    '本机部署说明.md',
    # 打包脚本自己（当源目录就是项目根时会被扫到）
    '打包分享包.ps1',
    # 临时产物
    'run-live-output.txt', 'wrapper-dry.txt', 'wrapper-run.txt', 'wrapper-live.txt'
)
# 扩展名
$ExcludeExt = @('.pyc', '.log')

# ── 准备目录 ────────────────────────────────────────────────
if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

# ── 复制 ────────────────────────────────────────────────────
$copied  = New-Object System.Collections.Generic.List[string]
# HashSet：整个目录被排除时只记一条，否则 .venv 会刷出上万行
$skipped = New-Object System.Collections.Generic.HashSet[string]

Get-ChildItem -Path $Source -Recurse -File -Force | ForEach-Object {
    $relative = $_.FullName.Substring($Source.Length).TrimStart('\')
    $segments = $relative -split '\\'
    $dirSegments = if ($segments.Count -gt 1) { $segments[0..($segments.Count - 2)] } else { @() }

    $blockedDir = $dirSegments | Where-Object { $ExcludeDirs -contains $_ } | Select-Object -First 1
    if ($blockedDir) {
        [void]$skipped.Add("$blockedDir\  (整个目录)")
        return
    }
    if ($ExcludeFiles -contains $_.Name) {
        [void]$skipped.Add("$relative  (文件)")
        return
    }
    if ($ExcludeExt -contains $_.Extension.ToLower()) {
        [void]$skipped.Add("$relative  (扩展名)")
        return
    }

    $destination = Join-Path $Stage $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    $copied.Add($relative)
}

# ── PowerShell 5.1 读中文脚本要求 UTF-8 带 BOM ───────────────
# config.json / .env 相反：必须不带 BOM，否则 Python 侧解析失败。
$BomNeeded = @('安装.ps1')
foreach ($name in $BomNeeded) {
    $target = Join-Path $Stage $name
    if (Test-Path $target) {
        $text = [System.IO.File]::ReadAllText($target, [System.Text.Encoding]::UTF8)
        [System.IO.File]::WriteAllText($target, $text, (New-Object System.Text.UTF8Encoding($true)))
    }
}

# ── 三重安全校验 ────────────────────────────────────────────
$Violations = New-Object System.Collections.Generic.List[string]

# 第 1 重：文件名黑名单
$ForbiddenNames = @('storage-state.json', 'storage-state.json.tmp', '.env', 'config.json', 'doh_proxy.py')
Get-ChildItem -Path $Stage -Recurse -File -Force | ForEach-Object {
    if ($ForbiddenNames -contains $_.Name) { $Violations.Add("敏感文件名：$($_.Name)") }
    if ($_.Name -like '*.tmp') { $Violations.Add("临时文件：$($_.Name)") }
}

# 第 2 重：JSON 结构。真实 Cookie 数组和 Playwright storage state 有固定结构，
# 按结构判定可以避开「文档里的示例文字」和「源码里的变量名」造成的误报。
Get-ChildItem -Path $Stage -Recurse -File -Filter *.json -Force | ForEach-Object {
    $short = $_.FullName.Substring($Stage.Length).TrimStart('\')
    try {
        $parsed = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch { return }

    if ($parsed -is [System.Array]) {
        $first = @($parsed) | Select-Object -First 1
        if ($null -ne $first) {
            $props = @($first.PSObject.Properties.Name)
            if (($props -contains 'domain') -and ($props -contains 'value')) {
                $Violations.Add("疑似 Cookie 数组：$short")
            }
        }
    } else {
        $props = @($parsed.PSObject.Properties.Name)
        if (($props -contains 'cookies') -or ($props -contains 'origins')) {
            $Violations.Add("疑似登录状态文件：$short")
        }
    }
}

# 第 3 重：真实会话值特征（sessionid 后面跟一长串十六进制）
$SessionValuePattern = 'sessionid(_ss)?\s*[:=]\s*"?[0-9a-f]{16,}'
Get-ChildItem -Path $Stage -Recurse -File -Force |
    Where-Object { $_.Length -lt 2MB -and $_.Extension -in @('.json', '.txt', '.py', '.md', '.ps1', '.cmd', '.yml') } |
    ForEach-Object {
        $text = [System.IO.File]::ReadAllText($_.FullName, [System.Text.Encoding]::UTF8)
        if ($text -match $SessionValuePattern) {
            $Violations.Add("疑似真实会话值：$($_.FullName.Substring($Stage.Length).TrimStart('\'))")
        }
    }

# 必须存在的文件
$Required = @('run.py', 'run-daily.py', 'local-login.py', '安装.ps1', '安装.cmd', '分享说明.md',
              'config.example.json', 'requirements.txt', 'LICENSE', 'README.md')
foreach ($name in $Required) {
    if (-not (Test-Path (Join-Path $Stage $name))) { $Violations.Add("缺少必需文件：$name") }
}

if ($Violations.Count -gt 0) {
    Write-Host ''
    Write-Host '打包中止，校验未通过：' -ForegroundColor Red
    $Violations | ForEach-Object { Write-Host "  x $_" -ForegroundColor Red }
    exit 1
}

# ── 压缩 ────────────────────────────────────────────────────
# 不能用 ZipFile::CreateFromDirectory：.NET Framework 版本会拿 '\' 当 ZIP 内的
# 路径分隔符。Windows 资源管理器能忍，但 7-Zip / macOS / Linux 会把
# "抖音续火花\app\main.py" 当成一个文件名，解压出来是一坨怪东西。
# 所以这里手动建条目，显式使用 '/'。
Add-Type -AssemblyName System.IO.Compression
$zipStream = [System.IO.File]::Open($ZipPath, [System.IO.FileMode]::Create)
try {
    $archive = [System.IO.Compression.ZipArchive]::new(
        $zipStream, [System.IO.Compression.ZipArchiveMode]::Create, $false)
    try {
        Get-ChildItem -Path $Stage -Recurse -File -Force | ForEach-Object {
            $relative = $_.FullName.Substring($Stage.Length).TrimStart('\') -replace '\\', '/'
            $entry = $archive.CreateEntry(
                "$StageName/$relative",
                [System.IO.Compression.CompressionLevel]::Optimal)
            $target = $entry.Open()
            try {
                $source = [System.IO.File]::OpenRead($_.FullName)
                try { $source.CopyTo($target) } finally { $source.Dispose() }
            } finally { $target.Dispose() }
        }
    } finally { $archive.Dispose() }
} finally { $zipStream.Dispose() }

# ── 输出 ────────────────────────────────────────────────────
Write-Host ''
Write-Host '==============================================' -ForegroundColor Cyan
Write-Host ' 打包完成' -ForegroundColor Cyan
Write-Host '==============================================' -ForegroundColor Cyan
Write-Host (" ZIP  ： {0}" -f $ZipPath)
Write-Host (" 大小 ： {0:N2} MB" -f ((Get-Item $ZipPath).Length / 1MB))
Write-Host (" 文件 ： {0} 个" -f $copied.Count)
Write-Host ''
Write-Host '包内文件：' -ForegroundColor Cyan
$copied | Sort-Object | ForEach-Object { Write-Host "   $_" }
Write-Host ''
Write-Host '已排除（不进包）：' -ForegroundColor Yellow
$skipped | Sort-Object | ForEach-Object { Write-Host "   $_" }
