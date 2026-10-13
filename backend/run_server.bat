@echo off
rem Launch the Azure backend (FastAPI: REST + WebSocket).
rem KataGo and the local LLM (llama-server) are launched / watched by this process.
cd /d "%~dp0"
if not defined AZURE_CONFIG if exist "%~dp0..\AzureGoGodot\config.json" set "AZURE_CONFIG=%~dp0..\AzureGoGodot\config.json"
if not defined AZURE_CONFIG if exist "%~dp0..\game\config.json" set "AZURE_CONFIG=%~dp0..\game\config.json"
python -m app.main
