# Azure-Go 本地一键测试（开发用）
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File dev_local.ps1
#       启动本地 LLM(8080) / TTS(9880) / KataGo 服务(8100) / 后端(8000)，然后运行游戏
#   powershell ... -File dev_local.ps1 -NoGame
#       只起服务，不启动游戏（用于确认各模型是否接上）
#   powershell ... -File dev_local.ps1 -Tests
#       打开一个控制台跑 run_test_env.bat tests（后端 + KataGo + 前端 headless）
#   powershell ... -File dev_local.ps1 -Stop
#       停止本脚本启动的服务与游戏
#
# 说明：本脚本属于开发便利工具，不进入游戏运行时（游戏本身仍然只连后端）。

param(
    [switch]$NoGame,
    [switch]$Tests,
    [switch]$Stop,
    [switch]$QualityTts
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root = $PSScriptRoot

# —— 本机模型 / 引擎路径（换机器时改这里，或用同名环境变量覆盖）——
$KataGoExe   = if ($env:KATAGO_EXE)   { $env:KATAGO_EXE }   else { "D:\KataGo\bin\katago\katago.exe" }
$KataGoModel = if ($env:KATAGO_MODEL) { $env:KATAGO_MODEL } else { "D:\KataGo\models\model-b18c384nbt.bin.gz" }
$KataGoCfg   = if ($env:KATAGO_CONFIG){ $env:KATAGO_CONFIG }else { "D:\KataGo\bin\katago\analysis_example.cfg" }
$GodotExe    = if ($env:GODOT_EXE)    { $env:GODOT_EXE }    else { "d:\Godot\Godot_v4.6.3-stable_win64.exe" }
$LlmStarter  = Join-Path $Root "tools\start_llm_server.py"   # 自用版内置，不再依赖 AzureGo-legacy 目录
$TtsDir      = "D:\KataGo\cosyvoice"
$TtsPort     = 9880
$LlmPort     = 8080
$KataPort    = 8100
$BackendPort = 8000

$LogDir = Join-Path $Root "backend\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$PidFile = Join-Path $LogDir "dev_local_pids.json"

# 本地语音目录：环境变量 > D:\KataGo\cosyvoice > 仓库内 cosyvoice > 仓库 server。
# 判定标准与经典版一致：必须同时有 .venv\Scripts\python.exe 与 azure_tts_server.py，
# 只有源码、没有运行环境的目录不能算「可用」。
$TtsCandidates = @()
if ($env:AZURE_TTS_DIR) { $TtsCandidates += $env:AZURE_TTS_DIR }
$TtsCandidates += @(
    "D:\KataGo\cosyvoice",
    (Join-Path $Root "cosyvoice"),
    (Join-Path $Root "server"),
    (Join-Path (Split-Path $Root -Parent) "cosyvoice")
)
foreach ($d in $TtsCandidates) {
    if ($d -and (Test-Path (Join-Path $d ".venv\Scripts\python.exe")) -and
        (Test-Path (Join-Path $d "azure_tts_server.py"))) {
        $TtsDir = $d
        break
    }
}

function Say([string]$s) { Write-Host $s }

function Test-Url([string]$url, [string]$pattern = "", [int]$timeout = 2) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec $timeout -Uri $url
        if ($r.StatusCode -ne 200) { return $false }
        if ($pattern -ne "") { return ($r.Content -match $pattern) }
        return $true
    } catch { return $false }
}

function Wait-Url([string]$url, [string]$pattern, [int]$seconds, [string]$label) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Url $url $pattern 3) { Say ("      " + $label + " 就绪"); return $true }
        Start-Sleep -Seconds 2
    }
    Say ("      [超时] " + $label + " 未就绪：" + $url)
    return $false
}

function Start-Hidden([string]$exe, [string[]]$argv, [string]$wd, [string]$outLog, [string]$errLog) {
    $p = Start-Process -FilePath $exe -ArgumentList $argv -WorkingDirectory $wd -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    return $p
}

