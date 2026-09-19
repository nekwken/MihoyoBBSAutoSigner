# Build MihoyoBBSAutoSigner.exe
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = "C:\Users\nekwken\AppData\Local\Programs\Python\Python313\python.exe"
# regenerate icon assets first so tray/exe/settings share one source
& $py make_icon.py
& $py -m PyInstaller --noconfirm --onefile --noconsole `
    --name MihoyoBBSAutoSigner `
    --icon assets\icon.ico `
    --add-data "assets;assets" `
    main.py
Write-Host "Done: $PSScriptRoot\dist\MihoyoBBSAutoSigner.exe"
