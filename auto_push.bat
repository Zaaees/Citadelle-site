@echo off
REM 📌 Chemin vers Python
set "PYTHON_PATH=C:\Users\freed\AppData\Local\Programs\Python\Python313\python.exe"

REM 📌 Chemin vers ton script Python
set "SCRIPT_PATH=C:\Users\freed\Desktop\Code site\Citadelle-site\Auto-push.py"

echo 🔄 Lancement du script de mise à jour Git...
"%PYTHON_PATH%" "%SCRIPT_PATH%"

pause
