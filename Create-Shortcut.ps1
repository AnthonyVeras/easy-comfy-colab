$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'Easy Comfy Colab\Easy Comfy Colab.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Compile com Build-App.ps1 ou extraia o pacote da release primeiro.' }
$shellLink = New-Object -ComObject WScript.Shell
$shortcut = $shellLink.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Easy Comfy Colab.lnk'))
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "$exe,0"
$shortcut.Description = 'Easy Comfy Colab — ComfyUI no Google Colab'
$shortcut.Save()
Write-Host 'Atalho Easy Comfy Colab criado.'
