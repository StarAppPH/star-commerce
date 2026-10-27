# Runs one job_*.py once on Cloud Run, using the service's image and env vars:
# creates (or updates a leftover) temporary job named <service>-<jobfile>-once,
# executes it, waits, prints its logs and status, and deletes it after a
# successful run. On failure the job is kept for inspection and the console
# link, re-run command and delete command are printed.
#
# Cloud Run jobs are never listed or picked here - only job FILES are. The job
# name is always derived from the file and always ends in -once, so nothing
# this script creates, executes or deletes can be an existing scheduled job.

# The region this repo deploys to (create-star-app cloud-setup may change it).
# >>> star-app per-repo: region
$Region = "asia-east1"
# <<< star-app per-repo: region

# Shared pickers / cache / confirmation / naming (see _gcloudTarget.ps1).
. (Join-Path $PSScriptRoot "_gcloudTarget.ps1")

# Pick project and service. Never changes the global gcloud default: every
# gcloud call below passes --project=$Project explicitly.
$Project = Select-GcloudProject
$source = Select-GcloudService -Project $Project -Region $Region

# Pick the job file; the temporary job name follows the shared naming rule.
# Files whose scheduled job already exists in Cloud Run are hidden first
# (read-only listing); "a" shows them all.
$jobFile = Select-JobFile -Service $source -Suffix "once" -HideExistingJobs -Project $Project -Region $Region
$jobShortName = $jobFile -replace "^job_", "" -replace "\.py$", ""
if (-not $jobShortName) {
    Exit-WithPause "Job file $jobFile has no name after the job_ prefix - rename it (e.g. job_myTask.py)."
}
$target = Get-JobName -Service $source -JobFile $jobFile -Suffix "once"
$scheduledName = Get-JobName -Service $source -JobFile $jobFile
# The exact file chosen above, in server/ (the only file this script may delete).
$jobFilePath = Join-Path (Split-Path -Parent $PSScriptRoot) $jobFile
Write-Host "Job name: $target"

# Everything printed on the failure paths, built from $target only.
$consoleUrl = "https://console.cloud.google.com/run/jobs/details/$Region/$target/executions?project=$Project"
$rerunCommand = "gcloud run jobs execute $target --project=$Project --region=$Region --wait"
$deleteCommand = "gcloud run jobs delete $target --project=$Project --region=$Region --quiet"

function Write-KeptJobHelp {
    param([string]$Reason, [switch]$MayNotExist)
    # Shown whenever the script stops while the -once job may still exist.
    Write-Host ""
    Write-Host $Reason
    if ($MayNotExist) {
        Write-Host "The temporary job $target may not have been created. If it exists, inspect, re-run or delete it with:"
    } else {
        Write-Host "The temporary job $target was KEPT. Inspect, re-run or delete it with:"
    }
    Write-Host "  Console: $consoleUrl"
    Write-Host "  Re-run:  $rerunCommand"
    Write-Host "  Delete:  $deleteCommand"
}

function Remove-LocalJobFileIfWanted {
    param([Parameter(Mandatory = $true)][string]$Path)
    # Offered only after a successful run (job executed AND the -once job
    # deleted). Default is NO: only an exact y/Y deletes, and only the file
    # chosen by Select-JobFile is ever touched. Read-Host is cast to [string]
    # so a $null (non-console stdin) keeps the file.
    $answer = [string](Read-Host "Delete the local job file ${Path}? (y/N)")
    if ($answer.Trim() -notin @("y", "Y")) {
        Write-Host "Kept $Path."
        return
    }
    $dir = Split-Path -Parent $Path
    $name = Split-Path -Leaf $Path
    $isTracked = $false
    if (Get-Command git -ErrorAction SilentlyContinue) {
        git -C $dir ls-files --error-unmatch -- $name 2>$null | Out-Null
        $isTracked = ($LASTEXITCODE -eq 0)
    }
    if (-not $isTracked) {
        try {
            Remove-Item -LiteralPath $Path -ErrorAction Stop
            Write-Host "Deleted $Path (it was not tracked in git, so it is gone for good)."
        } catch {
            Write-Host "Could not delete ${Path}: $($_.Exception.Message)"
        }
        return
    }
    # Tracked: remove it from git too. git rm refuses a file with uncommitted
    # changes, so local edits are never lost. The commit names only this path,
    # so other staged work stays out of it, and it is made on main only (on any
    # other branch the deletion is just staged). Nothing is pushed.
    git -C $dir rm -q -- $name
    if ($LASTEXITCODE -ne 0) {
        Write-Host "git rm failed (see above) - kept $Path."
        return
    }
    $branch = [string](git -C $dir branch --show-current)
    if ($branch.Trim() -ne "main") {
        Write-Host "WARNING: the checkout is on '$branch', not main. Deleted $Path and staged the deletion, but did not commit it."
        return
    }
    git -C $dir commit -q -m "Remove $name after one-off run" -- $name
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Deleted $Path and staged the deletion, but git commit failed (see above) - commit it yourself."
        return
    }
    Write-Host "Deleted $Path and committed the removal on main. Push when ready."
}

