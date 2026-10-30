from contextlib import contextmanager
from typing import Any, Callable, Generator, Optional
from pymongo import MongoClient, ReturnDocument, ASCENDING
from pymongo.client_session import ClientSession
from pymongo.results import BulkWriteResult
from pymongo.server_api import ServerApi
from pymongo.errors import ConnectionFailure, OperationFailure, DuplicateKeyError
from AppConfig import AppConfig
from singleton import Singleton
import time
import os

# The unique index of a set-up store's slug (ensureIndexes), and the
# unfiltered one it replaced.
STORE_SLUG_INDEX = 'slug_setUp_unique'
LEGACY_STORE_SLUG_INDEX = 'slug_1'


@contextmanager
def _timed(label: str) -> Generator[None, None, None]:
    start = time.time()
    yield
    elapsed_ms = (time.time() - start) * 1000
    print(f"{label} Response time: {elapsed_ms:.2f} ms")


class OutdatedDataError(Exception):
    """db.update found no document at the expected _version: someone saved it
    first. Its fixed message is user-facing (routeErrors answers it with a
    400). Not a ValueError, so no `except ValueError` swallows it."""


class _ProjectionStage:
    """db.projectionStage's stage: expanded by aggregateResponses only."""

    def __init__(self, model, readWhole: tuple) -> None:
        self.model, self.readWhole = model, readWhole


def _sessionKwargs(session: Optional[ClientSession]) -> dict:
    """`session` as a read's keyword, only when there is one: the read-and-cut
    helpers call read()/aggregate() as a route would without one."""
    return {} if session is None else {'session': session}


def _cutResponse(model, doc: dict, prepare: Optional[Callable]) -> dict:
    """A document a read-and-cut helper read, prepared, then cut down to
    `model`."""
    from objects import projectedCopy
    if prepare is not None:
        doc = prepare(doc)
        if doc is None:
            raise TypeError('prepare returned None: it returns the document')
    return projectedCopy(model, doc)


