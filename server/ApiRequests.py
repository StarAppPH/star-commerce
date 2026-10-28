"""
ApiRequests.py — Define your API endpoints here.

HOW IT WORKS:
  - Each method decorated with @route_config becomes a Flask route.
  - Run `ENVIRONMENT=localdev python AppCreator.py` to generate app.py, ServerRequests.ts, and schemas.
  - Methods are grouped into action classes, then combined into ApiRequests via inheritance.
  - A route another service calls over HTTP (a sibling repo, a job, a script)
    goes in actions/ServiceToServiceActions.py: its response is a contract
    with that service.

WRITING AN ENDPOINT:
  1. Add a method to an existing action class, or create a new one.
  2. Decorate it with @route_config(...), including responseModel=<the
     objects.py model describing what it returns>. AppCreator refuses to
     generate without one.
  3. Every parameter MUST have a type annotation (used for TypeScript generation).
  4. Return a dict with explicit keys, exactly the fields its responseModel
     (a ResponseModel) declares: read them with
     db.readResponse(s)(Model, query, collection), or build the dict. A list or a
     primitive is wrapped ({'todos': [...]}).

@route_config OPTIONS:
  - httpMethod:        'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH'
  - jwtRequired:       True  → requires JWT token in Authorization header
  - createAccessToken: True  → returns a new JWT (login endpoints only, cannot combine with jwtRequired)
  - successMessage:    Response message on success
  - roleAccess:        Role ID string → checks if user has this role before calling the method
  - responseModel:     The objects.py ResponseModel of the return value. The
                       generated client method returns
                       Promise<ApiResponse<Model>>, and in localdev (so in
                       tests) every call checks the return value against it,
                       raising on a mismatch or a key the model does not
                       declare; elsewhere the check is skipped. Required on
                       every route here (PubSubRequests handlers are exempt).

PARAMETER RULES:
  - GET/HEAD methods cannot have parameters.
  - POST methods receive parameters from the JSON body.
  - If jwtRequired=True, include `userId: str` as a parameter — it will be checked against the JWT identity.
  - Parameters named `publishToProdPubSub` and `publishToPubSubMockDb` are reserved for Pub/Sub.
  - A param is a str/int/float/bool, a Literal, an objects.py model, or a
    list/dict/tuple of those (`| None` allowed). Never a bare dict/list/Any,
    never datetime/date/time: app.py passes the raw JSON value, so take an
    ISO `str` and parse it with utils.appDayStart / appDayBounds /
    parseIsoInstant. A `= None` default needs `| None` in the annotation.

ADDING A NEW ACTION CLASS:
  1. Create a class with your methods.
  2. Add it to the ApiRequests inheritance list at the bottom.
  3. Add __init__ call in ApiRequests.__init__().

EXAMPLE (the models are in objects.py's docstring):
  class ProductActions():

      @route_config(httpMethod='POST', jwtRequired=True,
                    successMessage='Product created', responseModel=ProductRow)
      def createProduct(self, userId: str, name: str, price: float) -> dict:
          product = Product(name=name, price=price, _version=0)
          created = db.create(product.model_dump(by_alias=True), 'Products')
          return projectedCopy(ProductRow, created)

      @route_config(httpMethod='POST', jwtRequired=True,
                    successMessage='Products fetched',
                    responseModel=ProductListResponse)
      def listProducts(self, userId: str) -> dict:
          return {'products': db.readResponses(ProductRow, {}, 'Products')}
"""

from pydantic import ValidationError

from route_config import route_config
from actions.ChatActions import ChatActions
from actions.CustomerActions import CustomerActions
from actions.FulfillmentActions import FulfillmentActions
from actions.OrderActions import OrderActions
from actions.PaymentActions import PaymentActions
from actions.PhotoActions import PhotoActions
from actions.PolicyActions import PolicyActions
from actions.ProductActions import ProductActions
from actions.ServiceToServiceActions import ServiceToServiceActions
from actions.StoreActions import StoreActions
from objects import (DevUserListResponse, DevUserRow, FirebaseUserObject,
                     NoContentResponse, RoleListResponse, UserResponse,
                     projectedCopy)
from mongoDb import db
from AppConfig import Environment
from roles import AllRoles


def _requireLocalDev() -> None:
    """Refuse a dev-only route outside localdev."""
    if not Environment().getisLocalDevEnvironment():
        raise PermissionError('This endpoint is only available in localdev')


