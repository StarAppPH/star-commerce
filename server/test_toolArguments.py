"""toolArguments.py: a top-level object or list argument the chat model
sent as a JSON string is decoded only where its field takes that shape and
no str; a refusal names each field with its shape error's reason, never a
value. No database."""

import json
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from objects import NewOrderDelivery, NewOrderItem
from toolArguments import decodedArguments, refusedFields

DELIVERY = {'address': '7 Mabini St', 'latitude': 14.5, 'longitude': 121.0}
ITEMS = [{'productId': 'p1', 'quantity': 2}]


class Args(BaseModel):
    model_config = ConfigDict(extra='ignore', hide_input_in_errors=True)
    note: str | None = None
    kind: Literal['a', 'b'] | None = None
    delivery: NewOrderDelivery | None = None
    items: list[NewOrderItem] = []
    either: str | NewOrderDelivery | None = None
    aliased: NewOrderDelivery | None = Field(None, alias='where')


def test_aJsonStringObjectOrList_isDecodedWhereItsFieldTakesIt():
    sent = {'delivery': json.dumps(DELIVERY), 'items': json.dumps(ITEMS),
            'where': json.dumps(DELIVERY)}

    args = Args.model_validate(decodedArguments(Args, sent))

    assert args.delivery.model_dump(exclude_none=True) == DELIVERY
    assert [item.model_dump() for item in args.items] == ITEMS
    assert args.aliased.address == DELIVERY['address']
    assert sent['delivery'] == json.dumps(DELIVERY)  # the input is kept


@pytest.mark.parametrize('key, value', [
    ('note', json.dumps(DELIVERY)),        # a str field: never decoded
    ('either', json.dumps(DELIVERY)),      # takes a str too
    ('kind', json.dumps('a')),             # a Literal of strings
    ('delivery', 'not json {'),            # not JSON
    ('delivery', json.dumps(ITEMS)),       # a list for an object
    ('items', json.dumps(DELIVERY)),       # an object for a list
    ('delivery', json.dumps('text')),      # a JSON string
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
    assert message == ('kind, delivery (expected an object), '
                       'items (expected a list)')
    assert 'SECRET' not in message