class mongoDb():

    def __init__(self,
                 uri: Optional[str] = None,
                 databaseName: Optional[str] = None,
                 replicaSetEnabled: bool = False,
                 useMainDb: bool = True,
                 connect: bool = True) -> None:
        """if uri is None, connect to the appropriate uri based on the environment
            if uri is provided, we need the databaseName as well
            connect=False defers every pymongo connection (including the
            topology monitors) until the first operation
        """

        # connect to custom uri if provided
        if uri is not None:
            if databaseName is None:
                raise Exception(
                    'Database name is required when custom uri is provided')

            self.client = MongoClient(uri,
                                      server_api=ServerApi('1'),
                                      tz_aware=True,
                                      connect=connect)

        # connect to the appropriate uri based on the environment: both cloud
        # environments read the Atlas connection string from MONGO_URI (set on
        # the Cloud Run service; databaseName keeps clouddev and cloudprod apart)
        elif AppConfig().getIsCloudEnvironment():
            if useMainDb:
                uri = os.getenv('MONGO_URI')
                if not uri:
                    raise Exception(
                        'MONGO_URI environment variable is not set')
            else:
                if uri is None:
                    uri = os.getenv('MONGO_URI')
                    if not uri:
                        raise Exception(
                            'MONGO_URI environment variable is not set and no uri was provided')

            self.client = MongoClient(uri,
                                      server_api=ServerApi('1'),
                                      tz_aware=True,
                                      connect=connect)

        # testEnvironment is used for automated testing while the actual is used for production / development
        elif AppConfig().getEnvironment() == 'localdev':
            self.client = MongoClient('localhost', 27017, tz_aware=True,
                                      connect=connect)

        elif AppConfig().getEnvironment() == 'localprod':
            self.client = MongoClient('localhost', 27017, tz_aware=True,
                                      connect=connect)

        if useMainDb:
            self.databaseName = AppConfig().getDatabaseName(
            ) if databaseName is None else databaseName
        else:
            if databaseName is None:
                raise Exception(
                    'Database name is required when useMainDb is False')
            if AppConfig().getIsProductionEnvironment():
                self.databaseName = databaseName
            else:
                self.databaseName = 'test' + databaseName

        self.db = self.client[self.databaseName]

        self.replicaSetEnabled = self.checkReplicaSet()

    def ping(self) -> Optional[float]:
        try:
            start = time.time()
            self.client.admin.command('ping')
            elapsed_ms = (time.time() - start) * 1000
            print(f"Pinged your deployment. You successfully connected to MongoDB! Response time: {elapsed_ms:.2f} ms")
            return elapsed_ms
        except Exception as e:
            print(e)

    def ensureIndexes(self) -> None:
        """Create indexes for frequent query patterns."""
        # One store per admin, and every store link name taken once: both
        # enforced by the database, so two racing creates cannot both win.
        # Only a set-up store has a slug (a string), so the slug index is
        # partial: the many stores not set up yet never collide on it.
        self.db['Stores'].create_index('ownerUserId', unique=True)
        self.db['Stores'].create_index(
            'slug', unique=True, name=STORE_SLUG_INDEX,
            partialFilterExpression={'slug': {'$type': 'string'}})
        # The index it replaces: same key, no filter. The new one is built
        # first, so the slug stays unique throughout.
        if LEGACY_STORE_SLUG_INDEX in self.db['Stores'].index_information():
            self.db['Stores'].drop_index(LEGACY_STORE_SLUG_INDEX)
        # One policy per store.
        self.db['Policies'].create_index('storeId', unique=True)
        # A store's products, best sellers first (listStoreProducts).
        self.db['Products'].create_index([('storeId', 1), ('isAvailable', 1),
                                          ('soldCount', -1), ('name', 1)])
        # No two products of one store share a name (productNameKey), so
        # two racing adds of the same name cannot both win.
        self.db['Products'].create_index([('storeId', 1), ('nameKey', 1)],
                                         unique=True)
        # A store's orders, newest first, with or without a status
        # (getStoreOrders matches every status with $in when none is given,
        # so the prefix still applies).
        self.db['Orders'].create_index([('storeId', 1), ('status', 1),
                                        ('createdAt', -1)])
        # A customer's orders at one store, newest first (getMyOrders).
        self.db['Orders'].create_index([('customerId', 1), ('storeId', 1),
                                        ('createdAt', -1)])
        # The pending orders past their payment deadline, at every store
        # (job_cancelExpiredOrders.py's sweep) and at one store (the sweep
        # the order routes run first).
        self.db['Orders'].create_index([('status', 1), ('expiresAt', 1)])
        self.db['Orders'].create_index([('storeId', 1), ('status', 1),
                                        ('expiresAt', 1)])
        # A store's returns, newest first, with or without a status
        # (getStoreReturns), a customer's at one store (getMyReturns), and
        # an order's returns by status (requestReturn sums what is already
        # returned).
        self.db['Returns'].create_index([('storeId', 1), ('status', 1),
                                         ('createdAt', -1)])
        self.db['Returns'].create_index([('customerId', 1), ('storeId', 1),
                                         ('createdAt', -1)])
        self.db['Returns'].create_index([('orderId', 1), ('status', 1)])
        # A user's conversations at one store, newest first
        # (listMyConversations).
        self.db['Conversations'].create_index([('userId', 1), ('storeId', 1),
                                               ('lastMessageAt', -1)])
        # One turn per place in a conversation, read in order (the chat
        # history, getConversationTurns).
        self.db['ChatTurns'].create_index([('conversationId', 1),
                                           ('seq', 1)], unique=True)
        # The daily chat counters, deleted once their day is over.
        self.db['ChatUsage'].create_index('expireAt', expireAfterSeconds=0)
        # A store's generated photos, newest first (and the daily photo
        # counters, deleted once their day is over).
        self.db['GeneratedPhotos'].create_index([('storeId', 1),
                                                 ('createdAt', -1)])
        self.db['PhotoUsage'].create_index('expireAt', expireAfterSeconds=0)
        # The daily voice counters (seconds, characters), deleted once their
        # day is over.
        self.db['VoiceUsage'].create_index('expireAt', expireAfterSeconds=0)
        # What a chat turn is doing (chatThinking.py), deleted when the turn
        # ends, else soon after.
        self.db['ChatThinking'].create_index('expireAt', expireAfterSeconds=0)
        # The spoken thinking texts, looked up by text when one is removed
        # (chatThinking.syncThinkingSpeech).
        self.db['ThinkingSpeech'].create_index('text')

    def create(self, data: dict, collection_name: str, session: Optional[ClientSession] = None) -> dict:
        """Insert a document into the collection and return the created data."""
        print("Creating data in collection: " + collection_name)

        if '_version' not in data:
            raise ValueError("'_version' field is required when creating")

        with _timed(collection_name):
            result = self.db[collection_name].insert_one(data, session=session)
        return self.db[collection_name].find_one({"_id": result.inserted_id},
                                                 session=session)

    def createMany(self, data_list: list[dict], collection_name: str, session: Optional[ClientSession] = None) -> list:
        """Insert multiple documents into the collection."""
        print(f"Creating {len(data_list)} documents in collection: " +
              collection_name)

        for data in data_list:
            if '_version' not in data:
                raise ValueError("'_version' field is required when creating")

        with _timed(f"{collection_name} Bulk insert"):
            result = self.db[collection_name].insert_many(data_list,
                                                          session=session,
                                                          ordered=False)
        return result.inserted_ids

    def aggregate(self,
                  pipeline: list,
                  collection_name: str,
                  session: Optional[ClientSession] = None,
                  findOne: bool = False) -> Optional[dict] | list:
        if any(isinstance(stage, _ProjectionStage) for stage in pipeline):
            raise TypeError('a projectionStage reads sub-documents whole, so '
                            'only db.aggregateResponses (which cuts every row) '
                            'takes one')
        cursor = self.db[collection_name].aggregate(pipeline, session=session)
        if findOne:
            try:
                return next(cursor, None)
            except StopIteration:
                return None
        return list(cursor)

    def read(
        self,
        query: dict,
        collection_name: str,
        projection: dict = {},
        session: Optional[ClientSession] = None,
        findOne: bool = False,
        countOnly: bool = False,
        limit: Optional[int] = None,
    ) -> Optional[dict] | list | int:
        """Read documents from the collection."""
        with _timed(f"{collection_name} read {sorted(query)}"):
            if findOne:
                data = self.db[collection_name].find_one(query,
                                                         projection,
                                                         session=session)
            elif countOnly:
                data = self.db[collection_name].count_documents(query,
                                                                session=session)
            else:
                cursor = self.db[collection_name].find(query,
                                                       projection,
                                                       session=session)
                if limit is not None:
                    cursor = cursor.limit(limit)
                data = list(cursor)
        return data

    # The read-and-cut helpers: the reads a route makes to answer with what
    # it read. Each reads with `model`'s projection and cuts every document
    # with projectedCopy, so a sub-document read whole (one that may be null)
    # never goes out uncut. `readWhole` names top-level keys read whole: a
    # declared field a legacy-shape repair needs as stored, or a key the
    # route reads for its own use, which the cut then drops. `prepare(doc)`
    # runs on every document as read, before the cut (a legacy repair, an
    # enrichment, a check that raises), and returns it.

    def readResponses(self, model, query: dict, collection_name: str, *,
                      readWhole: tuple = (), prepare: Optional[Callable] = None,
                      **kwargs) -> list:
        """The documents `query` matches as `model` sends them. kwargs go to
        read(), except findOne (readResponse reads one) and countOnly."""
        from objects import _projection_of_for_cut
        for refused in ('findOne', 'countOnly'):
            if kwargs.get(refused):
                raise ValueError(
                    f'readResponses reads a list: {refused} is refused '
                    '(readResponse reads one document)')
        docs = self.read(query,
                         collection_name,
                         projection=_projection_of_for_cut(model, readWhole),
                         **kwargs)
        return [_cutResponse(model, doc, prepare) for doc in docs]

    def readResponse(self, model, query: dict, collection_name: str,
                     session: Optional[ClientSession] = None, *,
                     readWhole: tuple = (),
                     prepare: Optional[Callable] = None) -> Optional[dict]:
        """The one document `query` matches as `model` sends it: None when
        none matches (prepare does not run)."""
        from objects import _projection_of_for_cut
        doc = self.read(query,
                        collection_name,
                        projection=_projection_of_for_cut(model, readWhole),
                        findOne=True,
                        **_sessionKwargs(session))
        return None if doc is None else _cutResponse(model, doc, prepare)

    def projectionStage(self, model, readWhole: tuple = ()) -> '_ProjectionStage':
        """A $project stage reading what `model` declares (with readWhole), for
        the middle of an aggregateResponses pipeline (before a $group that
        pushes the rows, say). Only aggregateResponses expands it, and it cuts
        what comes out: db.aggregate refuses it."""
        return _ProjectionStage(model, tuple(readWhole))

    def aggregateResponses(self, model, pipeline: list, collection_name: str,
                           session: Optional[ClientSession] = None, *,
                           readWhole: tuple = (),
                           prepare: Optional[Callable] = None) -> list:
        """The rows `pipeline` yields as `model` sends them: every
        projectionStage expanded, the pipeline ended with model's projection
        (a $project stage appended, the input not changed) and every row cut,
        as readResponses does for a find."""
        from objects import _projection_of_for_cut
        stages = [
            {'$project': _projection_of_for_cut(stage.model, stage.readWhole)}
            if isinstance(stage, _ProjectionStage) else stage
            for stage in pipeline
        ]
        rows = self.aggregate(
            [*stages, {'$project': _projection_of_for_cut(model, readWhole)}],
            collection_name, **_sessionKwargs(session))
        return [_cutResponse(model, row, prepare) for row in rows]

    def readWithPagination(self,
                           query: dict,
                           collection_name: str,
                           page: int,
                           limit: Optional[int],
                           projection: dict = {},
                           sort: dict = {
                               'keyToSort': None,
                               'sortOrder': None
                           },
                           reverse: bool = False,
                           session: Optional[ClientSession] = None) -> dict:

        # add validations for page and limit
        if limit == None or limit < 1:
            if page > 1:
                raise ValueError(
                    "Page must be 1 if limit is None or less than 1")
        """Read documents from the collection with pagination."""
        with _timed(f"{collection_name} read {sorted(query)}"):
            cursor = self.db[collection_name].find(query,
                                                   projection,
                                                   session=session)

            if reverse:
                cursor = cursor.sort([('$natural', -1)])

            # Apply sorting if keyToSort is provided
            if sort['keyToSort'] and sort['sortOrder'] != 0:
                cursor = cursor.sort(sort['keyToSort'], sort['sortOrder'])

            if limit is not None:
                skip = (page - 1) * limit
                cursor = cursor.skip(skip).limit(limit + 1)
                data = list(cursor)
                hasNextPage = len(data) > limit
                if hasNextPage:
                    data = data[:limit]
            else:
                data = list(cursor)
                hasNextPage = False

        return {
            'data': data,
            'page': page,
            'limit': limit if limit is not None else len(data),
            'hasNextPage': hasNextPage
        }

    def getDatabaseName(self) -> str:
        """Get the name of the database."""
        return self.databaseName

    def bulkUpdate(self, operations: list[dict], collection_name: str, session: Optional[ClientSession] = None) -> Optional[BulkWriteResult]:
        """Perform bulk update operations on the collection.

        Args:
            operations: List of dicts with 'filter' and 'update' keys
                Example: [{'filter': {'_id': '123'}, 'update': {'$set': {'field': 'value'}}}]
            collection_name: Name of the collection to update
            session: Optional MongoDB session for transactions

        Returns:
            Result of the bulk write operation
        """
        if not operations:
            return None

        print(
            f'Performing bulk update in collection: {collection_name} with {len(operations)} operations'
        )

        from pymongo import UpdateOne

        bulk_ops = [
            UpdateOne(op['filter'],
                      op['update'],
                      upsert=op.get('upsert', False)) for op in operations
        ]

        with _timed(f"{collection_name} Bulk update"):
            result = self.db[collection_name].bulk_write(bulk_ops, session=session)

        print(f"Matched: {result.matched_count}, Modified: {result.modified_count}")
        return result

    def update(self,
               query: dict,
               new_values: dict,
               collection_name: str,
               checkVersion: bool = True,
               incrementVersion: bool = True,
               upsert: bool = False,
               session: Optional[ClientSession] = None) -> dict:
        """Update documents in the collection."""
        # Keys only: a query's values (emails, names) must not reach the logs.
        print('Updating data in collection: ' + collection_name +
              ' with query keys: ' + str(sorted(query)))

        # Build the filter for find_one_and_update
        if checkVersion:
            update_filter = {
                '_id': query['_id'],
                '_version': query['_version']
            }
        else:
            update_filter = {'_id': query['_id']}

        # Build the update operation - use a copy to avoid mutating caller's dict
        set_values = {k: v for k, v in new_values.items() if k != '_version'} if incrementVersion else dict(new_values)
        update_op = {'$set': set_values}
        if incrementVersion:
            update_op['$inc'] = {'_version': 1}

        with _timed(f"{collection_name} update"):
            updatedData = self.db[collection_name].find_one_and_update(
                update_filter,
                update_op,
                return_document=ReturnDocument.AFTER,
                session=session)

        if updatedData is None:
            if checkVersion:
                raise OutdatedDataError(
                    'Your data is outdated. Please refresh the page and try again.'
                )
            else:
                raise ValueError("No documents were modified.")

        return updatedData

    def delete(self, query: dict, collection_name: str, session: Optional[ClientSession] = None) -> int | str:
        """Delete documents from the collection."""
        # Keys only: a query's values (emails, names) must not reach the logs.
        print('Deleting data in collection: ' + collection_name +
              ' with query keys: ' + str(sorted(query)))

        # clouddev reaches Atlas too (MONGO_URI): no environment in the cloud
        # may empty a whole collection by accident.
        if query == {} and AppConfig().getIsCloudEnvironment():
            raise Exception(
                'You cannot delete all documents in the collection in a cloud environment'
            )

        deleted_result = self.db[collection_name].delete_many(query,
                                                              session=session)

        if deleted_result.deleted_count > 0:
            return deleted_result.deleted_count
        else:
            return 'No document to delete'

    def deleteMany(self, query: dict, collection_name: str, session: Optional[ClientSession] = None) -> int | str:
        """Delete multiple documents from the collection (alias for delete)."""
        return self.delete(query, collection_name, session=session)

    def getAllCollections(self) -> list[str]:
        """Get all collections in the database."""
        return self.db.list_collection_names()

    def ensureCollection(self, collection_name: str) -> None:
        """Create the collection if it does not exist, without inserting any
        document. delete_many/find never create a collection, so this is how a
        collection that must be visible while empty (e.g. this project's
        pubSubMockDb collection before it has any publisher) comes to exist."""
        if collection_name not in self.getAllCollectionNames():
            self.db.create_collection(collection_name)

    def deleteAllDataInDatabaseForDevEnvironment(self) -> None:
        """Delete all documents from every collection. localdev only: clouddev
        runs against Atlas, so it must refuse too."""

        if not AppConfig().getisLocalDevEnvironment():
            raise Exception(
                'This function is only available in the localdev environment')

        allCollections = self.getAllCollections()
        for x in allCollections:
            self.db[x].delete_many({})

    def createTransaction(self, updateFunction: Callable) -> Any:
        # replicaSetEnabled is True in the cloud (checkReplicaSet trusts
        # Atlas), so it alone decides: a test that switches ENVIRONMENT to
        # a cloud one on a standalone local mongod still runs without a
        # transaction, which that server could not start.
        if self.replicaSetEnabled:
            max_retries = 5
            for attempt in range(max_retries):
                with self.client.start_session() as session:
                    try:
                        session.start_transaction()
                        res = updateFunction(session)
                        session.commit_transaction()
                        return res

                    except OperationFailure as e:
                        labels = e.details.get("errorLabels",
                                               []) if e.details else []
                        if "TransientTransactionError" in labels and attempt < (
                                max_retries - 1):
                            session.abort_transaction()
                            # exponential back-off (e.g. 0.1, 0.2, 0.4, …)
                            time.sleep(0.1 * (2**attempt))
                            continue
                        else:
                            session.abort_transaction()
                            raise

                    except Exception:
                        session.abort_transaction()
                        raise

            raise RuntimeError(
                f"Transaction failed after {max_retries} attempts.")
        else:
            return updateFunction(None)

    def getAllCollectionNames(self) -> list[str]:
        """Alias of getAllCollections (both names are used across repos)."""
        return self.getAllCollections()

    def findOneAndUpdate(self,
                         query,
                         update,
                         collectionName,
                         upsert=True,
                         session=None,
                         projection=None) -> dict:
        """The document after `update`; `projection` (None: the whole
        document) limits what comes back."""
        result = self.db[collectionName].find_one_and_update(
            query,  # Filter: find a document with the specified unit.
            update,  # Update: increment the sequence field by 1.
            projection=projection,
            upsert=upsert,  # Create the document if it doesn't exist.
            return_document=ReturnDocument.AFTER,
            session=session  # Return the document after applying the update.
        )

        return result

    def checkReplicaSet(self) -> bool:
        """
        Atlas (cloud) is always a replica set, so the cloud returns True with no
        network call. Locally, asks the connected server (self.client), not a new
        localhost client.
        """
        if AppConfig().getIsCloudEnvironment():
            return True

        try:
            ismaster_info = self.client.admin.command("isMaster")
        except (ConnectionFailure, OperationFailure) as e:
            print(f"ERROR: Could not connect or run isMaster: {e}")
            return False

        return "setName" in ismaster_info


