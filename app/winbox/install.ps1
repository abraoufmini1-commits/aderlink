# ADER LINK - activer le bouton Winbox sur ce PC (une seule fois, sans droits administrateur)
$ErrorActionPreference = 'Stop'
$dir = Join-Path $env:LOCALAPPDATA 'ADERLINK'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$exe = Join-Path $dir 'winbox64.exe'
if (-not (Test-Path $exe)) {
  $places = @("$env:USERPROFILE\Downloads", "$env:USERPROFILE\Desktop", "$env:USERPROFILE\Documents", "$env:USERPROFILE\OneDrive\Desktop")
  $found = Get-ChildItem -Path $places -Filter 'winbox*.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue | Sort-Object { $_.Name -notmatch '64' } | Select-Object -First 1
  if ($found) {
    Write-Host "Winbox trouve : $($found.FullName)"
    Copy-Item $found.FullName $exe
  } else {
    Write-Host 'Telechargement de Winbox depuis mikrotik.com ...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri 'https://mt.lv/winbox64' -OutFile $exe -UseBasicParsing
  }
}
$launcher = Join-Path $dir 'ouvrir-winbox.ps1'
Set-Content -Path $launcher -Encoding UTF8 -Value @'
param([string]$url)
$t = [Uri]::UnescapeDataString(($url -replace '^aderlink-winbox:(//)?', '' -replace '/+$', ''))
if ($t -notmatch '^[A-Za-z0-9\.\-:\[\]]{1,80}$') { exit 1 }
Start-Process -FilePath (Join-Path $PSScriptRoot 'winbox64.exe') -ArgumentList $t
'@
$cmd = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" `"%1`""
$k = 'HKCU:\Software\Classes\aderlink-winbox'
New-Item -Path $k -Force | Out-Null
Set-ItemProperty -Path $k -Name '(Default)' -Value 'URL:ADER LINK Winbox'
Set-ItemProperty -Path $k -Name 'URL Protocol' -Value ''
New-Item -Path "$k\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path "$k\shell\open\command" -Name '(Default)' -Value $cmd
Write-Host ''
Write-Host 'C est fait ! Le bouton Winbox d ADER LINK ouvre maintenant Winbox sur ce PC.' -ForegroundColor Green
Write-Host 'Revenez dans ADER LINK et cliquez sur "C est fait".'
