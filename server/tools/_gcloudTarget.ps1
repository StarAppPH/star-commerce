# Shared helpers for the GCP deploy scripts in this folder. Dot-sourced by
# create_or_update_job.ps1, update_all_jobs.ps1 and run_job_once.ps1:
#
#   . (Join-Path $PSScriptRoot "_gcloudTarget.ps1")
#
# Rules these helpers enforce:
# - The global gcloud default project is never changed or used implicitly.
#   Callers pass --project=$Project on every gcloud call.
# - The last confirmed project/service is remembered per repo in
#   .last_gcloud_target.json (gitignored; also read/written by pubSubAdmin.py).
#   Enter selects the remembered entry, any number overrides it. With nothing
#   remembered a number is required.
# - Nothing is written until Confirm-Deploy gets an explicit y (or, with
#   -TypeToConfirm, the exact value it asks for).
# - Every Cloud Run job name comes from Get-JobName, so the scripts cannot
#   drift on naming, and only names ending in -once are ever deleted
#   (Remove-OnceJob).
# - Select-GcloudProject stops with a clear message when the gcloud CLI is
#   not installed, instead of a CommandNotFound wall from every gcloud call.

$LastTargetPath = Join-Path $PSScriptRoot ".last_gcloud_target.json"

function Exit-WithPause {
    param([string]$Message, [int]$Code = 1)
    if ($Message) { Write-Host $Message }
    Read-Host "Press Enter to exit" | Out-Null
    exit $Code
}

function Assert-GcloudInstalled {
    # Called before the first gcloud invocation. A missing executable throws
    # CommandNotFoundException in Windows PowerShell 5.1 and does not set
    # $LASTEXITCODE, so the checks after each gcloud call cannot catch it.
    # Not run at dot-source time on purpose: 'exit' inside a dot-sourced file
    # only ends that file, and the calling script would carry on regardless.
    if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
        Exit-WithPause "gcloud CLI not found. Install the Google Cloud SDK (https://cloud.google.com/sdk/docs/install), then run 'gcloud auth login' and try again."
    }
}

function Get-LastTarget {
    # Returns @{ project; service; serviceAccount } with "" for unknown keys.
    # A missing or unreadable file simply means "no cache".
    $target = @{ project = ""; service = ""; serviceAccount = "" }
    if (-not (Test-Path $LastTargetPath)) { return $target }
    try {
        $json = Get-Content -Path $LastTargetPath -Raw -Encoding utf8 | ConvertFrom-Json
        foreach ($key in @("project", "service", "serviceAccount")) {
            $prop = $json.PSObject.Properties[$key]
            if ($prop -and $prop.Value) { $target[$key] = [string]$prop.Value }
        }
    } catch {
        Write-Host "Ignoring unreadable $LastTargetPath ($($_.Exception.Message))"
    }
    return $target
}

function Save-LastTarget {
    param(
        [Parameter(Mandatory = $true)][string]$Project,
        [Parameter(Mandatory = $true)][string]$Service
    )
    # Merge-write so the serviceAccount key written by pubSubAdmin.py survives,
    # but only for the project it was saved for.
    $target = Get-LastTarget
    $serviceAccount = ""
    if ($target.project -eq $Project) { $serviceAccount = $target.serviceAccount }
    $ordered = [ordered]@{
        project        = $Project
        service        = $Service
        serviceAccount = $serviceAccount
    }
    try {
        $ordered | ConvertTo-Json | Set-Content -Path $LastTargetPath -Encoding utf8
    } catch {
        Write-Host "Could not write $LastTargetPath ($($_.Exception.Message))"
    }
}

