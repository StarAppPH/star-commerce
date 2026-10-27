# Creates or updates a Cloud Run job of a service (and optionally its Cloud
# Scheduler trigger), with the service's image, env vars and service account.
#
# The pickers and the review step live here; the write itself is
# createOrUpdateJob.py (the same script create-star-app cloud-setup runs for
# the <service>-predeploy job), so both create jobs the same way: the env
# vars go through --env-vars-file (multiline values intact) and a service
# whose env vars reference secrets is refused before anything is written.

# The region this repo deploys to (create-star-app cloud-setup may change it).
# >>> star-app per-repo: region
$Region = "asia-east1"
# <<< star-app per-repo: region

# Shared pickers / cache / confirmation (see _gcloudTarget.ps1).
. (Join-Path $PSScriptRoot "_gcloudTarget.ps1")

$JobScript = Join-Path $PSScriptRoot "createOrUpdateJob.py"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Exit-WithPause "python not found on PATH. It runs $JobScript."
}

# Pick project and service. Never changes the global gcloud default: every
# gcloud call passes --project=$Project explicitly.
$Project = Select-GcloudProject
$source = Select-GcloudService -Project $Project -Region $Region

# The identity Cloud Scheduler uses to start the job. Empty: an existing
# scheduler keeps its account, a new one gets the service's own runtime
# account (it needs roles/run.invoker). The fleet repos keep their App
# Engine default account: "$Project@appspot.gserviceaccount.com".
# >>> star-app per-repo: scheduler service account
$SchedulerServiceAccount = ""
# <<< star-app per-repo: scheduler service account

# Pick the job file; the job name follows the shared naming rule.
$jobFile = Select-JobFile -Service $source
$target = Get-JobName -Service $source -JobFile $jobFile
# The -once suffix marks the temporary jobs of run_job_once.ps1 (the only
# jobs these scripts ever delete); a scheduled job must never carry it.
if ($target -like '*-once') {
    Exit-WithPause "Job file $jobFile would produce the name '$target'. The '-once' suffix is reserved for run_job_once.bat temporary jobs - rename the job file."
}
Write-Host "Job name: $target"

# Read-only: deploy time and ENVIRONMENT for the review step.
Write-Host ""
Write-Host "Reading service/$source (project: $Project, region: $Region)..."
$info = Get-ServiceDeployInfo -Project $Project -Service $source -Region $Region

# Existing scheduler (expected-failure probe, so stderr is hidden)
$schedulerName = "$target-scheduler"
$existingSchedule = $null
$existingTimezone = $null
$schedulerDesc = gcloud scheduler jobs describe $schedulerName --project=$Project --location=$Region --format="value(schedule,timeZone)" 2>$null
if ($LASTEXITCODE -eq 0 -and $schedulerDesc) {
    $parts = $schedulerDesc.Trim() -split "`t"
    if ($parts.Count -ge 2) {
        $existingSchedule = $parts[0].Trim()
        $existingTimezone = $parts[1].Trim()
    }
}

# Schedule prompt (createOrUpdateJob.py validates the cron fields)
Write-Host ""
Write-Host "Set a schedule (cron format: minute hour day-of-month month day-of-week)"
Write-Host "Example: 0 8 * * *  (every day at 8:00 AM)"
if ($existingSchedule) {
    Write-Host "Current schedule: $existingSchedule ($existingTimezone)"
    Write-Host ""
    $schedule = [string](Read-Host "Enter cron schedule (or press Enter to keep current, type 'skip' to skip)")
    if ($schedule -eq "") { $schedule = $existingSchedule }
    if ($schedule -eq "skip") { $schedule = "" }
} else {
    Write-Host ""
    $schedule = [string](Read-Host "Enter cron schedule (or press Enter to skip)")
}
$schedule = $schedule.Trim()

