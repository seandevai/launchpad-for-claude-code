# Launchpad for Claude Code - removes the shortcuts created by install.ps1.
# Your settings and logs in %USERPROFILE%\.launchpad-cc are kept unless you pass -RemoveData.
param([switch]$RemoveData)
$ws = New-Object -ComObject WScript.Shell
$links = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Launchpad for Claude Code.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Launchpad for Claude Code.lnk")
)
foreach ($l in $links) {
    if ((Test-Path $l) -and ($ws.CreateShortcut($l).Arguments -match "launchpad\.py")) { Remove-Item $l; Write-Host "Removed $l" }
}
$data = Join-Path $env:USERPROFILE ".launchpad-cc"
if ($RemoveData -and (Test-Path $data)) { Remove-Item -Recurse $data; Write-Host "Removed $data" }
elseif (Test-Path $data) { Write-Host "Kept your settings in $data (run with -RemoveData to delete them)" }
Write-Host "Launchpad's Python dependency (pywebview) was left installed: remove it with 'pip uninstall pywebview' if you no longer need it."