function Select-FromList {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Options,
        [string]$Cached,
        [Parameter(Mandatory = $true)][string]$Prompt,
        [string]$ExtraKey,
        [string]$ExtraLabel
    )
    # Numbered picker. The cached entry (if still listed) is marked and Enter
    # selects it; any number overrides it. With -ExtraKey (a letter) one more
    # entry is offered under that letter, and typing it returns the letter
    # itself. Anything else exits non-zero so a typo can never fall through
    # to a write. Read-Host is cast to [string] because it returns $null when
    # stdin is not a console, and a $null .Trim() would skip the check
    # instead of failing it.
    $cachedIdx = -1
    if ($Cached) { $cachedIdx = [array]::IndexOf($Options, $Cached) }
    Write-Host $Title
    for ($i = 0; $i -lt $Options.Count; $i++) {
        if ($i -eq $cachedIdx) {
            Write-Host "* $($i + 1). $($Options[$i])  (last used)"
        } else {
            Write-Host "  $($i + 1). $($Options[$i])"
        }
    }
    if ($ExtraKey) { Write-Host "  $ExtraKey = $ExtraLabel" }
    Write-Host ""
    if ($cachedIdx -ge 0) {
        $choice = [string](Read-Host "$Prompt (Enter = $Cached)")
        if ($choice.Trim() -eq "") { return $Options[$cachedIdx] }
    } else {
        $choice = [string](Read-Host "$Prompt")
        if ($choice.Trim() -eq "") {
            Exit-WithPause "No selection made (no last-used entry to default to)."
        }
    }
    if ($ExtraKey -and $choice.Trim() -eq $ExtraKey) { return $ExtraKey }
    # Digits only: [int] would happily round "2.5" or parse "0x3" to a different entry.
    if ($choice.Trim() -notmatch '^\d+$') {
        Exit-WithPause "Invalid selection: '$choice'."
    }
    try {
        $idx = [int]$choice.Trim() - 1
    } catch {
        Exit-WithPause "Invalid selection: '$choice'."
    }
    if ($idx -lt 0 -or $idx -ge $Options.Count) {
        Exit-WithPause "Invalid selection: '$choice'."
    }
    return $Options[$idx]
}

function Select-GcloudProject {
    Assert-GcloudInstalled
    $last = Get-LastTarget

    # Info only: the global gcloud default is never used implicitly.
    $defaultProject = gcloud config get-value project 2>$null
    Write-Host ""
    Write-Host "gcloud default project (info only, not used): $defaultProject"
    Write-Host ""

    $projects = gcloud projects list --format="value(projectId)"
    if ($LASTEXITCODE -ne 0) {
        Exit-WithPause "Could not list projects. Run 'gcloud auth login' and try again."
    }
    $projectList = @($projects -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
    if ($projectList.Count -eq 0) {
        Exit-WithPause "No projects visible. Run 'gcloud auth login' and try again."
    }

    $project = Select-FromList -Title "Available projects:" -Options $projectList -Cached $last.project -Prompt "Select project number"
    Write-Host "Using project: $project"
    Write-Host ""
    return $project
}

function Select-GcloudService {
    param(
        [Parameter(Mandatory = $true)][string]$Project,
        [Parameter(Mandatory = $true)][string]$Region
    )
    # The cached service only applies to the project it was saved for.
    $last = Get-LastTarget
    $cachedService = ""
    if ($last.project -eq $Project) { $cachedService = $last.service }

    $services = gcloud run services list --project=$Project --region=$Region --format="value(metadata.name)"
    if ($LASTEXITCODE -ne 0) {
        Exit-WithPause "Could not list Cloud Run services in $Project/$Region. Run 'gcloud auth login' and try again."
    }
    $serviceList = @($services -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
    if ($serviceList.Count -eq 0) {
        Exit-WithPause "No Cloud Run services in $Project/$Region - wrong project?"
    }

    $service = Select-FromList -Title "Available services in ${Project}:" -Options $serviceList -Cached $cachedService -Prompt "Select service number"
    Write-Host "Using service: $service"
    Write-Host ""
    return $service
}

function Get-JobName {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][string]$JobFile,
        [string]$Suffix
    )
    # The one naming rule for every Cloud Run job these scripts touch:
    # job_sync_Foo.py on service "svc" -> svc-sync-foo. A -Suffix is appended
    # with a dash (run_job_once.ps1 passes "once" -> svc-sync-foo-once).
    $name = $JobFile -replace "^job_", "" -replace "\.py$", ""
    $jobName = ("$Service-$name" -replace "_", "-").ToLower()
    if ($Suffix) { $jobName = "$jobName-$Suffix" }
    return $jobName
}

