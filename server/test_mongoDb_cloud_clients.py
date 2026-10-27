"""Cloud clients of mongoDb (Atlas Flex connection budget). Fresh-module unit
checks: each test executes its own copy of mongoDb under a cloud ENVIRONMENT
with a recording client and fake URIs, independent of the suite's `db` and of
every other test. No database is touched.

Pinned: in the cloud exactly two clients are built, the main database
connecting at once and pubSubMockDb with connect=False; no testDb; tz_aware
everywhere; nothing reads a client attribute during import; a missing or empty
MONGO_URI fails fast (never a client for localhost), and both cloud
environments use MONGO_URI, never a URI written in the code.

Shared by every Star App repo (create-star-app's fleet/sharedFiles.json):
change it in create-star-app's template. A repo's own pins go in its per-repo
blocks."""
import importlib.util
from unittest.mock import MagicMock

import pymongo
from pymongo.server_api import ServerApi
import pytest

import AppConfig as appConfigModule
import mongoDb as mongoDbModule

FAKE_URI = 'mongodb://fake-not-connected'

# >>> star-app per-repo: cloud client environment
# Variables this repo's mongoDb reads at import besides MONGO_URI, each set for
# every fresh module (a value of None removes that variable).
REPO_ENV: dict = {}
# <<< star-app per-repo: cloud client environment


class _RecordingHandle:
    """A database or collection handle of the recording client: item access
    (a collection) gives another handle; any attribute (a find, an index
    build, a command) would be a network call, so it is recorded and
    refused."""

    def __init__(self, path: str) -> None:
        self._path = path

    def __getitem__(self, name: str) -> '_RecordingHandle':
        return _RecordingHandle(self._path + '.' + name)

    def __getattr__(self, name: str):
        _RecordingClient.accessed.append(self._path + '.' + name)
        raise AssertionError('database handle used: ' + self._path + '.' + name)


class _RecordingClient:
    """Stands in for pymongo.MongoClient: remembers the arguments of every
    client built and every attribute read on one. Only item access (the
    database handle, itself refusing any use) is allowed during a module
    import; any other attribute would be a network call, so it is recorded
    and refused."""
    instances: list = []
    accessed: list = []

    def __init__(self, *args, **kwargs) -> None:
        self.args, self.kwargs = args, kwargs
        _RecordingClient.instances.append(self)

    def __getitem__(self, name: str) -> _RecordingHandle:
        return _RecordingHandle(name)

    @property
    def admin(self) -> MagicMock:
        _RecordingClient.accessed.append('admin')
        admin = MagicMock()
        admin.command.return_value = {}
        return admin

    def __getattr__(self, name: str):
        _RecordingClient.accessed.append(name)
        raise AssertionError('client attribute used: ' + name)


def _resetRecorder(monkeypatch) -> None:
    monkeypatch.setattr(_RecordingClient, 'instances', [])
    monkeypatch.setattr(_RecordingClient, 'accessed', [])


def _freshModule(monkeypatch, environment: str, env: dict | None = None):
    """A new copy of mongoDb executed under `environment` with the recording
    client and fake URIs (an `env` value of None removes that variable). The
    copy has its own Singleton classes, so the suite's `db` is untouched."""
    monkeypatch.setenv('ENVIRONMENT', environment)
    monkeypatch.setenv('databaseName', 'x')
    for _, variable in appConfigModule._serviceEnvVarNames():
        monkeypatch.setenv(variable, 'false')
    for name, value in {'MONGO_URI': FAKE_URI, **REPO_ENV,
                        **(env or {})}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    _resetRecorder(monkeypatch)
    monkeypatch.setattr(pymongo, 'MongoClient', _RecordingClient)
    monkeypatch.setattr(mongoDbModule, 'MongoClient', _RecordingClient)
    spec = importlib.util.find_spec('mongoDb')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _connectOf(database) -> bool:
    return database.client.kwargs.get('connect', True)


def _localClient(monkeypatch, databaseClass, *args):
    """A local-environment client of `databaseClass`, built through the real
    module with the recording client (no mongod needed)."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    _resetRecorder(monkeypatch)
    monkeypatch.setattr(mongoDbModule, 'MongoClient', _RecordingClient)
    monkeypatch.setattr(databaseClass, '_instance', None)
    return databaseClass(*args).client


CLOUD = pytest.mark.parametrize('environment', ['cloudprod', 'clouddev'])
UNSET_OR_EMPTY = pytest.mark.parametrize('value', [None, ''],
                                         ids=['unset', 'empty'])


@CLOUD
def test_cloud_onlyTheMainDbConnects(monkeypatch, environment):
    module = _freshModule(monkeypatch, environment)

    # No testDb in the cloud: only the tests use it, and there it would be a
    # second client to the main database.
    assert getattr(module, 'testDb', None) is None
    assert len(_RecordingClient.instances) == 2
    assert _connectOf(module.db) is True
    assert _connectOf(module.pubSubMockDb) is False
    assert all(client.kwargs['tz_aware'] is True
               for client in _RecordingClient.instances)
    assert all(isinstance(client.kwargs.get('server_api'), ServerApi)
               and client.kwargs['server_api'].version == '1'
               for client in _RecordingClient.instances)
    assert _RecordingClient.accessed == []


def test_cloud_databaseNamesAreUnchanged(monkeypatch):
    module = _freshModule(monkeypatch, 'cloudprod')

    assert module.db.databaseName == 'x'
    assert module.pubSubMockDb.databaseName == 'pubSubMockDb'


@CLOUD
def test_cloud_usesTheEnvUriNotACommittedOne(monkeypatch, environment):
    envUri = FAKE_URI + '-' + environment
    _freshModule(monkeypatch, environment, {'MONGO_URI': envUri})

    assert [client.args for client in _RecordingClient.instances] == \
        [(envUri,)] * 2


@CLOUD
@UNSET_OR_EMPTY
def test_cloud_missingMongoUri_failsFast(monkeypatch, environment, value):
    # The fleet's clouddev branch may name its environment; cloudprod never does.
    suffix = '( for clouddev)?' if environment == 'clouddev' else ''
    with pytest.raises(Exception, match=r'^MONGO_URI environment variable is not set' + suffix + '$'):
        _freshModule(monkeypatch, environment, {'MONGO_URI': value})

    assert _RecordingClient.instances == []


def test_customUri_passesConnectThrough(monkeypatch):
    module = _freshModule(monkeypatch, 'cloudprod')

    deferred = module.mongoDb(FAKE_URI, 'x', connect=False)
    eager = module.mongoDb(FAKE_URI, 'x')

    assert deferred.client.args == (FAKE_URI,)
    assert deferred.client.kwargs['connect'] is False
    assert _connectOf(eager) is True


def test_localdev_pubSubMockDbStaysTzAwareAndDeferred(monkeypatch):
    client = _localClient(monkeypatch, mongoDbModule.PubSubMockDb, None, 'x')

    assert client.args == ('localhost', 27017)
    assert client.kwargs['tz_aware'] is True
    assert client.kwargs['connect'] is False


# >>> star-app per-repo: cloud client pins
# This repo's own cloud-client tests (a second cluster, a Singleton guard).
# <<< star-app per-repo: cloud client pins