# 预热语音模型：快引擎首次合成 ~0.5s，质量引擎（CosyVoice）首次 15~25 秒。
function Warm-Tts([string]$voice) {
    $body = '{"model":"cosyvoice3","input":"你好。","voice":"' + $voice + '","speed":1.0,"response_format":"wav"}'
    $f = Join-Path $LogDir "tts_warm.json"
    [IO.File]::WriteAllText($f, $body, (New-Object Text.UTF8Encoding($false)))
    Start-Process -FilePath "curl.exe" -WindowStyle Hidden `
        -ArgumentList @("-sS", "-X", "POST", "http://127.0.0.1:$TtsPort/v1/audio/speech",
                        "-H", "Content-Type: application/json", "--data-binary", "@$f", "-o", "NUL") | Out-Null
}

# —— 本地语音服务：参考经典版 v2.0.1 的拉起 / 接入逻辑 ——
# 关键点：模型加载期端口还没监听，光看 /health 会误判成「没在跑」而重复拉起；
# 经典版用 .tts_server.lock 里的 pid 判断「已在启动」，这里照搬。
function Get-TtsHealthText {
    try {
        return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$TtsPort/health").Content
    } catch { return "" }
}

function Test-TtsReady {
    return ((Get-TtsHealthText) -ne "")
}

function Test-TtsStarting {
    $lock = Join-Path $TtsDir ".tts_server.lock"
    if (-not (Test-Path $lock)) { return $false }
    try {
        $text = (Get-Content $lock -Raw).Trim()
        $procId = 0
        if (-not [int]::TryParse($text, [ref]$procId)) { return $false }
        if ($procId -le 0) { return $false }
        return [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue)
    } catch { return $false }
}

function Stop-TtsServer {
    $c = Get-NetTCPConnection -LocalPort $TtsPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { try { Stop-Process -Id $c.OwningProcess -Force } catch { } }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'azure_tts_server' } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch { } }
    Start-Sleep -Seconds 2
    Remove-Item (Join-Path $TtsDir ".tts_server.lock") -Force -ErrorAction SilentlyContinue
}

function Start-TtsServer {
    $py = Join-Path $TtsDir ".venv\Scripts\python.exe"
    $script = Join-Path $TtsDir "azure_tts_server.py"
    $vbs = Join-Path $TtsDir "run_hidden.vbs"
    $bat = if ($QualityTts) { "start_tts_server.bat" } else { "start_tts_server_fast.bat" }
    $batPath = Join-Path $TtsDir $bat
    if (-not (Test-Path $py) -or -not (Test-Path $script) -or -not (Test-Path $vbs) -or
        -not (Test-Path $batPath)) {
        Say ("      [跳过] 本地语音环境不完整：" + $TtsDir)
        return $false
    }
    # 走经典版同一套 VBS（start_tts_server*.bat 内部 start "" wscript run_hidden.vbs）。
    # 不在 PowerShell 里直接拼 wscript 参数：start 的「空标题」引号在 PS 5.1 下很容易被
    # Start-Process 的 ArgumentList 重写；批处理已经把这层处理好了。
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$batPath`"") `
        -WorkingDirectory $TtsDir -WindowStyle Hidden | Out-Null
    return $true
}

# ---------------------------------------------------------------- Stop
if ($Stop) {
    Say "停止本次一键测试启动的进程…"
    if (Test-Path $PidFile) {
        try {
            $pids = Get-Content $PidFile -Raw | ConvertFrom-Json
            foreach ($p in @($pids)) {
                try {
                    $proc = Get-Process -Id ([int]$p) -ErrorAction Stop
                    Write-Host ("  停止 " + $proc.Id + "  " + $proc.ProcessName)
                    Stop-Process -Id $proc.Id -Force
                } catch { }
            }
        } catch { }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    # 本地语音服务由 VBS 拉起，不在 $started 里；按端口 / 命令行收掉，避免留下孤儿进程。
    $ttsConn = Get-NetTCPConnection -LocalPort $TtsPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ttsConn) {
        Stop-TtsServer
        Say "  停止本地语音服务"
    }
    # 兜底：清掉本仓库启动的 KataGo 服务 / 后端 / katago 引擎
    Get-CimInstance Win32_Process |
        Where-Object { ($_.CommandLine -match 'katago_service\.server') -or ($_.CommandLine -match '-m app\.main') } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch { } }
    Get-Process katago -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.Id -Force } catch { } }
    Say "完成。"
    exit 0
}

# ---------------------------------------------------------------- Tests
if ($Tests) {
    if (-not (Test-Path $KataGoExe))   { Say "[错误] 找不到 KataGo 引擎：$KataGoExe"; exit 1 }
    if (-not (Test-Path $KataGoModel)) { Say "[错误] 找不到 KataGo 权重：$KataGoModel"; exit 1 }
    if (-not (Test-Path $KataGoCfg))   { Say "[错误] 找不到 analysis 配置：$KataGoCfg"; exit 1 }
    if (-not (Test-Path $GodotExe))    { Say "[错误] 找不到 Godot：$GodotExe"; exit 1 }
    $env:KATAGO_EXE = $KataGoExe
    $env:KATAGO_MODEL = $KataGoModel
    $env:KATAGO_CONFIG = $KataGoCfg
    $env:GODOT_EXE = $GodotExe
    Say "打开控制台运行全量测试（后端 + KataGo + 前端 headless）…"
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/k", "`"$Root\run_test_env.bat`" tests") -WorkingDirectory $Root
    exit 0
}

