"""The response shape of the starter routes, called directly.

Each test validates a route's response against its responseModel and pins
its exact keys (assertExactShape): validation rejects a key the model does
not declare, but not a declared optional key the route stopped sending. route_config also
checks every call against the model (it raises in localdev), and
test_everyApiRoute_isCalledOutsidePytestRaises requires every route to be
called here (or in another test) outside pytest.raises.

Replace these with your own routes' tests as you replace the sample routes.
"""

from ApiRequests import ApiRequests
from conftest import assertExactShape, createTestUser, wipeCollections
from objects import NoContentResponse, RoleListResponse
from roles import AllRoles

ADMIN = 'rr-admin'

api = ApiRequests()


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
