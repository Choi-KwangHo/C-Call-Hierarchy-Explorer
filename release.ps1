param(
    [string]$Version = "",
    [switch]$CheckOnly
)

$releaseMutex = New-Object Threading.Mutex($false, "Local\CCallHierarchyExplorerRelease")
$releaseMutexOwned = $false
try {
try {
    $releaseMutexOwned = $releaseMutex.WaitOne(0)
} catch [Threading.AbandonedMutexException] {
    $releaseMutexOwned = $true
}
if (-not $releaseMutexOwned) {
    throw "다른 배포 작업이 이미 실행 중입니다. 기존 release.bat가 끝난 후 다시 실행하십시오."
}

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

Write-Host "[7/7] 원격 저장소의 최신 버전 태그 확인"
$remoteTagLines = @(git ls-remote --tags origin "refs/tags/v*")
if ($LASTEXITCODE -ne 0) {
    throw "원격 버전 태그를 확인하지 못했습니다."
}
$remoteVersions = @(
    foreach ($line in $remoteTagLines) {
        if ($line -match 'refs/tags/v(?<version>\d+\.\d+\.\d+)$') {
            [version]$Matches["version"]
        }
    }
)
if ($remoteVersions.Count -eq 0) {
    throw "원격 저장소에서 버전 태그를 찾지 못했습니다."
}
$latestRemoteVersion = ($remoteVersions | Sort-Object -Descending | Select-Object -First 1).ToString()
if ($latestRemoteVersion -ne $Version) {
    throw "원격 저장소의 최신 태그(v$latestRemoteVersion)가 배포 버전($tag)과 일치하지 않습니다."
}

$remoteTag = @($remoteTagLines | Where-Object { $_ -match "refs/tags/$([regex]::Escape($tag))$" })
if ($remoteTag.Count -ne 1) {
    throw "원격 저장소에서 $tag 태그를 정확히 확인하지 못했습니다."
}

$actionsUrl = "https://github.com/Choi-KwangHo/C-Call-Hierarchy-Explorer/actions/workflows/release.yml"
$releaseUrl = "https://github.com/Choi-KwangHo/C-Call-Hierarchy-Explorer/releases/tag/$tag"

$assetBase = "https://github.com/Choi-KwangHo/C-Call-Hierarchy-Explorer/releases/download/$tag"
$setupName = "C-Call-Hierarchy-Explorer-Setup-$Version.exe"
$portableName = "C-Call-Hierarchy-Explorer-Portable-$Version.exe"
$checksumName = "SHA256SUMS.txt"

$initialWaitSeconds = 90
Write-Host "GitHub Actions에서 $tag 빌드 자산을 생성하고 있습니다."
Write-Host -NoNewline "["
for ($elapsed = 0; $elapsed -lt $initialWaitSeconds; $elapsed += 10) {
    Start-Sleep -Seconds 10
    Write-Host -NoNewline "o"
}
$buildDeadline = (Get-Date).AddMinutes(10)
$buildReady = $false
while ((Get-Date) -lt $buildDeadline) {
    try {
        $setupHead = Invoke-WebRequest -Uri "$assetBase/$setupName" -Method Head -UseBasicParsing -TimeoutSec 20
        $portableHead = Invoke-WebRequest -Uri "$assetBase/$portableName" -Method Head -UseBasicParsing -TimeoutSec 20
        $checksumText = (New-Object Net.WebClient).DownloadString("$assetBase/$checksumName")
        $setupSize = [long]$setupHead.Headers["Content-Length"]
        $portableSize = [long]$portableHead.Headers["Content-Length"]
        $setupDigestMatch = [regex]::Match(
            $checksumText,
            "(?im)^(?<digest>[0-9a-f]{64})\s+\*?$([regex]::Escape($setupName))\s*$"
        )
        $portableDigestMatch = [regex]::Match(
            $checksumText,
            "(?im)^(?<digest>[0-9a-f]{64})\s+\*?$([regex]::Escape($portableName))\s*$"
        )
        if (
            [int]$setupHead.StatusCode -eq 200 -and
            [int]$portableHead.StatusCode -eq 200 -and
            $setupSize -gt 0 -and
            $portableSize -gt 0 -and
            $setupDigestMatch.Success -and
            $portableDigestMatch.Success
        ) {
            $buildReady = $true
            Write-Host "] 완료"
            Write-Host "태그와 빌드 자산 확인 완료: $tag" -ForegroundColor Green
            Write-Host "설치 파일: $setupName ($setupSize bytes)"
            Write-Host "설치 SHA-256: $($setupDigestMatch.Groups['digest'].Value.ToLowerInvariant())"
            Write-Host "포터블 파일: $portableName ($portableSize bytes)"
            Write-Host "포터블 SHA-256: $($portableDigestMatch.Groups['digest'].Value.ToLowerInvariant())"
            Write-Host "SHA-256 목록: $checksumName"
            break
        }
    } catch {
        # The tag appears before GitHub Actions finishes uploading release assets.
    }
    Start-Sleep -Seconds 10
    Write-Host -NoNewline "o"
}
if (-not $buildReady) {
    Write-Host "] 확인 시간 초과"
    throw "$tag 태그는 확인했지만 10분 안에 설치 빌드 자산을 확인하지 못했습니다. Actions 페이지를 확인하십시오: $actionsUrl"
}
Write-Host "릴리스 확인: $releaseUrl"
} finally {
    if ($releaseMutexOwned) {
        $releaseMutex.ReleaseMutex()
    }
    $releaseMutex.Dispose()
}
