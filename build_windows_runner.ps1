$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "C:\Users\neil\AppData\Local\Programs\Opentrons\resources\python\x64\python.exe"

Push-Location $repo
try {
    & $python --version
    & $python -m pip install pyinstaller
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onefile `
        --name "OT2ScriptRunner" `
        --add-data "apply_standard_offsets.py;." `
        --add-data "ot2_ensure_ssh_key.py;." `
        --add-data "ot2_pull_calibrations.py;." `
        --add-data "ot2_resolve_host.py;." `
        --add-data "pull_rpi_offsets.py;." `
        --add-data "offsets;offsets" `
        --add-data "definitions;definitions" `
        windows_script_runner.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
