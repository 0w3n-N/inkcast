# Inkcast setup for Windows 10/11. Every step explains itself and asks first;
# nothing changes unless you answer yes. Safe to run again.
#
# Run from the Inkcast folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
#   1. Python 3.9+ (winget: Python.Python.3.12)
#   2. Poppler for writing on PDFs (winget: oschwartz10612.Poppler)
#   3. Firewall: allow TCP 8770 on private networks (asks Windows for admin rights)
#   4. Optional: Start menu shortcut and an `inkcast` command
#
# Undo with scripts\uninstall.ps1.

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Port = 8770
if ($env:INKCAST_PORT -match '^\d+$') { $Port = [int]$env:INKCAST_PORT }
$RuleGroup = 'Inkcast'
$DataDir = Join-Path $env:LOCALAPPDATA 'Inkcast'

function Ask([string]$Question, [string]$Default = 'y') {
    $hint = if ($Default -eq 'y') { '[Y/n]' } else { '[y/N]' }
    $reply = Read-Host "$Question $hint"
    if (-not $reply) { $reply = $Default }
    return $reply -match '^[Yy]'
}

function Have([string]$Name) { return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Step([string]$Title) { Write-Host ''; Write-Host $Title -ForegroundColor Cyan }

# Run a command block with administrator rights (shows the Windows UAC prompt).
function Invoke-Elevated([string]$Script) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
    $p = Start-Process powershell -Verb RunAs -Wait -PassThru `
        -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded
    return $p.ExitCode -eq 0
}

# The Python that will run Inkcast, as "py -3" or "python", if it is 3.9 or newer.
function Find-Python {
    foreach ($candidate in @(@('py', '-3'), @('python'))) {
        if (-not (Have $candidate[0])) { continue }
        $exe = $candidate[0]
        $pre = @($candidate | Select-Object -Skip 1)
        try {
            # No quotes inside the code: Windows PowerShell 5.1 would mangle them.
            $out = & $exe @pre -c 'import sys; print(sys.executable * (sys.version_info >= (3, 9)))' 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out)) { return $out.Trim() }
    }
    return $null
}

Write-Host 'Inkcast setup. Each step asks before changing anything.'

# ----------------------------------------------------------------- 1. Python
Step '1. Python'
$python = Find-Python
if ($python) {
    Write-Host "Using $python"
} elseif (Have 'winget') {
    if (Ask 'Python 3.9 or newer is needed. Install Python 3.12 with winget?') {
        winget install -e --id Python.Python.3.12 --scope user
        Write-Host 'Close this window, open a new PowerShell, and run setup again.' -ForegroundColor Yellow
        exit 0
    }
    Write-Host 'Install Python from https://www.python.org/downloads/ and run setup again.'
    exit 1
} else {
    Write-Host 'Install Python 3.9+ from https://www.python.org/downloads/ (tick "Add to PATH"), then run setup again.'
    exit 1
}

# ----------------------------------------------------------------- 2. Poppler
Step '2. PDF support (Poppler)'
if ((Have 'pdftoppm') -and (Have 'pdfinfo')) {
    Write-Host 'Poppler is installed.'
} elseif (Have 'winget') {
    if (Ask 'Install Poppler with winget so you can write on PDFs?') {
        winget install -e --id oschwartz10612.Poppler
        Write-Host 'Poppler is installed. Open a new terminal before starting Inkcast so it can find it.'
    }
} else {
    Write-Host 'To write on PDFs, install Poppler and add its bin folder to PATH: https://github.com/oschwartz10612/poppler-windows/releases'
}

# ----------------------------------------------------------------- 3. Firewall
Step '3. Firewall'
Write-Host "Windows blocks incoming connections, including from your iPad. Inkcast needs TCP $Port."
Write-Host 'The rule only applies to networks marked Private (home or work), not public Wi-Fi.'
$existing = Get-NetFirewallRule -Group $RuleGroup -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host 'The Inkcast firewall rule is already there.'
} elseif (Ask "Allow TCP $Port on private networks? (Windows will ask for admin rights)") {
    $ok = Invoke-Elevated ("New-NetFirewallRule -DisplayName 'Inkcast whiteboard (TCP $Port)' -Group '$RuleGroup' " +
        "-Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null")
    if ($ok) {
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
        Set-Content -Path (Join-Path $DataDir 'firewall') -Value "port=$Port"
        Write-Host 'Firewall rule added.'
    } else {
        Write-Host 'The firewall rule was not added.' -ForegroundColor Yellow
    }
}
# A network marked Public ignores the rule above.
$public = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue |
    Where-Object { $_.NetworkCategory -eq 'Public' -and $_.IPv4Connectivity -ne 'Disconnected' })
foreach ($net in $public) {
    Write-Host "Your network '$($net.Name)' is marked Public, so the iPad can't connect." -ForegroundColor Yellow
    if (Ask "Mark '$($net.Name)' as Private? Only do this for your own home or work network." 'n') {
        $null = Invoke-Elevated "Set-NetConnectionProfile -InterfaceIndex $($net.InterfaceIndex) -NetworkCategory Private"
    }
}

# ----------------------------------------------------------------- 4. Extras
Step '4. Extras (optional)'
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
if (Ask 'Add Inkcast to the Start menu?' 'y') {
    $lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'Inkcast.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($lnk)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = '-B "' + (Join-Path $Root 'bin\inkcast') + '" board'
    $shortcut.WorkingDirectory = $Root
    $shortcut.IconLocation = (Join-Path $Root 'assets\icon.ico')
    $shortcut.Description = 'Apple Pencil whiteboard for your iPad'
    $shortcut.Save()
    Write-Host "Added $lnk"
}
$bin = Join-Path $Root 'bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $bin) {
    if (Ask "Add the 'inkcast' command to your PATH? (adds $bin)" 'n') {
        $newPath = if ($userPath) { "$userPath;$bin" } else { $bin }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Host 'Added. Open a new terminal to use: inkcast'
    }
}

Step 'Done'
& $python -B (Join-Path $Root 'bin\inkcast') doctor
Write-Host ''
Write-Host 'Start Inkcast from the Start menu, or run:  python bin\inkcast'
