"""Policies (one per store).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime
import typing

from pydantic import Field, field_validator
from typing import Annotated, Literal

from objects.base import MongoModel, StoredDocumentKeys, ResponseModel
from objects.order import Fulfillment, PaymentMethod
from objects.product import PRODUCT_PRICE_MAX


# ──────────────────────────────────────────────
# Policies (one per store)
# ──────────────────────────────────────────────

# How a customer can get an order: an order's Fulfillment.
DeliveryOption = Fulfillment
# How a returned item is refunded.
RefundMethod = Literal['originalPayment', 'storeCredit', 'exchange']
# Who pays the shipping of a returned item.
ReturnShippingPayer = Literal['customer', 'store']
# Whether a customer can cancel a paid order: never, or until it is handed
# over. (A customer cancelling an unpaid order is a rule the app enforces,
# so it is no policy field.)
PaidOrderCancellation = Literal['notAllowed', 'beforeHandover']
# The answers of a Policy, by name (updateMyStorePolicy's clear). Pinned
# to Policy's fields below (POLICY_ANSWER_FIELDS).
PolicyField = Literal['acceptsReturns', 'returnWindowDays', 'returnConditions',
                      'nonReturnableItems', 'refundMethod',
                      'returnShippingPaidBy', 'defectReportDays',
                      'deliveryOptions', 'deliveryAreas', 'deliveryFee',
                      'freeDeliveryMinimum', 'handoverDays',
                      'acceptedPaymentMethods', 'paidOrderCancellation',
                      'sendsMarketingMessages', 'additionalTerms']
POLICY_TEXT_MAX_LENGTH = 300
POLICY_TERMS_MAX_LENGTH = 1000
POLICY_DAYS_MAX = 365

# Each answer's type, limits and description, declared once: Policy stores
# it (default None, "not said yet") and StorePolicyResponse sends it
# (required, null while not said).
# Returns
AcceptsReturns = Annotated[bool | None, Field(
    description='Whether a customer can return an item that is not '
    'defective')]
ReturnWindowDays = Annotated[int | None, Field(
    ge=1, le=POLICY_DAYS_MAX,
    description='Days after handover a customer can return an item')]
ReturnConditions = Annotated[str | None, Field(
    min_length=1, max_length=POLICY_TEXT_MAX_LENGTH,
    description='What a returned item must be like, e.g. unused with tags')]
NonReturnableItems = Annotated[str | None, Field(
    min_length=1, max_length=POLICY_TEXT_MAX_LENGTH,
    description='The items that can never be returned')]
RefundMethodAnswer = Annotated[RefundMethod | None, Field(
    description='How a returned item is refunded')]
ReturnShippingPaidBy = Annotated[ReturnShippingPayer | None, Field(
    description='Who pays the shipping of a returned item')]
DefectReportDays = Annotated[int | None, Field(
    ge=1, le=POLICY_DAYS_MAX,
    description='Days after handover a customer has to report a defective '
    'or wrong item')]
# Delivery
DeliveryOptions = Annotated[list[DeliveryOption] | None, Field(
    min_length=1, description='How a customer can get an order')]
DeliveryAreas = Annotated[str | None, Field(
    min_length=1, max_length=POLICY_TEXT_MAX_LENGTH,
    description='Where the store delivers')]
DeliveryFee = Annotated[float | None, Field(
    ge=0, le=PRODUCT_PRICE_MAX,
    description="What delivery costs, in the store's currency")]
FreeDeliveryMinimum = Annotated[float | None, Field(
    ge=0, le=PRODUCT_PRICE_MAX,
    description="The order total from which delivery is free, in the "
    "store's currency")]
HandoverDays = Annotated[int | None, Field(
    ge=0, le=POLICY_DAYS_MAX,
    description='Days from payment until the order is handed over or '
    'delivered')]
# Payment
AcceptedPaymentMethods = Annotated[list[PaymentMethod] | None, Field(
    min_length=1, description='The ways a customer can pay')]
PaidOrderCancellationAnswer = Annotated[PaidOrderCancellation | None, Field(
    description='Whether a customer can cancel a paid order')]
# The rest
SendsMarketingMessages = Annotated[bool | None, Field(
    description='Whether the store messages customers about offers')]
AdditionalTerms = Annotated[str | None, Field(
    min_length=1, max_length=POLICY_TERMS_MAX_LENGTH,
    description='Any other terms, as the admin gave them')]

# The answers that only apply with returns accepted, and with delivery
# among the deliveryOptions (Policy.normalized clears them otherwise).
POLICY_RETURN_ANSWERS = ('returnWindowDays', 'returnConditions',
                         'refundMethod', 'returnShippingPaidBy')
POLICY_DELIVERY_ANSWERS = ('deliveryAreas', 'deliveryFee',
                           'freeDeliveryMinimum')


class Policy(MongoModel):
    """A Policies document: the store's answers to what customers ask
    (returns, delivery, payment), one per store (the unique storeId index,
    mongoDb.ensureIndexes). Every answer is None until the admin gives it:
    None is "the store has not said".

    Rules the app enforces itself (the payment window, a customer
    cancelling an unpaid order) are no fields here, so a policy can never
    contradict the code. Every write must $inc _version
    (updateMyStorePolicy's lock)."""
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    acceptsReturns: AcceptsReturns = None
    returnWindowDays: ReturnWindowDays = None
    returnConditions: ReturnConditions = None
    nonReturnableItems: NonReturnableItems = None
    refundMethod: RefundMethodAnswer = None
    returnShippingPaidBy: ReturnShippingPaidBy = None
    defectReportDays: DefectReportDays = None
    deliveryOptions: DeliveryOptions = None
    deliveryAreas: DeliveryAreas = None
    deliveryFee: DeliveryFee = None
    freeDeliveryMinimum: FreeDeliveryMinimum = None
    handoverDays: HandoverDays = None
    acceptedPaymentMethods: AcceptedPaymentMethods = None
    paidOrderCancellation: PaidOrderCancellationAnswer = None
    sendsMarketingMessages: SendsMarketingMessages = None
    additionalTerms: AdditionalTerms = None
    createdAt: datetime.datetime = Field(..., description='When it was created')
    updatedAt: datetime.datetime = Field(...,
                                         description='When it last changed')

    @field_validator('returnConditions', 'nonReturnableItems',
                     'deliveryAreas', 'additionalTerms', mode='before')
    @classmethod
    def _strip_text(cls, v):
        return v.strip() if isinstance(v, str) else v

    def normalized(self) -> 'Policy':
        """A copy with the answers that no longer apply cleared: no returns
        clears POLICY_RETURN_ANSWERS, no delivery POLICY_DELIVERY_ANSWERS."""
        cleared = {}
        if self.acceptsReturns is False:
            cleared.update(dict.fromkeys(POLICY_RETURN_ANSWERS))
        if (self.deliveryOptions is not None
                and 'delivery' not in self.deliveryOptions):
            cleared.update(dict.fromkeys(POLICY_DELIVERY_ANSWERS))
        return self.model_copy(update=cleared)

    def problems(self) -> str | None:
        """What the answers miss or repeat, as the question to ask the
        admin again; None when they hold together."""
        if self.acceptsReturns and self.returnWindowDays is None:
            return ('Returns are accepted: ask how many days after handover '
                    'a customer can return an item (returnWindowDays)')
        for field in ('deliveryOptions', 'acceptedPaymentMethods'):
            values = getattr(self, field)
            if values is not None and len(set(values)) != len(values):
                return (f'{field} lists an option twice: ask again which '
                        'options apply, and send each once')
        return None


# Policy's answer fields, in declaration order: every field but the keys
# and the timestamps. PolicyField must name exactly these.
POLICY_ANSWER_FIELDS = tuple(
    name for name in Policy.model_fields
    if name not in ('id', 'version', 'storeId', 'createdAt', 'updatedAt'))
if set(POLICY_ANSWER_FIELDS) != set(typing.get_args(PolicyField)):
    raise TypeError('PolicyField must name exactly the answer fields of '
                    'Policy')


class PolicyView(StoredDocumentKeys, Policy):
    """A Policies document as stored."""


class StorePolicyResponse(ResponseModel):
    """A store's policy as a route sends it: every answer, null while the
    store has not said. Each answer has Policy's own type alias, which
    test_route_response_models.py's MIRRORS keeps in step."""
    acceptsReturns: AcceptsReturns = Field(...)
    returnWindowDays: ReturnWindowDays = Field(...)
    returnConditions: ReturnConditions = Field(...)
    nonReturnableItems: NonReturnableItems = Field(...)
    refundMethod: RefundMethodAnswer = Field(...)
    returnShippingPaidBy: ReturnShippingPaidBy = Field(...)
    defectReportDays: DefectReportDays = Field(...)
    deliveryOptions: DeliveryOptions = Field(...)
    deliveryAreas: DeliveryAreas = Field(...)
    deliveryFee: DeliveryFee = Field(...)
    freeDeliveryMinimum: FreeDeliveryMinimum = Field(...)
    handoverDays: HandoverDays = Field(...)
    acceptedPaymentMethods: AcceptedPaymentMethods = Field(...)
    paidOrderCancellation: PaidOrderCancellationAnswer = Field(...)
    sendsMarketingMessages: SendsMarketingMessages = Field(...)
    additionalTerms: AdditionalTerms = Field(...)
    updatedAt: datetime.datetime = Field(...,
                                         description='When it last changed')
