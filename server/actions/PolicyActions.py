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
from actions.ProductActions import validBatch
from actions.StoreActions import (POLICIES, _validText, emptyPolicy,
                                  ensurePolicy, requireOwnerStore, setUpStore)
from mcpTools import mcp_tool
from mongoDb import db
from objects import (POLICY_ANSWER_FIELDS, POLICY_CANCELLATION_HOURS_ANSWERS,
                     DELIVERY_ZONE_RULES, POLICY_DELIVERY_ANSWERS,
                     POLICY_RETURN_ANSWERS, RETIRED_POLICY_FIELDS,
                     CHECK_PAYEE_MAX_LENGTH, PAYMENT_ACCOUNT_NAME_MAX_LENGTH,
                     PAYMENT_ACCOUNT_NUMBER_MAX_LENGTH, PAYMENT_ACCOUNTS_MAX,
                     DeliveryOption, NewBankAccount, NewEwalletAccount,
                     NewPaymentMethod, PaidOrderCancellation, PaymentMethod,
                     PaymentProvider, Policy, methodProblem,
                     RefundMethod, ReturnShippingPayer, StorePolicyResponse,
                     projectedCopy, ruledZones)
from route_config import route_config

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
}
# The same for the hours of a paid order cancellation (withinHours).
HOURS_NOT_APPLICABLE = dict.fromkeys(
    POLICY_CANCELLATION_HOURS_ANSWERS,
    'applies only with paidOrderCancellation withinHours')
_DROPPED = {**NOT_APPLICABLE, **HOURS_NOT_APPLICABLE}


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
    if field == PAYMENT_METHODS_FIELD:
        return (f'a list of 1 to {len(typing.get_args(PaymentMethod))} '
                'payment methods, each with its method and what paying that '
                'way needs')
    if typing.get_origin(answerType) is list:
        return f'a list of {_oneOf(typing.get_args(answerType)[0])}'
    raise TypeError(f'No rule for the policy answer {field}')


# The one answer that is a list of objects, checked by _validPaymentMethods.
PAYMENT_METHODS_FIELD = 'authorizedPaymentMethods'
INVALID_METHOD = f'method is {_oneOf(PaymentMethod)}'
INVALID_PROVIDER = (f'provider is {_oneOf(PaymentProvider)}, or null for a '
                    'method paid by hand to the store')


def _accounts(value, label: str) -> list | None:
    """An account list of a payment method: None, or a list of 1 to
    PAYMENT_ACCOUNTS_MAX."""
    if value is None:
        return None
    if not isinstance(value, list) or not (
            1 <= len(value) <= PAYMENT_ACCOUNTS_MAX):
        raise ValueError(f'{label} is a list of 1 to {PAYMENT_ACCOUNTS_MAX}')
    return value


def _validAccounts(value, model, nameFields: tuple[str, ...],
                   label: str) -> list | None:
    """The e-wallet or bank accounts of a payment method (`nameFields` and
    accountNumber each), stripped; a refusal names the account's position,
    never its value."""
    if _accounts(value, label) is None:
        return None

    def account(item, earlier):
        return {
            **{field: _validText(item.get(field), field,
                                 PAYMENT_ACCOUNT_NAME_MAX_LENGTH)
               for field in nameFields},
            'accountNumber': _validText(item.get('accountNumber'),
                                        'accountNumber',
                                        PAYMENT_ACCOUNT_NUMBER_MAX_LENGTH),
        }
    return validBatch(value, model, label, account)


