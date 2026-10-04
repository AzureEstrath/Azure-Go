@echo off
rem 手动启动「轻量引擎」（MeloTTS，快几十倍、几乎不吃 CPU），VBS 隐藏窗口后台运行
cd /d %~dp0
start "" wscript.exe "%~dp0run_hidden.vbs" "%~dp0.venv\Scripts\python.exe" "%~dp0azure_tts_server.py" --port 9880 --threads 6 --cosyvoice 0
exit