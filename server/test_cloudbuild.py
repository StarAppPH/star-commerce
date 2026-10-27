"""Pins the push-to-main build: cloudbuild.yaml, the Dockerfile's guard and
tools/updateJobsFromService.py.

cloudbuild.yaml builds the image, runs it as the Cloud Run job
<service>-predeploy (job_predeploy.py: the Pub/Sub topic check and the index
builds), deploys the service only when that job passed, and then gives its
Cloud Run jobs the same image and env vars. The Dockerfile refuses a build that did not
come from cloudbuild.yaml, so a trigger still on its old inline build fails
before anything ships. The yaml is identical in every Star App repo: every
repo-specific value is a trigger substitution.

Stdlib and pytest only: no DB, no network. gcloud is a fake.
"""
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVER_DIR.parent
CLOUDBUILD_PATH = REPO_ROOT / 'cloudbuild.yaml'
DOCKERFILE_PATH = SERVER_DIR / 'Dockerfile'
SCRIPT_PATH = SERVER_DIR / 'tools' / 'updateJobsFromService.py'

IMAGE = '$_AR_HOSTNAME/$_AR_PROJECT_ID/$_AR_REPOSITORY/$REPO_NAME/$_SERVICE_NAME:$COMMIT_SHA'
ALLOWED_SUBSTITUTIONS = {
    '_AR_HOSTNAME', '_AR_PROJECT_ID', '_AR_REPOSITORY', 'REPO_NAME',
    '_SERVICE_NAME', 'COMMIT_SHA', 'BUILD_ID', '_TRIGGER_ID', '_DEPLOY_REGION',
    'PROJECT_ID'}
STEP_IDS = ['build', 'push', 'update-predeploy-job', 'run-predeploy-job',
            'deploy-service', 'update-jobs']
FLEET_SERVICE_NAMES = re.compile(r'ecommerce|warehouse|salesapp|accounting', re.IGNORECASE)