def _validPaymentMethod(item: dict, earlier: list[dict]) -> dict:
    """One payment method of authorizedPaymentMethods (a dict), as stored:
    its method, provider and the details it needs (methodProblem). A method
    listed twice is Policy.problems()' to refuse."""
    method = item.get('method')
    if not isinstance(method, str) or method not in typing.get_args(
            PaymentMethod):
        raise ValueError(INVALID_METHOD)
    provider = item.get('provider')
    if isinstance(provider, str):
        provider = provider.strip() or None
    if provider is not None and (not isinstance(provider, str)
                                 or provider not in typing.get_args(
                                     PaymentProvider)):
        raise ValueError(INVALID_PROVIDER)
    eWallets = _validAccounts(item.get('eWallets'), NewEwalletAccount,
                              ('ewalletName',), 'E-wallet')
    banks = _validAccounts(item.get('banks'), NewBankAccount,
                           ('bankName', 'accountName'), 'Bank')
    checkPayees = _accounts(item.get('checkPayees'), 'checkPayees')
    if checkPayees is not None:
        checkPayees = [
            _validText(payee, f'Check payee {position}',
                       CHECK_PAYEE_MAX_LENGTH)
            for position, payee in enumerate(checkPayees, start=1)]
    problem = methodProblem(method, provider, eWallets, banks, checkPayees)
    if problem is not None:
        raise ValueError(problem)
    return {'method': method, 'provider': provider, 'eWallets': eWallets,
            'banks': banks, 'checkPayees': checkPayees}


def _validPaymentMethods(value) -> list[dict]:
    """authorizedPaymentMethods as stored, each refusal naming the method's
    position and never quoting a value."""
    if not isinstance(value, list) or not (
            1 <= len(value) <= len(typing.get_args(PaymentMethod))):
        raise ValueError(
            f'{PAYMENT_METHODS_FIELD} is {_rule(PAYMENT_METHODS_FIELD)}')
    return validBatch(value, NewPaymentMethod, 'Payment method',
                      _validPaymentMethod)


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
- payment: authorizedPaymentMethods, the ways a customer can pay, each an
  object with its `method` ({_oneOf(PaymentMethod)}) and what paying that
  way needs, asked as follow-up questions for each method the admin picks:
  cash nothing (paid by hand at the handover); card and crypto their
  `provider` ({_oneOf(PaymentProvider)}: they are paid online); eWallet
  either the store's own accounts customers pay to by hand, `eWallets` (a
  list of ewalletName, e.g. GCash, and accountNumber), or a `provider` to
  pay online, or both; bankTransfer the store's `banks` (a list of
  bankName, accountName, the name the account is under, and
  accountNumber); check `checkPayees`, the account names a check is
  made out to. A payment made by hand waits for the admin to confirm it
  (confirmOrderPayment). The list replaces the stored one: send every
  method the store takes, with its details. Then
  paidOrderCancellation, whether a customer can cancel a paid order
  ({_oneOf(PaidOrderCancellation)}); with withinHours,
  paidOrderCancellationHours, how many hours after payment (and before
  handover) a customer can cancel;
