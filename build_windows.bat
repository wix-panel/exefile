@echo off
REM ============================================================================
REM  InstagramOps - Compilation Windows (.exe) avec Nuitka
REM  A LANCER SUR WINDOWS (PC ou VPS), pas sur le Mac.
REM
REM  Pre-requis :
REM    1) Python 3.10+ installe (https://www.python.org, cocher "Add to PATH")
REM    2) Les dependances :  pip install nuitka flask flask-cors requests
REM    3) Un compilateur C : Nuitka telecharge MinGW automatiquement
REM       (repondre "yes", gere par --assume-yes-for-downloads)
REM
REM  Resultat : dist\InstagramOps.exe (un seul fichier, code compile illisible)
REM ============================================================================

setlocal
cd /d "%~dp0"

echo [1/2] Installation / mise a jour des dependances...
python -m pip install --upgrade pip
python -m pip install --upgrade nuitka flask flask-cors requests

echo [2/2] Compilation avec Nuitka (peut prendre 5-15 min)...
python -m nuitka ^
  --standalone ^
  --onefile ^
  --output-dir=dist ^
  --output-filename=InstagramOps.exe ^
  --include-module=insta_core ^
  --include-module=worker ^
  --include-data-files=panel.html=panel.html ^
  --include-package=flask ^
  --include-package=flask_cors ^
  --include-package=requests ^
  --assume-yes-for-downloads ^
  --remove-output ^
  server.py

echo.
echo ============================================================================
echo  Termine. L'executable est dans :  dist\InstagramOps.exe
echo.
echo  A COTE de l'exe, place (ils seront crees/utilises au runtime) :
echo    - proxies.json, swipe_proxies.json   (tes proxies)
echo    - config.json, panel_settings.json   (ta config)
echo    - accounts_created.json              (cree automatiquement)
echo  Et installe adb (platform-tools) sur le VPS, ou pose adb.exe a cote.
echo ============================================================================
endlocal
pause