function Get-ExistingJobNames {
    param(
        [Parameter(Mandatory = $true)][string]$Project,
        [Parameter(Mandatory = $true)][string]$Region
    )
    # READ-ONLY: the names of the Cloud Run jobs in the project and region.
    # Returns @{ Ok = $true; Names = [string[]] }, or @{ Ok = $false } when
    # they cannot be listed (a hashtable, so an empty list is not mistaken
    # for a failure: PowerShell unrolls a returned empty array to $null).
    $names = gcloud run jobs list --project=$Project --region=$Region --format="value(metadata.name)" 2>$null
    if ($LASTEXITCODE -ne 0) { return @{ Ok = $false } }
    return @{ Ok = $true; Names = @($names | Where-Object { $_ } | ForEach-Object { $_.Trim() }) }
}

function Get-VisibleJobFiles {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$JobFiles,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$ExistingJobNames
    )
    # The job files whose scheduled Cloud Run job (Get-JobName, no suffix)
    # does not exist yet: the one-off jobs. A leftover <name>-once job does
    # not count. Pure, so it can be checked without gcloud.
    return @($JobFiles | Where-Object {
        $ExistingJobNames -notcontains (Get-JobName -Service $Service -JobFile $_)
    })
}

function Select-JobFile {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [string]$Suffix,
        [switch]$HideExistingJobs,
        [string]$Project,
        [string]$Region
    )
    # Numbered picker over the job_*.py files in server/ (the parent of this
    # tools/ folder), each shown with the Cloud Run job name it maps to.
    # Deliberately not cached: a job file is chosen explicitly every time.
    # Returns the file name (e.g. job_sync_Foo.py).
    #
    # -HideExistingJobs (run_job_once.ps1 only; needs -Project and -Region)
    # first hides every file whose scheduled job already exists in Cloud Run
    # (read-only 'gcloud run jobs list'), leaving the one-off jobs, and offers
    # "a = show all jobs" to list everything (to run a scheduled job once by
    # hand). If the jobs cannot be listed, it warns and shows all. Without
    # the switch every file is listed (create_or_update_job.ps1).
    $serverDir = Split-Path -Parent $PSScriptRoot
    $jobFiles = @(Get-ChildItem -Path $serverDir -Filter "job_*.py" -Name)
    if ($jobFiles.Count -eq 0) {
        Exit-WithPause "No job_*.py files found in server/."
    }
    $shown = $jobFiles
    if ($HideExistingJobs) {
        if (-not $Project -or -not $Region) {
            Exit-WithPause "Select-JobFile -HideExistingJobs needs -Project and -Region."
        }
        $existing = Get-ExistingJobNames -Project $Project -Region $Region
        if (-not $existing.Ok) {
            Write-Host "WARNING: could not list the Cloud Run jobs in $Project ($Region); showing every job file."
        } else {
            # @(): a one-element result would come back as a bare string.
            $shown = @(Get-VisibleJobFiles -Service $Service -JobFiles $jobFiles -ExistingJobNames $existing.Names)
        }
    }
    $hiddenCount = $jobFiles.Count - $shown.Count
    if ($hiddenCount -gt 0 -and $shown.Count -eq 0) {
        Write-Host "Every job file ($hiddenCount) already has a Cloud Run job, so none is listed."
        $choice = [string](Read-Host "a = show all jobs (anything else exits)")
        if ($choice.Trim() -ne "a") { Exit-WithPause "No job selected." }
        $shown = $jobFiles
        $hiddenCount = 0
    }
    while ($true) {
        $labels = @()
        foreach ($jobFile in $shown) {
            $labels += "$jobFile  ->  $(Get-JobName -Service $Service -JobFile $jobFile -Suffix $Suffix)"
        }
        if ($hiddenCount -gt 0) {
            $label = Select-FromList -Title "Available jobs ($hiddenCount with a Cloud Run job hidden):" -Options $labels -Prompt "Select job number" -ExtraKey "a" -ExtraLabel "show all jobs"
            if ($label -eq "a") {
                $shown = $jobFiles
                $hiddenCount = 0
                continue
            }
        } else {
            $label = Select-FromList -Title "Available jobs:" -Options $labels -Prompt "Select job number"
        }
        return $shown[[array]::IndexOf($labels, $label)]
    }
}

