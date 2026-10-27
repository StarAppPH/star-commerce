"""ServiceToServiceActions: the routes another service calls over HTTP.

A route here is called by a sibling repo (its services/<name>.py client), a
job or a script, not by this repo's client. Its response is a contract with
that service: the caller reads it field by field, so never trim it to what
this repo's client reads.

test_unused_response_fields.py skips every route defined in this class, and
checks the other way round too: a route a sibling calls over HTTP must be
here, and a route here must be called by a sibling.

Every route here declares `@route_config(serviceOnly=True)`, and no route
elsewhere does (test_callerAuth.py): outside localdev it answers only a
sibling sending a Google ID token from callerAuth.SERVICE_CALLER_ACCOUNTS
(services/__request.py sends one on Cloud Run). It takes no JWT.

Combined into ApiRequests like every other actions mixin. Empty in a new
project.
"""


class ServiceToServiceActions():
    pass