- delivery: deliveryOptions (a list, {_oneOf(DeliveryOption)}); with
  delivery, the zones it delivers to, each with its own delivery policy (a
  name, a flat fee, the days a delivery takes, and optionally a minimum
  order and an order total from which delivery is free), drawn and set on
  a map (in the chat, showDeliveryZonesForm; never asked as text or saved
  with this tool): a store that delivers takes no delivery order until it
  has a zone; then deliveryAreas, an optional name for them all (e.g. Metro
  Manila); handoverDays, the days from payment until a pickup order is
  ready (a delivery takes its zone's days);
- returns: acceptsReturns; if so, returnWindowDays, returnConditions,
  nonReturnableItems, refundMethod ({_oneOf(RefundMethod)}) and
  returnShippingPaidBy ({_oneOf(ReturnShippingPayer)}); defectReportDays, how
  long a customer has to report a defective or wrong item;
- then the rest: additionalTerms.
Use only what the admin said, never guess, and confirm the answers with the
admin before calling. Send only the fields the admin answered: a field left
out keeps its answer. To take an answer back, list the field in `clear`
(never with a value too). returnWindowDays and defectReportDays count from
the handover (pickup or delivery); handoverDays counts from payment. Amounts
are in the store's currency. Setting acceptsReturns to false drops the
return window, conditions, refund method and who pays return shipping;
leaving delivery out of deliveryOptions drops deliveryAreas; a
paidOrderCancellation other than withinHours drops its hours. A customer
cancels a paid order themselves (refunded) when paidOrderCancellation allows
it. The time a customer has to pay an order, and a customer cancelling an
unpaid order, are set by the app, not by a policy: never ask about them.
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
    if field == PAYMENT_METHODS_FIELD:
        value = _validPaymentMethods(value)
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


# What getMyStorePolicy, getStorePolicy and updateMyStorePolicy read of
# the store: its policy's key and its delivery zones.
POLICY_STORE_FIELDS = {'_id': 1, 'createdAt': 1, 'deliveryZones': 1}


def _withZones(policy: dict, zones: list[dict] | None) -> dict:
    """`policy` (answers, as stored) with deliveryZones: the store's zones'
    policies, without their shapes; none while it offers no delivery."""
    delivers = 'delivery' in (policy.get('deliveryOptions') or [])
    return {**policy, 'deliveryZones': [
        {rule: zone.get(rule) for rule in DELIVERY_ZONE_RULES}
        for zone in (ruledZones(zones) if delivers else [])]}


def _storePolicy(store: dict) -> dict:
    """The policy of `store` (POLICY_STORE_FIELDS) as StorePolicyResponse
    sends it. A store with no Policies document (see
    StoreActions.ensurePolicy) has said nothing yet: emptyPolicy."""
    zones = store.get('deliveryZones')
    policy = db.readResponse(
        StorePolicyResponse, {'storeId': store['_id']}, POLICIES,
        prepare=lambda stored: _withZones(stored, zones))
    if policy is None:
        return projectedCopy(StorePolicyResponse, _withZones(
            emptyPolicy(store['_id'], store['createdAt']), zones))
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
        store = requireOwnerStore(userId, POLICY_STORE_FIELDS)
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
        A delivery goes only into one of deliveryZones, each with its own
        flat deliveryFee, deliveryDays, minimumOrder (null: none) and
        freeDeliveryFrom (null: never free); no zone: no delivery. Amounts
        are in the store's currency."""
        customerAtStore(userId, storeId)
        return _storePolicy(setUpStore(storeId, POLICY_STORE_FIELDS))

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
                            handoverDays: int | None = None,
                            authorizedPaymentMethods:
                            list[NewPaymentMethod] | None = None,
                            paidOrderCancellation: str | None = None,
                            paidOrderCancellationHours: int | None = None,
                            additionalTerms: str | None = None,
                            clear: list[str] | None = None) -> dict:
        params = dict(locals())
        store = requireOwnerStore(userId, POLICY_STORE_FIELDS)
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
                    f'{field} {_DROPPED[field]}: ask again')
        problem = policy.problems()
        if problem is not None:
            raise ValueError(problem)
        # Read whole, then cut by projectedCopy below: projection_of cannot
        # read authorizedPaymentMethods, a list of models that may be null.
        updated = db.findOneAndUpdate(
            {'_id': stored['_id'], '_version': stored['_version']},
            {
                '$set': {
                    **policy.model_dump(include=set(POLICY_ANSWER_FIELDS)),
                    'updatedAt': now,
                },
                '$inc': {'_version': 1},
                '$unset': dict.fromkeys(RETIRED_POLICY_FIELDS, ''),
            },
            POLICIES,
            upsert=False)
        if updated is None:
            raise ValueError(POLICY_CHANGED)
        return projectedCopy(StorePolicyResponse,
                             _withZones(updated, store.get('deliveryZones')))


# updateMyStorePolicy takes exactly the answer fields, besides userId and
# clear: an answer added to Policy needs its param too.
if tuple(inspect.signature(PolicyActions.updateMyStorePolicy).parameters
         )[2:-1] != POLICY_ANSWER_FIELDS:
    raise TypeError('updateMyStorePolicy must take the answers of '
                    'POLICY_ANSWER_FIELDS, in order')
