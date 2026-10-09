# ============================================================
# Seer中控台Pro 一键启动脚本（自动请求管理员权限）
#
# 用法：双击「启动Seer中控台.bat」即可；也可右键本脚本→使用 PowerShell 运行。
# 说明：
#   - 游戏客户端 requireAdministrator，中控台需以管理员身份启动才能拉起客户端。
#   - 本脚本自动检测权限；若未提权则弹出 UAC 窗口，批准后以管理员身份重新运行。
#   - 启动前用 python.exe 做一次依赖预检，失败会弹窗提示并写入 startup-error.log，
#     避免用 pythonw（无控制台）启动时失败被静默吞掉。
#   - 本机踩坑：GBK 代码页下 Python 读 UTF-8 文件会报错，故设置 PYTHONUTF8=1。
# ============================================================

$ErrorActionPreference = 'Stop'

# 脚本所在目录 = 项目根目录
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---- 判断当前是否已拥有管理员权限 ----
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdmin   = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host '正在请求管理员权限，请在弹出的 UAC 窗口中点击「是」…' -ForegroundColor Yellow

    # 以管理员身份重新运行本脚本（路径由本机重新解析，避免提权后丢失工作目录）
    $ps1 = Join-Path $ProjectDir 'start-seer-console.ps1'
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$ps1`"") `
        -WorkingDirectory $ProjectDir `
        -Verb RunAs
    exit
}

# ---- 已是管理员：进入项目目录 ----
Set-Location $ProjectDir
$env:PYTHONUTF8 = '1'   # 避免 GBK 下 Python 读 UTF-8/含中文文件报 UnicodeDecodeError

# ---- 依赖预检（用 python.exe 跑一次完整 import 链） ----
$pyConsole = (Get-Command python.exe -ErrorAction Stop).Source
$probe = "import PySide6, psutil, win32api; import seer_console.main"
$errText = (& $pyConsole -c $probe 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    Write-Host '依赖检查失败，请先安装缺失依赖：' -ForegroundColor Red
    Write-Host $errText -ForegroundColor Red
    $log = Join-Path $ProjectDir 'startup-error.log'
    $errText | Out-File -FilePath $log -Encoding UTF8
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            "依赖检查失败：`r`n$errText`r`n`r`n详情已写入：$log",
            'Seer中控台Pro — 启动失败', 'OK', 'Error') | Out-Null
    } catch { }
    exit 1
}

# ---- 启动中控台（优先 pythonw，无控制台黑窗） ----
$py = Join-Path (Split-Path -Parent $pyConsole) 'pythonw.exe'
if (-not (Test-Path $py)) { $py = $pyConsole }

Start-Process -FilePath $py `
    -ArgumentList @('seer_console\main.py') `
    -WorkingDirectory $ProjectDir

Write-Host 'Seer中控台Pro 已启动（管理员身份）。可关闭本窗口，程序在后台运行。' -ForegroundColor Green
