<#
.SYNOPSIS
    The five-step walkthrough from docs/demo.md, end to end. The twin of scripts/demo.sh.

.DESCRIPTION
    1, 2  POST /v1/jobs                     submit, get a job id back
    3     GET  /v1/jobs/{job_id}            poll until the status is terminal
          GET  /v1/jobs                     everything this caller has submitted
    4     GET  /v1/artifacts?job_id=...     what that job produced
    5     GET  /v1/artifacts/{id}/content   the bytes, written to a file

    There is no Idempotency-Key header any more (scope override item 2), so two runs of this
    script are two jobs. X-User-Id is optional (scope override item 1: no authentication, and
    an absent header becomes APP_DEFAULT_PRINCIPAL_ID); it is sent anyway so the script shows
    where identity goes, at the credential position rather than in the body.

.EXAMPLE
    ./scripts/demo.ps1
    ./scripts/demo.ps1 -BaseUrl http://localhost:8000 -UserId u_demo -Out lesson.mp4
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$UserId = "u_demo",
    [string]$Out = "lesson.mp4",
    [int]$PollAttempts = 60,
    [int]$PollSeconds = 2
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")
$headers = @{ "X-User-Id" = $UserId }

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "=== $Text"
}

function Write-Json {
    param($Value)
    Write-Host ($Value | ConvertTo-Json -Depth 8 -Compress)
}

# One call. Returns the parsed body; on an HTTP error prints the error envelope and stops.
function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        $Body = $null
    )
    $uri = "$BaseUrl$Path"
    Write-Host "$Method $Path"
    try {
        if ($null -ne $Body) {
            $json = $Body | ConvertTo-Json -Depth 8 -Compress
            return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers `
                -ContentType "application/json" -Body $json
        }
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers
    }
    catch {
        $status = $null
        if ($null -ne $_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
        }
        Write-Host "  HTTP $status"
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            Write-Host "  $($_.ErrorDetails.Message)"
        }
        if ($status -eq 429) {
            throw "admission refused this job: three are already QUEUED or RUNNING (D090)"
        }
        throw "$Method $Path failed with HTTP $status"
    }
}

Write-Host "demo: base url $BaseUrl, caller $UserId"

# --------------------------------------------------------------------------- 0. is it up

Write-Step "0. GET /health"
$health = Invoke-Api -Method GET -Path "/health"
Write-Json $health

# --------------------------------------------------------------------------- 1, 2. submit

Write-Step "1, 2. POST /v1/jobs"
$request = @{
    instruction = "why do atoms form covalent bonds"
    context     = @(@{ kind = "LEVEL"; text = "grade 9" })
}
$job = Invoke-Api -Method POST -Path "/v1/jobs" -Body $request
Write-Json $job

$jobId = $job.job_id
if (-not $jobId) { throw "no job id in the response" }
Write-Host "job id: $jobId"

# --------------------------------------------------------------------------- 3. poll

Write-Step "3. GET /v1/jobs/$jobId until it is terminal"
$terminal = @("SUCCEEDED", "FAILED", "CANCELLED")
$attempt = 0
while ($attempt -lt $PollAttempts) {
    $attempt++
    $job = Invoke-Api -Method GET -Path "/v1/jobs/$jobId"
    $percent = 0
    if ($null -ne $job.progress) { $percent = $job.progress.percent }
    Write-Host "  attempt ${attempt}: $($job.status) $($job.stage) $percent%"
    if ($terminal -contains $job.status) { break }
    Start-Sleep -Seconds $PollSeconds
}

Write-Json $job

if ($job.status -ne "SUCCEEDED") {
    if ($terminal -contains $job.status) {
        throw "the job ended $($job.status); the document above holds failure.code"
    }
    throw "the job was still $($job.status) after $PollAttempts polls"
}

Write-Step "3b. GET /v1/jobs -- everything this caller has submitted"
$jobs = Invoke-Api -Method GET -Path "/v1/jobs"
Write-Json $jobs

# --------------------------------------------------------------------------- 4. artifacts

Write-Step "4. GET /v1/artifacts?job_id=$jobId"
$artifacts = Invoke-Api -Method GET -Path "/v1/artifacts?job_id=$jobId"
Write-Json $artifacts

$artifactId = $null
if ($artifacts.items -and $artifacts.items.Count -gt 0) {
    $artifactId = $artifacts.items[0].artifact_id
}
if (-not $artifactId) { throw "the job succeeded but produced no artifact" }
Write-Host "artifact id: $artifactId"

# --------------------------------------------------------------------------- 5. the video

Write-Step "5. GET /v1/artifacts/$artifactId/content -> $Out"
try {
    Invoke-WebRequest -Method GET -Uri "$BaseUrl/v1/artifacts/$artifactId/content" `
        -Headers $headers -OutFile $Out | Out-Null
}
catch {
    $status = $null
    if ($null -ne $_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    if (Test-Path $Out) { Remove-Item $Out -Force }
    throw "downloading the artifact failed with HTTP $status"
}

$bytes = (Get-Item $Out).Length
Write-Host ""
Write-Host "demo: wrote $Out ($bytes bytes). Play it."
