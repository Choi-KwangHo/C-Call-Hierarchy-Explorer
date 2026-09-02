$ErrorActionPreference = "SilentlyContinue"

$roots = @(
    (Join-Path $env:LOCALAPPDATA "Programs\EmbedForge"),
    (Join-Path $env:LOCALAPPDATA "Programs\CCallHierarchyExplorer")
)
$names = @("EmbedForge", "C Call Hierarchy Explorer", "CCallHierarchyExplorer")
foreach ($name in $names) {
    Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force
    Remove-Item -LiteralPath (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$name.lnk") -Force
    Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath("Desktop")) "$name.lnk") -Force
}
foreach ($id in @("EmbedForge", "CCallHierarchyExplorer")) {
    Remove-Item -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$id" -Recurse -Force
}
$cleanup = ($roots | ForEach-Object { "if (Test-Path -LiteralPath '$_') { Remove-Item -LiteralPath '$_' -Recurse -Force }" }) -join "; "
Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "Start-Sleep -Milliseconds 700; $cleanup"
)
