[CmdletBinding()]
param(
    [switch]$Build
)

$ErrorActionPreference = 'Stop'
$venvDirectory = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venvDirectory 'Scripts\python.exe'

Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath $venvDirectory)) {
        & py -3.11 -m venv $venvDirectory
        if ($LASTEXITCODE -ne 0) {
            throw 'Cannot create .venv. Install Python 3.11 with the Python launcher.'
        }
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw '.venv exists but Scripts\python.exe is missing. Check the environment before retrying.'
    }

    & $venvPython -c "import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 11) and struct.calcsize('P') == 8 and sys.prefix != sys.base_prefix else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw 'Expected a Python 3.11 x64 virtual environment.'
    }

    $requirementsFile = 'requirements.txt'
    if ($Build) {
        $requirementsFile = 'requirements-build.txt'
    }
    & $venvPython -m pip install -r $requirementsFile
    if ($LASTEXITCODE -ne 0) {
        throw 'Dependency installation failed. See pip output above.'
    }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw 'Installed dependencies are inconsistent.'
    }

    Write-Host 'Environment ready: .venv'
    Write-Host 'Run: .\.venv\Scripts\python.exe gui.py'
    Write-Host 'Activate (optional): .\.venv\Scripts\Activate.ps1'
}
finally {
    Pop-Location
}