# ---------------------------------------------------------------- Start
if (-not (Test-Path $KataGoExe))   { Say "[错误] 找不到 KataGo 引擎：$KataGoExe"; exit 1 }
if (-not (Test-Path $KataGoModel)) { Say "[错误] 找不到 KataGo 权重：$KataGoModel"; exit 1 }
if (-not (Test-Path $KataGoCfg))   { Say "[错误] 找不到 analysis 配置：$KataGoCfg"; exit 1 }

$started = @()

Say "============================================================"
Say " Azure-Go 本地一键测试"
Say "============================================================"

# [1/4] 本地 LLM（llama-server，端口 8080）
if (Test-Url "http://127.0.0.1:$LlmPort/v1/models") {
    Say "[1/4] 本地 LLM 已在运行：http://127.0.0.1:$LlmPort/v1"
} elseif (Test-Path $LlmStarter) {
    Say "[1/4] 启动本地 LLM（首次加载约 15~30 秒，不阻塞开局）…"
    $p = Start-Hidden "python" @($LlmStarter, "--port", "$LlmPort", "--threads", "16") $Root `
        (Join-Path $LogDir "llm.out.log") (Join-Path $LogDir "llm.err.log")
    $started += $p.Id
} else {
    Say "[1/4] [跳过] 找不到启动器：$LlmStarter（聊天/点评会优雅降级）"
}

# [2/4] 本地语音（默认快引擎 MeloTTS；-QualityTts 用 CosyVoice）
# 质量引擎每句 15~25 秒（后端同步等合成，一回合要 1~2 分钟）；快引擎每句 ~0.4 秒。
# 参考经典版：先看 /health；没监听时再看 .tts_server.lock 是否已有实例在加载，
# 避免「模型还在加载 → 健康检查失败 → 又拉起一个」的重复启动（两个实例抢 CPU 会让启动一直失败）。
$TtsVoice = if ($QualityTts) { "azure" } else { "xiaoya" }
$TtsReady = $false
$ttsHealth = Get-TtsHealthText
if ($ttsHealth -ne "") {
    $runningQuality = ($ttsHealth -match '"model_dir"\s*:\s*"[^"]+"')
    if ($runningQuality -eq [bool]$QualityTts) {
        $TtsReady = $true
        Say ("[2/4] 本地语音已在运行（" + $(if ($QualityTts) { "质量引擎" } else { "快引擎" }) + "）：http://127.0.0.1:$TtsPort")
    } else {
        Say "[2/4] 切换语音引擎：停止现有服务…"
        Stop-TtsServer
    }
}
if (-not $TtsReady) {
    $ttsCanWait = $true
    if (Test-TtsStarting) {
        Say "[2/4] 已有语音服务在启动（模型加载中），等待它就绪，不重复拉起…"
    } else {
        Say ("[2/4] 启动本地语音（" + $(if ($QualityTts) { "质量引擎 CosyVoice" } else { "快引擎 MeloTTS" }) + "，后台隐藏窗口）…")
        if (-not (Start-TtsServer)) {
            $ttsCanWait = $false
            Say "      （本地语音环境不完整，跳过；游戏仍可用系统语音）"
        }
    }
    if ($ttsCanWait) {
        # 经典版等 160 次 × 1.5 秒 ≈ 240 秒；质量引擎给到 300 秒。
        $ttsWait = 240
        if ($QualityTts) { $ttsWait = 300 }
        $TtsReady = Wait-Url "http://127.0.0.1:$TtsPort/health" "" $ttsWait "本地语音服务"
    }
    if (-not $TtsReady) {
        Say "      （本地语音未就绪：保持原朗读方式，不把配置伪装成已切到本地）"
    }
}

# 只在语音服务真就绪时写配置，避免「脚本说切了本地、后端却连不上」的假成功。
$cfgPath = Join-Path $Root "game\config.json"
$ttsConfigChanged = $false
$oldMode = ""
if (Test-Path $cfgPath) {
    $cfgText = [IO.File]::ReadAllText($cfgPath, [Text.Encoding]::UTF8)
    if ($cfgText -match '"tts_mode"\s*:\s*"([^"]*)"') { $oldMode = $Matches[1] }
    if ($TtsReady) {
        $localUrl = "http://127.0.0.1:$TtsPort/v1"
        $oldUrl = ""
        $oldVoice = ""
        if ($cfgText -match '"tts_local_base_url"\s*:\s*"([^"]*)"') { $oldUrl = $Matches[1] }
        if ($cfgText -match '"tts_local_voice"\s*:\s*"([^"]*)"') { $oldVoice = $Matches[1] }
        $cfgText = [regex]::Replace($cfgText, '"tts_local_base_url"\s*:\s*"[^"]*"',
                                    ('"tts_local_base_url": "' + $localUrl + '"'))
        $cfgText = [regex]::Replace($cfgText, '"tts_local_voice"\s*:\s*"[^"]*"',
                                    ('"tts_local_voice": "' + $TtsVoice + '"'))
        $cfgText = [regex]::Replace($cfgText, '"tts_mode"\s*:\s*"[^"]*"',
                                    '"tts_mode": "local"')
        $ttsConfigChanged = ($oldMode -ne "local") -or ($oldUrl -ne $localUrl) -or ($oldVoice -ne $TtsVoice)
        [IO.File]::WriteAllText($cfgPath, $cfgText, (New-Object Text.UTF8Encoding($false)))
        Say "[2/4] 已写入本地语音配置（tts_mode=local、voice=$TtsVoice）"
        if ($ttsConfigChanged) {
            Say "      （后端在运行时会重启以生效）"
        } else {
            Say "      （配置未变化，后端无需重启）"
        }
    } else {
        Say ("[2/4] 保持原朗读方式（tts_mode=" + $oldMode + "），不改 config")
    }
}
if ($TtsReady) { Warm-Tts $TtsVoice }

# [3/4] KataGo 云服务（8100）
if (Test-Url "http://127.0.0.1:$KataPort/health" "ready") {
    Say "[3/4] KataGo 服务已就绪：http://127.0.0.1:$KataPort"
} else {
    Say "[3/4] 启动 KataGo 服务（OpenCL 预热需数十秒）…"
    $argv = @("-m", "katago_service.server", "--port", "$KataPort",
              "--katago-exe", $KataGoExe, "--model", $KataGoModel, "--config", $KataGoCfg,
              "--analysis-threads", "1")
    $p = Start-Hidden "python" $argv $Root `
        (Join-Path $LogDir "katago_service.out.log") (Join-Path $LogDir "katago_service.err.log")
    $started += $p.Id
    if (-not (Wait-Url "http://127.0.0.1:$KataPort/health" "ready" 240 "KataGo 服务")) {
        Say "      （继续尝试启动后端；后端会等待它就绪）"
    }
}

