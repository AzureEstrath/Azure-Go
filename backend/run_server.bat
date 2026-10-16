@echo off
rem Launch the Azure backend (FastAPI: REST + WebSocket).
rem KataGo is launched by this process; the LLM is called over the cloud API.
cd /d "%~dp0"
if not defined AZURE_CONFIG if exist "%~dp0..\AzureGoGodot\config.json" set "AZURE_CONFIG=%~dp0..\AzureGoGodot\config.json"
if not defined AZURE_CONFIG if exist "%~dp0..\game\config.json" set "AZURE_CONFIG=%~dp0..\game\config.json"
python -m app.main
