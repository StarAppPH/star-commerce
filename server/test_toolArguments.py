"""toolArguments.py: a top-level object or list argument the chat model
sent as a JSON string is decoded only where its field takes that shape and
no str; a refusal names each field with its shape error's reason, never a
value; JSON nested too deep to parse is refused, never a 500. No
database."""

import json
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from objects import NewOrderDelivery, NewOrderItem
from toolArguments import (ArgumentsRefused, decodedArguments, refusedFields,
                           validated)

DELIVERY = {'address': '7 Mabini St', 'latitude': 14.5, 'longitude': 121.0}
ITEMS = [{'productId': 'p1', 'quantity': 2}]
# Deeper than json.loads can parse: a RecursionError, not a ValueError.
TOO_DEEP = '[' * 3000

type DeliveryAlias = NewOrderDelivery


class Args(BaseModel):
    model_config = ConfigDict(extra='ignore', hide_input_in_errors=True)
    note: str | None = None
    kind: Literal['a', 'b'] | None = None
    delivery: NewOrderDelivery | None = None
    items: list[NewOrderItem] = []
    either: str | NewOrderDelivery | None = None
    aliased: NewOrderDelivery | None = Field(None, alias='where')
    annotated: Annotated[NewOrderDelivery, Field(title='x')] | None = None
    typeAlias: DeliveryAlias | None = None
    anything: Any = None
    mapping: dict[str, int] | None = None
    listOrOne: list[NewOrderItem] | NewOrderItem | None = None


def test_aJsonStringObjectOrList_isDecodedWhereItsFieldTakesIt():
    sent = {'delivery': json.dumps(DELIVERY), 'items': json.dumps(ITEMS),
            'where': json.dumps(DELIVERY)}

    args = Args.model_validate(decodedArguments(Args, sent))

    assert args.delivery.model_dump(exclude_none=True) == DELIVERY
    assert [item.model_dump() for item in args.items] == ITEMS
    assert args.aliased.address == DELIVERY['address']
    assert sent['delivery'] == json.dumps(DELIVERY)  # the input is kept


@pytest.mark.parametrize('key, value', [
    ('annotated', DELIVERY),        # Annotated[Model, ...]
    ('typeAlias', DELIVERY),        # a `type X = Model` alias
    ('mapping', {'a': 1}),          # dict[str, ...]
    ('listOrOne', ITEMS),           # list[Model] | Model: a list
    ('listOrOne', ITEMS[0]),        # list[Model] | Model: an object
], ids=['annotated', 'typeAlias', 'dict', 'unionList', 'unionObject'])
def test_eachFieldShape_isDecoded(key, value):
    assert decodedArguments(Args, {key: json.dumps(value)}) == {key: value}
    Args.model_validate({key: value})


@pytest.mark.parametrize('key, value', [
    ('note', json.dumps(DELIVERY)),        # a str field: never decoded
    ('either', json.dumps(DELIVERY)),      # takes a str too
    ('kind', json.dumps('a')),             # a Literal of strings
    ('anything', json.dumps(DELIVERY)),    # Any takes a str
    ('delivery', 'not json {'),            # not JSON
    ('delivery', json.dumps(ITEMS)),       # a list for an object
    ('items', json.dumps(DELIVERY)),       # an object for a list
    ('delivery', json.dumps('text')),      # a JSON string
    ('items', TOO_DEEP),                   # nested too deep to parse
    ('unknown', json.dumps(DELIVERY)),     # no such field
])
def test_anythingElse_isLeftAsSent(key, value):
    assert decodedArguments(Args, {key: value}) == {key: value}


def test_aStringItemInsideAList_isNotDecoded():
    """Only the top level: an item of a list stays as sent."""
    sent = {'items': [json.dumps(ITEMS[0])]}
    assert decodedArguments(Args, sent) == sent


def test_aRefusal_namesTheFieldAndItsShape_neverTheValue():
    with pytest.raises(ValidationError) as refused:
        Args.model_validate(decodedArguments(
            Args, {'delivery': 'SECRET {', 'items': 'SECRET [',
                   'kind': 'SECRET'}))

    message = refusedFields(refused.value, 'arguments')
    assert set(message.split(', ')) == {
        'kind', 'delivery (expected an object)', 'items (expected a list)'}
    assert 'SECRET' not in message


@pytest.mark.parametrize('argumentsJson, refusal', [
    (TOO_DEEP, 'The arguments of tool are not valid JSON'),
    (json.dumps({'items': TOO_DEEP}),
     'Invalid arguments for tool: items (expected a list)'),
    ('[1]', 'The arguments of tool must be a JSON object'),
], ids=['tooDeepArguments', 'tooDeepArgument', 'notAnObject'])
def test_validated_refusesWhatItCannotParse(argumentsJson, refusal):
    with pytest.raises(ArgumentsRefused) as refused:
        validated(Args, 'tool', argumentsJson)
    assert str(refused.value) == refusal


@pytest.mark.parametrize('argumentsJson', [
    TOO_DEEP, json.dumps({'delivery': TOO_DEEP})])
def test_tooDeepJson_isAToolRefusal_inTheChatAndFormTools(argumentsJson):
    """Both chat paths refuse it before running anything."""
    import chatForms
    import chatModel
    content, _ = chatModel.toolResult('customer', 'previewMyCheckout',
                                      argumentsJson, 'u', 's')
    assert json.loads(content)['error'].startswith(
        ('The arguments of previewMyCheckout are not valid JSON',
         'Invalid arguments for previewMyCheckout: delivery'))
    content, form = chatForms.runFormTool('customer', 'showDeliveryPinForm',
                                          TOO_DEEP, 'u', 's')
    assert form is None
    assert json.loads(content) == {
        'error': 'The arguments of showDeliveryPinForm are not valid JSON'}


def test_validated_decodesAJsonStringObject():
    args = validated(Args, 'tool', json.dumps(
        {'delivery': json.dumps(DELIVERY)}))
    assert args.delivery.address == DELIVERY['address']