def _loadScript():
    spec = importlib.util.spec_from_file_location('updateJobsFromService', SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _yaml() -> str:
    return CLOUDBUILD_PATH.read_text(encoding='utf-8')


def _steps(text: str) -> dict[str, str]:
    """Each step's text, keyed by its id, in file order."""
    parts = re.split(r'^  - id: ', text, flags=re.MULTILINE)
    steps = {}
    for part in parts[1:]:
        stepId, _, body = part.partition('\n')
        body = re.split(r'^\S', body, maxsplit=1, flags=re.MULTILINE)[0]
        steps[stepId.strip()] = body
    return steps


def _assertListsArgs(stepText: str, args) -> None:
    """Each arg is its own `- <arg>` list item in the step."""
    for arg in args:
        assert re.search(r'^\s+- ' + re.escape(arg) + r'\n', stepText, flags=re.MULTILINE), arg


# --- cloudbuild.yaml -------------------------------------------------------

def test_cloudbuildHasExactlyTheSixStepsInOrder():
    assert list(_steps(_yaml())) == STEP_IDS
    assert len(re.findall(r'^\s*- id:', _yaml(), flags=re.MULTILINE)) == len(STEP_IDS)


def test_buildStepPassesTheGuardArg():
    build = _steps(_yaml())['build']
    assert re.search(r'- --build-arg\n\s+- BUILD_CONFIG=cloudbuild\.yaml\n', build)
    assert '- ' + IMAGE in build
    assert re.search(r'- server\n\s+- -f\n\s+- server/Dockerfile', build)


def test_deployServiceStepMatchesTheInlineTrigger():
    deploy = _steps(_yaml())['deploy-service']
    assert 'entrypoint: gcloud' in deploy
    _assertListsArgs(deploy, (
        'run', 'services', 'update', '$_SERVICE_NAME', '--platform=managed',
        '--image=' + IMAGE,
        '--startup-probe=httpGet.path=/startupCheck,httpGet.port=8080,'
        'periodSeconds=1,timeoutSeconds=1,failureThreshold=120',
        '--labels=managed-by=gcp-cloud-build-deploy-cloud-run,commit-sha=$COMMIT_SHA,'
        'gcb-build-id=$BUILD_ID,gcb-trigger-id=$_TRIGGER_ID',
        '--region=$_DEPLOY_REGION', '--quiet'))


def test_updateJobsStepRunsTheScript():
    updateJobs = _steps(_yaml())['update-jobs']
    assert 'entrypoint: python3' in updateJobs
    _assertListsArgs(updateJobs, (
        'server/tools/updateJobsFromService.py', '--project=$PROJECT_ID',
        '--region=$_DEPLOY_REGION', '--service=$_SERVICE_NAME', '--image=' + IMAGE))
    assert SCRIPT_PATH.is_file()


PREDEPLOY_JOB = '$_SERVICE_NAME-predeploy'
PREDEPLOY_STEP_IDS = ['update-predeploy-job', 'run-predeploy-job']


def test_thePredeployJobRunsAfterThePushAndBeforeTheDeploy():
    order = list(_steps(_yaml()))
    for stepId in PREDEPLOY_STEP_IDS:
        assert order.index('push') < order.index(stepId) < order.index('deploy-service'), stepId
    assert order.index('update-predeploy-job') < order.index('run-predeploy-job')


def test_updatePredeployJobStepUpdatesOnlyThatJob():
    step = _steps(_yaml())['update-predeploy-job']
    assert 'entrypoint: python3' in step
    _assertListsArgs(step, (
        'server/tools/updateJobsFromService.py', '--project=$PROJECT_ID',
        '--region=$_DEPLOY_REGION', '--service=$_SERVICE_NAME', '--image=' + IMAGE,
        '--only=' + PREDEPLOY_JOB))


def test_runPredeployJobStepWaitsForTheJob():
    step = _steps(_yaml())['run-predeploy-job']
    assert 'entrypoint: gcloud' in step
    _assertListsArgs(step, ('run', 'jobs', 'execute', PREDEPLOY_JOB,
                            '--region=$_DEPLOY_REGION', '--wait'))
    assert (SERVER_DIR / 'job_predeploy.py').is_file()


def test_runPredeployJobStepAsksForThisBuildsCommit():
    """An overlapping build may re-image the job between this build's update
    and its run; job_predeploy.py compares this with the commit baked into
    the image it runs (BUILD_COMMIT_SHA) and fails on a difference."""
    step = _steps(_yaml())['run-predeploy-job']
    _assertListsArgs(step, ('--update-env-vars=EXPECTED_COMMIT_SHA=$COMMIT_SHA',))
    build = _steps(_yaml())['build']
    assert re.search(r'- --build-arg\n\s+- COMMIT_SHA=\$COMMIT_SHA\n', build)


def test_dockerfileBakesTheCommitIntoTheImage():
    lines = DOCKERFILE_PATH.read_text(encoding='utf-8').splitlines()
    argIndex = lines.index('ARG COMMIT_SHA')
    envIndex = lines.index('ENV BUILD_COMMIT_SHA=$COMMIT_SHA')
    cmdIndex = next(i for i, line in enumerate(lines) if line.startswith('CMD '))
    assert argIndex < envIndex < cmdIndex


def test_theJobNameFollowsTheJobFile(script):
    assert script.jobNameForFile('svc', 'job_predeploy.py') == 'svc-predeploy'


def test_oneImageExpressionEverywhere():
    text = _yaml()
    assert text.count(IMAGE) == 6
    images = re.findall(r'\$_AR_HOSTNAME\S*', text)
    assert set(images) == {IMAGE}


def test_onlyKnownSubstitutions():
    names = set(re.findall(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)', _yaml()))
    assert names <= ALLOWED_SUBSTITUTIONS, names - ALLOWED_SUBSTITUTIONS


def test_noServiceNamesSoItIsIdenticalInEveryRepo():
    assert not FLEET_SERVICE_NAMES.search(_yaml())


def test_optionsAndTimeout():
    text = _yaml()
    assert re.search(r'^images:\n  - ' + re.escape(IMAGE) + r'\n', text, flags=re.MULTILINE)
    assert re.search(r'^timeout: 1500s$', text, flags=re.MULTILINE)
    assert re.search(r'^  logging: CLOUD_LOGGING_ONLY$', text, flags=re.MULTILINE)
    assert re.search(r'^  substitutionOption: ALLOW_LOOSE$', text, flags=re.MULTILINE)


# --- Dockerfile guard ------------------------------------------------------

def test_dockerfileRefusesABuildWithoutCloudbuild():
    lines = DOCKERFILE_PATH.read_text(encoding='utf-8').splitlines()
    argIndex = lines.index('ARG BUILD_CONFIG')
    guardIndex = next(i for i, line in enumerate(lines)
                      if line.startswith('RUN ') and '"$BUILD_CONFIG" = "cloudbuild.yaml"' in line)
    firstCopyOrPip = next(i for i, line in enumerate(lines)
                          if line.startswith('COPY') or line.startswith('RUN pip'))
    assert argIndex < guardIndex < firstCopyOrPip
    guardBlock = []
    for line in lines[guardIndex:]:
        guardBlock.append(line)
        if not line.rstrip().endswith('\\'):
            break
    assert 'exit 1' in '\n'.join(guardBlock)


def test_dockerfileStartsOnlyGunicorn():
    """The checks run in the predeploy job; a cold start runs gunicorn alone,
    as PID 1 (exec) so Cloud Run's SIGTERM reaches it."""
    cmd = [line for line in DOCKERFILE_PATH.read_text(encoding='utf-8').splitlines()
           if line.startswith('CMD ')]
    assert len(cmd) == 1
    assert '"exec gunicorn -b 0.0.0.0:8080 ' in cmd[0]
    assert '&&' not in cmd[0] and 'python ' not in cmd[0]


def test_dockerfilePrecompilesTheApp():
    """.dockerignore drops __pycache__, so the image compiles the app once at
    build time, after the last file lands, instead of on every cold start."""
    lines = DOCKERFILE_PATH.read_text(encoding='utf-8').splitlines()
    compileIndexes = [i for i, line in enumerate(lines)
                      if line.startswith('RUN python -m compileall')]
    assert len(compileIndexes) == 1
    compileIndex = compileIndexes[0]
    lastCopyOrPip = max(i for i, line in enumerate(lines)
                        if line.startswith('COPY') or line.startswith('RUN pip install'))
    cmdIndex = next(i for i, line in enumerate(lines) if line.startswith('CMD '))
    assert lastCopyOrPip < compileIndex < cmdIndex
    assert lines[compileIndex].split()[-1] == '/app'


# --- tools/updateJobsFromService.py ----------------------------------------

SERVICE = 'svc'
PRIVATE_KEY = '-----BEGIN KEY-----\nline-two-secret\n-----END KEY-----\n'
ENV = [
    {'name': 'ENVIRONMENT', 'value': 'prodValueMarker'},
    {'name': 'FIREBASE_PRIVATE_KEY', 'value': PRIVATE_KEY},
]
SECRET_VALUES = ('prodValueMarker', 'line-two-secret')
REPOSITORY = 'host/proj/repo/name/svc'
SERVICE_IMAGE = REPOSITORY + ':service-sha'
DEFAULT_JOBS = {
    'svc-sync-foo': REPOSITORY + ':old-sha',
    'svc-orphan': REPOSITORY + '@sha256:0123abcd',
    'svc-sync-foo-once': REPOSITORY + ':old-sha',
    'other-sync-foo': 'host/proj/repo/name/other:old-sha',
    'svcx-thing': 'host/proj/repo/name/svcx:old-sha',
}


class FakeGcloud:
    """Answers describe and list; records each call, and for jobs update the
    env file's content as it was at that moment."""

    def __init__(self, env=None, image=SERVICE_IMAGE, jobs=None, failJobs=()):
        self.env = ENV if env is None else env
        self.image = image
        self.jobs = dict(DEFAULT_JOBS if jobs is None else jobs)
        self.failJobs = set(failJobs)
        self.calls = []
        self.updates = {}
        self.updateArgs = {}
        self.envFiles = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:3] == ['run', 'services', 'describe']:
            container = {'image': self.image}
            if self.env:
                container['env'] = self.env
            return 0, json.dumps({'spec': {'template': {'spec': {'containers': [container]}}}}), ''
        if args[:3] == ['run', 'jobs', 'list']:
            return 0, ''.join(name + '\t' + image + '\n' for name, image in self.jobs.items()), ''
        if args[:3] == ['run', 'jobs', 'update']:
            job = args[3]
            envArg = next(a for a in args if a.startswith('--env-vars-file='))
            envFile = Path(envArg.split('=', 1)[1])
            self.envFiles.append(envFile)
            self.updates[job] = {
                'image': next(a for a in args if a.startswith('--image=')).split('=', 1)[1],
                'env': json.loads(envFile.read_text(encoding='utf-8')),
            }
            self.updateArgs[job] = list(args)
            if job in self.failJobs:
                return 1, '', 'boom'
            return 0, '', ''
        raise AssertionError('unexpected gcloud call: ' + repr(args))


@pytest.fixture
def script():
    return _loadScript()


@pytest.fixture
def jobsDir(tmp_path):
    folder = tmp_path / 'server'
    folder.mkdir()
    (folder / 'job_sync_Foo.py').write_text('', encoding='utf-8')
    (folder / 'job_missing_Job.py').write_text('', encoding='utf-8')
    (folder / 'job_predeploy.py').write_text('', encoding='utf-8')
    (folder / 'notAJob.py').write_text('', encoding='utf-8')
    return folder


def _run(script, monkeypatch, fake, jobsDir, *extra):
    monkeypatch.setattr(script, 'gcloud', fake)
    return script.main(['--project=proj', '--region=reg', '--service=' + SERVICE,
                        '--jobs-dir=' + str(jobsDir), *extra])


def _assertNoValues(capsys):
    out = capsys.readouterr()
    for value in SECRET_VALUES:
        assert value not in out.out and value not in out.err
    return out.out


def test_jobNameForFileMirrorsGetJobName(script):
    assert script.jobNameForFile('warehouse', 'job_update3MonthAverageSales.py') == \
        'warehouse-update3monthaveragesales'
    assert script.jobNameForFile('svc', 'job_sync_Foo.py') == 'svc-sync-foo'


def test_listJobsKeepsThePrefixAndDropsOnce(script, monkeypatch):
    monkeypatch.setattr(script, 'gcloud', FakeGcloud())
    assert list(script.listJobs('proj', 'reg', SERVICE)) == ['svc-orphan', 'svc-sync-foo']


def test_imageRepositoryDropsTagAndDigestButKeepsAPort(script):
    assert script.imageRepository('host/a/b:tag') == 'host/a/b'
    assert script.imageRepository('host/a/b@sha256:abc') == 'host/a/b'
    assert script.imageRepository('host/a/b:tag@sha256:abc') == 'host/a/b'
    assert script.imageRepository('host:5000/a/b') == 'host:5000/a/b'
    assert script.imageRepository('host:5000/a/b:tag') == 'host:5000/a/b'


def test_happyPathUpdatesEveryJobWithTheImageAndEnv(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud()
    image = REPOSITORY + ':abc123'
    assert _run(script, monkeypatch, fake, jobsDir, '--image=' + image) == 0
    assert sorted(fake.updates) == ['svc-orphan', 'svc-sync-foo']
    for update in fake.updates.values():
        assert update['image'] == image
        assert update['env'] == {'ENVIRONMENT': 'prodValueMarker', 'FIREBASE_PRIVATE_KEY': PRIVATE_KEY}
        assert '\n' in update['env']['FIREBASE_PRIVATE_KEY']
    assert fake.envFiles and not any(f.exists() for f in fake.envFiles)
    assert not any(f.parent.exists() for f in fake.envFiles)
    out = _assertNoValues(capsys)
    assert 'ENVIRONMENT' in out and 'FIREBASE_PRIVATE_KEY' in out
    assert 'Done. Updated 2, failed 0.' in out


def test_withoutImageTheJobsGetTheServicesImage(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir) == 0
    assert sorted(fake.updates) == ['svc-orphan', 'svc-sync-foo']
    assert {u['image'] for u in fake.updates.values()} == {SERVICE_IMAGE}
    _assertNoValues(capsys)


def test_secretRefRefusesAndUpdatesNothing(script, monkeypatch, capsys, jobsDir):
    env = ENV + [{'name': 'API_KEY', 'valueFrom': {'secretKeyRef': {'name': 's', 'key': '1'}}}]
    fake = FakeGcloud(env=env)
    assert _run(script, monkeypatch, fake, jobsDir) == 2
    assert fake.updates == {}
    out = _assertNoValues(capsys)
    assert 'API_KEY' in out


def test_emptyEnvRefusesAndUpdatesNothing(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(env=[])
    assert _run(script, monkeypatch, fake, jobsDir) == 2
    assert fake.updates == {}


def test_oneFailureExitsOneButTheOthersStillUpdate(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(failJobs={'svc-orphan'})
    assert _run(script, monkeypatch, fake, jobsDir) == 1
    assert sorted(fake.updates) == ['svc-orphan', 'svc-sync-foo']
    out = _assertNoValues(capsys)
    assert 'FAILED svc-orphan' in out
    assert 'Done. Updated 1, failed 1.' in out
    assert not any(f.exists() for f in fake.envFiles)


def test_dryRunUpdatesNothing(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir, '--dry-run') == 0
    assert fake.updates == {}
    assert not any(call[:3] == ['run', 'jobs', 'update'] for call in fake.calls)
    out = _assertNoValues(capsys)
    assert 'Would update svc-sync-foo' in out


def test_missingJobWarningAndOrphanNote(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir) == 0
    out = _assertNoValues(capsys)
    assert 'WARNING: job_missing_Job.py has no Cloud Run job svc-missing-job' in out
    orphanLine = next(line for line in out.splitlines() if 'svc-orphan' in line and 'Updating' in line)
    assert '(no job_*.py in server/)' in orphanLine
    fooLine = next(line for line in out.splitlines() if line.startswith('Updating svc-sync-foo'))
    assert 'no job_*.py' not in fooLine


def test_jobWithAForeignImageIsSkippedWithAWarning(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs={**DEFAULT_JOBS, 'svc-foreign': 'evil.example/other/thing:1'})
    assert _run(script, monkeypatch, fake, jobsDir, '--image=' + REPOSITORY + ':abc123') == 0
    assert sorted(fake.updates) == ['svc-orphan', 'svc-sync-foo']
    out = _assertNoValues(capsys)
    assert 'WARNING: skipping svc-foreign' in out
    assert 'evil.example/other/thing:1' in out


def test_withoutImageTheServicesRepositoryDecides(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(image='host/proj/repo/name/elsewhere:sha')
    assert _run(script, monkeypatch, fake, jobsDir) == 0
    assert fake.updates == {}
    out = _assertNoValues(capsys)
    assert 'WARNING: skipping svc-sync-foo' in out


@pytest.mark.parametrize('error', [
    subprocess.TimeoutExpired(cmd='gcloud', timeout=1), FileNotFoundError('gone')])
def test_gcloudTurnsAHangOrAStartFailureIntoAFailedResult(script, monkeypatch, error):
    def raising(*args, **kwargs):
        raise error
    monkeypatch.setattr(script.shutil, 'which', lambda name: 'gcloud')
    monkeypatch.setattr(script.subprocess, 'run', raising)
    code, out, err = script.gcloud(['run', 'jobs', 'update', 'x'])
    assert code != 0 and out == '' and err


def test_aHungJobUpdateFailsButTheOthersAreStillTried(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud()
    attempted = []

    def run(command, **kwargs):
        args = command[1:]
        if args[:3] == ['run', 'jobs', 'update']:
            attempted.append(args[3])
            if args[3] == 'svc-orphan':
                raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get('timeout'))
        code, out, err = fake(args)
        return subprocess.CompletedProcess(command, code, out, err)

    monkeypatch.setattr(script.shutil, 'which', lambda name: 'gcloud')
    monkeypatch.setattr(script.subprocess, 'run', run)
    assert script.main(['--project=proj', '--region=reg', '--service=' + SERVICE,
                        '--jobs-dir=' + str(jobsDir)]) == 1
    assert attempted == ['svc-orphan', 'svc-sync-foo']
    assert sorted(fake.updates) == ['svc-sync-foo']
    out = _assertNoValues(capsys)
    assert 'FAILED svc-orphan' in out and 'timed out' in out
    assert 'Done. Updated 1, failed 1.' in out


# --- --only (the predeploy job) --------------------------------------------

ONLY_JOBS = {**DEFAULT_JOBS, 'svc-predeploy': REPOSITORY + ':old-sha'}


def test_onlyUpdatesJustThatJob(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs=ONLY_JOBS)
    image = REPOSITORY + ':abc123'
    assert _run(script, monkeypatch, fake, jobsDir, '--image=' + image, '--only=svc-predeploy') == 0
    assert list(fake.updates) == ['svc-predeploy']
    assert fake.updates['svc-predeploy']['image'] == image
    assert fake.updates['svc-predeploy']['env'] == {
        'ENVIRONMENT': 'prodValueMarker', 'FIREBASE_PRIVATE_KEY': PRIVATE_KEY}
    out = _assertNoValues(capsys)
    assert 'job_missing_Job.py' not in out
    assert 'Done. Updated 1, failed 0.' in out


def test_onlyAMissingJobIsAnErrorThatNamesTheFix(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir, '--only=svc-predeploy') == 2
    assert fake.updates == {}
    out = _assertNoValues(capsys)
    assert 'ERROR: Cloud Run job svc-predeploy does not exist' in out
    assert 'tools/create_or_update_job.bat' in out


@pytest.mark.parametrize('job', ['svc-sync-foo-once', 'other-sync-foo', 'svcx-thing'])
def test_onlyRefusesAJobThatIsNotTheServicesOwn(script, monkeypatch, capsys, jobsDir, job):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir, '--only=' + job) == 2
    assert fake.updates == {}
    assert 'is not a job of service svc' in _assertNoValues(capsys)


def test_onlyAJobOnAForeignImageIsAnError(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs={**DEFAULT_JOBS, 'svc-predeploy': 'evil.example/other/thing:1'})
    assert _run(script, monkeypatch, fake, jobsDir, '--image=' + REPOSITORY + ':abc123',
                '--only=svc-predeploy') == 2
    assert fake.updates == {}
    assert 'runs an image from another repository' in _assertNoValues(capsys)


def test_onlyAFailedUpdateExitsOne(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs=ONLY_JOBS, failJobs={'svc-predeploy'})
    assert _run(script, monkeypatch, fake, jobsDir, '--only=svc-predeploy') == 1
    assert 'FAILED svc-predeploy' in _assertNoValues(capsys)


def test_onlyDryRunUpdatesNothing(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs=ONLY_JOBS)
    assert _run(script, monkeypatch, fake, jobsDir, '--only=svc-predeploy', '--dry-run') == 0
    assert fake.updates == {}
    out = _assertNoValues(capsys)
    assert 'Would update svc-predeploy' in out
    assert 'Would update svc-sync-foo' not in out


def test_onlySetsTheJobsCommandAndNoRetries(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs=ONLY_JOBS)
    assert _run(script, monkeypatch, fake, jobsDir, '--only=svc-predeploy') == 0
    args = fake.updateArgs['svc-predeploy']
    for flag in ('--command=python', '--args=job_predeploy.py', '--max-retries=0'):
        assert flag in args, flag
    _assertNoValues(capsys)


def test_aFullUpdateLeavesTheCommandsAlone(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs=ONLY_JOBS)
    assert _run(script, monkeypatch, fake, jobsDir) == 0
    for job, args in fake.updateArgs.items():
        assert not [a for a in args if a.startswith(('--command', '--args', '--max-retries'))], job
    _assertNoValues(capsys)


def test_onlyAJobWithNoJobFileIsAnError(script, monkeypatch, capsys, tmp_path):
    emptyDir = tmp_path / 'empty'
    emptyDir.mkdir()
    fake = FakeGcloud(jobs=ONLY_JOBS)
    assert _run(script, monkeypatch, fake, emptyDir, '--only=svc-predeploy') == 2
    assert fake.updates == {}
    assert 'svc-predeploy has no job_*.py' in _assertNoValues(capsys)
