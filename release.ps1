param(
    [string]$Version = "",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$pythonRoot = Join-Path $PSScriptRoot "Python"
$projectCandidates = @(
    Get-ChildItem -LiteralPath $pythonRoot -Directory |
        Where-Object {
            (Test-Path -LiteralPath (Join-Path $_.FullName "app.py")) -and
            (Test-Path -LiteralPath (Join-Path $_.FullName "packaging\build_release.ps1"))
        }
)
if ($projectCandidates.Count -ne 1) {
    throw "Unable to locate one Python application directory containing app.py."
}
$project = $projectCandidates[0].FullName
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
        (Join-Path $project "app.py"),
        (Join-Path $project "README.md"),
        (Join-Path $project "packaging\build_release.ps1"),
        (Join-Path $project "packaging\Installer.cs"),
        (Join-Path $project "packaging\RELEASE_README.txt"),
        (Join-Path $project "packaging\version_info.txt"),
        (Join-Path $project "tests\test_app_integration.py")
    )

    foreach ($path in $versionFiles) {
        $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $path
        if (-not $content.Contains($CurrentVersion)) {
            throw "Version $CurrentVersion was not found in $path. Release files may be inconsistent."
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
    $installScript = Join-Path $project "install.ps1"
    if (-not (Test-Path -LiteralPath $installScript)) {
        throw "Python virtual environment and install.ps1 are missing."
    }
    Write-Host "Python virtual environment is missing. Creating it now..."
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installScript
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
        throw "Python virtual environment setup failed."
    }
}

$appPath = Join-Path $project "app.py"
$appText = Get-Content -Raw -Encoding UTF8 -LiteralPath $appPath
$versionMatch = [regex]::Match($appText, 'APP_VERSION\s*=\s*"(?<version>\d+\.\d+\.\d+)"')
if (-not $versionMatch.Success) {
    throw "APP_VERSION was not found in app.py."
}
$currentVersion = $versionMatch.Groups["version"].Value
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = $currentVersion
    Write-Host "Using app.py release version $Version"
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Release version must use the major.minor.patch format."
}
$tag = "v$Version"
$existingTag = git tag --list $tag
if ($existingTag) { throw "Tag $tag already exists." }
Set-ReleaseVersion -CurrentVersion $currentVersion -NewVersion $Version
if ($CheckOnly) {
    Write-Host "Release prerequisites are ready for $tag"
    exit 0
}

Write-Host "[1/7] Running tests"
Push-Location $project
try {
    & $python -m unittest discover -s tests -p "test_*.py"
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }

    Write-Host "[2/7] Building Windows release"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\packaging\build_release.ps1"
    if ($LASTEXITCODE -ne 0) { throw "Release build failed." }
} finally {
    Pop-Location
}

Write-Host "[3/7] Staging changes"
git add .
if ($LASTEXITCODE -ne 0) { throw "git add failed." }
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) { throw "There are no changes to commit." }

Write-Host "[4/7] Committing Release $Version and creating tag"
git commit -m "Release $Version"
if ($LASTEXITCODE -ne 0) { throw "git commit failed." }
git tag $tag
if ($LASTEXITCODE -ne 0) { throw "git tag failed." }

Write-Host "[5/7] Pushing main"
git push origin main
if ($LASTEXITCODE -ne 0) { throw "main push failed." }

Write-Host "[6/7] Pushing $tag to start the GitHub Actions release"
git push origin $tag
if ($LASTEXITCODE -ne 0) { throw "$tag push failed." }

Write-Host "[7/7] Waiting for GitHub Release assets"
$releaseApi = "https://api.github.com/repos/Choi-KwangHo/C-Call-Hierarchy-Explorer/releases/tags/$tag"
$releaseDeadline = (Get-Date).AddMinutes(15)
$releaseReady = $false
while ((Get-Date) -lt $releaseDeadline) {
    try {
        $release = Invoke-RestMethod -Uri $releaseApi -Headers @{
            Accept = "application/vnd.github+json"
            "User-Agent" = "C-Call-Hierarchy-Explorer-Release"
        }
        $assetNames = @($release.assets | ForEach-Object { $_.name })
        $expectedAssets = @(
            "C-Call-Hierarchy-Explorer-Setup-$Version.exe",
            "C-Call-Hierarchy-Explorer-Portable-$Version.exe",
            "SHA256SUMS.txt"
        )
        $missingAssets = @($expectedAssets | Where-Object { $_ -notin $assetNames })
        if (-not $release.draft -and $missingAssets.Count -eq 0) {
            $releaseReady = $true
            Write-Host "Release published: $($release.html_url)" -ForegroundColor Green
            break
        }
    } catch {
        $statusCode = $_.Exception.Response.StatusCode.value__
        if ($statusCode -and $statusCode -ne 404) {
            Write-Verbose "Release status check failed: $($_.Exception.Message)"
        }
    }
    Write-Host "GitHub Actions is still building $tag..."
    Start-Sleep -Seconds 10
}
if (-not $releaseReady) {
    Write-Warning "The tag was pushed, but GitHub Release was not ready within 15 minutes: $tag"
}
