# One-time setup: put a "Project Nemesis" shortcut on the desktop.
# The shortcut runs Nemesis.bat (default mode = game + live dashboard + browser).
# Run me once:  powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'Project Nemesis.lnk'

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath       = 'F:\Project-Nemesis\Nemesis.bat'
$sc.WorkingDirectory = 'F:\Project-Nemesis'
$sc.Description      = 'Play with the learning buddy - game + weight dashboard'
$sc.IconLocation     = 'F:\Project-Nemesis\BuddyDoom\run\buddydoom.exe,0'
$sc.WindowStyle      = 7   # minimized: the launcher only prints status, then exits
$sc.Save()

Write-Host "[OK] Desktop shortcut created: $lnkPath"
Write-Host "     Double-click it to play. Extra shortcuts you can make the same way:"
Write-Host '       "Nemesis Agent"   -> Nemesis.bat agent'
Write-Host '       "Nemesis Reset"   -> Nemesis.bat reset'
