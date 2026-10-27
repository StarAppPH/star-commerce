"""Pins tools/createOrUpdateJob.py: one Cloud Run job of a service (and
optionally its Cloud Scheduler trigger), created or updated with the
service's image, env vars and service account.

Stdlib and pytest only: no DB, no network. gcloud is a fake.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = SERVER_DIR / 'tools' / 'createOrUpdateJob.py'

SERVICE = 'svc'
ACCOUNT = 'svc-run@proj.iam.gserviceaccount.com'
IMAGE = 'host/proj/repo/name/svc:service-sha'
PRIVATE_KEY = '-----BEGIN KEY-----\nline-two-secret\n-----END KEY-----\n'
ENV = [
    {'name': 'ENVIRONMENT', 'value': 'prodValueMarker'},
    {'name': 'FIREBASE_PRIVATE_KEY', 'value': PRIVATE_KEY},
]
SECRET_VALUES = ('prodValueMarker', 'line-two-secret')
RUN_URI = ('https://reg-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/proj/jobs/'
           'svc-sync-foo:run')


def _loadScript():
    spec = importlib.util.spec_from_file_location('createOrUpdateJob', SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGcloud:
    """Answers describe and the two lists; records every call and, for a
    job write, the env file's content as it was at that moment."""

    def __init__(self, env=None, account=ACCOUNT, jobs=(), schedulers=(), failWrites=()):
        self.env = ENV if env is None else env
        self.account = account
        self.jobs = list(jobs)
        self.schedulers = list(schedulers)
        self.failWrites = set(failWrites)
        self.calls = []
        self.writes = []
        self.envFiles = []
        self.envContent = None

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:3] == ['run', 'services', 'describe']:
            container = {'image': IMAGE}
            if self.env:
                container['env'] = self.env
            spec = {'containers': [container]}
            if self.account:
                spec['serviceAccountName'] = self.account
            return 0, json.dumps({'spec': {'template': {'spec': spec}}}), ''
        if args[:3] == ['run', 'jobs', 'list']:
            return 0, ''.join(name + '\n' for name in self.jobs), ''
        if args[:3] == ['scheduler', 'jobs', 'list']:
            return 0, ''.join('projects/proj/locations/reg/jobs/' + name + '\n'
                              for name in self.schedulers), ''
        if args[:2] == ['run', 'jobs'] and args[2] in ('create', 'update'):
            envArg = next(a for a in args if a.startswith('--env-vars-file='))
            envFile = Path(envArg.split('=', 1)[1])
            self.envFiles.append(envFile)
            self.envContent = json.loads(envFile.read_text(encoding='utf-8'))
            self.writes.append(list(args))
            return (1, '', 'boom') if 'job' in self.failWrites else (0, '', '')
        if args[:2] == ['scheduler', 'jobs'] and args[2] in ('create', 'update'):
            self.writes.append(list(args))
            return (1, '', 'boom') if 'scheduler' in self.failWrites else (0, '', '')
        raise AssertionError('unexpected gcloud call: ' + repr(args))


@pytest.fixture
def script():
    return _loadScript()


@pytest.fixture
def jobsDir(tmp_path):
    folder = tmp_path / 'server'
    folder.mkdir()
    (folder / 'job_sync_Foo.py').write_text('', encoding='utf-8')
    (folder / 'job_cleanup_once.py').write_text('', encoding='utf-8')
    return folder


def _run(script, monkeypatch, fake, jobsDir, *extra, jobFile='job_sync_Foo.py'):
    monkeypatch.setattr(script, 'gcloud', fake)
    return script.main(['--project=proj', '--region=reg', '--service=' + SERVICE,
                        '--job-file=' + jobFile, '--jobs-dir=' + str(jobsDir), *extra])


def _assertNoValues(capsys):
    out = capsys.readouterr()
    for value in SECRET_VALUES:
        assert value not in out.out and value not in out.err
    return out.out


def _jobWrite(verb, envFile):
    return ['run', 'jobs', verb, 'svc-sync-foo', '--project=proj', '--region=reg',
            '--image=' + IMAGE, '--env-vars-file=' + str(envFile), '--command=python',
            '--args=job_sync_Foo.py', '--max-retries=0', '--service-account=' + ACCOUNT, '--quiet']


def test_aMissingJobIsCreatedWithTheServicesImageEnvAndAccount(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs=['svc-other'])
    assert _run(script, monkeypatch, fake, jobsDir, '--yes') == 0
    assert fake.writes == [_jobWrite('create', fake.envFiles[0])]
    assert fake.envContent == {'ENVIRONMENT': 'prodValueMarker', 'FIREBASE_PRIVATE_KEY': PRIVATE_KEY}
    assert not fake.envFiles[0].exists() and not fake.envFiles[0].parent.exists()
    out = _assertNoValues(capsys)
    assert 'Created job svc-sync-foo' in out
    assert 'ENVIRONMENT, FIREBASE_PRIVATE_KEY' in out


def test_anExistingJobIsUpdated(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs=['svc-sync-foo'])
    assert _run(script, monkeypatch, fake, jobsDir, '--yes') == 0
    assert fake.writes == [_jobWrite('update', fake.envFiles[0])]
    assert 'Updated job svc-sync-foo' in _assertNoValues(capsys)


