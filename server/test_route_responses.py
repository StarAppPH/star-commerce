"""The response shape of every sample route, called directly.

Each test validates a route's response against its responseModel and pins
its exact keys (assertExactShape): validation rejects a key the model does
not declare, but not a declared optional key the route stopped sending. route_config also
checks every call against the model (it raises in localdev), and
test_everyApiRoute_isCalledOutsidePytestRaises requires every route to be
called here (or in another test) outside pytest.raises.

Replace these with your own routes' tests as you replace the sample routes.
"""

import pytest

from ApiRequests import ApiRequests
from conftest import (assertExactShape, assertRowKeys, createTestUser,
                      wipeCollections)
from mongoDb import db
from objects import (HandshakeResponse, NoContentResponse, RoleListResponse,
                     SampleTodoListResponse, SampleTodoRow, SampleTodoView)
from roles import AllRoles

ADMIN = 'rr-admin'

api = ApiRequests()


def test_handshake():
    res = api.handshake()

    assertExactShape(HandshakeResponse, res)
    assert res == {
        'environment': 'localdev',
        'status': 'running'
    }


def test_sampleTodos_createListCompleteDelete():
    wipeCollections(['SampleTodos'])
    try:
        # The page reloads the list after a change and reads nothing from a
        # mutation's answer, so a mutation sends nothing; the stored todo is
        # checked instead.
        res = api.createTodo('Rr first todo')

        assertExactShape(NoContentResponse, res)
        assert res == {}
        [created] = db.read({}, 'SampleTodos')
        assertExactShape(SampleTodoView, created)
        assert created['title'] == 'Rr first todo'
        assert created['completed'] is False

        listed = api.getTodos()

        assertExactShape(SampleTodoListResponse, listed)
        assertRowKeys(listed['todos'], SampleTodoRow)
        assert listed['todos'] == [{
            '_id': created['_id'],
            'title': 'Rr first todo',
            'completed': False
        }]

        res = api.markTodoAsCompleted(created['_id'])

        assertExactShape(NoContentResponse, res)
        assert res == {}
        completed = db.read({'_id': created['_id']}, 'SampleTodos',
                            findOne=True)
        assert completed['completed'] is True
        assert completed['_version'] == created['_version'] + 1

        res = api.markTodoAsUnfinished(created['_id'])

        assertExactShape(NoContentResponse, res)
        assert res == {}
        assert db.read({'_id': created['_id']}, 'SampleTodos',
                       findOne=True)['completed'] is False

        res = api.deleteTodo(created['_id'])

        assertExactShape(NoContentResponse, res)
        assert res == {}
        assert db.read({}, 'SampleTodos') == []
        with pytest.raises(ValueError, match='^Todo not found$'):
            api.markTodoAsCompleted(created['_id'])
        # Refused before it is stored, not by the response check after.
        with pytest.raises(ValueError, match='title'):
            api.createTodo('')
        assert db.read({}, 'SampleTodos') == []
    finally:
        wipeCollections(['SampleTodos'])


def test_roles_list_andTheRoleTypesSendNothing():
    wipeCollections(['Users'])
    try:
        createTestUser(ADMIN)

        roles = api.getRoles(ADMIN)

        assertExactShape(RoleListResponse, roles)
        assert [role['_id'] for role in roles['roles']
                ] == AllRoles().getAllRoleIds()
        assert all(set(role) == {'_id', 'value', 'description'}
                   for role in roles['roles'])

        # The starter page reads no role types: the route sends nothing.
        roleTypes = api.getUserRoleTypes(ADMIN)

        assertExactShape(NoContentResponse, roleTypes)
        assert roleTypes == {}
    finally:
        wipeCollections(['Users'])
