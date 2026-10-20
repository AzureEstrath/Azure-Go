@echo off
rem ============================================================================
rem  Azure-Go 一键测试脚本（Windows 单平台）
rem
rem  用法（在本文件所在目录直接双击或命令行调用）：
rem    run_test_env.bat            启动全套：KataGo 云服务 + 后端 + 游戏（三个窗口）
rem    run_test_env.bat backend    只启动 KataGo 云服务 + 后端（不开游戏，适合自己跑 REST）
rem    run_test_env.bat tests      跑全部门卫：后端全量 + KataGo 服务全量 + 全部前端 headless
rem                                脚本 + --headless --quit，跑完自动还原 project.godot
rem    run_test_env.bat stop       停掉本脚本启动的服务、引擎与游戏窗口
rem    run_test_env.bat restore    还原 game/project.godot（用 4.7.1 跑过之后必做）
rem    run_test_env.bat help       看这段说明
rem
rem  可覆盖的环境变量：
rem    KATAGO_EXE / KATAGO_MODEL / KATAGO_CONFIG   引擎与 analysis 配置（缺省用下面探测到的路径）
rem    GODOT_EXE                                    Godot 可执行文件（缺省按下面顺序探测）
rem
rem  注意（AGENTS.md 的 Godot 纪律）：本机只有 Godot 4.7.1，工程按 4.6 配置。
rem  用 4.7.1 跑过游戏或门卫后，game/project.godot 的 config/features 会被改写、
rem  可能多出 *.import —— 跑完请执行 `run_test_env.bat restore`（tests 模式会自动做）。
rem
rem  另外：启动模式用 start 打开服务窗口，**不要**把本脚本的输出接到管道或重定向里
rem  （服务窗口会一直持有那个句柄，看起来像卡住）；直接双击、或在 cmd 窗口里运行即可。
rem ============================================================================
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"

set "MODE=%~1"
if /i "%MODE%"=="help"    goto :usage
if /i "%MODE%"=="-h"      goto :usage
if /i "%MODE%"=="tests"   goto :tests
if /i "%MODE%"=="stop"    goto :stop
if /i "%MODE%"=="restore" goto :restore
if /i "%MODE%"=="backend" goto :start_backend_only
if "%MODE%"==""           goto :start_all
echo [错误] 不认识的参数：%MODE%
echo.
goto :usage

rem ---------------------------------------------------------------------------
rem  路径探测（都可用环境变量覆盖）
rem ---------------------------------------------------------------------------
:detect
set "ENGINE_DIR=%ROOT%katago_service\engine"
if not defined KATAGO_EXE     if exist "%ENGINE_DIR%\katago.exe" set "KATAGO_EXE=%ENGINE_DIR%\katago.exe"
if not defined KATAGO_MODEL   if exist "%ENGINE_DIR%\model.bin" set "KATAGO_MODEL=%ENGINE_DIR%\model.bin"
if not defined KATAGO_CONFIG  if exist "D:\下载\KataGo\analysis_example.cfg" set "KATAGO_CONFIG=D:\下载\KataGo\analysis_example.cfg"
if not defined GODOT_EXE      if exist "E:\Godot\Godot_v4.7.1-stable_win64_console.exe" set "GODOT_EXE=E:\Godot\Godot_v4.7.1-stable_win64_console.exe"
if not defined GODOT_EXE      if exist "E:\Godot\Godot_v4.7.1-stable_win64.exe" set "GODOT_EXE=E:\Godot\Godot_v4.7.1-stable_win64.exe"
if not defined GODOT_EXE      for /f "delims=" %%G in ('where godot 2^>nul') do if not defined GODOT_EXE set "GODOT_EXE=%%G"
if not defined GODOT_EXE      for /f "delims=" %%G in ('where godot4 2^>nul') do if not defined GODOT_EXE set "GODOT_EXE=%%G"
echo   KataGo 引擎 : %KATAGO_EXE%
echo   KataGo 模型 : %KATAGO_MODEL%
echo   analysis 配置: %KATAGO_CONFIG%
echo   Godot       : %GODOT_EXE%
echo.
exit /b 0

