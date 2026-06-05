# ============================================================================
#  InstagramOps - Build "presque 1 clic" SUR LE VPS WINDOWS
#
#  Utilisation sur le VPS :
#    1) Copier tout le dossier du projet sur le VPS
#    2) Clic droit sur ce fichier -> "Executer avec PowerShell"
#       (ou dans PowerShell :  powershell -ExecutionPolicy Bypass -File build_on_vps.ps1)
#
#  Le script installe Python si absent, installe Nuitka, puis compile.
#  Resultat :  dist\InstagramOps.exe
# ============================================================================

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Find-Python {
    foreach ($cmd in @("python", "py")) {
        try {
            $v = & $cmd --version 2>&1
            if ($v -match "Python 3\.(\d+)") { if ([int]$Matches[1] -ge 10) { return $cmd } }
        } catch {}
    }
    return $null
}

# --- 1. Python ---------------------------------------------------------------
$py = Find-Python
if (-not $py) {
    Write-Host "[*] Python 3.10+ introuvable. Telechargement et installation..." -ForegroundColor Yellow
    $installer = "$env:TEMP\python-installer.exe"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $installer
    Write-Host "[*] Installation silencieuse de Python (PATH inclus)..." -ForegroundColor Yellow
    Start-Process -FilePath $installer -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1" -Wait
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $py = Find-Python
    if (-not $py) { Write-Error "Python installe mais introuvable. Ferme/rouvre PowerShell et relance le script."; exit 1 }
}
Write-Host "[OK] Python : $(& $py --version)" -ForegroundColor Green

# --- 2. Dependances ----------------------------------------------------------
Write-Host "[*] Installation des dependances (nuitka, flask, ...)..." -ForegroundColor Yellow
& $py -m pip install --upgrade pip
& $py -m pip install --upgrade nuitka flask flask-cors requests

# --- 3. Compilation ----------------------------------------------------------
Write-Host "[*] Compilation Nuitka (5-15 min la premiere fois)..." -ForegroundColor Yellow
& $py -m nuitka `
    --standalone `
    --onefile `
    --output-dir=dist `
    --output-filename=InstagramOps.exe `
    --include-module=insta_core `
    --include-module=worker `
    --include-data-files=panel.html=panel.html `
    --include-package=flask `
    --include-package=flask_cors `
    --include-package=requests `
    --assume-yes-for-downloads `
    --remove-output `
    server.py

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " OK -> dist\InstagramOps.exe" -ForegroundColor Green
Write-Host " Pose a cote : proxies.json, config.json, et adb.exe" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
