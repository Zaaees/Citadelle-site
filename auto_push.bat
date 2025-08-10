@echo off
REM 📌 Chemin vers Python
set "PYTHON_PATH=F:\Program Files\Python\python.exe"

REM 📌 Chemin vers ton script Python
set "SCRIPT_PATH=F:\Users\freed\Desktop\Citadelle site\Citadelle-site\Auto-push.py"

echo 🔄 Lancement du script de mise à jour Git...
"%PYTHON_PATH%" "%SCRIPT_PATH%"

pause
