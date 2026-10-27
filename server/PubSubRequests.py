"""
PubSubRequests.py — Define your Pub/Sub receiver endpoints here.

HOW IT WORKS:
  - This file handles INCOMING messages from other services via Google Cloud Pub/Sub.
  - Each method decorated with @route_config becomes a Pub/Sub push endpoint.
  - Run `python AppCreator.py` to generate appPubSub.py.
  - When another service publishes a message, Pub/Sub pushes it to the generated endpoint.

WRITING A PUB/SUB RECEIVER:
  1. Create an action class grouping related receivers (e.g., AccountingToMyAppActions).
  2. Decorate each method with @route_config(httpMethod='POST', jwtRequired=False, ...).
  3. The method receives a single `data` parameter — the decoded Pub/Sub message payload.
  4. Add the action class to PubSubRequests inheritance and __init__.

NAMING CONVENTION:
  - Name methods as: <functionName>_<publisherProjectName>
  - <functionName> is the publisher's @pubSubDecorator method and
    <publisherProjectName> is that repo's AppConfig().projectName (NOT the
    filename of your services/<name>.py, unless they happen to match).
  - e.g., createOrder_accounting → receives the return dict of the accounting
    repo's ApiRequests.createOrder, published on topic 'createOrder_accounting'.

THE CONSUMER-SIDE CONTRACT (the narrative is in ARCHITECTURE.md, "Pub/Sub mock
messages & contract tests"; test_pubSubContracts.py enforces it):
  - `data` is exactly the dict the publisher's method RETURNED. Do not guess
    its shape: read the publisher, or the MockData row its
    pubSubMockDataGenerator.py wrote to pubSubMockDb.<publisherProjectName>.
  - Return a dict with explicit keys (the stored document, {'updated': 1},
    {'deleted': True}, ...). Never a bare variable.
  - Idempotent on redelivery. GCP redelivers any message whose handler did not
    return 200. The generated route de-dupes most redeliveries by messageId,
    but PubSubMessages is written AFTER the handler returns, so a crash
    between the handler and the 200 redelivers the same messageId into the
    handler again: a handler that writes must be a no-op the second time it
    sees the same payload. Look the document up by its natural key first and
    return the existing doc / {'updated': 0} / {'deleted': False} instead of
    inserting twice. test_pubSubContracts.py replays every message twice to
    prove this (REDELIVERY_STEPS).
  - Never raise on a BENIGN missing document (the order was already deleted,
    the customer never synced): a raise → 400 → GCP retries the message up to
    max_delivery_attempts (5, pubSubAdmin.py) and then dead-letters it.
    Return the idempotent shape ({'updated': 0}, {'deleted': False}) instead.
    Raise only on a genuine data fault (a payload that cannot be valid --
    e.g. it references a customer that must exist), where a retry and then
    the dead-letter topic is the right outcome.
  - Every handler must have a REPLAY_STEPS + REDELIVERY_STEPS entry in
    test_pubSubContracts.py (seed the prerequisites it reads, replay the real
    mock message through it, assert the DB state it leaves behind) or be
    listed in UNTESTED_PUBSUB_FUNCTIONS there -- which is only for handlers no
    publisher will ever address: `ping`, and `createDeadLetter` if you add
    the dead-letter receiver AppCreator recognises. A handler no publisher
    addresses fails test_ifAllPubSubMockDataHasFunctions; a message no
    handler receives fails test_atExitValidation.
  - Everything in this file is a receiver. A method here never needs
    @pubSubDecorator or the publishTo* flags; publishing lives in ApiRequests.
  - Parse the message ONCE, with its generated model:
    `message = <Fn>Message.model_validate(data)`, imported from
    objects_<publisherProjectName>.py (python pubSubModels.py --pull writes it
    from the publisher's committed server/pubsub_models.json; never edit it).
    Read everything from `message`; touch `data` again only inside an
    `except ValidationError:` on that parse (a fallback for a message that
    fails its model). test_pubSubModels.py's
    test_everyHandlerParsesItsGeneratedMessage enforces it.
  - Build what you store through this repo's own models, never a dump of the
    message; keep the logic in plain helpers that take the message (the
    inline db calls below keep the example short).

EXAMPLE (the same createOrder story as the commented publisher in
ApiRequests.py and the REPLAY_STEPS example in test_pubSubContracts.py, seen
from the receiving side -- `accounting` is the publisher's projectName):
  class AccountingToMyAppActions():

      @route_config(httpMethod='POST',
                    jwtRequired=False,
                    successMessage='Order received from accounting')
      def createOrder_accounting(self, data):
          # from objects_accounting import CreateOrderMessage (generated)
          message = CreateOrderMessage.model_validate(data)
          # redelivery: the order is already here, hand it back unchanged
          existing = db.read({'_id': message.id}, 'Orders', findOne=True)
          if existing is not None:
              return existing
          # genuine data fault: an order for a customer this repo has never
          # seen cannot be valid, so let it retry and then dead-letter
          customer = db.read({'_id': message.customerId}, 'Customers',
                             findOne=True)
          if customer is None:
              raise ValueError(f"Unknown customer {message.customerId}")
          order = MyOrder(_id=message.id, customerId=message.customerId,
                          total=message.total, _version=0)
          return db.create(order.model_dump(by_alias=True), 'Orders')

      @route_config(httpMethod='POST',
                    jwtRequired=False,
                    successMessage='Item updated from accounting')
      def updateItem_accounting(self, data):
          # from objects_accounting import UpdateItemMessage (generated)
          message = UpdateItemMessage.model_validate(data)
          # benign missing document: nothing to update, do not raise
          stored = db.read({'_id': message.id}, 'Items', findOne=True)
          if stored is None:
              return {'updated': 0}
          item = MyItem(_id=message.id, name=message.name, _version=0)
          # db.update checks _version by default: pass the stored one
          return db.update({'_id': stored['_id'], '_version': stored['_version']},
                           item.model_dump(by_alias=True, exclude={'id', 'version'}),
                           'Items')

  # keep PingActions: /ping is the liveness probe and UNTESTED_PUBSUB_FUNCTIONS
  # in test_pubSubContracts.py expects it to exist
  class PubSubRequests(PingActions, AccountingToMyAppActions):
      def __init__(self):
          PingActions.__init__(self)
          AccountingToMyAppActions.__init__(self)
"""

from route_config import route_config

# The Pub/Sub map (pubSubMap.py writes server/pubsub_map.json and
# PUBSUB_MAP.md; test_pubSubMap.py checks them) reads every handler here by
# AST. Each handler parses its message with its generated <Fn>Message (see
# the docstring above); there is no escape hatch.
#
# Compat: test_payloadsSatisfyConsumerContracts fails, in this repo and in the
# publisher's, on every mismatch between a publisher's payload and what a
# handler here reads (e.g. the publisher may send null where the handler
# needs a value). Nothing suppresses one: fix it at its source
# (`python pubSubMap.py --explain <handler>` shows both sides). Static,
# type-level only: value-dependent logic (e.g. mapping type=='services' to
# None, then dereferencing it) is not modelled; ok means the declared types
# line up at every path read. test_pubSubContracts replay is the ground truth.


class PingActions():

    @route_config(httpMethod='POST',
                  jwtRequired=False,
                  successMessage='Pong')
    def ping(self, data):
        return {'ping': 'pong'}


class PubSubRequests(PingActions):

    def __init__(self):
        PingActions.__init__(self)
