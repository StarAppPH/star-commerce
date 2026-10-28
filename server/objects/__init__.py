"""
objects — Define your Pydantic models here (one module per domain, see
LAYOUT below).

HOW IT WORKS:
  - Models defined here are used for data validation, serialization, and TypeScript schema generation.
  - Run `ENVIRONMENT=localdev python AppCreator.py` to generate TypeScript interfaces from these models into client/app/schemas/.
  - Every ApiRequests route declares one of these models as its responseModel
    (@route_config(responseModel=...)): the generated client method returns
    Promise<ApiResponse<Model>>, and in localdev route_config checks every
    response against it (see "RESPONSE MODELS" below and ARCHITECTURE.md).
  - Models should contain pure data + validation logic. No database calls (those belong in ApiRequests or action files).

LAYOUT:
  - objects is a package with one module per domain (objects/user.py,
    objects/store.py, ...). objects/base.py holds MongoModel, AppModel,
    StoredDocumentKeys, ResponseModel, ApiResponse, the response check and
    the projections derived from a ResponseModel.
  - Add a model to the module of its domain (or to a new module), then
    re-export it below. Callers import every model from `objects`, never
    from a submodule, and AppCreator generates a TS schema only for the
    classes this package exposes (test_objects_package_exports.py).
  - A submodule imports its siblings directly
    (`from objects.store import StoreCurrency`), never `from objects import
    ...`, which would be circular.
  - A test that patches a name a model looks up (monkeypatch.setattr) must
    patch it on the submodule that uses it: patching it on `objects` does
    not reach the submodule's own binding.

MODEL HIERARCHY:
  - MongoModel: Base for documents stored in MongoDB (has _id, _version, and empty-string coercion).
  - AppModel: Base for embedded/non-persisted models (has empty-string coercion only).
  - ResponseModel: Base for what a route SENDS (its responseModel, a Pub/Sub
    publisher's payload model) and for every model nested in one. Never a
    database model: see "RESPONSE MODELS" below.
  - BaseModel: Use only for simple data carriers with no str | None fields (e.g., ApiResponse).
  - StoredDocumentKeys: mixin for a document as READ from MongoDB
    (`class FooView(StoredDocumentKeys, Foo)`): _id and _version required.

WRITING A MODEL:
  1. Create a class that extends MongoModel (for DB documents) or AppModel (for embedded models).
  2. Define fields with type annotations and Field(...) for metadata.
  3. MongoModel auto-generates _id and provides _version for optimistic locking.

FIELD CONVENTIONS:
  - MongoModel handles _id and _version automatically — you don't need to define them.
  - Use `Field(default_factory=...)` for dynamic defaults (e.g., timestamps, random IDs).
  - Use `str | None` for nullable fields — empty strings are auto-coerced to None.

RESPONSE MODELS (what a route declares as its responseModel):
  - A responseModel is a ResponseModel, and so is every model nested in it,
    all the way down: never a MongoModel, AppModel, StoredDocumentKeys or
    *View. A ResponseModel inherits from other ResponseModels only. The same
    holds for a Pub/Sub publisher's payload model. Its fields are built from
    allowed leaves only (a scalar, datetime, date, Literal, Enum or
    ResponseModel, through Optional, unions, list and dict[str, ...]: never
    Any, a bare dict or list, a TypedDict or a dataclass), and it declares
    no @computed_field. route_config refuses a model that breaks this when
    the route is decorated, AppCreator refuses to generate, naming the model
    with its file:line, and test_route_response_models.py asserts it too
    (responseModelCheck.py).
  - It declares EXACTLY the fields the route sends, each under its
    serialized key (its alias, else its name): in localdev (so in every
    test) the response check rejects any other key. So a route returns only
    those fields: it reads them with
    `db.readResponse(FooResponse, query, collection)` (or readResponses),
    which projects what the model declares (projection_of, never written by
    hand) and cuts what it read (projectedCopy), or builds the dict
    explicitly. A value the route reads for its own use is dropped before it
    returns (`projectedCopy(FooResponse, document)` keeps what the model
    declares). An aggregation answers with
    `db.aggregateResponses(FooResponse, pipeline, collection)`.
  - A field that can be one of two or more ResponseModels declares a str
    discriminator, `Field(discriminator='kind')`, each member a
    `kind: Literal[...]` of its own.
  - A route returns a dict with explicit keys: a list or a primitive is
    wrapped in a model with one field (`{'todos': [...]}`).
  - Omittable but never null:
    `x: str = Field(None)`. Omittable or null: `x: str | None = None`.
  - A defaulted field is optional (`x?:`) in the generated TS type. A model
    a route ALWAYS sends with every field may opt in to
    `model_config = ConfigDict(json_schema_serialization_defaults_required=True)`
    so its defaulted fields are required in TS.
  - No bare dict/list/Any fields, no dict[str, X] keyed by customer data,
    and no values in validator messages (they reach the client and logs).

VALIDATION:
  - Use @field_validator for single field validation.
  - Use @model_validator for cross-field validation.
  - A ResponseModel declares no @computed_field (refused): the generated TS
    type would declare it as always present, but a route returns a raw
    dict, which never carries it. Compute it in the route (a real field),
    or in the client.
  - No validator may EVER mutate a value it receives: return a new dict or
    list (copy-on-write) when it changes anything. That covers every
    before-validator (model_validator and field_validator alike) and an
    after-validator of an Any or bare dict/list field: all of them are
    handed the caller's own objects. In localdev route_config validates
    every route's return value against its responseModel without copying
    it, so a mutating validator would rewrite the response the caller
    receives (and the check raises when it detects that).

EXAMPLE:
  class Product(MongoModel):
      name: str = Field(..., description='Product name')
      price: float = Field(..., ge=0, description='Price in USD')
      category: str | None = Field(None, description='Product category')
      createdAt: datetime.datetime = Field(default_factory=datetime.datetime.now)

      @field_validator('name')
      @classmethod
      def name_not_empty(cls, v: str) -> str:
          if not v.strip():
              raise ValueError('Product name cannot be empty')
          return v


  class ProductRow(ResponseModel):
      \"\"\"One row of listProducts: what it sends, read with
      projection_of(ProductRow).\"\"\"
      id: str = Field(..., alias='_id')
      name: str


  class ProductListResponse(ResponseModel):
      products: list[ProductRow]
"""