class AuthActions():

    @route_config(httpMethod='POST',
                  jwtRequired=False,
                  createAccessToken=True,
                  successMessage='Login successful',
                  responseModel=UserResponse)
    def loginWithGoogle(self, firebaseUserObject: FirebaseUserObject) -> dict:
        """Sign in (or sign up) the Google account the client's Firebase ID
        token proves. The uid, email and name come from the VERIFIED token,
        never from what the client says: the uid is the Users _id, an
        unknown uid creates a user, and the first user of an empty Users
        collection is made superAdmin."""
        from AuthHandler import signInIdentity, verifyFirebaseIdToken
        # app.py hands over the raw JSON dict; a direct caller may pass the
        # model itself. pydantic's message quotes the input (fragments of
        # the token), so a malformed body gets a generic refusal.
        try:
            firebaseUser = FirebaseUserObject.model_validate(
                firebaseUserObject)
        except ValidationError:
            raise ValueError('Invalid sign-in request') from None
        if firebaseUser.idToken:
            decodedToken = verifyFirebaseIdToken(firebaseUser.idToken)
            if (not decodedToken.get('email')
                    or decodedToken.get('email_verified') is not True):
                raise PermissionError(
                    'Sign in with a Google account that has a verified email')
            if (decodedToken.get('firebase')
                    or {}).get('sign_in_provider') != 'google.com':
                raise PermissionError('Sign in with your Google account')
            return projectedCopy(
                UserResponse,
                signInIdentity(decodedToken['uid'], decodedToken['email'],
                               decodedToken.get('name') or ''))
        if Environment().getisLocalDevEnvironment():
            # SECURITY: the client-supplied identity is trusted ONLY here,
            # for signing in locally without a Firebase project.
            if not firebaseUser.uid or not firebaseUser.email:
                raise ValueError('uid and email are required')
            return projectedCopy(
                UserResponse,
                signInIdentity(firebaseUser.uid, firebaseUser.email,
                               firebaseUser.displayName or ''))
        raise PermissionError('Firebase ID token is required')

    @route_config(httpMethod='POST',
                  jwtRequired=False,
                  successMessage='Dev user list fetched successfully',
                  responseModel=DevUserListResponse)
    def devGetUserList(self) -> dict:
        # localdev only: it hands every user's _id to anyone who asks, and
        # devLogin signs in as any of them.
        _requireLocalDev()
        return {
            'users':
            db.readResponses(DevUserRow, {}, 'Users')
        }

    @route_config(httpMethod='POST',
                  jwtRequired=False,
                  createAccessToken=True,
                  successMessage='Dev login successful',
                  responseModel=UserResponse)
    def devLogin(self, userId: str) -> dict:
        # localdev only: it signs in as any user without a credential.
        _requireLocalDev()
        user = db.readResponse(UserResponse, {'_id': userId}, 'Users')
        if not user:
            raise ValueError('User not found')
        return user


class RolesActions():

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Roles fetched successfully',
                  roleAccess='canUpdateUserRole',
                  responseModel=RoleListResponse)
    def getRoles(self, userId: str) -> dict:
        allRoles = AllRoles()
        return {
            'roles':
            [x.model_dump(by_alias=True) for x in allRoles.getAllRoles()]
        }

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='User role types fetched successfully',
                  roleAccess='canUpdateUserRole',
                  responseModel=NoContentResponse)
    def getUserRoleTypes(self, userId: str) -> dict:
        # The starter page reads no role types, so this sends nothing
        # (test_unused_response_fields.py). A page that shows them gives it
        # a responseModel with the fields it reads, built from
        # roles.UserRoleTypes.
        return {}


# Add your project-specific action classes below


