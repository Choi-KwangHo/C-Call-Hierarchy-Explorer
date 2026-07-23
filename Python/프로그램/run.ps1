param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pythonw = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$App = Join-Path $ProjectDir "app.py"

if (-not (Test-Path -LiteralPath $Pythonw) -or -not (Test-Path -LiteralPath $Python)) {
    throw "Python 가상환경이 없습니다. install.bat를 먼저 실행하십시오."
}
if (-not (Test-Path -LiteralPath $App)) {
    throw "app.py를 찾을 수 없습니다: $App"
}

if ($Check) {
    & $Python -c "import PySide6, tree_sitter, tree_sitter_c, clang.cindex; import app; print('실행 환경 확인 완료:', PySide6.__version__)"
    exit $LASTEXITCODE
}

Start-Process -FilePath $Pythonw -ArgumentList ('"{0}"' -f $App) -WorkingDirectory $ProjectDir
