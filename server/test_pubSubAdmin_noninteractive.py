"""Pins pubSubAdmin.py's non-interactive mode (--project --service --yes /
--dry-run), the one create-star-app cloud-setup's siblings.wire runs: the
flags pick the project and the Cloud Run service, nothing is asked, a
consumer this service has not enabled is skipped, a missing deadLetterTopic
is created instead of failing every subscription, and any failure (auth,
another project's push account or env) exits non-zero. Without flags the
interactive pickers still run. In both modes the Pub/Sub service agent gets
the publisher role on the dead-letter topic.

No network, no GCP: Pub/Sub, Cloud Run and the credentials are fakes.
"""
import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = SERVER_DIR / 'pubSubAdmin.py'

PROJECT = 'proj'
REGION = 'reg'
OURS = 'shop'
CONSUMER = 'accounting'
CONSUMER_URL = 'https://accounting-xyz.a.run.app'
DISABLED = 'billing'                 # a consumer this service has not enabled
PUSH_ACCOUNT = 'shop-push@proj.iam.gserviceaccount.com'
SOURCE = (
    "@pubSubDecorator(projectNameConsumers=[accounting, billing])\n"
    "def orderCreated(self, order, publishToPubSubMockDb, publishToProdPubSub):\n"
    "    pass\n")


