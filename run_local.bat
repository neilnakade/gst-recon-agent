@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Install Python 3.11 from python.org first. & pause & exit /b 1)
if not exist .venv (
  echo First run: setting up, this takes a minute...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -r requirements.txt -q
streamlit run app.py
pause