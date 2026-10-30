"""PolicyActions: a store's policies.

The store's admin sets them through the admin chat, one answer at a time;
the customer AI answers policy questions ("can I return this?") from the
stored answers, never from prose it makes up. One Policies document per
store, created with the store (empty: every answer None, "not said yet").

The admin routes find the policy through the admin's own store
(StoreActions.requireOwnerStore), never through a store id the client or
the AI sends.
"""

import datetime
import inspect
import typing

import annotated_types
from pydantic import TypeAdapter, ValidationError

from actions.OrderActions import customerAtStore
from actions.StoreActions import (POLICIES, _validText, emptyPolicy,
                                  ensurePolicy, requireOwnerStore, setUpStore)
from actions.deliveryPricing import shownFeePerKm
from mcpTools import mcp_tool
from mongoDb import db
from objects import (POLICY_ANSWER_FIELDS, POLICY_DELIVERY_ANSWERS,
                     POLICY_FREE_DELIVERY_ANSWERS, POLICY_RETURN_ANSWERS,
                     DeliveryOption,
                     PaidOrderCancellation, PaymentMethod, Policy,
                     RefundMethod, ReturnShippingPayer, StorePolicyResponse,
                     projectedCopy, projection_of)
from route_config import route_config
from sharedConstants import DELIVERY_USD_PER_KM

POLICY_CHANGED = 'The policy just changed, try again'
CLEAR_FIELDS = 'clear is a list of policy field names'
NOTHING_SENT = 'Send at least one answer or a field to clear'
# Why a sent answer that normalized() would drop is refused instead.
NOT_APPLICABLE = {
    **dict.fromkeys(POLICY_RETURN_ANSWERS,
                    'applies only when returns are accepted '
                    '(acceptsReturns)'),
    **dict.fromkeys(POLICY_DELIVERY_ANSWERS,
                    'applies only with delivery among the deliveryOptions'),
    **dict.fromkeys(POLICY_FREE_DELIVERY_ANSWERS,
                    'applies only with delivery among the deliveryOptions '
                    'and offersFreeDelivery true'),
}


def _oneOf(literal) -> str:
    return 'one of ' + ', '.join(typing.get_args(literal))


def _answerType(field: str):
    """The type of Policy's answer `field`, without its None."""
    [answerType] = [member for member in typing.get_args(
        Policy.model_fields[field].annotation) if member is not type(None)]
    return answerType


def _limit(field: str, kind: type, name: str):
    """The limit `name` (ge, le, max_length) of Policy's answer `field`."""
    [limit] = [getattr(m, name) for m in Policy.model_fields[field].metadata
               if isinstance(m, kind)]
    return limit


def _rule(field: str) -> str:
    """What Policy's answer `field` must be, from its type and limits: the
    refusal of a value that is not, never quoting the value."""
    answerType = _answerType(field)
    if answerType is bool:
        return 'true or false'
    if answerType in (int, float):
        span = (f'from {_limit(field, annotated_types.Ge, "ge")} to '
                f'{_limit(field, annotated_types.Le, "le")}')
        if answerType is float:
            return f'an amount {span}'
        return f'a whole number of days {span}'
    if answerType is str:
        maxLength = _limit(field, annotated_types.MaxLen, 'max_length')
        return f'text of at most {maxLength} characters'
    if typing.get_origin(answerType) is typing.Literal:
        return _oneOf(answerType)
    if typing.get_origin(answerType) is list:
        return f'a list of {_oneOf(typing.get_args(answerType)[0])}'
    raise TypeError(f'No rule for the policy answer {field}')


def _answerAdapter(field: str) -> TypeAdapter:
    """Policy's answer `field`: its type with its limits (ge, max_length,
    ...)."""
    info = Policy.model_fields[field]
    if not info.metadata:
        return TypeAdapter(info.annotation)
    return TypeAdapter(typing.Annotated[(info.annotation, *info.metadata)])


# What each answer must be, and its type with Policy's limits, checked
# strictly: no "5" for 5, no true for 1.
ANSWER_RULES = {field: _rule(field) for field in POLICY_ANSWER_FIELDS}
_ANSWER_ADAPTERS = {
    field: _answerAdapter(field) for field in POLICY_ANSWER_FIELDS
}
# The text answers, with their length limits: _validText checks them first,
# refusing a control character.
TEXT_LIMITS = {
    field: _limit(field, annotated_types.MaxLen, 'max_length')
    for field in POLICY_ANSWER_FIELDS if _answerType(field) is str
}


