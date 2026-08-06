<#
.SYNOPSIS
    The five-step walkthrough from docs/demo.md, end to end. The twin of scripts/demo.sh.

.DESCRIPTION
    1, 2  POST /v1/jobs                     submit, get a job id back
    3     GET  /v1/jobs/{job_id}            poll until the status is terminal
          GET  /v1/jobs                     everything this caller has submitted
    4     GET  /v1/artifacts?job_id=...     what that job produced
    5     GET  /v1/artifacts/{id}/content   the bytes, written to a file
    6     sha256 the file and name which committed lesson it is

    Step 6 is what makes this a proof rather than a demonstration. A file of roughly the right
    size proves nothing; the assertion is the whole hash against the fixture the mock backend
    serves from, and the script says which of the three things happened -- it matched, it
    matched neither, or there was no fixture directory here to compare against.

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

# The PRIMARY row, not items[0]. A successful job publishes POSTER, PRIMARY and TRANSCRIPT in
# one transaction, so the listing's order is their ids' order and the first item is a PNG about
# a third of the time.
$primary = $artifacts.items | Where-Object { $_.role -eq "PRIMARY" } | Select-Object -First 1
$artifactId = $null
if ($null -ne $primary) { $artifactId = $primary.artifact_id }
if (-not $artifactId) { throw "the job succeeded but published no PRIMARY artifact" }
Write-Host "primary artifact id: $artifactId"

# The operator log is one of the four parts custody wrote and it is not in the response above:
# the listing is LEARNER and CLEAN only, and that predicate lives in the index (D073, A6).
Write-Host "roles listed: $(($artifacts.items | ForEach-Object { $_.role }) -join ' ')"

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

# --------------------------------------------------------------------------- 6. is it a lesson

Write-Step "6. is what came back one of the committed lessons?"

$digest = (Get-FileHash -Path $Out -Algorithm SHA256).Hash.ToLower()
Write-Host "sha256: $digest"

$fixtureDir = Join-Path $PSScriptRoot "../src/app/generation/backends/fixtures"
if (-not (Test-Path $fixtureDir)) {
    Write-Host "demo: no fixture directory at $fixtureDir."
    Write-Host "demo: $Out downloaded, bytes NOT compared against a fixture."
    exit 0
}

$matched = $null
$compared = 0
foreach ($lesson in @("lesson_a.mp4", "lesson_b.mp4")) {
    $path = Join-Path $fixtureDir $lesson
    if (-not (Test-Path $path)) { continue }
    $compared++
    if ((Get-FileHash -Path $path -Algorithm SHA256).Hash.ToLower() -eq $digest) {
        $matched = $lesson
        break
    }
}

if ($compared -eq 0) {
    Write-Host "demo: found no lesson fixtures in $fixtureDir."
    Write-Host "demo: $Out downloaded, bytes NOT compared against a fixture."
    exit 0
}

if ($matched) {
    Write-Host ""
    Write-Host "demo: MATCH. $Out is byte for byte src/app/generation/backends/fixtures/$matched."
    Write-Host "demo: a job submitted over HTTP came back as that video."
    exit 0
}

Write-Host ""
Write-Host "demo: NO MATCH. $Out ($bytes bytes) is neither committed lesson."
Write-Host "demo: the request path worked; what it served is not the file the mock backend holds."
exit 1
