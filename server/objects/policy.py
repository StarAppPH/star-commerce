"""Policies (one per store).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime
import typing

from typing import ClassVar
from pydantic import Field, field_validator
from typing import Annotated, Literal

from objects.base import MongoModel, StoredDocumentKeys, ResponseModel
from objects.order import Fulfillment
from objects.payment import (AuthorizedPaymentMethod,
                             AuthorizedPaymentMethodRow, PaymentMethod)
from objects.store import DELIVERY_ZONES_MAX, DeliveryZoneRule, ruledZones
from sharedConstants import (POLICY_OPTIONAL_QUESTIONS,
                             POLICY_RETURN_QUESTIONS, POLICY_ZONES_QUESTION)


# ──────────────────────────────────────────────
# Policies (one per store)
# ──────────────────────────────────────────────

# How a customer can get an order: an order's Fulfillment.
DeliveryOption = Fulfillment
# How a returned item is refunded.
RefundMethod = Literal['originalPayment', 'storeCredit', 'exchange']
# Who pays the shipping of a returned item.
ReturnShippingPayer = Literal['customer', 'store']
# Whether a customer can cancel a paid order: never, until it is handed
# over, or until it is handed over and within paidOrderCancellationHours of
# its payment. (A customer cancelling an unpaid order is a rule the app
# enforces, so it is no policy field.)
PaidOrderCancellation = Literal['notAllowed', 'beforeHandover', 'withinHours']
# The answers of a Policy, by name (updateMyStorePolicy's clear). Pinned
# to Policy's fields below (POLICY_ANSWER_FIELDS).
# Answers a policy no longer has: each delivery zone holds its own fee and
# free delivery total (objects/store.py), and authorizedPaymentMethods, with
# each method's details, replaced the bare list acceptedPaymentMethods. A
# stored policy may still carry them: they are read past (extra='ignore') and
# dropped on its next save.
RETIRED_POLICY_FIELDS = ('offersFreeDelivery', 'freeDeliveryMinimum',
                         'acceptedPaymentMethods')
PolicyField = Literal['acceptsReturns', 'returnWindowDays', 'returnConditions',
                      'nonReturnableItems', 'refundMethod',
                      'returnShippingPaidBy', 'defectReportDays',
                      'deliveryOptions', 'deliveryMethod', 'deliveryAreas',
                      'handoverDays', 'authorizedPaymentMethods', 'paidOrderCancellation',
                      'paidOrderCancellationHours', 'additionalTerms']
POLICY_TEXT_MAX_LENGTH = 300
POLICY_TERMS_MAX_LENGTH = 1000
POLICY_DAYS_MAX = 365
# A week: paidOrderCancellationHours' longest window.
POLICY_CANCELLATION_HOURS_MAX = 168

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
DeliveryMethod = Annotated[str | None, Field(
    min_length=1, max_length=POLICY_TEXT_MAX_LENGTH,
    description='How the store delivers an order, as the admin said it, '
    'e.g. its own rider, a courier')]
DeliveryAreas = Annotated[str | None, Field(
    min_length=1, max_length=POLICY_TEXT_MAX_LENGTH,
    description='Where the store delivers')]
HandoverDays = Annotated[int | None, Field(
    ge=0, le=POLICY_DAYS_MAX,
    description='Days from payment until a pickup order is ready (a '
    "delivery takes its zone's deliveryDays)")]
# Payment
PAYMENT_METHODS_COUNT = len(typing.get_args(PaymentMethod))
AuthorizedPaymentMethods = Annotated[
    list[AuthorizedPaymentMethod] | None, Field(
        min_length=1, max_length=PAYMENT_METHODS_COUNT,
        description='The ways a customer can pay, each with what paying '
        'that way needs')]
AuthorizedPaymentMethodRows = Annotated[
    list[AuthorizedPaymentMethodRow] | None, Field(
        min_length=1, max_length=PAYMENT_METHODS_COUNT,
        description='The ways a customer can pay, each with what paying '
        'that way needs')]
PaidOrderCancellationAnswer = Annotated[PaidOrderCancellation | None, Field(
    description='Whether a customer can cancel a paid order')]
PaidOrderCancellationHours = Annotated[int | None, Field(
    ge=1, le=POLICY_CANCELLATION_HOURS_MAX,
    description='Hours after payment a customer can cancel a paid order '
    '(before handover), with paidOrderCancellation withinHours')]
# The rest
AdditionalTerms = Annotated[str | None, Field(
    min_length=1, max_length=POLICY_TERMS_MAX_LENGTH,
    description='Any other terms, as the admin gave them')]

# The answers that only apply with returns accepted, with delivery among
# the deliveryOptions, and with paid orders
# cancellable within hours (Policy.normalized clears them otherwise).
POLICY_RETURN_ANSWERS = POLICY_RETURN_QUESTIONS
POLICY_DELIVERY_ANSWERS = ('deliveryMethod', 'deliveryAreas')
POLICY_CANCELLATION_HOURS_ANSWERS = ('paidOrderCancellationHours', )


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
    deliveryMethod: DeliveryMethod = None
    deliveryAreas: DeliveryAreas = None
    handoverDays: HandoverDays = None
    authorizedPaymentMethods: AuthorizedPaymentMethods = None
    paidOrderCancellation: PaidOrderCancellationAnswer = None
    paidOrderCancellationHours: PaidOrderCancellationHours = None
    additionalTerms: AdditionalTerms = None
    createdAt: datetime.datetime = Field(..., description='When it was created')
    updatedAt: datetime.datetime = Field(...,
                                         description='When it last changed')

    @field_validator('returnConditions', 'nonReturnableItems',
                     'deliveryMethod', 'deliveryAreas', 'additionalTerms',
                     mode='before')
    @classmethod
    def _strip_text(cls, v):
        return v.strip() if isinstance(v, str) else v

    def normalized(self) -> 'Policy':
        """A copy with the answers that no longer apply cleared: no returns
        clears POLICY_RETURN_ANSWERS, no delivery POLICY_DELIVERY_ANSWERS,
        and a paid order
        cancellation other than withinHours (or none)
        POLICY_CANCELLATION_HOURS_ANSWERS."""
        cleared = {}
        if self.acceptsReturns is False:
            cleared.update(dict.fromkeys(POLICY_RETURN_ANSWERS))
        if (self.deliveryOptions is not None
                and 'delivery' not in self.deliveryOptions):
            cleared.update(dict.fromkeys(POLICY_DELIVERY_ANSWERS))
        if self.paidOrderCancellation != 'withinHours':
            cleared.update(dict.fromkeys(POLICY_CANCELLATION_HOURS_ANSWERS))
        return self.model_copy(update=cleared)

    def problems(self) -> str | None:
        """What the answers miss or repeat, as the question to ask the
        admin again; None when they hold together."""
        if self.acceptsReturns and self.returnWindowDays is None:
            return ('Returns are accepted: ask how many days after handover '
                    'a customer can return an item (returnWindowDays)')
        if (self.paidOrderCancellation == 'withinHours'
                and self.paidOrderCancellationHours is None):
            return ('Paid orders can be cancelled within hours: ask how many '
                    'hours after payment (paidOrderCancellationHours)')
        for field in ('deliveryOptions', 'authorizedPaymentMethods'):
            values = getattr(self, field)
            if field == 'authorizedPaymentMethods' and values is not None:
                values = [method.method for method in values]
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


# A store's policy questions: its answers (POLICY_ANSWER_FIELDS) and, for
# a store that delivers, its delivery zones (POLICY_ZONES_QUESTION, drawn on
# the map, not saved with updateMyStorePolicy).
PolicyQuestion = Literal[(*typing.get_args(PolicyField),
                          POLICY_ZONES_QUESTION)]
# The order the admin's chat asks them in: payment, delivery, returns.
POLICY_QUESTION_ORDER = (
    'authorizedPaymentMethods', 'paidOrderCancellation',
    'paidOrderCancellationHours', 'deliveryOptions', POLICY_ZONES_QUESTION,
    'deliveryMethod', 'deliveryAreas', 'handoverDays', 'acceptsReturns',
    'returnWindowDays', 'returnConditions', 'nonReturnableItems',
    'refundMethod', 'returnShippingPaidBy', 'defectReportDays',
    'additionalTerms')
# Each question in plain words, for the admin (setMyStoreOpen's refusal).
POLICY_QUESTION_LABELS = {
    'authorizedPaymentMethods': 'how customers pay',
    'paidOrderCancellation': 'whether a customer can cancel a paid order',
    'paidOrderCancellationHours': 'how many hours after payment a paid '
    'order can be cancelled',
    'deliveryOptions': 'pickup, delivery or both',
    POLICY_ZONES_QUESTION: 'where you deliver (draw your delivery zones on '
    'the map)',
    'deliveryMethod': 'how you deliver',
    'deliveryAreas': 'a name for your delivery areas',
    'handoverDays': 'how many days until a pickup order is ready',
    'acceptsReturns': 'whether you accept returns',
    'returnWindowDays': 'how many days a customer has to return an item',
    'returnConditions': 'what a returned item must be like',
    'nonReturnableItems': 'which items can never be returned',
    'refundMethod': 'how a return is refunded',
    'returnShippingPaidBy': 'who pays the shipping of a return',
    'defectReportDays': 'how many days a customer has to report a defective '
    'or wrong item',
    'additionalTerms': 'any other terms',
}
if (set(POLICY_QUESTION_ORDER) != set(typing.get_args(PolicyQuestion))
        or set(POLICY_QUESTION_LABELS) != set(POLICY_QUESTION_ORDER)):
    raise TypeError('POLICY_QUESTION_ORDER and POLICY_QUESTION_LABELS must '
                    'name exactly the policy questions')


def policyQuestionApplies(question: str, answers: dict) -> bool:
    """Whether `question` applies to the store's `answers` (a policy, as
    stored or sent; a missing answer is not said): the return questions
    only while returns are accepted, the hours to cancel a paid order only
    with withinHours, the days until a pickup is ready only while pickup is
    offered (or deliveryOptions is not said yet: pickup is the default),
    and the delivery answers and zones only while delivery is offered. The
    client's policyQuestions.ts mirrors it for policy cards saved before
    the server sent unansweredQuestions."""
    options = answers.get('deliveryOptions')
    if question in POLICY_RETURN_QUESTIONS:
        return answers.get('acceptsReturns') is True
    if question in POLICY_CANCELLATION_HOURS_ANSWERS:
        return answers.get('paidOrderCancellation') == 'withinHours'
    if question == 'handoverDays':
        return options is None or 'pickup' in options
    if question in (*POLICY_DELIVERY_ANSWERS, POLICY_ZONES_QUESTION):
        return options is not None and 'delivery' in options
    return True


def unansweredPolicyQuestions(answers: dict,
                              deliveryZones: list[dict] | None
                              ) -> list[str]:
    """The required policy questions the store has not answered yet, in
    POLICY_QUESTION_ORDER: every question that applies
    (policyQuestionApplies) and is not optional (POLICY_OPTIONAL_QUESTIONS)
    whose answer is not said (None, missing, or an empty list), and
    POLICY_ZONES_QUESTION for a store that delivers with no delivery zone
    (ruledZones of `deliveryZones`, as stored). A store opens, and takes
    orders, only while this is empty. Pure: no database read."""
    unanswered = []
    for question in POLICY_QUESTION_ORDER:
        if (question in POLICY_OPTIONAL_QUESTIONS
                or not policyQuestionApplies(question, answers)):
            continue
        if question == POLICY_ZONES_QUESTION:
            if not ruledZones(deliveryZones):
                unanswered.append(question)
        elif answers.get(question) in (None, []):
            unanswered.append(question)
    return unanswered


def policyQuestionsText(questions: list[str]) -> str:
    """`questions` in plain words, joined for a sentence."""
    return '; '.join(POLICY_QUESTION_LABELS[q] for q in questions)


class PolicyView(StoredDocumentKeys, Policy):
    """A Policies document as stored."""


class StorePolicyResponse(ResponseModel):
    """A store's policy as a route sends it: every answer, null while the
    store has not said. Each answer has Policy's own type alias, which
    test_route_response_models.py's MIRRORS keeps in step. Beside them,
    deliveryZones: the zones the store delivers to, each with its own
    delivery policy (its shape is not sent)."""
    AI_READS: ClassVar[dict[str, str]] = {
        'acceptsReturns': 'a policy answer the AI tells or warns about',
        'returnWindowDays': 'a policy answer the AI tells or warns about',
        'returnConditions': 'a policy answer the AI tells or warns about',
        'nonReturnableItems': 'a policy answer the AI tells or warns about',
        'refundMethod': 'a policy answer the AI tells or warns about',
        'returnShippingPaidBy': 'a policy answer the AI tells or warns about',
        'defectReportDays': 'a policy answer the AI tells or warns about',
        'deliveryOptions': 'a policy answer the AI tells or warns about',
        'deliveryMethod': 'a policy answer the AI tells or warns about',
        'deliveryAreas': 'a policy answer the AI tells or warns about',
        'handoverDays': 'a policy answer the AI tells or warns about',
        'authorizedPaymentMethods': 'a policy answer the AI tells or warns about',
        'paidOrderCancellation': 'a policy answer the AI tells or warns about',
        'paidOrderCancellationHours': 'a policy answer the AI tells or warns about',
        'additionalTerms': 'a policy answer the AI tells or warns about',
        'deliveryZones': 'a policy answer the AI tells or warns about',
    }
    acceptsReturns: AcceptsReturns = Field(...)
    returnWindowDays: ReturnWindowDays = Field(...)
    returnConditions: ReturnConditions = Field(...)
    nonReturnableItems: NonReturnableItems = Field(...)
    refundMethod: RefundMethodAnswer = Field(...)
    returnShippingPaidBy: ReturnShippingPaidBy = Field(...)
    defectReportDays: DefectReportDays = Field(...)
    deliveryOptions: DeliveryOptions = Field(...)
    deliveryMethod: DeliveryMethod = Field(...)
    deliveryAreas: DeliveryAreas = Field(...)
    handoverDays: HandoverDays = Field(...)
    authorizedPaymentMethods: AuthorizedPaymentMethodRows = Field(...)
    paidOrderCancellation: PaidOrderCancellationAnswer = Field(...)
    paidOrderCancellationHours: PaidOrderCancellationHours = Field(...)
    additionalTerms: AdditionalTerms = Field(...)
    deliveryZones: list[DeliveryZoneRule] = Field(
        ..., max_length=DELIVERY_ZONES_MAX,
        description='The zones the store delivers to and their policies; '
        'empty: it takes no delivery order')
    updatedAt: datetime.datetime = Field(...,
                                         description='When it last changed')
