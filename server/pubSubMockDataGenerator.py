"""
Generates the Pub/Sub mock messages that OTHER repos replay in their own
test_pubSubContracts.py. This is the PUBLISHER half of the contract-test
system; the consumer half is documented at the top of this repo's
test_pubSubContracts.py and the full narrative lives in ARCHITECTURE.md
("Pub/Sub mock messages & contract tests").

WHAT IT DOES
  Calls every @pubSubDecorator function in this repo exactly once with
  publishToProdPubSub=False, publishToPubSubMockDb=True. Instead of publishing
  to production Pub/Sub, the decorator stores the function's return dict as a
  MockData row in the SHARED local MongoDB database `pubSubMockDb`, collection
  `starCommerce` (this repo's AppConfig().projectName), addressed to every
  consumer's service.getProjectName() and tagged with a running `index`.
  Consumer repos replay those rows, in `index` order, through their
  PubSubRequests handler named `<function>_starCommerce`.

RULES
  - localdev only: every entry point raises RuntimeError otherwise. It WIPES
    every collection in TEST_COLLECTIONS before and after generating, so never
    point it at a database you care about: run it with
    `ENVIRONMENT=localdev databaseName=teststarCommerceScratch` (the default
    teststarCommerce holds the restored prod dump, and the name is not
    checked here).
  - `__main__` first deletes this repo's whole pubSubMockDb collection so the
    `index` sequence restarts at 0 and no stale row lingers (consumers reject
    rows older than 3 days -- MockData.checkIfValid -- so re-run this whenever
    a consumer suite complains, or after another repo's suite wiped it).
  - The literal keyword `publishToPubSubMockDb=True` must appear on the call
    site itself: test_ifAllPubSubFunctionsGenerated finds the calls with an
    AST scan of this file, so a variable, a **kwargs dict or a helper wrapper
    does not count. Every @pubSubDecorator function in this repo must be
    called here.
  - Order matters: consumers replay in the order emitted here, so emit a
    create before the update/delete that touches the same document, and only
    seed (db.create) what a publisher function needs to run -- the message
    payload is whatever the function RETURNS.
  - Cross-repo order: run every PUBLISHER's generator before running a
    CONSUMER's test_pubSubContracts.py, and never run two repos' generators
    or test suites at the same time -- they all share pubSubMockDb.

Run from server/ with ENVIRONMENT=localdev:
    python pubSubMockDataGenerator.py
"""
from AppConfig import AppConfig
from mongoDb import db, pubSubMockDb

# Every collection this generator's setup and this repo's Pub/Sub handlers
# write, plus PubSubMessages (the generated route's messageId de-dupe log,
# appPubSub.py -- handlers called directly never write it, but a leftover from
# a route-level test would). This is the ONE list: test_pubSubContracts.py
# imports it and wipeDatabase() below, so the generator's wipe and the
# replay's wipe cannot drift apart. Extend it whenever a publisher's setup or
# a handler writes somewhere new.
TEST_COLLECTIONS = ['Users', 'Bootstrap', 'SampleTodos', 'PubSubMessages']


def wipeDatabase():
    """Empty TEST_COLLECTIONS of this repo's local test database. localdev
    only: under clouddev the same code would wipe the shared cloud dev DB."""
    if not AppConfig().getisLocalDevEnvironment():
        raise RuntimeError('wipeDatabase only runs in localdev')
    for collectionName in TEST_COLLECTIONS:
        db.delete({}, collectionName)


class pubSubMockDataGenerator():

    def create(self):
        if not AppConfig().getisLocalDevEnvironment():
            raise RuntimeError('pubSubMockDataGenerator only runs in localdev')
        try:
            wipeDatabase()

            # WORKED EXAMPLE (commented out: a fresh scaffold has no
            # @pubSubDecorator function, so this method is deliberately a
            # no-op). Once the `createOrder` publisher from the example in
            # ApiRequests.py is uncommented, this is the block that feeds it:
            #
            # from ApiRequests import ApiRequests
            # api = ApiRequests()
            #
            # # --- createOrder -> exampleService ---------------------------
            # # Seed only what createOrder READS (use your test factories once
            # # you have them); the Orders document it creates is the payload
            # # the consumer will replay.
            # customer = db.create(
            #     {
            #         '_id': 'mock-customer',
            #         '_version': 0,
            #         'name': 'Mock Customer'
            #     }, 'Customers')
            # api.createOrder(userId='mock-user',
            #                 customerId=customer['_id'],
            #                 items=[{
            #                     'unitId': 'mock-unit-1',
            #                     'quantity': 2
            #                 }],
            #                 publishToProdPubSub=False,
            #                 publishToPubSubMockDb=True)
            #
            # ... and add 'Customers' and 'Orders' to TEST_COLLECTIONS.
            pass
        finally:
            wipeDatabase()


if __name__ == '__main__':
    if not AppConfig().getisLocalDevEnvironment():
        raise RuntimeError('pubSubMockDataGenerator only runs in localdev')
    pubSubMockDb.delete({}, AppConfig().projectName)
    # delete_many never creates a collection, so with zero publishers this
    # repo would leave no `starCommerce` collection behind. Two things need
    # it to exist even when empty: consumer repos'
    # test_ifAllServicesMatchConsumerProjectName (a service's getProjectName()
    # must be an existing pubSubMockDb collection) and, on a fresh machine,
    # this repo's own _getConsumedMockFunctionNames, which refuses to run
    # against a pubSubMockDb with no collections at all. ensureCollection
    # creates it without inserting a placeholder document.
    pubSubMockDb.ensureCollection(AppConfig().projectName)
    pubSubMockDataGenerator().create()
    print("pubSubMockDataGenerator executed successfully.")
