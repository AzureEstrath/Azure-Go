@echo off
chcp 65001 >nul
cd /d %~dp0
echo ==================================================
echo  Azure 本地语音（CosyVoice 双模型：0.5B 克隆 + 300M-SFT 预设）
echo  - 克隆音色 azure 由你的三段语料生成；预设音色 中文女 / 粤语女 / 英文女
echo  - 纯 CPU 上较慢（约 10 秒合成 1 秒语音）；启动加载需 2-4 分钟
echo  - 游戏里：朗读方式选「CosyVoice 官方 fastapi」，地址 http://127.0.0.1:9880
echo ==================================================
".venv\Scripts\python.exe" azure_tts_server.py --port 9880 --threads 12 --cosyvoice 1 ^
  --model_dir pretrained_models\Fun-CosyVoice3-0.5B --sft_dir pretrained_models\CosyVoice-300M-SFT
pause