# Fetch env vars, image and deploy time from the service (read-only)
Write-Host ""
Write-Host "Copying env vars from service/$source to job/$target (project: $Project, region: $Region)..."
Write-Host ""
$info = Get-ServiceDeployInfo -Project $Project -Service $source -Region $Region

# Leftover -once job from a failed run? Scheduled sibling? (expected-failure
# probes, so stderr is hidden). The sibling probe is best-effort, for the
# summary note only: a scheduled job created by hand may be named
# differently (e.g. warehouse-update-settings vs warehouse-updatesettings).
gcloud run jobs describe $target --project=$Project --region=$Region --format="value(metadata.name)" 2>$null | Out-Null
$jobExists = ($LASTEXITCODE -eq 0)
gcloud run jobs describe $scheduledName --project=$Project --region=$Region --format="value(metadata.name)" 2>$null | Out-Null
$scheduledExists = ($LASTEXITCODE -eq 0)

# Single review step before anything is written
if ($jobExists) {
    $jobLine = "Job:      $target  (leftover from a previous run -> will be UPDATED and re-executed)"
} else {
    $jobLine = "Job:      $target  (NEW temporary job)"
}
$summary = @(
    "Project:  $Project",
    "Service:  $source",
    "Region:   $Region",
    $jobLine,
    "Image:    $($info.Image)",
    "Deployed: $($info.DeployTime)",
    "Command:  python $jobFile",
    "Env vars: $($info.EnvCount) copied from $source",
    "ENVIRONMENT=$($info.Environment)"
)
if ($info.ServiceProjectId -and $info.ServiceProjectId -ne $Project) {
    $summary += "WARNING: service env GOOGLE_CLOUD_PROJECT_ID=$($info.ServiceProjectId) does not match selected project $Project"
}
if ($scheduledExists) {
    $summary += "Note: a scheduled job $scheduledName exists; this one-off run is separate and will not touch it."
}
$summary += "After a successful run the job is DELETED. On failure it is kept for inspection."
$summary += "After success you will be asked whether to delete the local job file (if tracked in git, the removal is committed on main, never pushed)."
Confirm-Deploy -Lines $summary -TypeToConfirm $jobShortName
Save-LastTarget -Project $Project -Service $source

# Deterministic update or create; stop here if gcloud fails
if ($jobExists) {
    Write-Host "Updating job $target..."
    gcloud run jobs update $target --project=$Project --region=$Region --set-env-vars "$($info.EnvVars)" --image="$($info.Image)" --command="python" --args="$jobFile"
} else {
    Write-Host "Creating job $target..."
    gcloud run jobs create $target --project=$Project --region=$Region --set-env-vars "$($info.EnvVars)" --image="$($info.Image)" --command="python" --args="$jobFile"
}
if ($LASTEXITCODE -ne 0) {
    if ($jobExists) {
        Write-KeptJobHelp "gcloud run jobs update failed (exit code $LASTEXITCODE). Nothing was executed."
    } else {
        Write-KeptJobHelp "gcloud run jobs create failed (exit code $LASTEXITCODE). Nothing was executed." -MayNotExist
    }
    Exit-WithPause
}

# Newest execution BEFORE running (empty for a new job). A leftover job may
# carry old executions; the one we judge below must differ from this one.
$prevExecRaw = gcloud run jobs executions list --job=$target --project=$Project --region=$Region --limit=1 --sort-by="~metadata.creationTimestamp" --format="value(metadata.name)"
$prevExec = ""
if ($LASTEXITCODE -eq 0 -and $prevExecRaw) { $prevExec = (@($prevExecRaw)[0]).Trim() }

# Execute and wait. The exit code of 'execute --wait' is not trusted on its
# own: the execution's Completed condition below is the verdict.
$startedAt = (Get-Date).ToUniversalTime().ToString("o")
Write-Host ""
Write-Host "Executing $target (started $startedAt, waiting for completion)..."
gcloud run jobs execute $target --project=$Project --region=$Region --wait
Write-Host "gcloud run jobs execute exit code: $LASTEXITCODE"

