# Rebuilds both apps:  powershell -ExecutionPolicy Bypass -File photo_link\build.ps1
#   dist\BioBuzzPhotoLink.exe   Windows receiver (PyInstaller; needs photo_link\pc\.venv with qrcode, pillow, pyinstaller, aiohttp, pystray)
#   dist\BioBuzzPhotoLink.apk   Android camera app (Gradle; needs the Android SDK, platform 34, build-tools)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
New-Item -ItemType Directory -Force "$root\dist" | Out-Null

# --- PC exe ---
$py = "$root\pc\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    & "$root\..\.venv\Scripts\python.exe" -I -m venv "$root\pc\.venv"
}
& $py -I -m pip install --quiet --disable-pip-version-check "qrcode[pil]" pillow pyinstaller aiohttp pystray
& $py -I "$root\make_icons.py"
Push-Location "$root\pc"
& $py -I -m PyInstaller --noconfirm --onefile --windowed --name BioBuzzPhotoLink --icon "$root\pc\icon.ico" `
    --add-data "$root\pc\ui;ui" --add-data "$root\pc\icon.png;." --hidden-import pystray._win32 `
    --distpath "$root\dist" --workpath "$root\build\pyi" --specpath "$root\build" main.py
Pop-Location

# --- Android apk ---
if (-not $env:ANDROID_HOME) { $env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk" }
"sdk.dir=$($env:ANDROID_HOME -replace '\\','/')" | Set-Content -Encoding ascii "$root\android\local.properties"
# Gradle's JVM cannot create its internal socket when the temp path is not usable for AF_UNIX
# (seen on this machine: "Unable to establish loopback connection"); a short folder fixes it.
New-Item -ItemType Directory -Force "C:\jt" | Out-Null
$env:JAVA_TOOL_OPTIONS = "-Djdk.net.unixdomain.tmpdir=C:/jt"
Push-Location "$root\android"
gradle assembleDebug
Pop-Location
Copy-Item "$root\android\app\build\outputs\apk\debug\app-debug.apk" "$root\dist\BioBuzzPhotoLink.apk" -Force
Write-Host "Done: $root\dist"
