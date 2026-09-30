# Launch VS 2022 BuildTools install elevated (UAC prompt) and report the outcome.
# NOTE: PS 5.1 Start-Process -ArgumentList does NOT quote elements containing spaces,
# so the --override payload carries its own literal double quotes.
$override = '"--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"'
$args = @(
    'install', '--id', 'Microsoft.VisualStudio.2022.BuildTools', '--exact', '--silent',
    '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity',
    '--override', $override
)
try {
    Start-Process -FilePath 'winget.exe' -ArgumentList $args -Verb RunAs -ErrorAction Stop
    Write-Output 'ELEVATION_APPROVED - installer starting'
} catch {
    Write-Output ('ELEVATION_FAILED: ' + $_.Exception.Message)
    exit 1
}
