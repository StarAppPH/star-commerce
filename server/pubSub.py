from google.cloud import pubsub_v1
from AppConfig import AppConfig
from singleton import Singleton
import datetime
import json
import base64
import inspect
from utils import generateRandomString
from objects import stripSecretKeys
import functools
import os
import sys
from services.__abstractService import AbstractService
from typing import Callable, Dict, List, Optional, TYPE_CHECKING
from pydantic import BaseModel, ConfigDict, Field, field_validator, field_serializer
import google.auth
from google import api_core

if TYPE_CHECKING:
    from mongoDb import PubSubMockDb


class _LazyPubSubMockDb:
    """mongoDb.pubSubMockDb, imported on first use. Importing mongoDb builds
    every db singleton (and, locally, its indexes), which checkPubSubTopics.py
    never needs."""

    def __getattr__(self, name):
        from mongoDb import pubSubMockDb
        return getattr(pubSubMockDb, name)


pubSubMockDb = _LazyPubSubMockDb()


def _get_caller_name() -> str:
    # inspect.stack()[0] is this function,
    # [1] is Pydantic internals, so [2] is your caller.
    return inspect.stack()[3].function


def _getIndex():
    data = pubSubMockDb.read({}, AppConfig().projectName)
    return len(data)


class MockData(BaseModel):
    id: str = Field(default_factory=generateRandomString,
                    alias='_id',
                    description='Unique identifier for the mock data')
    version: int = Field(default=1,
                         description='Version of the mock data',
                         alias='_version')
    data: dict = Field(..., description='Mock data to be stored')
    dateOfCreation: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        description='Date of creation of the mock data')
    consumeFunctionName: str = Field(
        ..., description='Function name that will consume this data')
    createdByFunctionName: str = Field(
        default_factory=_get_caller_name,
        description='Function name that created this mock data')
    projectName: str = Field(
        default=AppConfig().projectName,
        description='Project name where the mockData is from')
    projectNameConsumers: list[str] = Field(
        ...,
        description='Project names where the mockData will be consumed from')
    index: int = Field(
        default_factory=_getIndex,
        description=
        'count of the mock data in the pubSubMockDb. This is used for testing so that we can know which message comes first'
    )
    type: str = Field(
        ...,
        description='Type of the mock data',
    )

    @field_validator("type", mode="before")
    @classmethod
    def validate_prod(cls, v):
        if v not in ['pubSub', 'request', 'devData']:
            raise ValueError("Type must be either 'pubSub' or 'request'")
        return v

    def checkIfValid(self):
        difference = datetime.datetime.now(
            datetime.timezone.utc) - self.dateOfCreation
        if difference > datetime.timedelta(days=3):
            raise Exception(
                'Mock data is older than 3 days. Please update it by running pubSubMockDataGenerator.py in the repository of the service that created this mock data.'
            )

    def getData(self):
        if self.type == 'pubSub':
            return self.data['body']['message']['data']
        else:
            return self.data

    model_config = ConfigDict(validate_assignment=True)  # ← so that __setattr__ still runs

    def __setattr__(self, name, value):
        # if they're trying to change one of our locked fields _after_ init, block it:
        if name in ('dateOfCreation',
                    'createdByFunctionName') and name in self.__dict__:
            raise AttributeError(f"{name!r} is read-only")
        super().__setattr__(name, value)


def createMockData(mockData: MockData, pubSubMockDb: 'PubSubMockDb') -> MockData:
    if mockData.type == 'pubSub':
        pubSubMockDb.create(mockData.model_dump(by_alias=True),
                            AppConfig().projectName)
    if mockData.type == 'devData':
        pubSubMockDb.create(mockData.model_dump(by_alias=True),
                            AppConfig().projectName + '_devData')


def decodeMessage(message):
    # Step 1: Decode the base64-encoded message
    decoded_message = base64.b64decode(message).decode('utf-8')

    # Step 2: Try to convert the decoded message to a JSON object
    try:
        # If it's a JSON string, convert it to a dictionary
        decoded_message = json.loads(decoded_message)
    except json.JSONDecodeError:
        # If it's not a JSON, keep the string as is
        pass

    return decoded_message


custom_retry = api_core.retry.Retry(
    initial=0.250,  # seconds (default: 0.1)
    maximum=90.0,  # seconds (default: 60.0)
    multiplier=1.45,  # default: 1.3
    deadline=300.0,  # seconds (default: 60.0)
    predicate=api_core.retry.if_exception_type(
        api_core.exceptions.Aborted,
        api_core.exceptions.DeadlineExceeded,
        api_core.exceptions.InternalServerError,
        api_core.exceptions.ResourceExhausted,
        api_core.exceptions.ServiceUnavailable,
        api_core.exceptions.Unknown,
        api_core.exceptions.Cancelled,
    ),
)