UPDATE_POLICY_DESCRIPTION = f"""Save answers of the admin's store policies.
Ask the admin one section at a time, one question per message (the admin
may skip any):
- payment: acceptedPaymentMethods (a list, {_oneOf(PaymentMethod)}) and
  paidOrderCancellation, whether a customer can cancel a paid order
  ({_oneOf(PaidOrderCancellation)});
- delivery: deliveryOptions (a list, {_oneOf(DeliveryOption)}); with
  delivery, deliveryAreas and offersFreeDelivery, and if so
  freeDeliveryMinimum (the order total from which delivery is free);
  handoverDays, the days from payment until the order is ready for pickup
  or delivered. What a delivery costs is set by the app, never asked:
  {DELIVERY_USD_PER_KM} US dollars per km in a straight line from the
  store's pin, in the store's currency at today's rate (deliveryFeePerKm),
  so a store that delivers needs its pin (setUserStoreLocation);
- returns: acceptsReturns; if so, returnWindowDays, returnConditions,
  nonReturnableItems, refundMethod ({_oneOf(RefundMethod)}) and
  returnShippingPaidBy ({_oneOf(ReturnShippingPayer)}); defectReportDays, how
  long a customer has to report a defective or wrong item;
- then the rest: sendsMarketingMessages and additionalTerms.
Use only what the admin said, never guess, and confirm the answers with the
admin before calling. Send only the fields the admin answered: a field left
out keeps its answer. To take an answer back, list the field in `clear`
(never with a value too). returnWindowDays and defectReportDays count from
the handover (pickup or delivery); handoverDays counts from payment. Amounts
are in the store's currency. Setting acceptsReturns to false drops the
return window, conditions, refund method and who pays return shipping;
leaving delivery out of deliveryOptions drops the delivery areas and free
delivery; setting offersFreeDelivery to false drops the minimum. The time a
customer has to pay an order, and a customer cancelling an unpaid order, are
set by the app, not by a policy: never ask about them.
If this tool refuses, relay its message to the admin and ask that question
again."""


def _validAnswer(field: str, value):
    """`value` as the answer `field` stores, refused unless it holds to
    Policy's field. A text is stripped, and an empty one is None
    (unchanged), as None itself is."""
    if isinstance(value, str):
        value = value.strip() or None
    if value is None:
        return None
    if field in TEXT_LIMITS:
        value = _validText(value, field, TEXT_LIMITS[field])
    try:
        _ANSWER_ADAPTERS[field].validate_python(value, strict=True)
    except ValidationError:
        raise ValueError(f'{field} is {ANSWER_RULES[field]}') from None
    return value


def _validClear(clear: list[str] | None) -> list[str]:
    """The fields `clear` sets back to not said; refused unless each is a
    policy field."""
    if clear is None:
        return []
    if not isinstance(clear, list) or any(
            not isinstance(field, str) or field not in POLICY_ANSWER_FIELDS
            for field in clear):
        raise ValueError(CLEAR_FIELDS)
    return clear


def _withFeePerKm(policy: dict, currency: str) -> dict:
    """`policy` (answers, as stored) with deliveryFeePerKm: what a km of
    delivery costs in `currency`, None without delivery offered."""
    delivers = 'delivery' in (policy.get('deliveryOptions') or [])
    return {**policy,
            'deliveryFeePerKm': shownFeePerKm(currency) if delivers else None}


def _storePolicy(store: dict) -> dict:
    """The policy of `store` (_id, createdAt, currency) as
    StorePolicyResponse sends it. A store with no Policies document (see
    StoreActions.ensurePolicy) has said nothing yet: emptyPolicy."""
    currency = store['currency']
    policy = db.readResponse(
        StorePolicyResponse, {'storeId': store['_id']}, POLICIES,
        prepare=lambda stored: _withFeePerKm(stored, currency))
    if policy is None:
        return projectedCopy(StorePolicyResponse, _withFeePerKm(
            emptyPolicy(store['_id'], store['createdAt']), currency))
    return policy


