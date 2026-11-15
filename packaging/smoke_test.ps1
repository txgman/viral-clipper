# End-to-end test of the built ViralClipper.exe on Windows.
# Usage: pwsh packaging/smoke_test.ps1 -Exe dist/ViralClipper/ViralClipper.exe
param([string]$Exe = "dist/ViralClipper/ViralClipper.exe")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$work = Join-Path $env:RUNNER_TEMP "smoke"
New-Item -ItemType Directory -Force -Path $work | Out-Null
$bin = Join-Path (Split-Path (Resolve-Path $Exe)) "bin"
$ffmpeg = Join-Path $bin "ffmpeg.exe"
$ffprobe = Join-Path $bin "ffprobe.exe"

# 1. A test video with real speech (Windows text-to-speech) over a moving test pattern
Add-Type -AssemblyName System.Speech
$tts = New-Object System.Speech.Synthesis.SpeechSynthesizer
$tts.SetOutputToWaveFile((Join-Path $work "speech.wav"))
$tts.Speak(@"
Welcome back to the show. Today we are talking about money and the lessons I learned the hard way.
I grew up in a small town. My parents ran a hardware store, and it was a pretty normal childhood.
But here is the thing nobody tells you. The biggest mistake I ever made cost me one million dollars, and it happened in a single afternoon.
I signed a contract without reading it. I trusted a friend, and that friend walked away with half of my company. Never do that.
Anyway, after that we moved offices. The new office had a nice kitchen and decent parking.
Why do most people stay broke? It is not because they earn too little. It is because they never learned one rule. Pay yourself first.
Before rent, before food, before anything else, take ten percent and lock it away. Do that for ten years and you will be shocked.
Thanks to everyone who sent questions this week. Stop waiting for permission. The best time to start is right now.
"@)
$tts.Dispose()
& $ffmpeg -v error -y -f lavfi -i "testsrc2=size=1280x720:rate=30" -i (Join-Path $work "speech.wav") `
    -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -shortest (Join-Path $work "test.mp4")
if ($LASTEXITCODE -ne 0) { throw "could not create test video" }
& $ffprobe -v error -show_entries format=duration -of csv=p=0 (Join-Path $work "test.mp4")

# 2. Serve it over HTTP so the link path (yt-dlp) gets exercised
$server = Start-Process python -ArgumentList "-m", "http.server", "8765", "--bind", "127.0.0.1" `
    -WorkingDirectory $work -PassThru -WindowStyle Hidden

# 3. Launch the packaged app
$env:CLIPPER_DATA_DIR = Join-Path $work "data"
$env:CLIPPER_WHISPER_MODEL = "tiny"
$app = Start-Process $Exe -ArgumentList "--no-browser", "--port", "8123" -PassThru `
    -RedirectStandardOutput (Join-Path $work "app.out.txt") -RedirectStandardError (Join-Path $work "app.err.txt")
$base = "http://127.0.0.1:8123"

function Show-Logs {
    Write-Host "----- app stdout -----"; Get-Content (Join-Path $work "app.out.txt") -ErrorAction SilentlyContinue
    Write-Host "----- app stderr -----"; Get-Content (Join-Path $work "app.err.txt") -ErrorAction SilentlyContinue
}

$up = $false
for ($i = 0; $i -lt 120; $i++) {
    try { $status = Invoke-RestMethod "$base/api/status"; $up = $true; break } catch { Start-Sleep 2 }
    if ($app.HasExited) { break }
}
if (-not $up) { Show-Logs; throw "app did not start" }
Write-Host "status: $($status | ConvertTo-Json -Compress)"
if (-not $status.ffmpeg) { throw "bundled ffmpeg not found by the app" }
if (-not $status.js_runtime) { throw "bundled deno not found by the app" }
Invoke-WebRequest "$base/" -UseBasicParsing | Out-Null

function Wait-Job($id, $minutes) {
    $deadline = (Get-Date).AddMinutes($minutes)
    while ((Get-Date) -lt $deadline) {
        $job = Invoke-RestMethod "$base/api/jobs/$id"
        if ($job.status -in @("done", "error", "cancelled")) { return $job }
        Write-Host ("  {0,5:N1}%  {1}" -f $job.progress, $job.stage)
        Start-Sleep 10
    }
    throw "job $id timed out"
}

function Test-Clips($job, $label) {
    if ($job.status -ne "done") { Show-Logs; throw "$label job failed: $($job.error)" }
    $done = @($job.clips | Where-Object { $_.status -eq "done" })
    if ($done.Count -lt 1) { throw "$label job produced no clips" }
    foreach ($c in $done) {
        $f = Join-Path $env:CLIPPER_DATA_DIR "jobs/$($job.id)/clips/$($c.file)"
        $info = & $ffprobe -v error -select_streams v:0 -show_entries stream=width,height,codec_name -of csv=p=0 $f
        Write-Host "  $label clip $($c.index): $info | score $($c.score) | $($c.title)"
        if ($info -notmatch "h264,1080,1920") { throw "$label clip $($c.index) has wrong format: $info" }
        Invoke-WebRequest "$base/api/jobs/$($job.id)/clips/$($c.index)/video" -UseBasicParsing -OutFile (Join-Path $work "dl.mp4")
    }
    Invoke-WebRequest "$base/api/jobs/$($job.id)/zip" -UseBasicParsing -OutFile (Join-Path $work "clips.zip")
}

try {
    # 4. Link job
    Write-Host "== Link job"
    $r = curl.exe -s -F "url=http://127.0.0.1:8765/test.mp4" -F "count=2" -F "min_len=10" -F "max_len=25" "$base/api/jobs" | ConvertFrom-Json
    Test-Clips (Wait-Job $r.id 20) "link"

    # 5. Upload job
    Write-Host "== Upload job"
    $video = Join-Path $work "test.mp4"
    $r = curl.exe -s -F "file=@$video" -F "count=1" -F "min_len=10" -F "max_len=25" -F "layout=fit" -F "captions=green" "$base/api/jobs" | ConvertFrom-Json
    Test-Clips (Wait-Job $r.id 20) "upload"

    # 6. YouTube (informational: YouTube sometimes blocks cloud servers like GitHub's)
    Write-Host "== YouTube job (informational)"
    $r = curl.exe -s -F "url=https://www.youtube.com/watch?v=jNQXAC9IVRw" -F "count=1" -F "min_len=5" -F "max_len=15" "$base/api/jobs" | ConvertFrom-Json
    $yt = Wait-Job $r.id 15
    if ($yt.status -eq "done") { Write-Host "YOUTUBE: OK ($($yt.title))" }
    else { Write-Host "YOUTUBE: FAILED on this build server: $($yt.error)" }
    "youtube=$($yt.status)" | Out-File -Append -FilePath ($env:GITHUB_OUTPUT ?? (Join-Path $work "out.txt"))
    Write-Host "SMOKE TEST PASSED"
} finally {
    Show-Logs
    Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
