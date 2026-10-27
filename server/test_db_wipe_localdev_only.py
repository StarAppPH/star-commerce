"""deleteAllDataInDatabaseForDevEnvironment empties every collection, so it
runs in localdev only. clouddev runs against Atlas: a gate that also allowed
it (getIsDevEnvironment) would wipe that database after one wrong ENVIRONMENT
value. No test touches Mongo: the database under `db` is a fake that records
what a wipe the gate lets through would delete.
"""

import pytest

from mongoDb import db


class _FakeCollection:

    def __init__(self, wiped: list, name: str):
        self.wiped = wiped
        self.name = name

    def delete_many(self, query):
        self.wiped.append((self.name, query))


@pytest.fixture
def wipedCollections(monkeypatch) -> list:
    wiped: list = []
    monkeypatch.setattr(db, 'getAllCollections', lambda: ['Users'])
    monkeypatch.setattr(db, 'db', {'Users': _FakeCollection(wiped, 'Users')})
    return wiped


@pytest.mark.parametrize('environment, message', [
    ('clouddev', 'only available in the localdev environment'),
    ('localprod', 'only available in the localdev environment'),
    ('cloudprod', 'only available in the localdev environment'),
    # The app refuses to build its config for a value it does not know.
    ('staging', 'Invalid environment'),
])
def test_databaseWipe_isRefusedOutsideLocaldev(monkeypatch, wipedCollections,
                                               environment, message):
    monkeypatch.setenv('ENVIRONMENT', environment)
    with pytest.raises(Exception, match=message):
        db.deleteAllDataInDatabaseForDevEnvironment()
    assert wipedCollections == []


def test_databaseWipe_runsInLocaldev(monkeypatch, wipedCollections):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    db.deleteAllDataInDatabaseForDevEnvironment()
    assert wipedCollections == [('Users', {})]
