# Сборка LocalClass.exe + установщика на Windows. Запуск из корня проекта:
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# Требуется: Python 3.11+ (py -3), Inno Setup 6 (для установщика; если нет — соберётся только dist\LocalClass\).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv")) { py -3 -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip -q
.\.venv\Scripts\python -m pip install -e ".[dev]" -q

Write-Host "== tests =="
.\.venv\Scripts\python -m pytest -q tests/test_protocol.py tests/test_store.py

Write-Host "== pyinstaller =="
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist\LocalClass) { Remove-Item dist\LocalClass -Recurse -Force }
.\.venv\Scripts\pyinstaller --noconfirm --clean LocalClass.spec

$version = (.\.venv\Scripts\python -c "import localclass; print(localclass.__version__)")
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    Write-Host "== inno setup =="
    & $iscc "/DAppVersion=$version" installer\LocalClass.iss
    Write-Host "Готово: dist\LocalClass-$version-setup.exe"
} else {
    Write-Host "Inno Setup не найден — установщик пропущен. Портативная версия: dist\LocalClass\LocalClass.exe"
    Write-Host "Установить Inno Setup: winget install JRSoftware.InnoSetup"
}
