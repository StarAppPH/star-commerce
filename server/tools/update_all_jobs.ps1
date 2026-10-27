# Updates ALL Cloud Run jobs of a service with the service's image and env vars.
#
# A manual re-sync: cloudbuild.yaml already does this on every push to main.
# Use it when the jobs must follow a change that triggered no build (an env
# var edited in the console, say). The sync itself is updateJobsFromService.py
# (the same script the build runs), so both pick the same jobs (<service>-*,
# not *-once, only jobs on the service's own image repository) and both copy
# multiline env values intact (--env-vars-file).

# The region this repo deploys to (create-star-app cloud-setup may change it).
# >>> star-app per-repo: region
$Region = "asia-east1"
# <<< star-app per-repo: region

# Shared pickers / cache / confirmation (see _gcloudTarget.ps1).
. (Join-Path $PSScriptRoot "_gcloudTarget.ps1")

$SyncScript = Join-Path $PSScriptRoot "updateJobsFromService.py"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Exit-WithPause "python not found on PATH. It runs $SyncScript."
}

# Pick project and service. Never changes the global gcloud default: every
# gcloud call passes --project=$Project explicitly.
$Project = Select-GcloudProject
$source = Select-GcloudService -Project $Project -Region $Region

# Read-only: what the jobs will get from the service.
Write-Host "Reading service/$source (project: $Project, region: $Region)..."
$info = Get-ServiceDeployInfo -Project $Project -Service $source -Region $Region

# Read-only preview: the jobs the sync would update, and any it skips.
Write-Host ""
Write-Host "Previewing the jobs to update (dry run, nothing is written)..."
$preview = @(& python "$SyncScript" --project=$Project --region=$Region --service=$source --dry-run)
$previewCode = $LASTEXITCODE
$preview | ForEach-Object { Write-Host $_ }
if ($previewCode -ne 0) {
    Exit-WithPause "The dry run failed (exit code $previewCode). Nothing was changed." -Code $previewCode
}
$jobLines = @($preview | Where-Object { $_ -like "Would update *" })
if ($jobLines.Count -eq 0) {
    Exit-WithPause "No jobs to update." -Code 0
}

# Single review step before anything is written (the dry run above printed
# the image and the env var names)
$summary = @(
    "Project:  $Project",
    "Service:  $source",
    "Region:   $Region",
    "Deployed: $($info.DeployTime)",
    "ENVIRONMENT=$($info.Environment)"
)
if ($info.ServiceProjectId -and $info.ServiceProjectId -ne $Project) {
    $summary += "WARNING: service env GOOGLE_CLOUD_PROJECT_ID=$($info.ServiceProjectId) does not match selected project $Project"
}
$summary += "Jobs to UPDATE ($($jobLines.Count)) - env vars and image overwritten:"
foreach ($line in $jobLines) {
    $summary += "  - $($line.Substring('Would update '.Length))"
}
Confirm-Deploy -Lines $summary
Save-LastTarget -Project $Project -Service $source

# Update the jobs (no --image: the script gives them the service's image).
Write-Host ""
python "$SyncScript" --project=$Project --region=$Region --service=$source
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Exit-WithPause "All jobs updated." -Code 0
}
Exit-WithPause "updateJobsFromService.py exited with code $code (1: a job update failed, 2: nothing was updated)." -Code $code
