$ErrorActionPreference = "SilentlyContinue"

$appName = "C Call Hierarchy Explorer"
$appId = "CCallHierarchyExplorer"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\$appId"
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$appName.lnk"
$desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "$appName.lnk"

Get-Process -Name "C Call Hierarchy Explorer", "CCallHierarchyExplorer" -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item -LiteralPath $startMenu -Force
Remove-Item -LiteralPath $desktop -Force
Remove-Item -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$appId" -Recurse -Force

$cleanup = "Start-Sleep -Milliseconds 700; Remove-Item -LiteralPath '$($installDir.Replace("'", "''"))' -Recurse -Force"
Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList "-NoProfile", "-Command", $cleanup
