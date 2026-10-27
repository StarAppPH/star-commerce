import copy
import functools
import inspect
import sys
from typing import Callable, Optional
from pydantic import BaseModel, ValidationError
from AppConfig import Environment
from objects import User, validate_route_response
from responseModelCheck import assertResponseModel
from mongoDb import db

HTTP_METHODS = ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD')

# How many failing fields a mismatch names before it summarises the rest. A
# list route with one bad field per row would otherwise render a multi-kilobyte
# message into the client's notification.
_MAX_MISMATCHES_IN_MESSAGE = 10


def _loc(loc) -> str:
    """An error's loc as a dotted path, without the validator steps pydantic
    names inside a union member (`function-wrap[...]`)."""
    return '.'.join(
        str(part) for part in loc
        if not (isinstance(part, str) and part.startswith('function-')))


def _check_response_model(func: Callable, result, responseModel) -> None:
    """Validate a route's return value against `responseModel` (its
    route_config one, or a publisher's PUBSUB_PAYLOAD_MODELS one: see
    check_route_response), raising ValueError on a mismatch.

    Runs in localdev only: tests run there, so contract drift fails a test an
    agent can see. The route_config wrapper skips it in every other
    environment, because validating (and deep-copying) every response would
    cost every production call time.

    Responses carry customer, supplier and staff data (localdev can hold the
    restored prod dump), so the message never includes input values: it names
    each failing field's `loc` with pydantic's `msg`, capped at
    _MAX_MISMATCHES_IN_MESSAGE plus a count, and never chains the
    ValidationError, whose str() quotes the input. A `msg` CAN still quote a
    value (a custom validator's own message, union_tag_invalid naming the tag
    it got), and a dict-typed field puts its keys in the `loc`.

    `result` is validated as is, not copied, so every before-validator must
    be copy-on-write (see objects.py). A deep-copy snapshot taken first is
    compared afterwards, and a validator that mutated the response raises.
    The comparison is `==`: a mutation that leaves the response equal (a list
    swapped for an equal copy) is not caught, and a float('nan') that a
    validator replaced with another NaN object reads as a mutation, since
    NaN never equals itself (deepcopy keeps the same float object, so a
    NaN left alone compares equal by identity).

    Validation goes through objects.validate_route_response, under the
    response-check context, so the tests' assertMatchesModel and this check
    can never disagree.

    A ResponseModel rejects, under that context, every key it does not
    declare, at any depth: the route must send exactly what its model
    declares (objects.ResponseModel).

    Known limitations:
      - Some validators repair what they validate (the empty-string
        coercion turns '' into None). The check passes on the repaired value
        while the client receives the raw one.
    """
    snapshot = copy.deepcopy(result)
    errors = None
    try:
        validate_route_response(responseModel, result)
    except ValidationError as e:
        errors = e.errors(include_input=False,
                          include_url=False,
                          include_context=False)
    except Exception as e:
        # A validator that crashes instead of rejecting is reported like a
        # mismatch. Only the exception type is kept — its message may quote
        # the data.
        errors = [{
            'loc': (),
            'msg': f'validation crashed with {type(e).__name__}'
        }]

    if result != snapshot:
        raise ValueError(
            f'validating the response of {func.__name__} against '
            f'{responseModel.__name__} mutated it: before-validators must '
            'copy, not mutate')
    if errors is None:
        return

    mismatches = [
        f"{_loc(err['loc']) or '<root>'}: "
        f"{err['msg']}" for err in errors[:_MAX_MISMATCHES_IN_MESSAGE]
    ]
    if len(errors) > _MAX_MISMATCHES_IN_MESSAGE:
        mismatches.append(
            f'and {len(errors) - _MAX_MISMATCHES_IN_MESSAGE} more')
    # Raised outside the except blocks on purpose, so the ValidationError
    # (whose str() quotes input values) is not chained onto it.
    raise ValueError(
        f'{func.__name__} returned a response that does not match its '
        f'responseModel {responseModel.__name__}: '
        f"{'; '.join(mismatches)}")


def check_route_response(func: Callable, result) -> None:
    """The response check route_config runs on every return value of `func`
    (localdev only; see _check_response_model), also for a payload published
    on its Pub/Sub topic outside the route (a hand-publish site), or by a
    publisher that is no route. Consumers parse the topic with a model
    generated from the payload model (pubSubModels.py), so every payload on
    it is held to that model: func.responseModel, else the
    PUBSUB_PAYLOAD_MODELS entry of func's module (the two pubSubModels.py
    reads). Raises ValueError when func has neither."""
    if Environment().getisLocalDevEnvironment():
        _check_response_model(func, result, _payload_model(func))


def _payload_model(func: Callable):
    model = getattr(func, 'responseModel', None)
    if model is not None:
        return model
    module = sys.modules.get(func.__module__)
    declared = getattr(module, 'PUBSUB_PAYLOAD_MODELS', {}).get(func.__name__)
    model = getattr(module, declared, None) if isinstance(declared, str) else None
    if model is None:
        raise ValueError(
            f'{func.__name__} has no payload model: give its @route_config a '
            'responseModel, or add it to PUBSUB_PAYLOAD_MODELS in '
            f'{func.__module__}')
    return model