class Rec:
    """Any pubsub_v1.types message: keeps its keyword arguments."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakePubSub:
    """pubsub_v1 with in-memory topics and subscriptions."""

    def __init__(self, notFound, topics=()):
        self.notFound = notFound
        self.topics = set(topics)
        self.subscriptions = {}
        self.createdTopics = []
        self.createdSubscriptions = []
        self.updatedSubscriptions = []
        pushConfig = type('PushConfig', (Rec,), {'OidcToken': Rec})
        self.types = SimpleNamespace(PushConfig=pushConfig, DeadLetterPolicy=Rec, RetryPolicy=Rec,
                                     ExpirationPolicy=Rec, Subscription=Rec)
        fake = self

        class Client:
            def __init__(self, credentials=None):
                pass

            def topic_path(self, project, topic):
                return f'projects/{project}/topics/{topic}'

            def subscription_path(self, project, name):
                return f'projects/{project}/subscriptions/{name}'

            def get_topic(self, request):
                if request['topic'] not in fake.topics:
                    raise fake.notFound(request['topic'])

            def create_topic(self, request):
                fake.topics.add(request['name'])
                fake.createdTopics.append(request['name'])

            def get_subscription(self, request):
                if request['subscription'] not in fake.subscriptions:
                    raise fake.notFound(request['subscription'])

            def create_subscription(self, request):
                fake.subscriptions[request['name']] = request
                fake.createdSubscriptions.append(request)

            def update_subscription(self, request):
                fake.updatedSubscriptions.append(request)

        self.PublisherClient = Client
        self.SubscriberClient = Client


def _cloudRunService(name, uri, env):
    container = SimpleNamespace(env=[SimpleNamespace(name=k, value=v) for k, v in env.items()])
    return SimpleNamespace(name=f'projects/{PROJECT}/locations/{REGION}/services/{name}', uri=uri,
                           template=SimpleNamespace(containers=[container]))


@pytest.fixture
def admin(monkeypatch):
    """pubSubAdmin.py loaded fresh, every outside call replaced."""
    monkeypatch.syspath_prepend(str(SERVER_DIR))
    spec = importlib.util.spec_from_file_location('pubSubAdmin_noninteractive', SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses resolve their annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    calls = SimpleNamespace(granted=[], prompts=[], picked=[], cached=[])
    pubsub = FakePubSub(module.NotFound)
    services = {
        OURS: _cloudRunService(OURS, 'https://shop-xyz.a.run.app',
                               {'PUBSUB_ENABLED_ACCOUNTING': 'true', 'PUBSUB_ENABLED_BILLING': 'false',
                                'GOOGLE_CLOUD_PROJECT_ID': PROJECT}),
        CONSUMER: _cloudRunService(CONSUMER, CONSUMER_URL, {}),
    }
    consumers = {name: SimpleNamespace(name=name, cloudRunServiceName=name) for name in (CONSUMER, DISABLED)}

    def refuse(what):
        def call(*args, **kwargs):
            calls.picked.append(what)
            raise AssertionError(f'{what} must not run in non-interactive mode')
        return call

    def noPrompt(prompt=''):
        calls.prompts.append(prompt)
        raise AssertionError(f'unexpected prompt: {prompt!r}')

    monkeypatch.setattr(module.google.auth, 'default', lambda: (object(), 'adc-project'))
    monkeypatch.setattr(module, 'pubsub_v1', pubsub)
    monkeypatch.setattr(module, 'AppConfig', lambda: SimpleNamespace(projectName=OURS, gcpRegion=REGION))
    monkeypatch.setattr(module, 'serviceGetter', SimpleNamespace(
        listOfServices=list(consumers.values()), getServiceByName=lambda name: consumers[name]))
    monkeypatch.setattr(module, '__fetchCloudRunServices', lambda credentials, project_id: services)
    monkeypatch.setattr(module, '__pushServiceAccount', lambda: PUSH_ACCOUNT)
    monkeypatch.setattr(module, '__getAllFunctionsThatHasPubSubDecorator',
                        lambda: [ast.parse(SOURCE).body[0]])
    monkeypatch.setattr(module, '__grant_dlq_permissions',
                        lambda sub, topic, project, credentials: calls.granted.append((sub, topic)))
    monkeypatch.setattr(module, '__selectProject', refuse('__selectProject'))
    monkeypatch.setattr(module, '__selectCurrentService', refuse('__selectCurrentService'))
    monkeypatch.setattr(module, '__saveLastTarget', lambda *args: calls.cached.append(args))
    monkeypatch.setattr('builtins.input', noPrompt)
    return SimpleNamespace(module=module, pubsub=pubsub, calls=calls, services=services)


TOPIC = f'projects/{PROJECT}/topics/orderCreated_{OURS}'
DEAD_LETTER = f'projects/{PROJECT}/topics/deadLetterTopic'
SUBSCRIPTION = f'projects/{PROJECT}/subscriptions/orderCreated_{OURS}_to_{CONSUMER}'


@pytest.mark.parametrize('argv', [
    ['--project', PROJECT, '--service', OURS, '--yes'],
    [f'--project={PROJECT}', f'--service={OURS}', '--yes'],
])
def test_flags_pick_the_project_and_service_and_nothing_is_asked(admin, argv):
    admin.module.main(argv)
    assert admin.calls.prompts == [] and admin.calls.picked == []
    assert admin.calls.cached == []                       # the pickers' cache is left alone
    assert [request['name'] for request in admin.pubsub.createdSubscriptions] == [SUBSCRIPTION]
    request = admin.pubsub.createdSubscriptions[0]
    assert request['topic'] == TOPIC
    assert request['push_config'].push_endpoint == f'{CONSUMER_URL}/orderCreated_{OURS}'
    assert request['push_config'].oidc_token.service_account_email == PUSH_ACCOUNT
    assert request['dead_letter_policy'].dead_letter_topic == DEAD_LETTER
    # the dead-letter topic (not the source topic) gets the publisher role
    assert admin.calls.granted == [(SUBSCRIPTION, DEAD_LETTER)]


def test_a_consumer_not_enabled_here_is_skipped(admin, capsys):
    admin.module.main(['--project', PROJECT, '--service', OURS, '--yes'])
    assert all(DISABLED not in request['name'] for request in admin.pubsub.createdSubscriptions)
    assert f'{DISABLED}: skipped, its own cloud-setup subscribes it' in capsys.readouterr().out


@pytest.mark.parametrize('argv', [['--project', PROJECT, '--service', OURS, '--yes'], []])
def test_an_auth_failure_exits_1_in_both_modes(admin, monkeypatch, argv):
    def refused():
        raise admin.module.google.auth.exceptions.DefaultCredentialsError('no ADC')
    monkeypatch.setattr(admin.module.google.auth, 'default', refused)
    with pytest.raises(SystemExit) as stopped:
        admin.module.main(argv)
    assert stopped.value.code == 1
    assert admin.pubsub.createdTopics == []


def test_another_projects_service_env_stops_before_any_write(admin):
    admin.services[OURS].template.containers[0].env.append(
        SimpleNamespace(name='GOOGLE_CLOUD_PROJECT_ID', value='elsewhere'))
    admin.services[OURS].template.containers[0].env = [
        item for item in admin.services[OURS].template.containers[0].env
        if not (item.name == 'GOOGLE_CLOUD_PROJECT_ID' and item.value == PROJECT)]
    with pytest.raises(SystemExit) as stopped:
        admin.module.main(['--project', PROJECT, '--service', OURS, '--yes'])
    assert 'does not match --project' in str(stopped.value.code)
    assert admin.pubsub.createdTopics == []


def test_a_push_account_of_another_project_stops(admin, monkeypatch):
    monkeypatch.setattr(admin.module, '__pushServiceAccount',
                        lambda: 'shop-push@elsewhere.iam.gserviceaccount.com')
    with pytest.raises(SystemExit) as stopped:
        admin.module.main(['--project', PROJECT, '--service', OURS, '--yes'])
    assert 'not an account of project proj' in str(stopped.value.code)
    assert admin.pubsub.createdTopics == []


def test_a_missing_dead_letter_topic_is_created(admin):
    admin.module.main(['--project', PROJECT, '--service', OURS, '--yes'])
    assert admin.pubsub.createdTopics == [DEAD_LETTER, TOPIC]


def test_an_existing_dead_letter_topic_is_kept(admin):
    admin.pubsub.topics.add(DEAD_LETTER)
    admin.module.main(['--project', PROJECT, '--service', OURS, '--yes'])
    assert admin.pubsub.createdTopics == [TOPIC]


def test_dry_run_writes_nothing(admin):
    with pytest.raises(SystemExit) as stopped:
        admin.module.main(['--project', PROJECT, '--service', OURS, '--dry-run'])
    assert stopped.value.code == 0
    assert admin.pubsub.createdTopics == [] and admin.pubsub.createdSubscriptions == []
    assert admin.calls.prompts == []


def test_without_yes_nothing_is_written(admin):
    with pytest.raises(SystemExit) as stopped:
        admin.module.main(['--project', PROJECT, '--service', OURS])
    assert 'only with --yes' in str(stopped.value.code)
    assert admin.pubsub.createdTopics == [] and admin.pubsub.createdSubscriptions == []


def test_an_unknown_service_stops_before_any_write(admin):
    with pytest.raises(SystemExit) as stopped:
        admin.module.main(['--project', PROJECT, '--service', 'nope', '--yes'])
    assert "'nope' not found" in str(stopped.value.code)
    assert admin.pubsub.createdTopics == []


@pytest.mark.parametrize('argv, message', [
    (['--yes'], 'need --project'),
    (['--service', OURS], 'need --project'),
    (['--project', PROJECT, '--yes'], 'needs --service'),
    (['--project'], '--project needs a value'),
    (['--project', '--yes'], '--project needs a value'),
])
def test_incomplete_flags_are_refused(admin, argv, message):
    with pytest.raises(SystemExit) as stopped:
        admin.module.main(argv)
    assert message in str(stopped.value.code)
    assert admin.pubsub.createdTopics == []


def test_without_flags_the_interactive_pickers_run(admin):
    with pytest.raises(AssertionError, match='__selectProject'):
        admin.module.main([])
    assert admin.calls.picked == ['__selectProject']
