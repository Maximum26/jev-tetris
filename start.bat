@echo off
cd /d "%~dp0"

if not exist .env echo TYPESAFE_API_KEY=> .env

findstr /r "TYPESAFE_API_KEY=." .env >nul
if errorlevel 1 (
    echo Paste your API key after the = sign, save the file and close Notepad.
    notepad .env
)

python -m pip install -q -r requirements.txt
python tetris_jev.py
if errorlevel 1 (
    echo.
    echo Something went wrong. Check that Python is installed and added to PATH.
    pause
)