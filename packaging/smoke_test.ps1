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

# 3. Launch the packaged app (TikTok calls go to a local mock of TikTok's API that enforces its documented rules)
$mock = Start-Process python -ArgumentList "tests/mock_tiktok.py", "--port", "9555" -PassThru -WindowStyle Hidden
$env:CLIPPER_TIKTOK_AUTH_URL = "http://127.0.0.1:9555/v2/auth/authorize/"
$env:CLIPPER_TIKTOK_API_BASE = "http://127.0.0.1:9555"
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
    $linkJob = Wait-Job $r.id 20
    Test-Clips $linkJob "link"

    # 5. Upload job
    Write-Host "== Upload job"
    $video = Join-Path $work "test.mp4"
    $r = curl.exe -s -F "file=@$video" -F "count=1" -F "min_len=10" -F "max_len=25" -F "layout=fit" -F "captions=green" "$base/api/jobs" | ConvertFrom-Json
    Test-Clips (Wait-Job $r.id 20) "upload"

    # 6. TikTok: connect (OAuth + PKCE) and post a clip
    Write-Host "== TikTok posting (mock TikTok API)"
    $body = @{ tiktok_client_key = "mock_client_key"; tiktok_client_secret = "mock_client_secret" } | ConvertTo-Json
    Invoke-RestMethod -Method Post "$base/api/settings" -ContentType "application/json" -Body $body | Out-Null
    $final = curl.exe -s -L -o NUL -w "%{url_effective}" "$base/api/tiktok/connect"
    if ($final -notmatch "tiktok=connected") { Show-Logs; throw "TikTok connect flow failed (ended at $final)" }
    $tts = Invoke-RestMethod "$base/api/tiktok/status"
    if ($tts.account.display_name -ne "Clip Fan") { throw "TikTok account not stored" }
    $post = @{ items = @(@{ job_id = $linkJob.id; index = 1; title = "Smoke test caption #test" }); mode = "direct";
               privacy_level = "SELF_ONLY"; consent = $true } | ConvertTo-Json -Depth 5
    $created = Invoke-RestMethod -Method Post "$base/api/tiktok/posts" -ContentType "application/json" -Body $post
    $deadline = (Get-Date).AddMinutes(4)
    do {
        Start-Sleep 5
        $p = (Invoke-RestMethod "$base/api/tiktok/posts") | Where-Object { $_.id -eq $created[0].id }
        Write-Host "  TikTok post: $($p.status) $($p.error)"
    } while ($p.status -notin @("posted", "failed") -and (Get-Date) -lt $deadline)
    if ($p.status -ne "posted") { Show-Logs; throw "TikTok post did not complete: $($p.status) $($p.error)" }
    $mstate = Invoke-RestMethod "http://127.0.0.1:9555/mock/state"
    $up = $mstate.posts | Select-Object -First 1
    if ($up.received -ne $up.size -or $up.post_info.privacy_level -ne "SELF_ONLY") { throw "TikTok upload incomplete" }
    Write-Host "TIKTOK: OK (uploaded $($up.size) bytes)"

    # 7. YouTube (informational: YouTube sometimes blocks cloud servers like GitHub's)
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
    Stop-Process -Id $mock.Id -Force -ErrorAction SilentlyContinue
}
