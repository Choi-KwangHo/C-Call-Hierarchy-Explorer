$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $ProjectDir ".venv"

function Find-Python {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { return @($launcher.Source, "-3") }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return @($python.Source) }
    throw "Python 3.10 이상을 먼저 설치하십시오: https://www.python.org/downloads/windows/"
}

$PythonCommand = Find-Python
if (-not (Test-Path $VenvDir)) {
    if ($PythonCommand.Count -gt 1) {
        & $PythonCommand[0] $PythonCommand[1] -m venv $VenvDir
    } else {
        & $PythonCommand[0] -m venv $VenvDir
    }
}
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $ProjectDir "requirements.txt")
& $VenvPython -c "import PySide6, tree_sitter, tree_sitter_c, clang.cindex; print('설치 확인 완료:', PySide6.__version__)"
Write-Host "설치가 완료되었습니다. run.bat를 실행하십시오." -ForegroundColor Green
