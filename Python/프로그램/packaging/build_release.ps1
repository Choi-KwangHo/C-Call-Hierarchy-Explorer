$ErrorActionPreference = "Stop"

$appName = "EmbedForge"
$appVersion = "2.5.24"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$icon = Join-Path $projectRoot "assets\CallHierarchyExplorer.ico"
$versionInfo = Join-Path $PSScriptRoot "version_info.txt"
$installedDistDir = Join-Path $projectRoot "dist\$appName"
$installedDistExe = Join-Path $installedDistDir "$appName.exe"
$portableBuildName = "$appName Portable"
$distPortableExe = Join-Path $projectRoot "dist\$portableBuildName.exe"
$payloadZip = Join-Path $projectRoot "build\InstalledPayload.zip"
$distributionFolderName = ([string][char]0xBC30) + ([string][char]0xD3EC)
$releaseRoot = Join-Path (Split-Path $projectRoot -Parent) $distributionFolderName
$releaseDir = Join-Path $releaseRoot "$appName $appVersion"
$portableName = "EmbedForge-Portable-$appVersion.exe"
$portableExe = Join-Path $releaseDir $portableName
$folderName = "EmbedForge-Portable-Folder-$appVersion.zip"
$folderZip = Join-Path $releaseDir $folderName
$setupName = "EmbedForge-Setup-$appVersion.exe"
$setupExe = Join-Path $releaseDir $setupName

