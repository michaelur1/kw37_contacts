@echo off
rem Builds dist\KW37-Kontakty.exe (window) and dist\kw37_contacts.exe (command line).
cd /d "%~dp0"
python -m PyInstaller --onefile --windowed --clean --noconfirm ^
  --name KW37-Kontakty --version-file version_info.txt kw37_contacts_gui.py
if errorlevel 1 exit /b 1
python -m PyInstaller --onefile --console --clean --noconfirm ^
  --name kw37_contacts --version-file version_info.txt kw37_contacts.py
if errorlevel 1 exit /b 1
echo.
echo Hotovo: dist\KW37-Kontakty.exe, dist\kw37_contacts.exe
rem Podpis (az bude certifikat):
rem   signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /n "Michael Urban" dist\*.exe
