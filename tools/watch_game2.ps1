# Launch the game and report the exact second it exits (or 40s alive).
$exe = 'F:\Project-Nemesis\BuddyDoom\run\buddydoom.exe'
$run = 'F:\Project-Nemesis\BuddyDoom\run'
$p = Start-Process -FilePath $exe `
    -ArgumentList '-iwad','freedoom1.wad','-warp','1','1','-skill','3','-nomonsters','-buddyhostile','-aidirector','31666' `
    -WorkingDirectory $run -PassThru
$t0 = Get-Date
while (-not $p.HasExited -and ((Get-Date) - $t0).TotalSeconds -lt 40) { Start-Sleep -Milliseconds 500 }
if ($p.HasExited) {
    'DIED at {0:n1}s' -f ((Get-Date) - $t0).TotalSeconds
} else {
    'ALIVE after 40s - killing it'
    Stop-Process -Id $p.Id -Force
}