:require_paths
call :detect
if not exist "%KATAGO_EXE%" (
  echo [错误] 找不到 KataGo 引擎。请把 katago.exe 放到 "%ENGINE_DIR%\"，
  echo        或设 KATAGO_EXE 指向它。引擎与权重归部署侧持有、不入库。
  goto :fail
)
if not exist "%KATAGO_MODEL%" (
  echo [错误] 找不到 KataGo 权重。请把 model.bin 放到 "%ENGINE_DIR%\"，或设 KATAGO_MODEL。
  goto :fail
)
if not exist "%KATAGO_CONFIG%" (
  echo [错误] 找不到 analysis 配置（服务要用 analysis 模式跑引擎，不是 default_gtp.cfg）。
  echo        请设 KATAGO_CONFIG 指向一个 analysis 配置，例如 KataGo 自带的 analysis_example.cfg。
  goto :fail
)
exit /b 0

rem ---------------------------------------------------------------------------
rem  启动全套
rem ---------------------------------------------------------------------------
:start_all
echo ============================================================
echo  Azure-Go 一键启动（全套：KataGo 云服务 + 后端 + 游戏）
echo ============================================================
call :require_paths || goto :fail
if not exist "%GODOT_EXE%" (
  echo [警告] 没探测到 Godot，将只启动服务与后端。
  echo        设 GODOT_EXE 指向 Godot 可执行文件即可一并启动游戏。
  goto :start_backend_only
)

echo [1/3] 启动 KataGo 云服务（127.0.0.1:8100，引擎预热需要几十秒）...
start "AzureGo-KataGo" cmd /k ""%ROOT%katago_service\run_server.bat""
call :wait_kata || goto :fail

echo [2/3] 启动后端（127.0.0.1:8000；它会先等 KataGo 就绪，最长 180 秒）...
start "AzureGo-Backend" cmd /k ""%ROOT%backend\run_server.bat""
call :wait_backend || goto :fail

echo [3/3] 启动游戏（Godot）...
start "AzureGo-Godot" "%GODOT_EXE%" --path "%ROOT%game"

echo.
echo 全部起来了：
echo   后端健康 : http://127.0.0.1:8000/api/health
echo   KataGo   : http://127.0.0.1:8100/health   （state=ready 才算好）
echo   停止     : run_test_env.bat stop
echo   跑完还原 : run_test_env.bat restore       （用 4.7.1 跑过游戏后必做）
echo.
echo 提示：两个服务各自占用一个窗口，关窗口等于停服务。
exit /b 0

:start_backend_only
echo ============================================================
echo  Azure-Go 一键启动（KataGo 云服务 + 后端）
echo ============================================================
call :require_paths || goto :fail

echo [1/2] 启动 KataGo 云服务（127.0.0.1:8100）...
start "AzureGo-KataGo" cmd /k ""%ROOT%katago_service\run_server.bat""
call :wait_kata || goto :fail

echo [2/2] 启动后端（127.0.0.1:8000）...
start "AzureGo-Backend" cmd /k ""%ROOT%backend\run_server.bat""
call :wait_backend || goto :fail

echo.
echo 服务已就绪：http://127.0.0.1:8000/api/health 与 http://127.0.0.1:8100/health
echo 停止：run_test_env.bat stop
exit /b 0

rem ---------------------------------------------------------------------------
rem  就绪探测（用 PowerShell 轮询，不依赖 curl）
rem ---------------------------------------------------------------------------
:wait_kata
echo      等待 KataGo 服务就绪（最多约 180 秒）...
powershell -NoProfile -Command "$u='http://127.0.0.1:8100/health'; for($i=0;$i -lt 90;$i++){ try { $r=(Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $u).Content; if($r -match 'ready'){ Write-Host '      就绪：KataGo state=ready'; exit 0 } } catch {} ; Start-Sleep -Seconds 2 }; Write-Host '      超时：KataGo 未就绪'; exit 1"
if errorlevel 1 (
  echo [错误] KataGo 服务没在 180 秒内就绪。看一眼那个窗口的报错：
  echo        - 引擎/权重/配置路径不对，或显卡 OpenCL 初始化失败；
  echo        - 注意 /health 里 "boardXSize" 那类报错是服务自己的最小预热探针，不是故障。
  goto :fail
)
exit /b 0

:wait_backend
echo      等待后端就绪（最多约 180 秒）...
powershell -NoProfile -Command "$u='http://127.0.0.1:8000/api/health'; for($i=0;$i -lt 90;$i++){ try { $c=(Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $u).StatusCode; if($c -eq 200){ Write-Host '      就绪：后端 /api/health 200'; exit 0 } } catch {} ; Start-Sleep -Seconds 2 }; Write-Host '      超时：后端未就绪'; exit 1"
if errorlevel 1 (
  echo [错误] 后端没在 180 秒内起来。常见原因：
  echo        - 缺依赖：pip install -r backend\requirements.txt
  echo        - 端口被占：看那个窗口的报错；
  echo        - 后端在等 KataGo 就绪：确认 KataGo 窗口没挂。
  goto :fail
)
exit /b 0

