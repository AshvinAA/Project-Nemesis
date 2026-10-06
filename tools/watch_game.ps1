# Launch the game detached, capture its exit code and output.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch_game.ps1 [maxSeconds]
param([int]$MaxSeconds = 30)
$exe = 'F:\Project-Nemesis\BuddyDoom\run\buddydoom.exe'
$run = 'F:\Project-Nemesis\BuddyDoom\run'
$p = Start-Process -FilePath $exe `
    -ArgumentList '-iwad','freedoom1.wad','-warp','1','1','-skill','3','-nomonsters','-buddyhostile','-aidirector','31666' `
    -WorkingDirectory $run -PassThru `
    -RedirectStandardOutput "$run\game_out.txt" -RedirectStandardError "$run\game_err.txt"
if ($p.WaitForExit($MaxSeconds * 1000)) {
    "GAME EXITED code=$($p.ExitCode)"
} else {
    "GAME STILL ALIVE after ${MaxSeconds}s"
}
"--- stderr tail ---"
Get-Content "$run\game_err.txt" -Tail 10
"--- stdout tail ---"
Get-Content "$run\game_out.txt" -Tail 12
