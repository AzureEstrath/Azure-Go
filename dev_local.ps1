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
$LlmStarter  = "D:\KataGo\AzureGo-legacy\tools\start_llm_server.py"
$TtsDir      = "D:\KataGo\cosyvoice"
$TtsPort     = 9880
$LlmPort     = 8080
$KataPort    = 8100
$BackendPort = 8000

$LogDir = Join-Path $Root "backend\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$PidFile = Join-Path $LogDir "dev_local_pids.json"

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
$TtsVoice = if ($QualityTts) { "azure" } else { "xiaoya" }
$ttsHealth = ""
try { $ttsHealth = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$TtsPort/health").Content } catch { }
$runningQuality = ($ttsHealth -match '"model_dir"\s*:\s*"[^"]+"')
if ($ttsHealth -ne "" -and ($runningQuality -eq [bool]$QualityTts)) {
    Say ("[2/4] 本地语音已在运行（" + $(if ($QualityTts) { "质量引擎" } else { "快引擎" }) + "）：http://127.0.0.1:$TtsPort")
} else {
    if ($ttsHealth -ne "") {
        Say "[2/4] 切换语音引擎：停止现有服务…"
        $c = Get-NetTCPConnection -LocalPort $TtsPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { try { Stop-Process -Id $c.OwningProcess -Force } catch { } }
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -match 'azure_tts_server' } |
            ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch { } }
        Start-Sleep -Seconds 3
    }
    $bat = if ($QualityTts) { "start_tts_server.bat" } else { "start_tts_server_fast.bat" }
    if (Test-Path (Join-Path $TtsDir $bat)) {
        Say ("[2/4] 启动本地语音（" + $(if ($QualityTts) { "质量引擎 CosyVoice" } else { "快引擎 MeloTTS" }) + "，后台隐藏窗口）…")
        $p = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$(Join-Path $TtsDir $bat)`"") `
            -WorkingDirectory $TtsDir -WindowStyle Hidden -PassThru
        $started += $p.Id
    } else {
        Say "[2/4] [跳过] 找不到语音服务：$TtsDir（自动降级为系统语音）"
    }
}

# 把后端要用的「本地音色」写回本地配置（game/config.json 是本地开发配置，不入库）
# 云端音色 (tts_voice) 不动；本地端点优先，失败才用云端。
$cfgPath = Join-Path $Root "game\config.json"
if (Test-Path $cfgPath) {
    $cfgText = [IO.File]::ReadAllText($cfgPath, [Text.Encoding]::UTF8)
    $cfgText = [regex]::Replace($cfgText, '"tts_local_base_url"\s*:\s*"[^"]*"',
                                ('"tts_local_base_url": "http://127.0.0.1:' + $TtsPort + '/v1"'))
    $cfgText = [regex]::Replace($cfgText, '"tts_local_voice"\s*:\s*"[^"]*"',
                                ('"tts_local_voice": "' + $TtsVoice + '"'))
    [IO.File]::WriteAllText($cfgPath, $cfgText, (New-Object Text.UTF8Encoding($false)))
}
Warm-Tts $TtsVoice

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
if (Test-Url "http://127.0.0.1:$BackendPort/api/health") {
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
