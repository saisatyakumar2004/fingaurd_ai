# End-to-end pipeline for Windows 11 / PowerShell.
# Usage:  .\scripts\run_pipeline.ps1            (online ingestion, needs SEC_USER_AGENT in .env)
#         .\scripts\run_pipeline.ps1 -Offline   (reuse cached snapshots under data\raw)
param([switch]$Offline, [string]$Agent = "mock", [string]$Mode = "full")

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Step($name, [scriptblock]$cmd) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "Step failed: $name (exit $LASTEXITCODE)" }
}

if ($Offline) { Step "Ingest (offline, cached)" { python -m src.ingestion.run --offline } }
else          { Step "Ingest (online)"          { python -m src.ingestion.run } }
Step "Validate reference data"   { python -m src.quality.validate }
Step "Generate corrupted cases"  { python -m src.corruption.generate --seed 42 }
Step "Evaluate ($Agent, $Mode)"  { python -m src.evaluation.run --mode $Mode --agent $Agent }
Step "Generate reports"          { python -m src.reporting.generate }
Write-Host "`nDone. Open reports\scorecard.html" -ForegroundColor Green
