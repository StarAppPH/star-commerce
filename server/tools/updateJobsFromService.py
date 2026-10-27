"""Give a service's Cloud Run jobs the service's image and env vars.

Every Cloud Run job of a Star App service runs that service's image with
`python job_X.py`. cloudbuild.yaml runs this script twice: with
--only=<service>-predeploy before the deploy (that job then checks the new
image), and as its last step, after the service's new revision is ready, so
the jobs never lag behind the code the service serves. It can also be run by hand to re-sync the jobs, e.g. after an
env var was changed in the console (which triggers no build).

THIS WRITES TO GCP (`gcloud run jobs update`). Claude and its agents never run
it, not even with --dry-run (that still calls gcloud); they give the user the
command instead.

What it does:
  1. Reads the service's env vars and image (`gcloud run services describe`,
     the same field tools/_gcloudTarget.ps1's Get-ServiceDeployInfo reads).
     Only literal values can be copied: an env var that references a secret
     (valueFrom) is an error (exit 2, naming it), because --env-vars-file
     would silently drop it from the jobs. No env vars at all is an error too.
  2. Lists the jobs named `<service>-*`, skipping the temporary `*-once` jobs
     run_job_once.bat makes, and any job whose current image comes from
     another image repository than the one being deployed (a WARNING names
     it): a job is only ever moved to a newer build of its own image.
  3. Writes the env vars to a JSON file (JSON is YAML, and a multiline value
     such as a private key keeps its newlines) in a temp folder that is
     removed afterwards, and runs, per job,
     `gcloud run jobs update <job> --image=<image> --env-vars-file=<file>`.
     A failed update (gcloud exiting non-zero, hanging past the timeout or
     not starting) does not stop the others; any failure exits 1.
     --env-vars-file replaces each job's whole environment on every run, so
     a variable set only on a job is lost: the jobs mirror the service.

With --only=<job>, only that job is updated, and anything that would leave
it on another image is an error (exit 2): the job is missing (create it with
tools/create_or_update_job.bat), is a *-once job, is not named <service>-*,
has no job_*.py in --jobs-dir, or its current image is from another
repository. The update also sets the job's command to `python <its
job_*.py>` and --max-retries=0 (a check that failed fails the same way on a
retry), so a job created or edited by hand cannot run something else. The
job_*.py warnings are not printed.

It prints env var names and counts, never their values. A job_*.py with no
job is a WARNING (create it with tools/create_or_update_job.bat; new jobs and
their schedules stay manual). A job with no job_*.py is still updated, with a
note. Job names follow Get-JobName in tools/_gcloudTarget.ps1:
job_sync_Foo.py on service svc is svc-sync-foo.

Usage (from the repo root or anywhere):
  python server/tools/updateJobsFromService.py --project=<p> --region=<r>
      --service=<svc> [--image=<image>] [--only=<job>] [--dry-run]
      [--jobs-dir=<dir>]
Without --image the jobs get the image of the service's current template.

Exit codes: 0 all updated (or a dry run), 1 a job update failed, 2 nothing
was updated (bad service config, gcloud missing or a read failed).

Pure stdlib and Python 3.10-compatible on purpose: it runs in Cloud Build's
cloud-sdk image, without the app, its requirements or its env vars.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_JOBS_DIR = Path(__file__).resolve().parent.parent
JOB_FILE_GLOB = 'job_*.py'
ONCE_SUFFIX = '-once'
GCLOUD_TIMEOUT_SECONDS = 600
SERVICE_FORMAT = ('json(spec.template.spec.containers[0].env,'
                  'spec.template.spec.containers[0].image,'
                  'spec.template.spec.serviceAccountName)')
# On Windows gcloud is gcloud.cmd, and cmd.exe re-parses a .cmd's arguments
# (BatBadBut): these characters would redirect, expand or break quoting.
SHIM_SUFFIXES = ('.cmd', '.bat')
UNSAFE_SHIM_CHARS = frozenset('"%^&|<>!\r\n')
# Tab-separated: name, then the image of the job's (only) container.
JOB_LIST_FORMAT = ('value(metadata.name,'
                   'spec.template.spec.template.spec.containers[0].image)')


FAILED_TO_RUN = -1


class JobSyncError(Exception):
    """A problem that stops the run before any job is updated (exit 2)."""


def gcloud(args: list[str]) -> tuple[int, str, str]:
    """Run gcloud with args; returns (exit code, stdout, stderr).

    shutil.which resolves gcloud.cmd on Windows, which subprocess alone does
    not; an argument cmd.exe would re-parse is refused (JobSyncError). A call that hangs past GCLOUD_TIMEOUT_SECONDS or cannot start is a
    failed result (FAILED_TO_RUN), not an exception, so one bad job update
    does not stop the others. Tests replace this function with a fake.
    """
    executable = shutil.which('gcloud')
    if executable is None:
        raise JobSyncError('gcloud CLI not found on PATH.')
    if executable.lower().endswith(SHIM_SUFFIXES):
        for arg in args:
            if UNSAFE_SHIM_CHARS & set(arg):
                raise JobSyncError('Refusing to pass ' + repr(arg) + ' to ' + executable
                                   + ': cmd.exe would re-parse any of " % ^ & | < > ! or a '
                                   'line break in it.')
    try:
        completed = subprocess.run(
            [executable, *args], capture_output=True, text=True,
            timeout=GCLOUD_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return FAILED_TO_RUN, '', 'timed out after ' + str(GCLOUD_TIMEOUT_SECONDS) + 's'
    except OSError as error:
        return FAILED_TO_RUN, '', 'could not run gcloud: ' + type(error).__name__
    return completed.returncode, completed.stdout, completed.stderr


def gcloudFailure(code: int, err: str) -> str:
    """The '(gcloud exit N): <stderr>' tail of every failure message."""
    return '(gcloud exit ' + str(code) + '): ' + err.strip()


def imageRepository(image: str) -> str:
    """The image without its @digest and :tag (a port's colon is kept)."""
    repository = image.split('@', 1)[0]
    lastSlash = repository.rfind('/')
    colon = repository.rfind(':')
    if colon > lastSlash:
        repository = repository[:colon]
    return repository


def jobNameForFile(service: str, fileName: str) -> str:
    """Twin of Get-JobName in tools/_gcloudTarget.ps1; keep in step.

    job_sync_Foo.py on service svc -> svc-sync-foo.
    """
    name = re.sub(r'^job_', '', fileName)
    name = re.sub(r'\.py\Z', '', name)
    return (service + '-' + name).replace('_', '-').lower()


def describeService(project: str, region: str, service: str,
                    runner=None) -> tuple[dict[str, str], str, str]:
    """The service's env vars as {name: value} (serviceEnv's checks), its
    image and its service account ('' for the default compute account).
    runner: the gcloud function to call (default: this module's)."""
    code, out, err = (runner or gcloud)([
        'run', 'services', 'describe', service,
        '--project=' + project, '--region=' + region,
        '--format=' + SERVICE_FORMAT])
    if code != 0:
        raise JobSyncError(
            'Could not describe service ' + service + ' in ' + project + '/'
            + region + ' ' + gcloudFailure(code, err))
    try:
        spec = json.loads(out or '{}').get('spec', {}).get('template', {}).get('spec', {})
        container = (spec.get('containers') or [{}])[0]
        account = spec.get('serviceAccountName') or ''
    except (ValueError, AttributeError, IndexError, KeyError, TypeError) as error:
        raise JobSyncError('Unreadable describe output for ' + service + ': '
                           + type(error).__name__) from None
    return serviceEnv(container.get('env') or [], service), container.get('image') or '', account


def readService(project: str, region: str, service: str) -> tuple[dict[str, str], str]:
    """Returns the service's env vars as {name: value} and its image."""
    env, image, _ = describeService(project, region, service)
    return env, image


def serviceEnv(envList: list[dict], service: str) -> dict[str, str]:
    """A service's env list as {name: value}; JobSyncError when it is empty
    or holds a secret reference (also used by createOrUpdateJob.py)."""
    if not envList:
        raise JobSyncError('No env vars found on service ' + service
                           + '; refusing to give its jobs an empty environment.')
    secretRefs = [item.get('name', '?') for item in envList if 'valueFrom' in item]
    if secretRefs:
        raise JobSyncError(
            'Service ' + service + ' has env vars that reference secrets ('
            + ', '.join(secretRefs) + '). --env-vars-file would drop them from '
            'the jobs, so no job was updated. Copy them by hand or make this '
            'script carry secret refs.')
    env = {}
    for item in envList:
        env[item['name']] = item.get('value', '')
    return env


def listJobs(project: str, region: str, service: str, runner=None) -> dict[str, str]:
    """The service's jobs, {name: current image}: named <service>-*,
    without the *-once ones. runner: as in describeService."""
    code, out, err = (runner or gcloud)([
        'run', 'jobs', 'list', '--project=' + project, '--region=' + region,
        '--format=' + JOB_LIST_FORMAT])
    if code != 0:
        raise JobSyncError('Could not list Cloud Run jobs in ' + project + '/'
                           + region + ' ' + gcloudFailure(code, err))
    prefix = service + '-'
    jobs = {}
    for line in out.splitlines():
        name, _, image = line.strip().partition('	')
        name = name.strip()
        if name.startswith(prefix) and not name.endswith(ONCE_SUFFIX):
            jobs[name] = image.strip()
    return dict(sorted(jobs.items()))


def jobsOfRepository(jobs: dict[str, str], image: str) -> list[str]:
    """The jobs whose current image is from image's repository; a WARNING
    for each other one, which is left alone."""
    repository = imageRepository(image)
    kept = []
    for job, jobImage in jobs.items():
        if imageRepository(jobImage) == repository:
            kept.append(job)
        else:
            print('WARNING: skipping ' + job + ': its image ' + (jobImage or '<none>')
                  + ' is not from ' + repository)
    return kept


def onlyJob(allJobs: dict[str, str], job: str, service: str, image: str,
            fileByJob: dict[str, str]) -> list[str]:
    """[job] for --only, after checking it exists, has its job_*.py and runs
    image's repository; anything else raises JobSyncError, so the caller
    never goes on with the job still on its old image."""
    if not job.startswith(service + '-') or job.endswith(ONCE_SUFFIX):
        raise JobSyncError('--only=' + job + ' is not a job of service ' + service
                           + ' (it must be named ' + service + '-*, not *' + ONCE_SUFFIX + ').')
    if job not in allJobs:
        raise JobSyncError('Cloud Run job ' + job + ' does not exist. Create it once with '
                           'tools/create_or_update_job.bat (pick its job_*.py, no schedule), '
                           'then rerun the build.')
    if job not in fileByJob:
        raise JobSyncError('Cloud Run job ' + job + ' has no job_*.py in the jobs folder '
                           '(the file whose name maps to it), so its command cannot be set.')
    if not jobsOfRepository({job: allJobs[job]}, image):
        raise JobSyncError('Cloud Run job ' + job + ' runs an image from another repository '
                           'than ' + imageRepository(image) + '; it was not updated.')
    return [job]


def jobFileNames(jobsDir: Path) -> list[str]:
    return sorted(path.name for path in jobsDir.glob(JOB_FILE_GLOB) if path.is_file())


def jobCommandArgs(fileName: str) -> list[str]:
    """The flags that make a job run `python <fileName>` once per execution
    (an --only update here; every create/update in createOrUpdateJob.py)."""
    return ['--command=python', '--args=' + fileName, '--max-retries=0']


def updateJob(project: str, region: str, job: str, image: str, envFile: Path,
              extraArgs: list[str] = ()) -> bool:
    code, out, err = gcloud([
        'run', 'jobs', 'update', job, '--project=' + project, '--region=' + region,
        '--image=' + image, '--env-vars-file=' + str(envFile), *extraArgs, '--quiet'])
    if code != 0:
        print('  FAILED ' + job + ' ' + gcloudFailure(code, err))
        return False
    print('  Updated ' + job)
    return True


def parseArgs(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Give a service's Cloud Run jobs the service's image and env vars (writes to GCP).")
    parser.add_argument('--project', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--image', default='',
                        help="image for the jobs (default: the service's current image)")
    parser.add_argument('--only', default='',
                        help='update just this job (e.g. <service>-predeploy); a missing job is an error')
    parser.add_argument('--dry-run', action='store_true',
                        help='read and report only; update nothing')
    parser.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS_DIR,
                        help='folder holding the job_*.py files (default: server/)')
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    env, serviceImage = readService(args.project, args.region, args.service)
    image = args.image or serviceImage
    if not image:
        raise JobSyncError('Service ' + args.service + ' has no image and no --image was given.')
    print('Service ' + args.service + ': ' + str(len(env)) + ' env vars ('
          + ', '.join(sorted(env)) + ')')
    print('Image: ' + image)

    allJobs = listJobs(args.project, args.region, args.service)
    fileByJob = {jobNameForFile(args.service, name): name for name in jobFileNames(args.jobs_dir)}
    if args.only:
        jobs = onlyJob(allJobs, args.only, args.service, image, fileByJob)
    else:
        jobs = jobsOfRepository(allJobs, image)
        for jobName, fileName in sorted(fileByJob.items()):
            if jobName not in allJobs:
                print('WARNING: ' + fileName + ' has no Cloud Run job ' + jobName
                      + '; create it with tools/create_or_update_job.bat')
        if not allJobs:
            print('No Cloud Run jobs named ' + args.service + '-* found.')
    for job in jobs:
        note = '' if job in fileByJob else '  (no job_*.py in server/)'
        print(('Would update ' if args.dry_run else 'Updating ') + job + note)
    if args.dry_run:
        print('Dry run: nothing was updated.')
        return 0

    updated = failed = 0
    tempDir = Path(tempfile.mkdtemp(prefix='jobEnv'))
    try:
        envFile = tempDir / 'env.json'
        envFile.write_text(json.dumps(env, indent=2), encoding='utf-8')
        for job in jobs:
            extraArgs = jobCommandArgs(fileByJob[job]) if args.only else []
            if updateJob(args.project, args.region, job, image, envFile, extraArgs):
                updated += 1
            else:
                failed += 1
    finally:
        shutil.rmtree(tempDir, ignore_errors=True)
    print('Done. Updated ' + str(updated) + ', failed ' + str(failed) + '.')
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parseArgs(argv)
    try:
        return run(args)
    except JobSyncError as error:
        print('ERROR: ' + str(error))
        return 2


if __name__ == '__main__':
    sys.exit(main())
