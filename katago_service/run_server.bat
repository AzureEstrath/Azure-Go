@echo off
rem 启动 KataGo 云服务（FastAPI：/health + /analyze）。引擎由本服务自己拉起并预热。
rem 必须在仓库根目录运行：katago_service 是包，python -m katago_service.server 要求父目录在
rem sys.path 上，所以这里 cd 到 %~dp0..（对比 backend\run_server.bat 的 cd /d "%~dp0"——
rem 那里 app 包就在 backend\ 内部）。
rem 参数留空则由环境变量兜底：KATAGO_HOST / KATAGO_SERVICE_TOKEN / KATAGO_EXE / KATAGO_MODEL / KATAGO_CONFIG。
rem 后端在别的机器上时要让服务对外监听：设 KATAGO_HOST=0.0.0.0（默认只回环），并务必配 token + 内网/TLS 反代。
cd /d "%~dp0.."
python -m katago_service.server --host "%KATAGO_HOST%" --port 8100 --token "%KATAGO_SERVICE_TOKEN%" --katago-exe "%KATAGO_EXE%" --model "%KATAGO_MODEL%" --config "%KATAGO_CONFIG%"
