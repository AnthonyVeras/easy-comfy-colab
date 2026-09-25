$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'Easy Comfy Colab\Easy Comfy Colab.exe'
if (Test-Path -LiteralPath $exe) {
    Start-Process -FilePath $exe -WorkingDirectory $PSScriptRoot
} else {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Execute Setup.ps1 antes de abrir pelo código-fonte.' }
    Start-Process -FilePath $python -ArgumentList 'app/main.py' -WorkingDirectory $PSScriptRoot
}