# The Singleton returns the one instance, but Python still runs __init__ on
# every call: each class returns early once _initialized, so a second
# mainDb() / TestDb() / PubSubMockDb() builds no second client.
class mainDb(mongoDb, Singleton):

    def __init__(self, uri: Optional[str] = None, databaseName: Optional[str] = None) -> None:
        if hasattr(self, '_initialized'):
            return
        super().__init__(uri, databaseName)
        # No index build at import in the cloud: create_index is a blocking
        # round trip per index, paid by every cold start and every Cloud Run
        # job. There the predeploy job (job_predeploy.py) builds them once per
        # deploy, before the new image serves. Locally (python app.py) they
        # are built here, as TestDb builds them for the tests.
        if not AppConfig().getIsCloudEnvironment():
            self.ensureIndexes()
        self._initialized = True


class TestDb(mongoDb, Singleton):

    def __init__(self) -> None:
        if hasattr(self, '_initialized'):
            return
        super().__init__(None, None)
        self.ensureIndexes()
        self._initialized = True


# connect=False: the Atlas Flex cluster caps connections, and pubSubMockDb is
# never used in the cloud (pubSub.publishMessage refuses to write it in every
# cloud environment), so pymongo opens no connection or topology monitor for
# it there. connect=False only defers: a first operation would still connect.
class PubSubMockDb(mongoDb, Singleton):

    def __init__(self, uri: Optional[str] = None, databaseName: Optional[str] = None) -> None:
        if hasattr(self, '_initialized'):
            return
        super().__init__(uri, databaseName, connect=False)
        self._initialized = True


db = mainDb()
pubSubMockDb = PubSubMockDb(None, 'pubSubMockDb')

if __name__ == '__main__':
    pass