rem ---------------------------------------------------------------------------
rem  全部门卫
rem ---------------------------------------------------------------------------
:tests
echo ============================================================
echo  Azure-Go 全部门卫（后端 + KataGo 服务 + 全部前端 headless）
echo ============================================================
call :detect
if not exist "%GODOT_EXE%" (
  echo [错误] 没探测到 Godot，前端门卫跑不了。请设 GODOT_EXE。
  goto :fail
)
set "FAILS=0"

echo.
echo --- [1] 后端全量（cd backend ^&^& python -m unittest discover -s tests）
pushd "%ROOT%backend"
python -m unittest discover -s tests
if errorlevel 1 set /a FAILS+=1
popd

echo.
echo --- [2] KataGo 云服务全量（cd katago_service ^&^& python -m unittest discover -s tests）
pushd "%ROOT%katago_service"
python -m unittest discover -s tests
if errorlevel 1 set /a FAILS+=1
popd

echo.
echo --- [3] 前端 headless 门卫（game\tests\test_*.gd，逐个跑）
for %%F in ("%ROOT%game\tests\test_*.gd") do (
  echo.
  echo     ^>^> %%~nxF
  "%GODOT_EXE%" --path "%ROOT%game" --headless --script "res://tests/%%~nxF"
  if errorlevel 1 set /a FAILS+=1
)

echo.
echo --- [4] 启动自检（godot --path game --headless --quit，要求无 SCRIPT ERROR）
"%GODOT_EXE%" --path "%ROOT%game" --headless --quit
if errorlevel 1 set /a FAILS+=1

echo.
echo --- [5] 还原 game\project.godot（4.7.1 跑过之后必做）
call :restore_silent

echo.
echo ============================================================
if "%FAILS%"=="0" (
  echo  结果：全部通过
) else (
  echo  结果：有 %FAILS% 组失败（看上面的 FAIL / ERROR / SCRIPT ERROR）
)
echo ============================================================
if "%FAILS%"=="0" (exit /b 0) else (exit /b 1)

rem ---------------------------------------------------------------------------
rem  停止 / 还原
rem ---------------------------------------------------------------------------
:stop
echo 停止本脚本启动的服务、引擎与游戏窗口...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -match 'katago_service\.server') -or ($_.CommandLine -match '-m app\.main') } | ForEach-Object { Write-Host ('  结束 ' + $_.ProcessId + '  ' + $_.Name); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
taskkill /F /IM katago.exe >nul 2>nul && echo   结束 katago.exe
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'Godot*') -and ($_.CommandLine -match '--path') } | ForEach-Object { Write-Host ('  结束 ' + $_.ProcessId + '  ' + $_.Name); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo 完成。若还有残留窗口，直接关掉即可。
exit /b 0

:restore
echo 还原 game\project.godot（把 config/features 改回 4.6）...
call :restore_silent
exit /b 0

:restore_silent
git checkout -- game/project.godot 2>nul
echo  已还原 game/project.godot
git status --short
echo  上面若还有任务外的 *.import / *.uid 改动，请手动还原（AGENTS.md 的 Godot 纪律）
exit /b 0

rem ---------------------------------------------------------------------------
rem  说明 / 失败退出
rem ---------------------------------------------------------------------------
:usage
echo 用法：
echo   run_test_env.bat            启动全套（KataGo 云服务 + 后端 + 游戏）
echo   run_test_env.bat backend    只启动 KataGo 云服务 + 后端
echo   run_test_env.bat tests      跑全部门卫（后端 + KataGo 服务 + 全部前端 headless）
echo   run_test_env.bat stop       停掉本脚本启动的服务、引擎与游戏
echo   run_test_env.bat restore    还原 game\project.godot
echo   run_test_env.bat help       这段说明
echo.
echo 可覆盖：KATAGO_EXE / KATAGO_MODEL / KATAGO_CONFIG / GODOT_EXE
exit /b 0

:fail
echo.
echo [中止] 看上面的错误提示；修好后重跑同一个命令即可。
exit /b 1