# [4/4] 后端（8000）
# 后端只在启动时读 config.json；本地语音配置变了就必须重启后端，否则 tts_mode 还是旧的。
$backendRunning = Test-Url "http://127.0.0.1:$BackendPort/api/health"
if ($backendRunning -and $ttsConfigChanged) {
    Say "[4/4] 本地语音配置已变化：重启后端以生效…"
    $bc = Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($bc) { try { Stop-Process -Id $bc.OwningProcess -Force } catch { } }
    Start-Sleep -Seconds 2
    $backendRunning = $false
}
if ($backendRunning) {
    Say "[4/4] 后端已在运行：http://127.0.0.1:$BackendPort"
} else {
    Say "[4/4] 启动后端（127.0.0.1:8000）…"
    $env:AZURE_CONFIG = Join-Path $Root "game\config.json"
    $p = Start-Hidden "python" @("-m", "app.main") (Join-Path $Root "backend") `
        (Join-Path $LogDir "backend.out.log") (Join-Path $LogDir "backend.err.log")
    $started += $p.Id
    if (-not (Wait-Url "http://127.0.0.1:$BackendPort/api/health" "" 240 "后端")) {
        Say "[失败] 后端未启动，查看：$LogDir\backend.err.log"
        $started | ConvertTo-Json -Compress | Out-File -Encoding utf8 $PidFile
        exit 1
    }
}

$started | ConvertTo-Json -Compress | Out-File -Encoding utf8 $PidFile

Say ""
Say "服务状态："
Say ("  LLM     http://127.0.0.1:$LlmPort/v1   " + $(if (Test-Url "http://127.0.0.1:$LlmPort/v1/models") {"就绪"} else {"加载中（不阻塞开局）"}))
Say ("  TTS     http://127.0.0.1:$TtsPort       " + $(if (Test-Url "http://127.0.0.1:$TtsPort/health") {"就绪"} else {"加载中（不阻塞开局）"}))
Say ("  KataGo  http://127.0.0.1:$KataPort/health   " + $(if (Test-Url "http://127.0.0.1:$KataPort/health" "ready") {"ready"} else {"预热中/不可用"}))
Say ("  后端    http://127.0.0.1:$BackendPort/api/health   " + $(if (Test-Url "http://127.0.0.1:$BackendPort/api/health") {"200"} else {"未就绪"}))
Say ""

if ($NoGame) {
    Say "-NoGame：不启动游戏。"
    Say "停止：powershell -NoProfile -ExecutionPolicy Bypass -File dev_local.ps1 -Stop"
    exit 0
}

if (-not (Test-Path $GodotExe)) {
    Say "[提示] 找不到 Godot：$GodotExe —— 已起服务，请自行打开工程运行。"
    exit 0
}

Say "启动游戏（Godot 4.6.3）…"
$g = Start-Process -FilePath $GodotExe -ArgumentList @("--path", (Join-Path $Root "game")) -PassThru
$started += $g.Id
$started | ConvertTo-Json -Compress | Out-File -Encoding utf8 $PidFile
Say "已启动。停止：powershell -NoProfile -ExecutionPolicy Bypass -File dev_local.ps1 -Stop"
