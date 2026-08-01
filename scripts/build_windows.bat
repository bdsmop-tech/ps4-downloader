@echo off
REM Build standalone ps4-downloader.exe on Windows 11 (run this on a Windows machine).
setlocal
cd /d "%~dp0\.."

if not exist .venv (
  py -3.12 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -U pip
pip install -r requirements.txt pyinstaller

pyinstaller --noconfirm --clean ^
  --name ps4-downloader ^
  --onefile ^
  --console ^
  --paths src ^
  --add-data "vendor\dpi_payload.bin;vendor" ^
  --hidden-import ps4_downloader ^
  src\ps4_downloader\__main__.py

copy /Y config.example.toml dist\config.example.toml
copy /Y config.example.toml dist\config.toml
copy /Y queue.txt dist\queue.txt
if not exist dist\downloads mkdir dist\downloads
if not exist dist\vendor mkdir dist\vendor
copy /Y vendor\dpi_payload.bin dist\vendor\dpi_payload.bin

echo.
echo Built: dist\ps4-downloader.exe
echo Put config.toml next to the exe, then run: ps4-downloader.exe ping
