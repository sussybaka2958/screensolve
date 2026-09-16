$ErrorActionPreference = 'Stop'
if (-not (Test-Path .\supabase_config.json)) {
    throw 'Create supabase_config.json from supabase_config.example.json and add your public project values first.'
}
py -m pip install -r requirements.txt
py -m PyInstaller --noconfirm --clean --onefile --windowed --name ScreenSolve --version-file windows_version_info.txt ScreenSolve.pyw
if ($LASTEXITCODE -ne 0) {
    throw 'Build failed. Close every running ScreenSolve.exe window, then run build.ps1 again.'
}
Copy-Item .\supabase_config.json .\dist\supabase_config.json
if ($env:SCREENSOLVE_SIGN_CERT) {
    & signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f $env:SCREENSOLVE_SIGN_CERT .\dist\ScreenSolve.exe
    if ($LASTEXITCODE -ne 0) { throw 'Code signing failed.' }
}
$releaseArchive = '.\dist\ScreenSolve-windows-x64.zip'
Compress-Archive -Path '.\dist\ScreenSolve.exe', '.\dist\supabase_config.json' -DestinationPath $releaseArchive -Force
Write-Host 'Done. Upload dist\ScreenSolve-windows-x64.zip to GitHub Releases. It contains the EXE and its public Supabase configuration.'