# WORKED EXAMPLE OF A PUB/SUB PUBLISHER -- commented out so a fresh scaffold
# has no publisher and `pytest` stays green. The full narrative (flow diagram,
# rules, checklists) is ARCHITECTURE.md "Pub/Sub mock messages & contract
# tests"; the receiving side of this same story is the createOrder_accounting
# example in PubSubRequests.py and test_pubSubContracts.py.
#
# from pubSub import pubSubDecorator
# from services.exampleService import exampleService
#
#
# class OrderActions():
#
#     # @pubSubDecorator publishes the RETURN DICT of this method as a Pub/Sub
#     # message on the topic 'createOrder_starCommerce'. Every service in
#     # projectNameConsumers receives it in its PubSubRequests.py in a handler
#     # that must be named createOrder_starCommerce (<function>_<this
#     # repo's projectName>) and takes one `data` argument: this return dict.
#     # The handler has to exist in the consumer repo before this publishes in
#     # production, otherwise the message retries and dead-letters.
#     #
#     # - publishToProdPubSub MUST have no default and both flags can never be
#     #   True at once: pubSubDecorator raises ValueError at import / call
#     #   time. Production callers (app.py) pass True; tests pass False.
#     #   test_ifAllPubSubFunctionsGenerated additionally checks that every
#     #   call site in the top-level server/*.py files (non-recursive,
#     #   PubSubRequests.py excepted) passes the publishToProdPubSub keyword
#     #   explicitly, and that pubSubMockDataGenerator.py calls this method
#     #   with the literal publishToPubSubMockDb=True.
#     # - publishToPubSubMockDb defaults to False. ONLY pubSubMockDataGenerator
#     #   .py passes True, which stores the message in the shared local
#     #   pubSubMockDb for consumer repos to replay instead of publishing.
#     # - Where the message goes (pubSub.publishMessage): only cloudprod
#     #   publishes to the real topic, and only when at least one consumer
#     #   has PUBSUB_ENABLED_<SERVICE>=true. localdev never reaches GCP: a
#     #   mock row when publishToPubSubMockDb=True, otherwise a passive
#     #   'starCommerce_devData' capture row (skipped under pytest).
#     #   localprod/clouddev publish nothing.
#     # - @pubSubDecorator(..., publishToProdPubSub=False) makes a publisher
#     #   mock/replay-only: production never publishes it, generator rows are
#     #   still produced, and a call with publishToProdPubSub=True raises.
#     # - Keys in AppConfig.SECRET_RESPONSE_KEYS are stripped from the
#     #   published payload (not from the return value).
#     # - @pubSubDecorator goes OUTSIDE @route_config, so the response check
#     #   runs before the publish (test_publisherRoutes_checkTheResponseBefore
#     #   Publishing pins the order).
#     # - responseModel types the payload too: it IS what the route returns.
#     #   Order / OrderLine / OrderPayload are objects.py models you add: the
#     #   stored Order (a MongoModel) and OrderPayload, the ResponseModel
#     #   declaring exactly what is published. A param is never a bare list
#     #   or dict.
#     @pubSubDecorator(projectNameConsumers=[exampleService])
#     @route_config(httpMethod='POST',
#                   jwtRequired=True,
#                   successMessage='Order created',
#                   responseModel=OrderPayload)
#     def createOrder(self,
#                     userId: str,
#                     customerId: str,
#                     items: list[OrderLine],
#                     publishToProdPubSub: bool,
#                     publishToPubSubMockDb: bool = False) -> dict:
#         order = Order(customerId=customerId,
#                       items=items,
#                       createdBy=userId,
#                       _version=0)
#         # The return value IS the message payload: the stored document cut
#         # down to what OrderPayload declares -- never a bare variable,
#         # never {'data': order}.
#         created = db.create(order.model_dump(by_alias=True), 'Orders')
#         return projectedCopy(OrderPayload, created)
#
# Uncommenting it obliges you to (details: ARCHITECTURE.md "Adding a new
# publisher"):
#   1. add OrderActions to ApiRequests' bases and __init__ below, then run
#      `ENVIRONMENT=localdev python AppCreator.py` (app.py, ServerRequests.ts,
#      schemas regenerate);
#   2. point services/exampleService.py at the real consumer (projectName= to
#      its AppConfig().projectName if that differs from the filename) and
#      remove it from SERVICES_WITHOUT_MOCK_DATA_YET in
#      test_ifAllServicesMatchConsumerProjectName.py;
#   3. call it once in pubSubMockDataGenerator.py with the literal keywords
#      publishToProdPubSub=False, publishToPubSubMockDb=True, then run it;
#      and pin the payload in a test: compare lastPublishedPayload('createOrder')
#      (conftest.py) with what the route returned
#      (test_everyPublisherRoute_hasAPayloadPin fails without one);
#   4. in EVERY consumer repo add a createOrder_starCommerce handler to
#      PubSubRequests.py plus REPLAY_STEPS / REDELIVERY_STEPS entries in its
#      test_pubSubContracts.py (or its atExitValidation fails);
#   5. set PUBSUB_ENABLED_EXAMPLE_SERVICE=true in every environment that must
#      publish, then run pubSubAdmin.py from THIS repo BEFORE deploying: it
#      creates the topic and the push subscriptions
#      createOrder_starCommerce_to_<consumerService>, and checkPubSubTopics
#      aborts cloud start-up while the topic is missing.


class ApiRequests(AuthActions, RolesActions, StoreActions, CustomerActions,
                  ProductActions, OrderActions, PaymentActions,
                  FulfillmentActions, PolicyActions, ChatActions,
                  PhotoActions, ServiceToServiceActions):

    def __init__(self):
        AuthActions.__init__(self)
        RolesActions.__init__(self)
        StoreActions.__init__(self)
        CustomerActions.__init__(self)
        ProductActions.__init__(self)
        OrderActions.__init__(self)
        PaymentActions.__init__(self)
        FulfillmentActions.__init__(self)
        PolicyActions.__init__(self)
        ChatActions.__init__(self)
        PhotoActions.__init__(self)