class PolicyActions():

    @mcp_tool('admin',
              thinkingText=['Reading your store policy',
                            'Checking your policy',
                            'Looking up your policy'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Policy fetched',
                  responseModel=StorePolicyResponse)
    def getMyStorePolicy(self, userId: str) -> dict:
        """Get the policies of the admin's store: its answers on returns,
        delivery, payment and the rest. A null answer is one the admin has
        not given yet: use updateMyStorePolicy to set it."""
        store = requireOwnerStore(userId, {'_id': 1, 'createdAt': 1,
                                           'currency': 1})
        return _storePolicy(store)

    @mcp_tool('customer',
              thinkingText=['Reading the store policy',
                            "Checking the store's rules",
                            'Looking up the policy'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Policy fetched',
                  responseModel=StorePolicyResponse)
    def getStorePolicy(self, userId: str, storeId: str) -> dict:
        """Get this store's policies: use it when the customer asks about
        returns, refunds, delivery, payment or cancelling. Answer only from
        these fields and never invent a policy. A null field means the store
        has not said: tell the customer so and that they can ask the store.
        A defective or wrong item is always the store's to fix, whatever the
        return policy: defectReportDays (when set) is how long the customer
        has to report it. returnWindowDays and defectReportDays count from
        the handover (pickup or delivery); handoverDays counts from payment.
        A delivery costs deliveryFeePerKm per km in a straight line from the
        store to the customer's pin (null: the price can't be read right
        now), nothing from freeDeliveryMinimum on. Amounts are in the
        store's currency."""
        customerAtStore(userId, storeId)
        return _storePolicy(setUpStore(storeId, {'_id': 1, 'createdAt': 1,
                                                 'currency': 1}))

    # The tool description is UPDATE_POLICY_DESCRIPTION. The choice
    # params are plain str (lists of str), not Literals: the MCP SDK's
    # pydantic check would refuse a bad value before this runs, quoting it
    # (OrderActions._statusMatch's reason). _validAnswer and _validClear
    # check them on both paths. Its answer params are POLICY_ANSWER_FIELDS
    # (pinned below the class).
    @mcp_tool('admin', description=UPDATE_POLICY_DESCRIPTION,
              thinkingText=['Saving your policy',
                            'Updating your store policy',
                            'Writing down your policy'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Policy saved',
                  responseModel=StorePolicyResponse)
    def updateMyStorePolicy(self,
                            userId: str,
                            acceptsReturns: bool | None = None,
                            returnWindowDays: int | None = None,
                            returnConditions: str | None = None,
                            nonReturnableItems: str | None = None,
                            refundMethod: str | None = None,
                            returnShippingPaidBy: str | None = None,
                            defectReportDays: int | None = None,
                            deliveryOptions: list[str] | None = None,
                            deliveryAreas: str | None = None,
                            offersFreeDelivery: bool | None = None,
                            freeDeliveryMinimum: float | None = None,
                            handoverDays: int | None = None,
                            acceptedPaymentMethods: list[str] | None = None,
                            paidOrderCancellation: str | None = None,
                            sendsMarketingMessages: bool | None = None,
                            additionalTerms: str | None = None,
                            clear: list[str] | None = None) -> dict:
        params = dict(locals())
        store = requireOwnerStore(userId, {'_id': 1, 'createdAt': 1,
                                           'currency': 1})
        cleared = _validClear(clear)
        # The generated HTTP handler passes a missing body key as None, so
        # None is "unchanged" and `clear` is how an answer is taken back.
        sent = {}
        for field in POLICY_ANSWER_FIELDS:
            value = _validAnswer(field, params[field])
            if value is not None:
                sent[field] = value
        if not sent and not cleared:
            raise ValueError(NOTHING_SENT)
        both = [field for field in POLICY_ANSWER_FIELDS
                if field in sent and field in cleared]
        if both:
            raise ValueError(
                f'{both[0]} is both cleared and given a value: send one')
        ensurePolicy(store['_id'], store['createdAt'])
        stored = db.read({'storeId': store['_id']}, POLICIES, findOne=True)
        if stored is None:
            raise ValueError(POLICY_CHANGED)  # deleted since ensurePolicy
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        policy = Policy.model_validate({
            **stored,
            **sent,
            **dict.fromkeys(cleared),
            'updatedAt': now,
        }).normalized()
        # An answer that no longer applies is cleared when it was stored,
        # but one sent now is refused: never dropped without a word.
        for field in sent:
            if getattr(policy, field) is None:
                raise ValueError(
                    f'{field} {NOT_APPLICABLE[field]}: ask again')
        problem = policy.problems()
        if problem is not None:
            raise ValueError(problem)
        updated = db.findOneAndUpdate(
            {'_id': stored['_id'], '_version': stored['_version']},
            {
                '$set': {
                    **policy.model_dump(include=set(POLICY_ANSWER_FIELDS)),
                    'updatedAt': now,
                },
                '$inc': {'_version': 1},
            },
            POLICIES,
            upsert=False,
            projection=projection_of(StorePolicyResponse))
        if updated is None:
            raise ValueError(POLICY_CHANGED)
        return projectedCopy(StorePolicyResponse,
                             _withFeePerKm(updated, store['currency']))


# updateMyStorePolicy takes exactly the answer fields, besides userId and
# clear: an answer added to Policy needs its param too.
if tuple(inspect.signature(PolicyActions.updateMyStorePolicy).parameters
         )[2:-1] != POLICY_ANSWER_FIELDS:
    raise TypeError('updateMyStorePolicy must take the answers of '
                    'POLICY_ANSWER_FIELDS, in order')
