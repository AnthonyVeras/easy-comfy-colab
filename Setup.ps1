param(
    [string]$Distro = 'Ubuntu-24.04',
    [string]$WslUser = '',
    [switch]$SkipWindowsDependencies,
    [switch]$SkipWsl
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $SkipWindowsDependencies) {
    python -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
    if ($LASTEXITCODE -ne 0) { throw 'Instale Python 3.11+ para Windows e adicione ao PATH.' }
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao criar o ambiente Python local.' }
    }
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependências locais.' }
}
$wslArgs = @('--distribution', $Distro)
if ($WslUser) { $wslArgs += @('--user', $WslUser) }
if (-not $SkipWsl) {
    $linuxProject = (& wsl.exe @wslArgs --exec wslpath -a $PSScriptRoot).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $linuxProject) { throw 'Instale o Ubuntu no WSL e crie seu usuário Linux antes de executar Setup.ps1.' }
    & wsl.exe @wslArgs --exec bash "$linuxProject/scripts/setup-wsl.sh"
    if ($LASTEXITCODE -ne 0) { throw 'A preparação do WSL falhou. Confira a saída acima.' }
}
$settingsDir = Join-Path $env:LOCALAPPDATA 'EasyComfyColab'
New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null
@{distro=$Distro; user=$WslUser} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $settingsDir 'runtime.json') -Encoding UTF8
Write-Host 'Pronto. Execute Launch.ps1 ou abra o executável da versão publicada.'
