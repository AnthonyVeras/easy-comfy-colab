$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot
$BuildPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $BuildPython)) { $BuildPython = 'python' }

& $BuildPython -c 'import customtkinter, PIL, PyInstaller, psutil'
if ($LASTEXITCODE -ne 0) {
    throw 'Instale as dependências: python -m pip install customtkinter pillow pyinstaller psutil'
}

& $BuildPython app/create_icon.py
if ($LASTEXITCODE -ne 0) { throw 'Não foi possível gerar o ícone.' }

& $BuildPython -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name 'Easy Comfy Colab' `
    --icon 'app/assets/comfy-colab.ico' `
    --add-data 'app/assets/comfy-colab.ico;assets' `
    --add-data 'app/assets/comfy-colab.png;assets' `
    --collect-all customtkinter `
    --paths 'app' `
    --hidden-import backend `
    --hidden-import session `
    --hidden-import ui `
    --hidden-import services `
    --distpath 'dist' --workpath 'build' --specpath '.' `
    'app/main.py'
if ($LASTEXITCODE -ne 0) { throw 'Falha ao compilar o aplicativo.' }

$built = Join-Path $ProjectRoot 'dist\Easy Comfy Colab'
$target = Join-Path $ProjectRoot 'Easy Comfy Colab'
if (-not (Test-Path -LiteralPath $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
Copy-Item -Path (Join-Path $built '*') -Destination $target -Recurse -Force
Write-Host "Aplicativo pronto: $target\Easy Comfy Colab.exe"