class TopicLookupError(Exception):
    """A get_topic that failed with anything but NotFound."""

    def __init__(self, topicName: str, error: Exception):
        super().__init__(f'could not check topic {topicName}: '
                         f'{type(error).__name__}: {error}')
        self.topicName = topicName


def _printUncheckedTopics(errors: list) -> None:
    print('\033[91m' + '=' * 60)
    print('ERROR: These PubSub topics could not be checked in GCP:')
    for error in errors:
        print(f'  - {error.topicName}')
        print(f'      {error}')
    print('=' * 60 + '\033[0m')


def _refuseMockDbWriteInCloud() -> None:
    """pubSubMockDb is a local test fixture: no cloud environment writes it,
    so a cloud process never opens its (connect=False) client. Read when a
    publish asks for the mock database, not at import, so it follows
    ENVIRONMENT."""
    if AppConfig().getIsCloudEnvironment():
        raise Exception(
            'Cannot publish to Pub/Sub mock database in a cloud environment')


class PubSub():

    def __init__(self):
        self.isProductionEnvironment = AppConfig().getIsProductionEnvironment()
        self._publisher = None
        self._subscriber = None
        self._projectId = None

    def _ensureClients(self):
        if self._publisher is not None:
            return
        credentials, project_id = google.auth.default()
        self._publisher = pubsub_v1.PublisherClient(credentials=credentials)
        self._subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
        self._projectId = project_id

    @property
    def projectId(self):
        self._ensureClients()
        return self._projectId

    @property
    def publisher(self):
        self._ensureClients()
        return self._publisher

    @property
    def subscriber(self):
        self._ensureClients()
        return self._subscriber

    def _checkIfTopicExists(self, topicName):
        """True when the topic exists, False when GCP answers NotFound. Any
        other get_topic error (permission, network, ...) raises
        TopicLookupError: the topic may exist, but the check cannot tell, so
        checkPubSubTopics reports it as such and fails. A client that cannot
        be built raises as it is (no TopicLookupError)."""
        topicPath = self.publisher.topic_path(self.projectId, topicName)
        try:
            self.publisher.get_topic(request={"topic": topicPath})
            return True
        except api_core.exceptions.NotFound:
            return False
        except Exception as e:
            raise TopicLookupError(topicName, e) from e

    def _sendMessageToTopic(self, message: str | dict, topicName: str):
        topicPath = self.publisher.topic_path(self.projectId, topicName)
        data_bytes = self._serialize_for_pubsub(message)
        attrs = {'originalTopicPath': topicPath}

        print('Publishing message to Pub/Sub...', topicPath)
        future = self.publisher.publish(topic=topicPath,
                                        data=data_bytes,
                                        retry=custom_retry,
                                        **attrs)
        return future.result(timeout=15)

    def publishMessage(
        self,
        message: str | dict,
        topicName: str = None,
        projectNameConsumers: list[str] = None,
        publishToPubSubMockDb=False,
        test=False,
        publishToProdPubSub=True,
    ):
        """Publish a message.

        Behaviors:
          - If running against real Pub/Sub (prod/test): only at the final step do we JSON-serialize (if dict) and encode to bytes.
          - If using local mock (publishToPubSubMockDb): we keep the original dict structure (no forced string conversion) so tests can assert on native JSON.

        Args:
          message: dict (preferred) or str. Dicts are preserved until serialization boundary.
        """

        if publishToPubSubMockDb:
            _refuseMockDbWriteInCloud()
        if not isinstance(message, (str, dict)):
            raise TypeError('message must be str or dict')

        # Skip the devData capture under pytest: test runs would otherwise append
        # every published payload to '<projectName>_devData' with nothing pruning it.
        isRunningUnderPytest = 'PYTEST_CURRENT_TEST' in os.environ
        if AppConfig().getisLocalDevEnvironment(
        ) and test == False and publishToProdPubSub == True and not isRunningUnderPytest:
            # add to mock db
            mockData = MockData(
                data=message,
                projectNameConsumers=[x.getProjectName() for x in projectNameConsumers],
                consumeFunctionName=topicName,
                index=0,
                type='devData',
            )
            createMockData(mockData, pubSubMockDb)

        if self.isProductionEnvironment or test:

            # Filter consumers to only services with PubSub enabled
            if projectNameConsumers is not None:
                enabledConsumers = [x for x in projectNameConsumers if x.isPubSubEnabled()]
                if not enabledConsumers:
                    return {"status": "success", "message": "Pub/Sub is disabled for all target services"}

            if publishToProdPubSub:
                message_id = self._sendMessageToTopic(message, topicName)
                try:
                    return {"status": "success", "message": message_id}
                except Exception as e:
                    return {"status": "error", "message": str(e)}
            else:
                return {"status": "success", "message": "mocked"}
        elif publishToPubSubMockDb:
            if projectNameConsumers is None:
                raise ValueError(
                    "projectNameConsumers must be provided when using local Pub/Sub mock."
                )

            # create mock payload (store dict as-is; str stays str)
            now_local = datetime.datetime.now(AppConfig().getTimezone())
            now_in_utc = now_local.astimezone(datetime.timezone.utc)
            formatted_datetime = now_in_utc.isoformat(
                timespec='milliseconds').replace('+00:00', 'Z')
            mockMessageId = generateRandomString()
            req = {
                'body': {
                    'message': {
                        'data': message,
                        'messageId': mockMessageId,
                        'message_id': mockMessageId,
                        'publishTime': formatted_datetime,
                        'publish_time': formatted_datetime
                    }
                }
            }
            createMockData(
                MockData(data=req,
                         projectNameConsumers=[
                             x.getProjectName() for x in projectNameConsumers
                         ],
                         consumeFunctionName=topicName,
                         index=_getIndex(),
                         type='pubSub'), pubSubMockDb)
            return {"status": "mocked", "stored": True}
        elif not self.isProductionEnvironment:
            return {
                "status":
                "success",
                "message":
                "Not published since we are in local dev and publishToMockDb is False"
            }
        else:
            raise Exception("Invalid publish mode")

    def _serialize_for_pubsub(self, message: str | dict) -> bytes:
        """Serialize message to bytes for Pub/Sub.
        Dict -> JSON bytes. Str -> UTF-8 bytes.
        """
        if isinstance(message, dict):
            return self.convertToJson(message).encode('utf-8')
        if isinstance(message, str):
            return message.encode('utf-8')
        raise TypeError('Unsupported message type for serialization')

    def _maybe_parse_json_string(self, message: str | dict):
        if isinstance(message, dict):
            return message
        try:
            return json.loads(message)
        except Exception:
            return message

    def pushToTestGoogleCloudFunction(self, message):
        return self.publishMessage('test', message, test=True)

    # def _createMessageInTopicsDatabase(self,message):

    # Custom JSON serializer for datetime objects
    def convertToJson(self, message):

        def customSerializer(obj):
            # if isinstance(obj, bytes):
            #     return obj
            if isinstance(obj, datetime.datetime):
                return obj.isoformat()  # Convert datetime to ISO format string
            raise TypeError(f"Type {type(obj)} is not serializable")

        res = json.dumps(message, default=customSerializer)
        return res


