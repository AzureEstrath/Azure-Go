@echo off
rem 手动启动本地语音服务（VBS 隐藏窗口后台运行）。游戏启动时也会自动拉起，一般不需要手动点。
cd /d %~dp0
start "" wscript.exe "%~dp0run_hidden.vbs" "%~dp0.venv\Scripts\python.exe" "%~dp0azure_tts_server.py" --port 9880 --threads 12 --cosyvoice 1 --model_dir pretrained_models\Fun-CosyVoice3-0.5B --sft_dir pretrained_models\CosyVoice-300M-SFT
exit