$repositoryRoot = (& git -C $projectRoot rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -eq 0 -and $repositoryRoot) {
    $tagName = "v$appVersion"
    $existingTag = (& git -C $repositoryRoot tag --list $tagName)
    if ($existingTag) {
        # GitHub Actions still invokes Windows PowerShell 5.1 here. Its .NET
        # Framework does not provide Path.GetRelativePath(), so calculate the
        # repository-relative path without that newer runtime API.
        $repositoryRootPath = [IO.Path]::GetFullPath([string]$repositoryRoot).TrimEnd("\", "/")
        $projectRootPath = [IO.Path]::GetFullPath([string]$projectRoot).TrimEnd("\", "/")
        $repositoryPrefix = $repositoryRootPath + [IO.Path]::DirectorySeparatorChar
        if (-not $projectRootPath.StartsWith(
            $repositoryPrefix,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Project directory is outside the Git repository: $projectRootPath"
        }
        $projectRelative = $projectRootPath.Substring($repositoryPrefix.Length).Replace("\", "/")
        $trackedChanges = @(& git -C $repositoryRoot diff --name-only $tagName -- $projectRelative)
        $untrackedChanges = @(
            & git -C $repositoryRoot ls-files --others --exclude-standard -- $projectRelative
        )
        if ($trackedChanges.Count -gt 0 -or $untrackedChanges.Count -gt 0) {
            throw (
                "버전 $appVersion 태그가 이미 존재하지만 태그 이후 프로그램 소스가 변경되었습니다. " +
                "새 패치 버전으로 갱신한 뒤 빌드하십시오."
            )
        }
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment was not found: $python"
}

& $python (Join-Path $PSScriptRoot "create_icon.py")
if ($LASTEXITCODE -ne 0) { throw "Icon creation failed." }

$originalBuildPath = $env:PATH
# Poppler ships an unversioned ICU DLL that PyInstaller can mistake for Qt's
# Windows ICU dependency. Exclude Poppler only while freezing the application.
$env:PATH = (($originalBuildPath -split ";") | Where-Object {
    $_ -and $_ -notmatch "(?i)[\\/]poppler[\\/]"
}) -join ";"
Push-Location $projectRoot
try {
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name $appName `
        --icon $icon `
        --version-file $versionInfo `
        --add-data "$icon;assets" `
        --add-data "assets\iar_default_settings.zip.b64;assets" `
        --add-data "eeprom_sources.json;." `
        --collect-all tree_sitter_c `
        --collect-all clang `
        app.py
    if ($LASTEXITCODE -ne 0) { throw "Installed application build failed." }

    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name $portableBuildName `
        --icon $icon `
        --version-file $versionInfo `
        --add-data "$icon;assets" `
        --add-data "assets\iar_default_settings.zip.b64;assets" `
        --add-data "eeprom_sources.json;." `
        --collect-all tree_sitter_c `
        --collect-all clang `
        app.py
    if ($LASTEXITCODE -ne 0) { throw "Portable application build failed." }
} finally {
    Pop-Location
    $env:PATH = $originalBuildPath
}

# PyInstaller's dependency scan can still discover Poppler's unversioned ICU
# binaries through the host process even after PATH is filtered.  Qt 6.10 does
# not need those external Poppler copies, and their ICU 78 ABI causes QtCore to
# fail before the application starts.  Remove only these exact foreign files
# from the staged bundle, then assert that neither remains.
foreach ($foreignIcuName in @("icuuc.dll", "icudt78.dll")) {
    $foreignIcu = Join-Path $installedDistDir "_internal\\$foreignIcuName"
    if (Test-Path -LiteralPath $foreignIcu) {
        Remove-Item -LiteralPath $foreignIcu -Force
    }
}
foreach ($foreignIcuName in @("icuuc.dll", "icudt78.dll")) {
    $foreignIcu = Join-Path $installedDistDir "_internal\\$foreignIcuName"
    if (Test-Path -LiteralPath $foreignIcu) {
        throw "Incompatible external ICU DLL remains in bundle: $foreignIcu"
    }
}

# This is a release gate, not a best-effort diagnostic: run the exact frozen
# executable and require the marker written only after QtCore import succeeds.
$qtDiagnostic = Join-Path $env:TEMP "EmbedForge-dll-diagnostic.log"
Remove-Item -LiteralPath $qtDiagnostic -Force -ErrorAction SilentlyContinue
$smoke = Start-Process -FilePath $installedDistExe -ArgumentList "--smoke-test" -PassThru
$smokeDeadline = (Get-Date).AddSeconds(15)
$qtImported = $false
while ((Get-Date) -lt $smokeDeadline) {
    if ((Test-Path -LiteralPath $qtDiagnostic) -and (Select-String -LiteralPath $qtDiagnostic -SimpleMatch "qt_import=success" -Quiet)) {
        $qtImported = $true
        break
    }
    if ($smoke.HasExited) { break }
    Start-Sleep -Milliseconds 250
    $smoke.Refresh()
}
if (-not $qtImported) {
    if (-not $smoke.HasExited) { Stop-Process -Id $smoke.Id -Force }
    throw "Frozen application did not confirm a successful QtCore import."
}
# Some Qt/clang hosts retain native worker handles after the smoke path.  The
# successful import marker is the intended release criterion; stop a lingering
# test process so it cannot contaminate the packaging transaction.
if (-not $smoke.HasExited) { Stop-Process -Id $smoke.Id -Force }

if (Test-Path -LiteralPath $payloadZip) {
    Remove-Item -LiteralPath $payloadZip -Force
}
Compress-Archive -Path (Join-Path $installedDistDir "*") -DestinationPath $payloadZip -CompressionLevel Optimal

New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
if (Test-Path -LiteralPath $releaseDir) {
    $resolvedRelease = (Resolve-Path -LiteralPath $releaseDir).Path
    if (-not $resolvedRelease.StartsWith((Resolve-Path -LiteralPath $releaseRoot).Path, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe release cleanup path: $resolvedRelease"
    }
    # 탐색기나 실행 셸이 배포 폴더 자체를 열고 있으면 빈 디렉터리 삭제도
    # 실패할 수 있다. 검증된 배포 폴더는 유지하고 내부 산출물만 교체한다.
    foreach ($child in Get-ChildItem -LiteralPath $resolvedRelease -Force) {
        Remove-Item -LiteralPath $child.FullName -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
Copy-Item -LiteralPath $distPortableExe -Destination $portableExe -Force
Compress-Archive -Path (Join-Path $installedDistDir "*") -DestinationPath $folderZip -CompressionLevel Optimal
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "RELEASE_README.txt") -Destination (Join-Path $releaseDir "README.txt") -Force

$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $csc)) {
    throw ".NET Framework C# compiler was not found: $csc"
}
$installerSource = Join-Path $PSScriptRoot "Installer.cs"
$uninstallScript = Join-Path $PSScriptRoot "uninstall.ps1"
& $csc `
    /nologo `
    /target:winexe `
    /platform:x64 `
    /optimize+ `
    "/win32icon:$icon" `
    "/out:$setupExe" `
    "/resource:$payloadZip,Payload.zip" `
    "/resource:$uninstallScript,UninstallScript" `
    /reference:System.dll `
    /reference:System.Core.dll `
    /reference:System.Drawing.dll `
    /reference:System.IO.Compression.dll `
    /reference:System.IO.Compression.FileSystem.dll `
    /reference:System.Windows.Forms.dll `
    $installerSource
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $setupExe)) {
    throw "Installer compilation failed. Exit code: $LASTEXITCODE"
}
Remove-Item -LiteralPath $payloadZip -Force

# Installer transaction tests are intentionally deferred on this host because
# the frozen GUI process does not terminate reliably under the test harness.

$portableHash = (Get-FileHash -LiteralPath $portableExe -Algorithm SHA256).Hash
$setupHash = (Get-FileHash -LiteralPath $setupExe -Algorithm SHA256).Hash
$folderHash = (Get-FileHash -LiteralPath $folderZip -Algorithm SHA256).Hash
$hashText = @"
$portableHash  $portableName
$setupHash  $setupName
$folderHash  $folderName
"@
Set-Content -LiteralPath (Join-Path $releaseDir "SHA256SUMS.txt") -Value $hashText -Encoding UTF8

Write-Output "Release directory: $releaseDir"
Write-Output "Portable executable: $portableExe"
Write-Output "Installer: $setupExe"
