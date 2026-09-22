@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo === [1/4] Vendor drift check (src/yuktracker/seassist == SEASSIST_REPO) ===
python tools\sync_seassist_core.py --check
if errorlevel 2 (
  echo --- SEAssist 체크아웃이 없어 벤더 드리프트 검사를 건너뜁니다. 해시 핀은 [2/4] 테스트가 검사합니다.
) else if errorlevel 1 (
  echo *** 벤더 사본이 SEAssist 소스와 다릅니다. tools\sync_seassist_core.py 로 동기화한 뒤 다시 빌드하세요.
  goto :err
)

echo === [2/4] Tests ===
python -m pytest tests -q
if errorlevel 1 goto :err

echo === [3/4] Building single-file console executable (UAC admin manifest) ===
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