class MainPubSub(PubSub, Singleton):

    def __init__(self):
        super().__init__()


pubSub = MainPubSub()

_pubsubTopicRegistry = []


def pubSubDecorator(projectNameConsumers: list[AbstractService],
                    publishToProdPubSub: bool = True,
                    publishFunction: Callable = None):

    if projectNameConsumers is None:
        raise ValueError("projectNameConsumers parameter is required")
    for x in projectNameConsumers:
        if not isinstance(x, AbstractService):
            raise TypeError(
                "projectNameConsumers must contain only AbstractService instances"
            )

    # Default to pubSub.publishMessage if no custom function provided
    if publishFunction is None:
        publishFunction = pubSub.publishMessage

    def decorator(func):
        sig = inspect.signature(func)
        if 'publishToProdPubSub' in sig.parameters:
            param = sig.parameters['publishToProdPubSub']
            if param.default is not inspect._empty:
                raise ValueError(
                    f"{func.__name__}: publishToProdPubSub must not have a default value. "
                    "The value is controlled by the @pubSubDecorator."
                )

        func.publishToProdPubSub = publishToProdPubSub
        func.projectNameConsumers = projectNameConsumers
        _pubsubTopicRegistry.append((func.__name__, publishToProdPubSub))

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal publishToProdPubSub
            sig = inspect.signature(func)
            bound_args = sig.bind_partial(*args, **kwargs)
            try:
                bound_args.apply_defaults()
            except Exception:
                pass

            # Resolve using provided args or the function's defaults
            functionArgPublishToPubSubMockDb = bound_args.arguments.get(
                'publishToPubSubMockDb')
            functionArgPublishToProdPubSub = bound_args.arguments.get(
                'publishToProdPubSub')

            # Fallback (defensive) to signature defaults if apply_defaults() didn't populate
            if functionArgPublishToPubSubMockDb is None and 'publishToPubSubMockDb' in sig.parameters:
                param = sig.parameters['publishToPubSubMockDb']
                if param.default is not inspect._empty:
                    functionArgPublishToPubSubMockDb = param.default
            if functionArgPublishToProdPubSub is None and 'publishToProdPubSub' in sig.parameters:
                param = sig.parameters['publishToProdPubSub']
                if param.default is not inspect._empty:
                    functionArgPublishToProdPubSub = param.default

            if functionArgPublishToPubSubMockDb is None:
                raise ValueError(
                    f"publishToPubSubMockDb parameter is required for {func.__name__}")

            if functionArgPublishToProdPubSub is None:
                raise ValueError(
                    f"publishToProdPubSub parameter is required for {func.__name__}")

            if publishToProdPubSub == False and functionArgPublishToProdPubSub is True:
                raise ValueError(
                    f"publishToProdPubSub cannot be True in {func.__name__} "
                    "when the decorator sets publishToProdPubSub=False")

            if functionArgPublishToPubSubMockDb is True and functionArgPublishToProdPubSub is True:
                raise ValueError(
                    f"Cannot publish to both Prod and Mock Pub/Sub in {func.__name__}")

            result = func(*args, **kwargs)

            if result == 'idempotent':
                return result

            # Egress boundary: secrets listed in AppConfig.SECRET_RESPONSE_KEYS
            # never leave in pubsub payloads. The un-stripped result is still
            # returned so in-process callers keep full data; HTTP responses are
            # stripped by the ApiResponse.data serializer.
            pubSub.publishMessage(
                stripSecretKeys(result),
                projectNameConsumers=projectNameConsumers,
                publishToPubSubMockDb=functionArgPublishToPubSubMockDb,
                topicName=f'{func.__name__}_{AppConfig().projectName}',
                publishToProdPubSub=functionArgPublishToProdPubSub,
            )

            return result

        return wrapper

    return decorator


