param(
    [string]$RepoPath = "C:\Users\it.synoviasupport\Desktop\dev\Fusion_Flow_V2_BKD"
)

$ErrorActionPreference = "Stop"

function Read-Default {
    param(
        [string]$Prompt,
        [string]$Default = ""
    )
    if ($Default) {
        $value = Read-Host "$Prompt [$Default]"
        if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
        return $value.Trim()
    }
    $value = Read-Host $Prompt
    if ($null -eq $value) { return "" }
    return $value.Trim()
}

function Read-SecretPlain {
    param([string]$Prompt)
    $secure = Read-Host $Prompt -AsSecureString
    if ($secure.Length -eq 0) { return "" }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if (-not (Test-Path -LiteralPath $RepoPath)) {
    throw "Repo path not found: $RepoPath"
}

$pythonExe = Join-Path $RepoPath ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = "python"
}

Write-Host ""
Write-Host "Fusion Flow V2 - Submit Consignment + Goods" -ForegroundColor Cyan
Write-Host "Repo: $RepoPath"
Write-Host ""

$env:AZURE_SQL_SERVER = Read-Default "AZURE_SQL_SERVER" $env:AZURE_SQL_SERVER
$env:AZURE_SQL_DATABASE = Read-Default "AZURE_SQL_DATABASE" ($(if ($env:AZURE_SQL_DATABASE) { $env:AZURE_SQL_DATABASE } else { "Fusion_TSS" }))
$env:AZURE_SQL_USERNAME = Read-Default "AZURE_SQL_USERNAME" $env:AZURE_SQL_USERNAME

$dbPassword = Read-SecretPlain "AZURE_SQL_PASSWORD"
if ($dbPassword) { $env:AZURE_SQL_PASSWORD = $dbPassword }

$env:TENANT_CODE = Read-Default "TENANT_CODE" ($(if ($env:TENANT_CODE) { $env:TENANT_CODE } else { "BKD" }))
$env:CLIENT_CODE = Read-Default "CLIENT_CODE" ($(if ($env:CLIENT_CODE) { $env:CLIENT_CODE } else { "BKD" }))

Write-Host ""
Write-Host "TSS API values are fallback only if Admin Settings/AppConfiguration are empty." -ForegroundColor Yellow
$env:TSS_API_BASE_URL = Read-Default "TSS_API_BASE_URL" $env:TSS_API_BASE_URL
$env:TSS_API_USERNAME = Read-Default "TSS_API_USERNAME" $env:TSS_API_USERNAME

$tssPassword = Read-SecretPlain "TSS_API_PASSWORD"
if ($tssPassword) { $env:TSS_API_PASSWORD = $tssPassword }

$env:TSS_API_ACT_AS = Read-Default "TSS_API_ACT_AS (optional)" $env:TSS_API_ACT_AS

Write-Host ""
$consignmentIds = Read-Default "SUBMIT_PIPELINE_CONSIGNMENT_IDS (optional, comma-separated)" $env:SUBMIT_PIPELINE_CONSIGNMENT_IDS
if ($consignmentIds) {
    $env:SUBMIT_PIPELINE_CONSIGNMENT_IDS = $consignmentIds
}

$skipFinalSubmit = Read-Default "Skip final consignment submit? yes/no" "no"
$env:SUBMIT_PIPELINE_SKIP_SUPDECS = "1"
if ($skipFinalSubmit.Trim().ToLowerInvariant() -in @("y", "yes", "1", "true")) {
    $env:SUBMIT_PIPELINE_SKIP_CONSIGNMENT_SUBMIT = "1"
}
else {
    Remove-Item Env:\SUBMIT_PIPELINE_SKIP_CONSIGNMENT_SUBMIT -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Running submit_pipeline.py..." -ForegroundColor Cyan
Push-Location $RepoPath
try {
    & $pythonExe (Join-Path $RepoPath "scripts\submit_pipeline.py")
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