def route_config(httpMethod: str = 'GET',
                 jwtRequired: bool = False,
                 createAccessToken: bool = False,
                 successMessage: str = 'Success',
                 roleAccess: Optional[str | list[str]] = None,
                 responseModel: Optional[type[BaseModel]] = None,
                 serviceOnly: bool = False) -> Callable:
    """`responseModel` is the ResponseModel (defined in objects.py) declaring
    exactly what the route returns. AppCreator types the generated client
    method as `Promise<ApiResponse<Model>>`, and in localdev every call checks the
    return value against it (see _check_response_model). The route still
    returns its own value unchanged — the model only describes it. Every
    ApiRequests route declares one (AppCreator refuses to generate
    otherwise); a PubSubRequests push handler may omit it, since its return
    value only goes back to Pub/Sub. Routes return a dict with explicit keys: a
    RootModel (a bare list or number) is accepted here, but AppCreator
    allows one only for a route in CROSS_REPO_BARE_RESPONSE_ROUTES, whose
    consumers in other services read that bare value.

    `serviceOnly=True` marks a route only another service calls (every route
    in actions/ServiceToServiceActions.py, and no other): AppCreator emits
    @requireServiceCaller for it, which refuses a caller without a Google ID
    token from callerAuth.SERVICE_CALLER_ACCOUNTS outside localdev. It takes no
    JWT."""

    def decorator(func: Callable) -> Callable:
        # 1) Validate decorator arguments
        if httpMethod not in HTTP_METHODS:
            raise ValueError('Invalid http method')
        if not isinstance(jwtRequired, bool):
            raise ValueError('Invalid jwtRequired value')
        if not isinstance(createAccessToken, bool):
            raise ValueError('Invalid createAccessToken value')
        if createAccessToken and jwtRequired:
            raise ValueError(
                'createAccessToken and jwtRequired cannot be True at the same time'
            )
        if not isinstance(serviceOnly, bool):
            raise ValueError('Invalid serviceOnly value')
        if serviceOnly and (jwtRequired or createAccessToken
                            or roleAccess is not None):
            # Its caller is a service, verified by callerAuth.py: there is no
            # user, so no JWT, sign-in or role.
            raise ValueError(
                f'{func.__name__}: serviceOnly takes no jwtRequired, '
                'createAccessToken or roleAccess')
        if roleAccess is not None and not jwtRequired:
            # The role check reads the user named by the userId the body
            # sends; only a JWT route ties that userId to the caller, so on
            # any other route anyone could name an admin and pass it.
            raise ValueError(
                f'{func.__name__}: roleAccess needs jwtRequired=True')
        if responseModel is not None:
            # A ResponseModel all the way down (responseModelCheck.py), or the
            # import fails here, naming the route.
            assertResponseModel(func.__name__, responseModel)

        # 2) Attach metadata to the function object
        func.httpMethod = httpMethod
        func.jwtRequired = jwtRequired
        func.createAccessToken = createAccessToken
        func.successMessage = successMessage
        func.roleAccess = roleAccess
        func.responseModel = responseModel
        func.serviceOnly = serviceOnly

        # 3) Create the call-wrapper that runs before invoking func
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            if func.roleAccess != None:
                # Bind args/kwargs to parameter names so we can extract userId
                sig = inspect.signature(func)
                bound_args = sig.bind_partial(*args, **kwargs)
                userId = bound_args.arguments.get('userId')

                if userId is None:
                    raise ValueError(
                        f'userId parameter is required in function {func.__name__} for role access validation'
                    )

                # Fetch user data from MongoDB
                user_data = db.read({'_id': userId}, 'Users', findOne=True)
                if not user_data:
                    raise ValueError('User not found')

                user = User(**user_data)

                # Support both single role and list of roles (OR logic)
                roles_to_check = func.roleAccess if isinstance(func.roleAccess, list) else [func.roleAccess]
                authorized = any(user.isAuthorized(role) for role in roles_to_check)
                if not authorized:
                    if len(roles_to_check) == 1:
                        from roles import AllRoles
                        raise ValueError(AllRoles().getSpecificRole(roles_to_check[0]).unauthorizedMessage)
                    else:
                        raise ValueError("You are not authorized to access this resource")

            result = func(*args, **kwargs)
            if func.responseModel is not None:
                # Localdev only, read per call: see _check_response_model.
                check_route_response(func, result)
            return result

        # Enforce no parameters for GET or HEAD methods
        if httpMethod in ["GET", "HEAD"]:
            sig = inspect.signature(func)
            params = [
                p for p in sig.parameters.values()
                if p.name not in ("self", "cls")
            ]
            if len(params) > 0:
                raise ValueError(
                    f"{func.__name__} cannot have parameters when httpMethod is GET or HEAD"
                )

        return wrapped

    return decorator
