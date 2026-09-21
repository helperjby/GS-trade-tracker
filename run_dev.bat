@echo off
setlocal
cd /d "%~dp0"

rem 개발용 실행 — 소스로 관측기를 관리자 권한으로 띄운다. 인자 없으면 5분 수집 창(--capture 5).
rem 운영 배포는 build.bat 이 만든 dist\YukTracker.exe (UAC 매니페스트 내장) 를 쓴다.

set "PY_CON="
for %%P in (python.exe py.exe) do (
  if not defined PY_CON where %%P >nul 2>&1 && set "PY_CON=%%P"
)
if not defined PY_CON (
  echo [ERROR] Python not found on PATH. Install Python 3.11+ and check "Add python.exe to PATH".
  pause
  exit /b 1
)

set "AGENT_ARGS=%*"
if not defined AGENT_ARGS set "AGENT_ARGS=--capture 5"

echo === Launching YukTracker with admin privileges (%PY_CON%) %AGENT_ARGS% ===
rem -X pycache_prefix: OneDrive 폴더에 __pycache__ 를 남기지 않는다(SEAssist run_admin.bat 관례).
rem 작업 폴더는 src\ — `-m yuktracker` 가 설치 없이 패키지를 찾고, UAC 재기동도 같은 폴더를 쓴다.
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%PY_CON%' -ArgumentList ('-X \"pycache_prefix=' + $env:LOCALAPPDATA + '\YukTracker\pycache\" -m yuktracker %AGENT_ARGS%') -WorkingDirectory '%CD%\src'"
if errorlevel 1 (
  echo.
  echo [ERROR] Elevation failed (UAC declined or Python error above).
  pause
  exit /b 1
)
exit /b 0
