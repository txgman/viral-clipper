# Downloads FFmpeg/FFprobe (for video processing) and Deno (YouTube needs a JavaScript runtime)
# into packaging/bin, which the build copies next to ViralClipper.exe.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$bin = Join-Path $PSScriptRoot "bin"
$tmp = Join-Path $env:RUNNER_TEMP "tools"
New-Item -ItemType Directory -Force -Path $bin, $tmp | Out-Null

function Get-Zip($urls, $name) {
    foreach ($u in $urls) {
        try {
            Write-Host "Downloading $u"
            $zip = Join-Path $tmp "$name.zip"
            Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing
            $dir = Join-Path $tmp $name
            Expand-Archive -Path $zip -DestinationPath $dir -Force
            return $dir
        } catch {
            Write-Warning "Failed: $u ($_)"
        }
    }
    throw "Could not download $name"
}

$ff = Get-Zip @(
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
) "ffmpeg"
foreach ($exe in "ffmpeg.exe", "ffprobe.exe") {
    $found = Get-ChildItem -Path $ff -Recurse -Filter $exe | Select-Object -First 1
    if (-not $found) { throw "$exe not found in FFmpeg download" }
    Copy-Item $found.FullName -Destination $bin -Force
}
# FFmpeg license note
$lic = Get-ChildItem -Path $ff -Recurse -Filter "LICENSE*" | Select-Object -First 1
if ($lic) { Copy-Item $lic.FullName -Destination (Join-Path $bin "FFMPEG-LICENSE.txt") -Force }

$deno = Get-Zip @("https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip") "deno"
Copy-Item (Join-Path $deno "deno.exe") -Destination $bin -Force

& (Join-Path $bin "ffmpeg.exe") -hide_banner -version | Select-Object -First 1
& (Join-Path $bin "deno.exe") --version | Select-Object -First 1
Get-ChildItem $bin | Format-Table Name, Length
