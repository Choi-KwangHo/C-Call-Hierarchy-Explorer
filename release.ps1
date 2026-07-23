param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$tag = "v$Version"
$project = Join-Path $PSScriptRoot "Python\프로그램"
$python = Join-Path $project ".venv\Scripts\python.exe"

function Set-ReleaseVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CurrentVersion,
        [Parameter(Mandatory = $true)]
        [string]$NewVersion
    )

    if ($CurrentVersion -eq $NewVersion) {
        Write-Host "Release source already uses version $NewVersion"
        return
    }

    $versionFiles = @(
        "Python\프로그램\app.py",
        "Python\프로그램\README.md",
        "Python\프로그램\packaging\build_release.ps1",
        "Python\프로그램\packaging\Installer.cs",
        "Python\프로그램\packaging\RELEASE_README.txt",
        "Python\프로그램\packaging\version_info.txt",
        "Python\프로그램\tests\test_app_integration.py"
    )

    foreach ($relativePath in $versionFiles) {
        $path = Join-Path $PSScriptRoot $relativePath
        $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $path
        if (-not $content.Contains($CurrentVersion)) {
            throw "Version $CurrentVersion was not found in $relativePath. Release files may be inconsistent."
        }
        $updated = $content.Replace($CurrentVersion, $NewVersion)
        Set-Content -LiteralPath $path -Value $updated -Encoding UTF8 -NoNewline
    }

    Write-Host "Updated release source version: $CurrentVersion -> $NewVersion"
}

git rev-parse --is-inside-work-tree | Out-Null
if ($LASTEXITCODE -ne 0) { throw "This is not a Git repository." }
git remote get-url origin | Out-Null
if ($LASTEXITCODE -ne 0) { throw "The origin remote is not configured." }
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment is missing. Run Python\program\install.bat first."
}
$existingTag = git tag --list $tag
if ($existingTag) { throw "Tag $tag already exists." }

$appPath = Join-Path $project "app.py"
$appText = Get-Content -Raw -Encoding UTF8 -LiteralPath $appPath
$versionMatch = [regex]::Match($appText, 'APP_VERSION\s*=\s*"(?<version>\d+\.\d+\.\d+)"')
if (-not $versionMatch.Success) {
    throw "APP_VERSION was not found in app.py."
}
Set-ReleaseVersion -CurrentVersion $versionMatch.Groups["version"].Value -NewVersion $Version

Write-Host "[1/6] Running tests"
Push-Location $project
try {
    & $python -m unittest discover -s tests -p "test_*.py"
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }

    Write-Host "[2/6] Building Windows release"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\packaging\build_release.ps1"
    if ($LASTEXITCODE -ne 0) { throw "Release build failed." }
} finally {
    Pop-Location
}

Write-Host "[3/6] Staging changes"
git add .
if ($LASTEXITCODE -ne 0) { throw "git add failed." }
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) { throw "There are no changes to commit." }

Write-Host "[4/6] Committing Release $Version and creating tag"
git commit -m "Release $Version"
if ($LASTEXITCODE -ne 0) { throw "git commit failed." }
git tag $tag
if ($LASTEXITCODE -ne 0) { throw "git tag failed." }

Write-Host "[5/6] Pushing main"
git push origin main
if ($LASTEXITCODE -ne 0) { throw "main push failed." }

Write-Host "[6/6] Pushing $tag to start the GitHub Actions release"
git push origin $tag
if ($LASTEXITCODE -ne 0) { throw "$tag push failed." }

Write-Host "Done. Check the GitHub Actions page for the $tag release job."
