"""Create or update one Cloud Run job of a service, and optionally its
Cloud Scheduler trigger.

The job runs the service's image with `python <job file>`, with the
service's env vars and runtime service account. tools/create_or_update_job.ps1
(the interactive picker) and create-star-app cloud-setup (the
<service>-predeploy job) both call this script; new jobs and their schedules
are made here, and tools/updateJobsFromService.py keeps them on the
service's latest image on every build.

THIS WRITES TO GCP (`gcloud run jobs create|update`, `gcloud scheduler jobs
create|update`). Claude and its agents never run it, not even with
--dry-run (that still calls gcloud); they give the user the command instead.

What it does:
  1. Maps --job-file (a job_*.py in --jobs-dir, default server/) to the job
     name with updateJobsFromService.jobNameForFile (job_sync_Foo.py on
     service svc is svc-sync-foo). A name ending in -once is refused: that
     suffix marks run_job_once.bat's temporary jobs.
  2. Reads the service's env vars, image and service account
     (updateJobsFromService.describeService). An env var that references a
     secret (valueFrom) or no env vars at all is an error (exit 2), with the
     same wording as updateJobsFromService.py.
  3. Lists the service's jobs (updateJobsFromService.listJobs) to choose
     create or update, prints a summary (env var names and counts, never
     values) and asks for y unless --yes.
  4. Writes the env vars to a JSON file in a temp folder (removed
     afterwards) and runs `gcloud run jobs create|update <job> --image
     --env-vars-file --command=python --args=<file> --max-retries=0
     --service-account=<the service's account>`.
  5. With --schedule (5-field cron) and --time-zone: creates or updates the
     Cloud Scheduler job <job>-scheduler that POSTs to the job's :run URL
     with an OAuth token. Its identity is --scheduler-service-account; when
     that is not given, an existing scheduler keeps the account it has, and
     a new one gets the service's runtime account (which then needs
     roles/run.invoker; the fleet repos pass
     <project>@appspot.gserviceaccount.com). Without --schedule any
     existing scheduler is left alone.

Usage (from the repo root or anywhere):
  python server/tools/createOrUpdateJob.py --project=<p> --region=<r>
      --service=<svc> --job-file=job_X.py [--schedule="0 8 * * *"
      --time-zone=Asia/Manila] [--scheduler-service-account=<email>]
      [--dry-run] [--yes] [--jobs-dir=<dir>]

Exit codes: 0 done (or a dry run), 1 a gcloud write failed (a failed job
write skips the scheduler), 2 nothing was written (bad arguments, bad
service config, gcloud missing, a read failed or the confirmation was not y).

Pure stdlib and Python 3.10-compatible, like updateJobsFromService.py, whose
gcloud wrapper, naming rule, describe, job list and env checks it reuses
(every call goes through this module's gcloud(), which tests replace).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import updateJobsFromService as jobSync  # noqa: E402  (its sibling in tools/)

JobSyncError = jobSync.JobSyncError
JOB_FILE_PATTERN = re.compile(r'\Ajob_[A-Za-z0-9_]+\.py\Z')
SCHEDULER_SUFFIX = '-scheduler'
# Tab-separated: the scheduler's full name, then its OAuth service account.
SCHEDULER_LIST_FORMAT = 'value(name,httpTarget.oauthToken.serviceAccountEmail)'
# minute hour day-of-month month day-of-week
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
CRON_FIELD_NAMES = ('minute', 'hour', 'day-of-month', 'month', 'day-of-week')
# Every value that reaches gcloud (gcloud.cmd on Windows) is one of these.
ARG_PATTERNS = {
    'project': (re.compile(r'\A[a-z][a-z0-9-]*\Z'), 'a GCP project id'),
    'region': (re.compile(r'\A[a-z][a-z0-9-]*\Z'), 'a region such as asia-east1'),
    'service': (re.compile(r'\A[a-z]([-a-z0-9]{0,47}[a-z0-9])?\Z'), 'a Cloud Run service name'),
    'time_zone': (re.compile(r'\A[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*\Z'),
                  'an IANA time zone such as Asia/Manila'),
    'scheduler_service_account': (
        re.compile(r'\A[a-z0-9][a-z0-9.-]*@[a-z0-9][a-z0-9.-]*\.[a-z]+\Z'),
        'a service account e-mail'),
}


def gcloud(args: list[str]) -> tuple[int, str, str]:
    """updateJobsFromService.gcloud (timeouts, gcloud.cmd on Windows, the
    cmd.exe metacharacter guard). Tests replace this function with a fake."""
    return jobSync.gcloud(args)


def _gcloud(args: list[str]) -> tuple[int, str, str]:
    return gcloud(args)   # looked up per call, so a replaced gcloud() is used


def cronProblem(schedule: str) -> str:
    """Why schedule is not a 5-field cron ('' when it is fine): each field
    *, */N, A-B or a comma list of numbers within its range."""
    fields = schedule.split()
    if len(fields) != 5:
        return ('must have exactly 5 fields: minute hour day-of-month month day-of-week, '
                'e.g. "0 8 * * *"')
    for value, (low, high), name in zip(fields, CRON_RANGES, CRON_FIELD_NAMES):
        if value == '*' or re.fullmatch(r'\*/[0-9]+', value):
            continue
        rangeMatch = re.fullmatch(r'([0-9]+)-([0-9]+)', value)
        if rangeMatch:
            parts = [int(rangeMatch.group(1)), int(rangeMatch.group(2))]
        elif all(re.fullmatch(r'[0-9]+', part) for part in value.split(',')):
            parts = [int(part) for part in value.split(',')]
        else:
            return 'field ' + repr(value) + ' (' + name + ') is not *, */N, A-B or a number list'
        if any(part < low or part > high for part in parts):
            return ('field ' + repr(value) + ' (' + name + ') must be within '
                    + str(low) + '-' + str(high))
    return ''


def jobRunUri(project: str, region: str, job: str) -> str:
    return ('https://' + region + '-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/'
            + project + '/jobs/' + job + ':run')


def existingScheduler(project: str, region: str, name: str) -> tuple[bool, str]:
    """(exists, its OAuth service account or '') for the scheduler ``name``."""
    code, out, err = gcloud([
        'scheduler', 'jobs', 'list', '--project=' + project, '--location=' + region,
        '--format=' + SCHEDULER_LIST_FORMAT])
    if code != 0:
        raise JobSyncError('Could not list Cloud Scheduler jobs in ' + project + '/' + region
                           + ' ' + jobSync.gcloudFailure(code, err))
    for line in out.splitlines():
        fullName, _, account = line.strip().partition('\t')
        if fullName.strip().rsplit('/', 1)[-1] == name:
            return True, account.strip()
    return False, ''


def jobArgs(verb: str, args: argparse.Namespace, job: str, image: str, envFile: Path,
            account: str) -> list[str]:
    command = ['run', 'jobs', verb, job, '--project=' + args.project, '--region=' + args.region,
               '--image=' + image, '--env-vars-file=' + str(envFile),
               *jobSync.jobCommandArgs(args.job_file)]
    if account:
        command.append('--service-account=' + account)
    return command + ['--quiet']


def schedulerArgs(verb: str, args: argparse.Namespace, job: str, account: str) -> list[str]:
    return ['scheduler', 'jobs', verb, 'http', job + SCHEDULER_SUFFIX,
            '--project=' + args.project, '--location=' + args.region,
            '--schedule=' + args.schedule, '--time-zone=' + args.time_zone,
            '--uri=' + jobRunUri(args.project, args.region, job), '--http-method=POST',
            '--oauth-service-account-email=' + account, '--quiet']


def validateArgs(args: argparse.Namespace) -> str:
    """The job name; JobSyncError for anything wrong before gcloud is called."""
    for name, (pattern, description) in ARG_PATTERNS.items():
        value = getattr(args, name)
        if value and not pattern.match(value):
            raise JobSyncError('--' + name.replace('_', '-') + '=' + value + ' is not '
                               + description + '.')
    if Path(args.job_file).name != args.job_file or not JOB_FILE_PATTERN.match(args.job_file):
        raise JobSyncError('--job-file=' + args.job_file + ' must be a file name like '
                           'job_sync_Foo.py (no folder; it is looked up in --jobs-dir).')
    if not (args.jobs_dir / args.job_file).is_file():
        raise JobSyncError(args.job_file + ' not found in ' + str(args.jobs_dir) + '.')
    job = jobSync.jobNameForFile(args.service, args.job_file)
    if job.endswith(jobSync.ONCE_SUFFIX):
        raise JobSyncError('Job file ' + args.job_file + " would produce the name '" + job
                           + "'. The '" + jobSync.ONCE_SUFFIX + "' suffix is reserved for "
                           'run_job_once.bat temporary jobs - rename the job file.')
    if args.schedule:
        problem = cronProblem(args.schedule)
        if problem:
            raise JobSyncError('Invalid --schedule "' + args.schedule + '": ' + problem + '.')
        if not args.time_zone:
            raise JobSyncError('--schedule needs --time-zone (an IANA name, e.g. Asia/Manila).')
    elif args.time_zone or args.scheduler_service_account:
        raise JobSyncError('--time-zone and --scheduler-service-account only apply with --schedule.')
    return job


def confirmed(args: argparse.Namespace) -> bool:
    if args.yes or args.dry_run:
        return True
    if not sys.stdin.isatty():
        print('Not a terminal: pass --yes to proceed.')
        return False
    answer = input('Type y to proceed (anything else aborts): ')
    return answer.strip() in ('y', 'Y')


def parseArgs(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Create or update one Cloud Run job of a service and, optionally, '
                    'its Cloud Scheduler trigger (writes to GCP).')
    parser.add_argument('--project', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--job-file', required=True, help='the job_*.py the job runs')
    parser.add_argument('--schedule', default='', help='5-field cron, e.g. "0 8 * * *"')
    parser.add_argument('--time-zone', default='', help='IANA time zone of --schedule')
    parser.add_argument('--scheduler-service-account', default='',
                        help="OAuth identity of the scheduler (default: an existing scheduler's "
                             "own, else the service's account)")
    parser.add_argument('--dry-run', action='store_true',
                        help='read and report only; write nothing')
    parser.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    parser.add_argument('--jobs-dir', type=Path, default=jobSync.DEFAULT_JOBS_DIR,
                        help='folder holding the job_*.py files (default: server/)')
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    job = validateArgs(args)
    env, image, account = jobSync.describeService(args.project, args.region, args.service,
                                                  runner=_gcloud)
    if not image:
        raise JobSyncError('Service ' + args.service + ' has no image.')
    exists = job in jobSync.listJobs(args.project, args.region, args.service, runner=_gcloud)
    schedulerName = job + SCHEDULER_SUFFIX
    schedulerFound, schedulerCurrent = (
        existingScheduler(args.project, args.region, schedulerName) if args.schedule
        else (False, ''))
    schedulerAccount = args.scheduler_service_account or schedulerCurrent or account
    if args.schedule and not schedulerAccount:
        raise JobSyncError('Service ' + args.service + ' runs as the default compute account, '
                           'so the scheduler has no identity: pass --scheduler-service-account.')

    print('Project:  ' + args.project)
    print('Service:  ' + args.service + '  (region ' + args.region + ')')
    print('Job:      ' + job + ('  (EXISTS -> will be UPDATED: env vars, image, command '
                                'overwritten)' if exists else '  (NEW -> will be CREATED)'))
    print('Image:    ' + image)
    print('Command:  python ' + args.job_file)
    print('Account:  ' + (account or '<default compute account>'))
    print('Env vars: ' + str(len(env)) + ' copied from ' + args.service + ' ('
          + ', '.join(sorted(env)) + ')')
    if args.schedule:
        print('Schedule: ' + args.schedule + ' (' + args.time_zone + ') -> ' + schedulerName
              + (' (UPDATED)' if schedulerFound else ' (CREATED)') + ' as ' + schedulerAccount)
    verb = 'update' if exists else 'create'
    if args.dry_run:
        print('Would ' + verb + ' job ' + job)
        if args.schedule:
            print('Would ' + ('update' if schedulerFound else 'create') + ' scheduler '
                  + schedulerName)
        print('Dry run: nothing was written.')
        return 0
    if not confirmed(args):
        print('Aborted. Nothing was changed.')
        return 2

    tempDir = Path(tempfile.mkdtemp(prefix='jobEnv'))
    try:
        envFile = tempDir / 'env.json'
        envFile.write_text(json.dumps(env, indent=2), encoding='utf-8')
        code, _, err = gcloud(jobArgs(verb, args, job, image, envFile, account))
    finally:
        shutil.rmtree(tempDir, ignore_errors=True)
    if code != 0:
        print('FAILED to ' + verb + ' job ' + job + ' ' + jobSync.gcloudFailure(code, err)
              + (' Scheduler step skipped.' if args.schedule else ''))
        return 1
    print(('Updated' if exists else 'Created') + ' job ' + job)
    if not args.schedule:
        return 0
    schedulerVerb = 'update' if schedulerFound else 'create'
    code, _, err = gcloud(schedulerArgs(schedulerVerb, args, job, schedulerAccount))
    if code != 0:
        print('FAILED to ' + schedulerVerb + ' scheduler ' + schedulerName + ' '
              + jobSync.gcloudFailure(code, err))
        return 1
    print(('Updated' if schedulerFound else 'Created') + ' scheduler ' + schedulerName)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parseArgs(argv)
    try:
        return run(args)
    except JobSyncError as error:
        print('ERROR: ' + str(error))
        return 2


if __name__ == '__main__':
    sys.exit(main())
