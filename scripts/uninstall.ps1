# Undo scripts\setup.ps1. Asks before removing the firewall rule or your notebook.
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $env:LOCALAPPDATA 'Inkcast'

function Ask([string]$Question) {
    $reply = Read-Host "$Question [y/N]"
    return $reply -match '^[Yy]'
}

# Stop the server if it is running.
foreach ($candidate in @(@('py', '-3'), @('python'))) {
    if (Get-Command $candidate[0] -ErrorAction SilentlyContinue) {
        $pre = @($candidate | Select-Object -Skip 1)
        & $candidate[0] @pre -B (Join-Path $Root 'bin\inkcast') stop 2>$null | Out-Null
        break
    }
}

$lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'Inkcast.lnk'
if (Test-Path $lnk) { Remove-Item $lnk; Write-Host "Removed $lnk" }

$bin = Join-Path $Root 'bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -and (($userPath -split ';') -contains $bin)) {
    $newPath = (($userPath -split ';') | Where-Object { $_ -and $_ -ne $bin }) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Write-Host 'Removed the inkcast command from PATH.'
}

if ((Get-NetFirewallRule -Group 'Inkcast' -ErrorAction SilentlyContinue) -and
    (Ask 'Remove the Inkcast firewall rule? (Windows will ask for admin rights)')) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("Remove-NetFirewallRule -Group 'Inkcast'"))
    Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile', '-EncodedCommand', $encoded
    Remove-Item (Join-Path $DataDir 'firewall') -ErrorAction SilentlyContinue
    Write-Host 'Firewall rule removed.'
}

if ((Test-Path $DataDir) -and (Ask "Delete your saved notebook and pairing key in $DataDir?")) {
    Remove-Item -Recurse -Force $DataDir
    Write-Host "Deleted $DataDir"
}

Write-Host 'Done. Python and Poppler were left installed. You can now delete the Inkcast folder.'
