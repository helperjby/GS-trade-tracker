@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo === [1/4] Origin declaration check (src/yuktracker/seassist == VENDOR.json) ===
python tools\sync_seassist_core.py --check
if errorlevel 1 (
  echo *** 출처와 달라진 파일이 VENDOR.json 의 diverged 에 선언되지 않았습니다. 선언을 맞춘 뒤 다시 빌드하세요.
  goto :err
)

echo === [2/4] Tests ===
python -m pytest tests -q
if errorlevel 1 goto :err

echo === [3/4] Building single-file console executable (UAC admin manifest) ===
rem 허브 기본 주소 주입 — %YUKTRACKER_HUB_URL% 이 있을 때만 _build_config.py 를 만든다(gitignore).
python tools\gen_build_config.py
if errorlevel 1 goto :err
if exist "dist" rd /s /q "dist"
if exist "build_temp" rd /s /q "build_temp"
del /f /q "*.spec" 2>nul
python -m PyInstaller --noconfirm --onefile --console --uac-admin ^
  --name "YukTracker" ^
  --paths "src" ^
  --workpath "build_temp" ^
  run_yuktracker.py
if errorlevel 1 goto :err

echo === [4/4] SHA256 ===
certutil -hashfile "dist\YukTracker.exe" SHA256

echo.
echo === Done. Output: dist\YukTracker.exe  (배포: deploy\ 로 복사 후 공유) ===
exit /b 0

:err
echo.
echo *** Build FAILED ***
exit /b 1