# The names the single-file objects.py imported. It had no __all__, so
# `from objects import *` (the generated app.py) exported every public one
# of them too; they stay here so it still does.
import functools
import types
import typing

from pydantic import BaseModel, Field, RootModel, TypeAdapter, ValidationError, ValidationInfo, model_validator, computed_field, field_validator, field_serializer, ConfigDict
from pydantic_core import InitErrorDetails
import datetime
from typing import Any, Optional, Literal, List
from utils import generateRandomString
from roles import Role, RoleSetter, AllRoles, UserRoles
from AppConfig import SECRET_RESPONSE_KEYS

# Every name the submodules define, in dependency order.
from objects.base import (_is_union, _OPTIONAL_STR_FIELDS_CACHE,
                          _coerce_empty_strings_to_none,
                          RESPONSE_CHECK_CONTEXT_KEY, validate_route_response,
                          MongoModel, AppModel, StoredDocumentKeys,
                          _sent_keys, ResponseModel, _union_of, _members,
                          _model_members, _holds_a_response_model,
                          _may_be_null, _dotted_models, projection_of,
                          _guard_inside, _refuse_a_guard_read_whole,
                          _projection_of_for_cut, _projection, projectedCopy,
                          _str_tag, _tag_of, _cut, _cut_union, _adapter,
                          _carries_several_models, _literal_values,
                          _tag_literals, _tag_value, _member_for,
                          stripSecretKeys, ApiResponse, NoContentResponse)