def test_withoutAScheduleTheSchedulerIsNotTouched(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(schedulers=['svc-sync-foo-scheduler'])
    assert _run(script, monkeypatch, fake, jobsDir, '--yes') == 0
    assert not any(call[0] == 'scheduler' for call in fake.calls)


def test_aOnceJobNameIsRefusedBeforeGcloud(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir, '--yes', jobFile='job_cleanup_once.py') == 2
    assert fake.calls == []
    assert "'-once' suffix is reserved" in capsys.readouterr().out


@pytest.mark.parametrize('jobFile', ['job_missing.py', 'sub/job_sync_Foo.py', 'sync_Foo.py'])
def test_aBadOrMissingJobFileIsRefusedBeforeGcloud(script, monkeypatch, capsys, jobsDir, jobFile):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir, '--yes', jobFile=jobFile) == 2
    assert fake.calls == []


def test_aSecretRefIsRefusedWithTheSharedWording(script, monkeypatch, capsys, jobsDir):
    env = ENV + [{'name': 'DB_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'pw'}}}]
    fake = FakeGcloud(env=env)
    assert _run(script, monkeypatch, fake, jobsDir, '--yes') == 2
    assert fake.writes == []
    out = _assertNoValues(capsys)
    assert 'reference secrets (DB_PASSWORD)' in out


def test_anEmptyEnvIsRefused(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(env=[])
    assert _run(script, monkeypatch, fake, jobsDir, '--yes') == 2
    assert fake.writes == []
    assert 'refusing to give its jobs an empty environment' in capsys.readouterr().out


def test_dryRunWritesNothing(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir, '--dry-run', '--schedule=0 8 * * *',
                '--time-zone=Asia/Manila') == 0
    assert fake.writes == []
    out = _assertNoValues(capsys)
    assert 'Would create job svc-sync-foo' in out
    assert 'Would create scheduler svc-sync-foo-scheduler' in out


def test_withoutYesOutsideATerminalNothingIsWritten(script, monkeypatch, capsys, jobsDir):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False, raising=False)
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir) == 2
    assert fake.writes == []


def test_theSchedulerDefaultsToTheServicesRuntimeAccount(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir, '--yes', '--schedule=0 8 * * 1-5',
                '--time-zone=Asia/Manila') == 0
    assert fake.writes[1] == [
        'scheduler', 'jobs', 'create', 'http', 'svc-sync-foo-scheduler', '--project=proj',
        '--location=reg', '--schedule=0 8 * * 1-5', '--time-zone=Asia/Manila', '--uri=' + RUN_URI,
        '--http-method=POST', '--oauth-service-account-email=' + ACCOUNT, '--quiet']


def test_anExistingSchedulerIsUpdatedWithTheGivenAccount(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(jobs=['svc-sync-foo'], schedulers=['svc-sync-foo-scheduler'])
    appspot = 'proj@appspot.gserviceaccount.com'
    assert _run(script, monkeypatch, fake, jobsDir, '--yes', '--schedule=*/15 * * * *',
                '--time-zone=Etc/UTC', '--scheduler-service-account=' + appspot) == 0
    assert fake.writes[1][:5] == ['scheduler', 'jobs', 'update', 'http', 'svc-sync-foo-scheduler']
    assert '--oauth-service-account-email=' + appspot in fake.writes[1]


def test_aFailedJobWriteSkipsTheScheduler(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(failWrites={'job'})
    assert _run(script, monkeypatch, fake, jobsDir, '--yes', '--schedule=0 8 * * *',
                '--time-zone=Asia/Manila') == 1
    assert len(fake.writes) == 1
    assert not fake.envFiles[0].exists()
    assert 'Scheduler step skipped' in capsys.readouterr().out


def test_aFailedSchedulerWriteExitsOne(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(failWrites={'scheduler'})
    assert _run(script, monkeypatch, fake, jobsDir, '--yes', '--schedule=0 8 * * *',
                '--time-zone=Asia/Manila') == 1
    assert len(fake.writes) == 2


def test_theDefaultComputeAccountNeedsAnExplicitSchedulerAccount(script, monkeypatch, capsys, jobsDir):
    fake = FakeGcloud(account='')
    assert _run(script, monkeypatch, fake, jobsDir, '--yes', '--schedule=0 8 * * *',
                '--time-zone=Asia/Manila') == 2
    assert fake.writes == []
    fake = FakeGcloud(account='')
    assert _run(script, monkeypatch, fake, jobsDir, '--yes') == 0
    assert not any(a.startswith('--service-account=') for a in fake.writes[0])


@pytest.mark.parametrize('extra', [
    ['--schedule=0 8 * *', '--time-zone=Asia/Manila'],
    ['--schedule=60 8 * * *', '--time-zone=Asia/Manila'],
    ['--schedule=0 8 * * mon', '--time-zone=Asia/Manila'],
    ['--schedule=0 8 * * *'],
    ['--time-zone=Asia/Manila'],
])
def test_badScheduleArgumentsAreRefusedBeforeGcloud(script, monkeypatch, capsys, jobsDir, extra):
    fake = FakeGcloud()
    assert _run(script, monkeypatch, fake, jobsDir, '--yes', *extra) == 2
    assert fake.calls == []


@pytest.mark.parametrize('schedule, ok', [
    ('0 8 * * *', True), ('*/5 * * * *', True), ('0 0 1,15 * *', True), ('0 9 * * 1-5', True),
    ('0 24 * * *', False), ('0 0 0 * *', False), ('0 0 * 13 *', False), ('0 0 * * 7', False),
    ('0 0 * *', False), ('a b c d e', False),
])
def test_cronProblem(script, schedule, ok):
    assert (script.cronProblem(schedule) == '') is ok


def test_theJobNameFollowsTheSharedRule(script):
    assert script.jobSync.jobNameForFile('svc', 'job_sync_Foo.py') == 'svc-sync-foo'
