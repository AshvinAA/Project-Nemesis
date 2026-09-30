$ev = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000} -MaxEvents 5 -ErrorAction SilentlyContinue
foreach ($e in $ev) {
    Write-Output ('--- ' + $e.TimeCreated)
    $lines = $e.Message -split "`r?`n"
    Write-Output (($lines | Select-Object -First 12) -join ' | ')
}
if (-not $ev) { Write-Output 'NO_EVENTS_FOUND' }