from objects.user import (User, UserView, RoleResponse, UserResponse,
                          FIREBASE_ID_TOKEN_MAX_LENGTH, FirebaseUserObject,
                          DevUserRow, DevUserListResponse, RoleListResponse)
from objects.sample import (SampleTodo, SampleTodoView, SampleTodoRow,
                            SampleTodoListResponse)
from objects.store import (STORE_NAME_MAX_LENGTH,
                           STORE_DESCRIPTION_MAX_LENGTH,
                           STORE_ADDRESS_MAX_LENGTH,
                           STORE_OPENING_HOURS_MAX_LENGTH, LATITUDE_LIMIT,
                           LONGITUDE_LIMIT, STORE_SLUG_PATTERN,
                           STORE_SLUG_MAX_LENGTH, PHONE_NUMBER_PATTERN,
                           StoreCurrency, DEFAULT_STORE_CURRENCY,
                           StoreLocation, StoreLocationResponse, Store,
                           StoreView, Customer, CustomerView, StoreResponse,
                           MyStoreResponse, CustomerSessionResponse,
                           PhoneVerifiedResponse)
from objects.product import (PRODUCT_NAME_MAX_LENGTH,
                             PRODUCT_DESCRIPTION_MAX_LENGTH,
                             PRODUCT_INVENTORY_MAX, PRODUCT_PRICE_MAX,
                             PRODUCT_BATCH_MAX, PRODUCTS_PER_STORE_MAX,
                             productNameKey, ReservedInventory, Product,
                             ProductView, NewProduct, ProductRow,
                             ProductListResponse, ReservedInventoryResponse,
                             AdminProductRow, AdminProductListRow,
                             AdminProductListResponse)
from objects.order import (OrderStatus, CancelReason, PaymentMethod,
                           PAYMENT_REFERENCE_PREFIX, REFUND_REFERENCE_PREFIX,
                           ORDER_ITEMS_MAX, ORDER_PAYMENT_MINUTES,
                           MAX_PENDING_ORDERS_PER_STORE, OrderItem,
                           NewOrderItem, Order, OrderView, OrderItemRow,
                           StoreOrderRow, MyOrderRow, StoreOrderListResponse,
                           MyOrderListResponse, CreatedOrderResponse)
from objects.policy import (DeliveryOption, RefundMethod, ReturnShippingPayer,
                            PaidOrderCancellation, PolicyField,
                            POLICY_TEXT_MAX_LENGTH, POLICY_TERMS_MAX_LENGTH,
                            POLICY_DAYS_MAX, AcceptsReturns, ReturnWindowDays,
                            ReturnConditions, NonReturnableItems,
                            RefundMethodAnswer, ReturnShippingPaidBy,
                            DefectReportDays, DeliveryOptions, DeliveryAreas,
                            DeliveryFee, FreeDeliveryMinimum, HandoverDays,
                            AcceptedPaymentMethods,
                            PaidOrderCancellationAnswer,
                            SendsMarketingMessages, AdditionalTerms,
                            POLICY_RETURN_ANSWERS, POLICY_DELIVERY_ANSWERS,
                            Policy, POLICY_ANSWER_FIELDS, PolicyView,
                            StorePolicyResponse)


# AppCreator (_classes_from_module) generates TS schemas only for classes
# whose __module__ is 'objects', and test_route_response_models.py asserts
# it, so every class a submodule defines is reported as defined here.
for _value in list(globals().values()):
    if isinstance(_value, type) and _value.__module__.startswith('objects.'):
        _value.__module__ = 'objects'
del _value

# What `from objects import *` exports: every public name above except the
# submodules, the same names the single-file objects.py exported.
__all__ = sorted(
    name for name, value in globals().items()
    if not name.startswith('_') and not (
        isinstance(value, types.ModuleType)
        and value.__name__.startswith('objects.')))