# Newest execution AFTER running. It must exist and must not be the one seen
# before the execute, otherwise no new execution was created and an old
# Completed=True execution must never pass as this run.
$execRaw = gcloud run jobs executions list --job=$target --project=$Project --region=$Region --limit=1 --sort-by="~metadata.creationTimestamp" --format="value(metadata.name)"
$execName = ""
if ($LASTEXITCODE -eq 0 -and $execRaw) { $execName = (@($execRaw)[0]).Trim() }
if (-not $execName -or $execName -eq $prevExec) {
    Write-KeptJobHelp "No new execution was created for $target (newest execution: '$execName', before the run: '$prevExec')."
    Exit-WithPause
}
# Status as JSON, filtered here: success iff the Completed condition is True.
# failedCount is omitted from the JSON when it is zero.
$statusJson = gcloud run jobs executions describe $execName --project=$Project --region=$Region --format="json(status.succeededCount,status.failedCount,status.conditions,status.completionTime,metadata.creationTimestamp)"
if ($LASTEXITCODE -ne 0 -or -not $statusJson) {
    Write-KeptJobHelp "Could not describe execution $execName."
    Exit-WithPause
}
$completed = ""
$succeededCount = "0"
$failedCount = "0"
$execCreated = ""
try {
    $status = ($statusJson -join "`n") | ConvertFrom-Json
    $completedCondition = @($status.status.conditions | Where-Object { $_.type -eq "Completed" })
    if ($completedCondition.Count -gt 0) { $completed = [string]$completedCondition[0].status }
    if ($status.status.succeededCount) { $succeededCount = [string]$status.status.succeededCount }
    if ($status.status.failedCount) { $failedCount = [string]$status.status.failedCount }
    if ($status.metadata.creationTimestamp) { $execCreated = [string]$status.metadata.creationTimestamp }
} catch {
    Write-KeptJobHelp "Could not parse the status of execution $execName ($($_.Exception.Message))."
    Exit-WithPause
}
# Belt-and-braces only (the name comparison above is the real gate): an
# execution created before $startedAt, with a 5 min clock-skew allowance,
# cannot be this run.
$isNewExecution = $true
if ($execCreated) {
    try {
        $createdUtc = [datetime]::Parse($execCreated, [cultureinfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AdjustToUniversal)
        $startedUtc = [datetime]::Parse($startedAt, [cultureinfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AdjustToUniversal)
        if ($createdUtc -lt $startedUtc.AddMinutes(-5)) { $isNewExecution = $false }
    } catch {
        Write-Host "Could not compare execution time '$execCreated' with '$startedAt' ($($_.Exception.Message))"
    }
}
if (-not $isNewExecution) {
    Write-KeptJobHelp "The newest execution $execName ($execCreated) predates this run - no new execution was started."
    Exit-WithPause
}

# Logs of this execution only. The filter is one string; PowerShell 5.1
# passes inner double quotes to gcloud only when they are backslash-escaped.
# The time bound is generous (2 min before start) so local clock skew cannot
# hide the first lines; the execution_name label is what selects the lines.
# Logs lag completion by a few seconds, so retry a few times when empty.
# Read newest-first so the limit keeps the END of a long log (where a
# traceback lands), then reverse for chronological printing.
$logSince = ([datetime]::Parse($startedAt, [cultureinfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AdjustToUniversal)).AddMinutes(-2).ToString("o")
$filter = 'resource.type="cloud_run_job" AND resource.labels.job_name="' + $target + '" AND labels."run.googleapis.com/execution_name"="' + $execName + '" AND timestamp>="' + $logSince + '"'
$escapedFilter = $filter -replace '"', '\"'
$logLimit = 1000
Write-Host ""
Write-Host "==================== LOGS: $execName ===================="
$logLines = @()
for ($attempt = 1; $attempt -le 4; $attempt++) {
    $logRaw = gcloud logging read $escapedFilter --project=$Project --limit=$logLimit --order=desc --format="value(timestamp,severity,textPayload)"
    if ($LASTEXITCODE -ne 0) { break }
    $logLines = @($logRaw | Where-Object { $null -ne $_ -and $_.ToString().Trim() -ne "" })
    if ($logLines.Count -gt 0) { break }
    if ($attempt -lt 4) { Start-Sleep -Seconds 5 }
}
if ($logLines.Count -eq 0) {
    Write-Host "(no log lines yet - see $consoleUrl)"
} else {
    [array]::Reverse($logLines)
    if ($logLines.Count -ge $logLimit) {
        Write-Host "(showing the last $logLimit lines - see $consoleUrl for the rest)"
    }
    foreach ($line in $logLines) { Write-Host $line }
}
Write-Host "=============================================================="
Write-Host ""
# A task attempt can crash and be retried while the execution still succeeds,
# so the log above and this status line are both worth reading.
Write-Host "Execution: $execName  Completed=$completed  succeeded=$succeededCount  failed=$failedCount"

if ($completed -ne "True") {
    if ($completed -eq "False") {
        Write-KeptJobHelp "Execution $execName did NOT complete successfully."
    } else {
        Write-KeptJobHelp "Execution $execName did not report completion (Completed='$completed'; it may still be running)."
    }
    Exit-WithPause
}

# Success: the temporary job has done its work; remove it.
Write-Host ""
Write-Host "Execution succeeded. Deleting temporary job $target..."
$deleted = Remove-OnceJob -Project $Project -Region $Region -Target $target
if (-not $deleted) {
    Write-KeptJobHelp "gcloud run jobs delete failed (exit code $LASTEXITCODE)."
    Exit-WithPause
}
Write-Host "Deleted $target."

# One-off jobs are often throwaway backfills: offer to remove the file too.
Write-Host ""
Remove-LocalJobFileIfWanted -Path $jobFilePath

Write-Host ""
Write-Host "Done."
Read-Host "Press Enter to exit"