def checkPubSubTopics(topics: list[tuple[str, bool]] | None = None):
    """Deploy check (job_predeploy.py): exit 1 if a prod-enabled PubSub topic
    is missing in GCP, or if the topics cannot be looked up at all (no
    credentials, a client that cannot be built): it fails closed. Cloud only.
    topics: (function name, publishes to prod) pairs; by default the
    registry the imported publishers filled. checkPubSubTopics.py passes the
    ones pubsub_models.json lists, so it never imports the app."""
    if not AppConfig().getIsCloudEnvironment():
        return
    if topics is None:
        topics = _pubsubTopicRegistry
    if not topics:
        return
    try:
        missingTopics = []
        uncheckedTopics = []
        for funcName, prodEnabled in topics:
            if not prodEnabled:
                continue
            topicName = f'{funcName}_{AppConfig().projectName}'
            try:
                if not pubSub._checkIfTopicExists(topicName):
                    missingTopics.append(topicName)
            except TopicLookupError as e:
                uncheckedTopics.append(e)
        if uncheckedTopics:
            _printUncheckedTopics(uncheckedTopics)
            if not missingTopics:
                sys.exit(1)
        if missingTopics:
            print('\033[91m' + '=' * 60)
            print('ERROR: The following PubSub topics do not exist in GCP:')
            for t in missingTopics:
                print(f'  - {t}')
            print('Run pubSubAdmin.py to create them.')
            print('=' * 60 + '\033[0m')
            sys.exit(1)
    except Exception as e:
        print(f'\033[91mERROR: Could not verify PubSub topics: {e}\033[0m')
        sys.exit(1)


class PubsubMessage(BaseModel):
    """
    Wire-format Pub/Sub message.

    Notes:
      - `data` is stored as raw bytes in the model.
      - When exporting to JSON, `data` is serialized to base64 (string),
        matching Pub/Sub's REST/HTTP format.
      - `messageId` and `publishTime` are set by Pub/Sub (present on delivery).
      - `orderingKey` appears only if message ordering is enabled.
    """
    data: bytes = Field(default=b"", description="Raw payload; base64 in JSON")
    attributes: Dict[str, str] = Field(default_factory=dict)
    messageId: Optional[str] = None
    publishTime: Optional[datetime.datetime] = None
    orderingKey: Optional[str] = None

    # Accept data as bytes or base64 string
    @field_validator("data", mode="before")
    @classmethod
    def _decodeData(cls, v):
        if isinstance(v, bytes):
            message = decodeMessage(v)
            if not isinstance(message, dict):
                raise TypeError('data must be a dictionary once decoded')
            return v
        raise TypeError("data must be bytes or base64 string")

    # Serialize bytes -> base64 string for JSON export
    @field_serializer("data")
    def _serialize_data(self, v: bytes, _info):
        return base64.b64encode(v).decode("ascii")