$timezone = ""
if ($schedule -ne "") {
    Write-Host "Select timezone:"
    $tzLabels = @(
        "UTC+0   (Etc/UTC)",
        "UTC+1   (Europe/Paris)",
        "UTC+2   (Europe/Athens)",
        "UTC+3   (Asia/Riyadh)",
        "UTC+4   (Asia/Dubai)",
        "UTC+5   (Asia/Karachi)",
        "UTC+6   (Asia/Dhaka)",
        "UTC+7   (Asia/Bangkok)",
        "UTC+8   (Asia/Manila) (default)",
        "UTC+9   (Asia/Tokyo)",
        "UTC+10  (Australia/Sydney)",
        "UTC+11  (Pacific/Noumea)",
        "UTC+12  (Pacific/Auckland)"
    )
    $tzValues = @(
        "Etc/UTC",
        "Europe/Paris",
        "Europe/Athens",
        "Asia/Riyadh",
        "Asia/Dubai",
        "Asia/Karachi",
        "Asia/Dhaka",
        "Asia/Bangkok",
        "Asia/Manila",
        "Asia/Tokyo",
        "Australia/Sydney",
        "Pacific/Noumea",
        "Pacific/Auckland"
    )
    for ($t = 0; $t -lt $tzLabels.Count; $t++) {
        Write-Host "  $($t + 1). $($tzLabels[$t])"
    }
    Write-Host ""
    if ($existingTimezone) {
        $tzChoice = [string](Read-Host "Select timezone number (or press Enter to keep $existingTimezone)")
        if ($tzChoice -eq "") {
            $timezone = $existingTimezone
        }
    } else {
        $tzChoice = [string](Read-Host "Select timezone number (or press Enter for UTC+8 Asia/Manila)")
        if ($tzChoice -eq "") { $tzChoice = "9" }
    }
    if ($tzChoice) {
        if ($tzChoice.Trim() -notmatch '^\d+$') {
            Exit-WithPause "Invalid selection: '$tzChoice'."
        }
        $tzIdx = [int]$tzChoice.Trim() - 1
        if ($tzIdx -lt 0 -or $tzIdx -ge $tzValues.Count) {
            Exit-WithPause "Invalid selection: '$tzChoice'."
        }
        $timezone = $tzValues[$tzIdx]
    }
}

$jobArgs = @("--project=$Project", "--region=$Region", "--service=$source", "--job-file=$jobFile")
if ($schedule -ne "") {
    $jobArgs += @("--schedule=$schedule", "--time-zone=$timezone")
    if ($SchedulerServiceAccount) {
        $jobArgs += "--scheduler-service-account=$SchedulerServiceAccount"
    }
}

# Read-only preview: validates the input and prints what would be written.
Write-Host ""
Write-Host "Previewing (dry run, nothing is written)..."
$preview = @(& python "$JobScript" @jobArgs --dry-run)
$previewCode = $LASTEXITCODE
if ($previewCode -ne 0) {
    $preview | ForEach-Object { Write-Host $_ }
    Exit-WithPause "The dry run failed (exit code $previewCode). Nothing was changed." -Code $previewCode
}

# Single review step before anything is written
$summary = @($preview | Where-Object { $_ -and $_ -notlike "Dry run:*" })
$summary += "Deployed: $($info.DeployTime)"
$summary += "ENVIRONMENT=$($info.Environment)"
if ($info.ServiceProjectId -and $info.ServiceProjectId -ne $Project) {
    $summary += "WARNING: service env GOOGLE_CLOUD_PROJECT_ID=$($info.ServiceProjectId) does not match selected project $Project"
}
Confirm-Deploy -Lines $summary
Save-LastTarget -Project $Project -Service $source

Write-Host ""
python "$JobScript" @jobArgs --yes
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Exit-WithPause "Done." -Code 0
}
Exit-WithPause "createOrUpdateJob.py exited with code $code (1: a gcloud write failed, 2: nothing was written)." -Code $code
