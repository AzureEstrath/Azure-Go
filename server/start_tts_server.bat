@echo off
rem 手动启动自建语音服务（VBS 隐藏窗口后台运行）。游戏不再自动拉起，要用语音就手动点这里。
cd /d %~dp0
start "" wscript.exe "%~dp0run_hidden.vbs" "%~dp0.venv\Scripts\python.exe" "%~dp0azure_tts_server.py" --port 9880 --threads 12 --cosyvoice 1 --model_dir pretrained_models\Fun-CosyVoice3-0.5B --sft_dir pretrained_models\CosyVoice-300M-SFT
exit