class MockDataConsumer(Singleton):

    def __init__(self):
        self.previousIndexConsumed = {}
        self.indexConsumed = {}
        self.toConsumeIndex = {}
        # '*_devData' collections are passive capture logs (see publishMessage),
        # never consumed by tests; skip them like atExitValidation does.
        collectionNames = [
            x for x in pubSubMockDb.getAllCollectionNames()
            if not x.endswith('_devData')
        ]
        for x in collectionNames:
            if AppConfig().projectName == x:
                continue
            self.previousIndexConsumed[x] = 0
            self.indexConsumed[x] = []
            self.toConsumeIndex[x] = []

        for x in collectionNames:
            data = pubSubMockDb.read({},
                                     x,
                                     projection={
                                         'index': 1,
                                         'projectNameConsumers': 1
                                     })
            toConsume = [
                y['index'] for y in data
                if AppConfig().projectName in y['projectNameConsumers']
            ]
            self.toConsumeIndex[x] = toConsume

    def consumeMockData(
        self,
        serviceProjectName: str,
        function: Callable,
    ) -> None:

        mockData = pubSubMockDb.read(
            {
                'consumeFunctionName':
                function.__name__,
                'index':
                self.toConsumeIndex[serviceProjectName][
                    self.previousIndexConsumed[serviceProjectName]]
            },
            serviceProjectName,
            findOne=True)

        if mockData == None:
            mockData = pubSubMockDb.read(
                {
                    'index':
                    self.toConsumeIndex[serviceProjectName][
                        self.previousIndexConsumed[serviceProjectName]]
                },
                serviceProjectName,
                findOne=True)
            if mockData == None:
                raise Exception(
                    f'No mock data found for index {self.toConsumeIndex[serviceProjectName][self.previousIndexConsumed[serviceProjectName]]} in {serviceProjectName}. Please ensure that projectName and index exists'
                )
            raise Exception(
                f'Please use the function {mockData["consumeFunctionName"]} instead to simulate the same test from the {mockData["projectName"]} project. We do this so that all tests are synced among services.'
            )

        mockData = MockData(**mockData)
        mockData.checkIfValid()
        data = mockData.getData()

        indexConsumed = mockData.index
        self.indexConsumed[serviceProjectName].append(indexConsumed)
        self.previousIndexConsumed[serviceProjectName] += 1
        res = function(data)

        return {'res': res, 'message': data}

    def atExitValidation(self):

        allCollections = pubSubMockDb.getAllCollectionNames()
        for serviceProjectName in allCollections:
            if serviceProjectName[-8:] == '_devData':
                continue

            if AppConfig().projectName == serviceProjectName:
                continue
            data = pubSubMockDb.read(
                {},
                serviceProjectName,
            )

            allIndexesForThisService = [
                x['index'] for x in data
                if AppConfig().projectName in x['projectNameConsumers']
            ]

            if len(self.indexConsumed[serviceProjectName]) == 0 and len(
                    self.toConsumeIndex[serviceProjectName]) > 0:

                raise Exception(
                    f'No mock data has been consumed for the {serviceProjectName} service. Please ensure that the tests are running correctly.'
                )

            for i in allIndexesForThisService:
                if i not in self.indexConsumed[serviceProjectName]:
                    notConsumedData = [x for x in data if x['index'] == i][0]
                    raise Exception(
                        f'Mock data with function name {notConsumedData["consumeFunctionName"]} from {notConsumedData["projectName"]} has not been consumed. Please consume this data.'
                    )


# Only instantiate MockDataConsumer in non-production environments (used for testing)
if not AppConfig().getIsProductionEnvironment():
    class _LazyMockDataConsumer:
        _instance = None

        def __getattr__(self, name):
            if _LazyMockDataConsumer._instance is None:
                _LazyMockDataConsumer._instance = MockDataConsumer()
            return getattr(_LazyMockDataConsumer._instance, name)

    mockDataConsumer = _LazyMockDataConsumer()
else:
    mockDataConsumer = None

if __name__ == '__main__':
    # Example usage
    mockDataConsumer.atExitValidation()
