#requires -Version 5
<#
Quick smoke check for a running companion stack on Windows.
Doesn't start anything — just probes the public surfaces and reports
which layer is live and which isn't.

Usage:
  .\scripts\smoke.ps1

Override URLs via env vars:
  $env:ZEROCLAW_URL  = "http://127.0.0.1:8080"
  $env:COMPANION_URL = "http://127.0.0.1:9181"
  $env:TTS_URL       = "http://127.0.0.1:9880"
#>

$ErrorActionPreference = 'Continue'

$ZeroclawUrl  = if ($env:ZEROCLAW_URL)  { $env:ZEROCLAW_URL }  else { 'http://127.0.0.1:8080' }
$CompanionUrl = if ($env:COMPANION_URL) { $env:COMPANION_URL } else { 'http://127.0.0.1:9181' }
$TtsUrl       = if ($env:TTS_URL)       { $env:TTS_URL }       else { 'http://127.0.0.1:9880' }

function Heading($s) { Write-Host "`n── $s ──" -ForegroundColor Cyan }
function Ok($s)      { Write-Host "  ✓ $s" -ForegroundColor Green }
function Bad($s)     { Write-Host "  ✗ $s" -ForegroundColor Red }
function Warn($s)    { Write-Host "  ! $s" -ForegroundColor Yellow }
function Note($s)    { Write-Host "  - $s" -ForegroundColor DarkGray }

function Probe($label, $url, $expect) {
  try {
    $body = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 5 -ErrorAction Stop
    if ($body.Content -like "*$expect*") {
      Ok "$label  ($url)"
    } else {
      $snippet = ($body.Content -as [string]).Substring(0, [Math]::Min(80, $body.Content.Length))
      Warn "$label reachable but body unexpected: $snippet"
    }
  } catch {
    Bad "$label unreachable  ($url)"
  }
}

Heading "zeroclaw upstream"
Probe "/health" "$ZeroclawUrl/health" "ok"

Heading "companion-server"
Probe "/health"     "$CompanionUrl/health"     "ok"
Probe "/api/status" "$CompanionUrl/api/status" '"ok":true'

Heading "TTS port"
try {
  $resp = Invoke-WebRequest -UseBasicParsing -Uri "$TtsUrl/health" -TimeoutSec 5 -ErrorAction Stop
  Ok "/health reachable"
  Write-Host ("    " + $resp.Content.Substring(0, [Math]::Min(200, $resp.Content.Length)))
} catch {
  Bad "/health unreachable  ($TtsUrl)"
}

Heading "synthesis round trip (TTS only)"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("companion-smoke-{0}.wav" -f ([guid]::NewGuid().ToString('N')))
try {
  $body = '{"text":"smoke test","language":"en"}'
  Invoke-WebRequest -UseBasicParsing -Uri "$TtsUrl/tts" `
    -Method POST `
    -ContentType 'application/json' `
    -Body $body `
    -TimeoutSec 30 `
    -OutFile $tmp `
    -ErrorAction Stop | Out-Null
  $size = (Get-Item $tmp).Length
  if ($size -gt 0) {
    Ok "/tts produced $size bytes  ($tmp)"
  } else {
    Bad "/tts produced an empty file"
  }
} catch {
  Bad "/tts did not produce audio: $($_.Exception.Message)"
} finally {
  if (Test-Path $tmp) { Remove-Item $tmp -ErrorAction SilentlyContinue }
}

Heading "Pulse"
try {
  $resp = Invoke-WebRequest -UseBasicParsing -Uri "$CompanionUrl/api/pulse/status" -TimeoutSec 5 -ErrorAction Stop
  if ($resp.Content -like '*"collectors"*') {
    Ok "/api/pulse/status reachable (Pulse enabled)"
  } else {
    Warn "Pulse appears disabled or returned an unexpected body"
  }
} catch {
  Note "Pulse not enabled in companion.toml (or unreachable)"
}

Write-Host ""
Write-Host "smoke check complete" -ForegroundColor Green
