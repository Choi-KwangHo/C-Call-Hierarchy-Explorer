$ErrorActionPreference = "Stop"

$appName = "C Call Hierarchy Explorer"
$appVersion = "1.1.19"
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
$portableName = "C-Call-Hierarchy-Explorer-Portable-$appVersion.exe"
$portableExe = Join-Path $releaseDir $portableName
$setupName = "C-Call-Hierarchy-Explorer-Setup-$appVersion.exe"
$setupExe = Join-Path $releaseDir $setupName

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment was not found: $python"
}

& $python (Join-Path $PSScriptRoot "create_icon.py")
if ($LASTEXITCODE -ne 0) { throw "Icon creation failed." }

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
        --collect-all tree_sitter_c `
        --collect-all clang `
        app.py
    if ($LASTEXITCODE -ne 0) { throw "Portable application build failed." }
} finally {
    Pop-Location
}

$installedSmoke = Start-Process -FilePath $installedDistExe -ArgumentList "--smoke-test" -Wait -PassThru
if ($installedSmoke.ExitCode -ne 0) {
    throw "Installed application smoke test failed with exit code $($installedSmoke.ExitCode)."
}
$portableSmoke = Start-Process -FilePath $distPortableExe -ArgumentList "--smoke-test" -Wait -PassThru
if ($portableSmoke.ExitCode -ne 0) {
    throw "Portable application smoke test failed with exit code $($portableSmoke.ExitCode)."
}

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

$installerTestRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$installerTestDir = Join-Path $installerTestRoot ("CCH-InstallerTest-" + [guid]::NewGuid().ToString("N"))
$previousInstallerTestDir = $env:CCH_INSTALLER_TEST_DIR
try {
    $env:CCH_INSTALLER_TEST_DIR = $installerTestDir
    $installerSmoke = Start-Process -FilePath $setupExe -ArgumentList "/S" -Wait -PassThru
    if ($installerSmoke.ExitCode -ne 0) {
        throw "Installer transaction test failed with exit code $($installerSmoke.ExitCode)."
    }
    $installedTestExe = Get-ChildItem -LiteralPath $installerTestDir -Filter "$appName.exe" -Recurse |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    $installedTestDll = Join-Path (Split-Path $installedTestExe -Parent) "_internal\python312.dll"
    if (-not (Test-Path -LiteralPath $installedTestExe) -or -not (Test-Path -LiteralPath $installedTestDll)) {
        throw "Installer transaction test did not produce the application and Python DLL."
    }
    $installedTestSmoke = Start-Process -FilePath $installedTestExe -ArgumentList "--smoke-test" -Wait -PassThru
    if ($installedTestSmoke.ExitCode -ne 0) {
        throw "Installed application test failed with exit code $($installedTestSmoke.ExitCode)."
    }

    $lockedExecutable = [IO.File]::Open(
        $installedTestExe,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::None
    )
    try {
        $lockedUpgrade = Start-Process -FilePath $setupExe -ArgumentList "/S" -Wait -PassThru
        if ($lockedUpgrade.ExitCode -ne 0) {
            throw "Installer lock-resistant upgrade failed with exit code $($lockedUpgrade.ExitCode)."
        }
        if (-not (Test-Path -LiteralPath $installedTestExe) -or -not (Test-Path -LiteralPath $installedTestDll)) {
            throw "Installer lock-resistant upgrade did not preserve the running application."
        }
        $installedCopies = @(Get-ChildItem -LiteralPath $installerTestDir -Filter "$appName.exe" -Recurse)
        if ($installedCopies.Count -lt 2) {
            throw "Installer lock-resistant upgrade did not create a separate version directory."
        }
    } finally {
        $lockedExecutable.Dispose()
    }

    $retryUpgrade = Start-Process -FilePath $setupExe -ArgumentList "/S" -Wait -PassThru
    if ($retryUpgrade.ExitCode -ne 0) {
        throw "Installer retry test failed with exit code $($retryUpgrade.ExitCode)."
    }
    $installedTestExe = Get-ChildItem -LiteralPath $installerTestDir -Filter "$appName.exe" -Recurse |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    $remainingCopies = @(Get-ChildItem -LiteralPath $installerTestDir -Filter "$appName.exe" -Recurse)
    if ($remainingCopies.Count -ne 1) {
        throw "Installer cleanup test left $($remainingCopies.Count) application versions instead of one."
    }
    $retrySmoke = Start-Process -FilePath $installedTestExe -ArgumentList "--smoke-test" -Wait -PassThru
    if ($retrySmoke.ExitCode -ne 0) {
        throw "Retried installation application test failed with exit code $($retrySmoke.ExitCode)."
    }
} finally {
    $env:CCH_INSTALLER_TEST_DIR = $previousInstallerTestDir
    $resolvedInstallerTest = [IO.Path]::GetFullPath($installerTestDir)
    $installerTestUnderTemp = $resolvedInstallerTest.StartsWith($installerTestRoot, [StringComparison]::OrdinalIgnoreCase)
    $installerTestSafeName = (Split-Path $resolvedInstallerTest -Leaf).StartsWith("CCH-InstallerTest-", [StringComparison]::Ordinal)
    if ($installerTestUnderTemp -and $installerTestSafeName -and (Test-Path -LiteralPath $resolvedInstallerTest)) {
        Remove-Item -LiteralPath $resolvedInstallerTest -Recurse -Force
    }
}

$portableHash = (Get-FileHash -LiteralPath $portableExe -Algorithm SHA256).Hash
$setupHash = (Get-FileHash -LiteralPath $setupExe -Algorithm SHA256).Hash
$hashText = @"
$portableHash  $portableName
$setupHash  $setupName
"@
Set-Content -LiteralPath (Join-Path $releaseDir "SHA256SUMS.txt") -Value $hashText -Encoding UTF8

Write-Output "Release directory: $releaseDir"
Write-Output "Portable executable: $portableExe"
Write-Output "Installer: $setupExe"
