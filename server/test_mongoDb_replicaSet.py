"""checkReplicaSet runs on every mongoDb object. In the cloud it trusts Atlas
(always a replica set) and makes no network call: a probe of localhost there
cost every db object a 5 s timeout at cold start. Locally it asks the server
the object is already connected to (self.client), never a new MongoClient.

The checks run on a bare mongoDb (no __init__), so the shared `db` singleton
is never patched.
"""

from unittest.mock import MagicMock, patch

from pymongo.errors import ConnectionFailure, OperationFailure

import mongoDb as mongoDbModule
from AppConfig import AppConfig
from mongoDb import db


def _bareMongoDb():
    return mongoDbModule.mongoDb.__new__(mongoDbModule.mongoDb)


def test_checkReplicaSet_trustsAtlasInCloud_asksConnectedServerLocally():
    probeDb = _bareMongoDb()

    # Cloud: True without building a client or sending a command
    probeDb.client = MagicMock()
    with patch.object(AppConfig, 'getIsCloudEnvironment', return_value=True), \
            patch.object(mongoDbModule, 'MongoClient') as mockMongoClient:
        assert probeDb.checkReplicaSet() is True
    mockMongoClient.assert_not_called()
    probeDb.client.admin.command.assert_not_called()

    # Local: the connected server's isMaster answer decides
    for isMasterAnswer, expected in (({'ismaster': True, 'setName': 'rs0'}, True),
                                     ({'ismaster': True}, False)):
        probeDb.client = MagicMock()
        probeDb.client.admin.command.return_value = isMasterAnswer
        with patch.object(AppConfig, 'getIsCloudEnvironment', return_value=False), \
                patch.object(mongoDbModule, 'MongoClient') as mockMongoClient:
            assert probeDb.checkReplicaSet() is expected
        probeDb.client.admin.command.assert_called_once_with('isMaster')
        mockMongoClient.assert_not_called()

    # Local: an unreachable server or a refused command means no replica set
    for error in (ConnectionFailure('server down'), OperationFailure('isMaster refused')):
        probeDb.client = MagicMock()
        probeDb.client.admin.command.side_effect = error
        with patch.object(AppConfig, 'getIsCloudEnvironment', return_value=False), \
                patch.object(mongoDbModule, 'MongoClient') as mockMongoClient:
            assert probeDb.checkReplicaSet() is False
        probeDb.client.admin.command.assert_called_once_with('isMaster')
        mockMongoClient.assert_not_called()

    # Local, real mongod: a spy over db's connected client, whatever mode it runs in
    expected = 'setName' in db.client.admin.command('isMaster')
    probeDb.client = MagicMock(wraps=db.client)
    with patch.object(AppConfig, 'getIsCloudEnvironment', return_value=False), \
            patch.object(mongoDbModule, 'MongoClient') as mockMongoClient:
        assert probeDb.checkReplicaSet() == expected
    probeDb.client.admin.command.assert_called_once_with('isMaster')
    mockMongoClient.assert_not_called()