function Get-ServiceDeployInfo {
    param(
        [Parameter(Mandatory = $true)][string]$Project,
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][string]$Region
    )
    # Read-only. Everything a job copies from its service, as a hashtable:
    #   EnvVars          name=value,... ready for --set-env-vars
    #   EnvCount         number of env vars found
    #   Image            image of the service's current template
    #   DeployTime       creation time of the latest ready revision
    #   Environment      value of ENVIRONMENT ("<not set>" if absent)
    #   ServiceProjectId value of GOOGLE_CLOUD_PROJECT_ID ("" if absent)
    # Stops with Exit-WithPause when any of it cannot be read.
    $jsonOutput = gcloud run services describe $Service --project=$Project --region=$Region --format="json(spec.template.spec.containers[0].env)"
    if ($LASTEXITCODE -ne 0) {
        Exit-WithPause "Could not describe service $Service in $Project/$Region."
    }
    $parsed = ($jsonOutput -join "`n") | ConvertFrom-Json
    $envArray = $parsed.spec.template.spec.containers[0].env

    if ($null -eq $envArray -or $envArray.Count -eq 0) {
        Exit-WithPause "No env vars found on $Service."
    }

    $pairs = @()
    $environmentValue = "<not set>"
    $serviceProjectId = ""
    Write-Host "Found $($envArray.Count) env vars."
    foreach ($env in $envArray) {
        $pairs += "$($env.name)=$($env.value)"
        if ($env.name -eq "ENVIRONMENT") { $environmentValue = $env.value }
        if ($env.name -eq "GOOGLE_CLOUD_PROJECT_ID") { $serviceProjectId = $env.value }
    }

    Write-Host ""
    $sourceImage = gcloud run services describe $Service --project=$Project --region=$Region --format="value(spec.template.spec.containers[0].image)"
    if ($LASTEXITCODE -ne 0) {
        Exit-WithPause "Could not read the image of service $Service."
    }
    $latestRevision = gcloud run services describe $Service --project=$Project --region=$Region --format="value(status.latestReadyRevisionName)"
    if ($LASTEXITCODE -ne 0) {
        Exit-WithPause "Could not read the latest revision of service $Service."
    }
    $deployTime = gcloud run revisions describe $latestRevision --project=$Project --region=$Region --format="value(metadata.creationTimestamp)"
    if ($LASTEXITCODE -ne 0) {
        Exit-WithPause "Could not describe revision $latestRevision."
    }

    return @{
        EnvVars          = ($pairs -join ",")
        EnvCount         = $envArray.Count
        Image            = $sourceImage
        DeployTime       = $deployTime
        Environment      = $environmentValue
        ServiceProjectId = $serviceProjectId
    }
}

function Confirm-Deploy {
    param(
        [Parameter(Mandatory = $true)][string[]]$Lines,
        [string]$TypeToConfirm
    )
    # Only an explicit y/Y proceeds. There is no default. With -TypeToConfirm
    # the caller names a value the user must type exactly (case-sensitive)
    # instead of y - for actions where a reflex y is too cheap. Read-Host is
    # cast to [string]: it returns $null when stdin is not a console, and a
    # $null .Trim() would skip the check - which here would mean PROCEED.
    Write-Host ""
    Write-Host "==================== REVIEW BEFORE DEPLOY ===================="
    foreach ($line in $Lines) {
        Write-Host "  $line"
    }
    Write-Host "=============================================================="
    Write-Host ""
    if ($TypeToConfirm) {
        $answer = [string](Read-Host "Type '$TypeToConfirm' to proceed (anything else aborts)")
        if ($answer.Trim() -cne $TypeToConfirm) {
            Exit-WithPause -Message "Aborted. Nothing was changed." -Code 0
        }
    } else {
        $answer = [string](Read-Host "Type y to proceed (anything else aborts)")
        if ($answer.Trim() -notin @("y", "Y")) {
            Exit-WithPause -Message "Aborted. Nothing was changed." -Code 0
        }
    }
}

function Remove-OnceJob {
    param(
        [Parameter(Mandatory = $true)][string]$Project,
        [Parameter(Mandatory = $true)][string]$Region,
        [Parameter(Mandatory = $true)][string]$Target
    )
    # Deletes the temporary job made by run_job_once.ps1 and nothing else:
    # the name must end in -once, which no scheduled job ever does. Refuses
    # (exit 1, no gcloud call) otherwise. --quiet is the global no-prompt
    # flag. Returns $true when the delete succeeded.
    if ($Target -notlike "*-once") {
        Exit-WithPause "Refusing to delete '$Target': these scripts only ever delete jobs named *-once."
    }
    gcloud run jobs delete $Target --project=$Project --region=$Region --quiet | Out-Host
    return ($LASTEXITCODE -eq 0)
}
