"""Product photos the store's admin generates (PhotoActions).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime
from typing import Literal

from typing import ClassVar
from pydantic import Field

from objects.base import AppModel, MongoModel, ResponseModel
from objects.product import PRODUCT_BATCH_MAX
from objects.store import HEX_COLOR_PATTERN
# The photo limits the client checks too (sharedConstants.py says each):
# the photos a call makes, the longest prompt and notes, the most reference
# photos per call.
from sharedConstants import (PHOTO_NOTES_MAX_LENGTH, PHOTO_PROMPT_MAX_LENGTH,
                             PHOTO_REFERENCES_MAX, PHOTOS_PER_CALL)


# ──────────────────────────────────────────────
# Generated product photos
# ──────────────────────────────────────────────

# The most products findProductsInPhoto finds in one photo.
PHOTO_FIND_ITEMS_MAX = 12
# The most colors findColorsInPhoto reads from one photo, and the longest
# focus phrase it sends SAM.
PHOTO_COLORS_MAX = 6
PHOTO_COLOR_FOCUS_MAX_LENGTH = 60

# Where a store photo came from: the image model made it, or it is the
# admin's own photo, copied as it is (setUploadedProductPhotos; before it,
# makeProductPhotoChoices).
PhotoSource = Literal['generated', 'original']


class GeneratedPhoto(MongoModel):
    """A GeneratedPhotos document: one photo the image model made for a
    store, saved at `path` in the Firebase bucket. (storeId, createdAt) is
    indexed (mongoDb.ensureIndexes), for the store's photos newest first."""
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    createdBy: str = Field(..., min_length=1,
                           description="The admin's Users _id")
    path: str = Field(..., min_length=1,
                      description='Its object path in the bucket')
    url: str = Field(..., min_length=1, description='Its download URL')
    sourcePaths: list[str] = Field(
        ..., min_length=1, max_length=PHOTO_REFERENCES_MAX,
        description="The object paths of the admin's reference photos (an "
        "original's: the upload it was copied from, which "
        'setUploadedProductPhotos compares to copy an upload once)')
    notes: str | None = Field(None, max_length=PHOTO_NOTES_MAX_LENGTH,
                              description="The admin's direction typed in "
                              'chat for this call, added to its prompt as '
                              'the special request')
    productId: str | None = Field(
        None, description='The Products _id it was copied for (an original, '
        'setUploadedProductPhotos) or made for (makeProductPhotoChoices); '
        'null otherwise, and on a photo saved before photos kept it')
    model: str | None = Field(
        ..., description='The image model that made it; null for an '
        'original')
    source: PhotoSource = Field(
        'generated', description='Made by the model, or the original '
        "(a document saved before originals existed is 'generated')")
    prompt: str | None = Field(
        None, description='The prompt the model was given; null for an '
        'original and for a photo saved before prompts were kept')
    createdAt: datetime.datetime = Field(..., description='When it was saved')


class GeneratedPhotoRow(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {
        '_id': 'the photoId setProductPhoto takes',
    }
    id: str = Field(..., alias='_id', description='GeneratedPhotos _id')
    url: str = Field(..., min_length=1, description='Its download URL')
    prompt: str | None = Field(
        None, description='The prompt the model was given for it; null on '
        'a card saved before each photo carried its own')


class GeneratedPhotosResponse(ResponseModel):
    """What generateProductPhotos sends: the photos it saved, each with
    the prompt it was made with."""
    AI_READS: ClassVar[dict[str, str]] = {
        'photos': 'their _ids',
        'notGenerated': 'why fewer were made: told to the admin',
    }
    photos: list[GeneratedPhotoRow] = Field(
        ..., max_length=PHOTOS_PER_CALL,
        description='The photos made, all from the one prompt')
    notGenerated: str | None = Field(
        ..., description='Why fewer photos than asked were made; null when '
        'all were')


class PhotoChoiceRow(ResponseModel):
    """One photo the admin may put on the product."""
    AI_READS: ClassVar[dict[str, str]] = {
        '_id': 'the photoId setProductPhoto takes',
        'source': 'numbering: 1 is the original',
    }
    id: str = Field(..., alias='_id', description='GeneratedPhotos _id')
    url: str = Field(..., min_length=1, description='Its download URL')
    source: PhotoSource = Field(..., description='Styled, or the original')
    prompt: str | None = Field(
        None, description='The prompt the model was given for it; null for '
        'the original, and on a card saved before each photo carried its '
        'own')


class ProductPhotoChoicesResponse(ResponseModel):
    """What makeProductPhotoChoices sends: the product, its original photo
    first, then the ones made, each with its prompt."""
    AI_READS: ClassVar[dict[str, str]] = {
        'productId': 'setProductPhoto takes it',
        'productName': 'names the product',
        'photos': 'their _ids',
        'notGenerated': 'why fewer were made: told to the admin',
    }
    productId: str = Field(..., min_length=1, description='The Products _id')
    productName: str = Field(..., min_length=1, description='Product name')
    photos: list[PhotoChoiceRow] = Field(
        ..., min_length=1, max_length=1 + PHOTOS_PER_CALL,
        description='The original first, then the photos made')
    notGenerated: str | None = Field(
        ..., description="Why fewer photos than asked were made (today's "
        'limit reached, generation unavailable); null when all were')


class UploadedProductPhoto(AppModel):
    """One product of a setUploadedProductPhotos call: the photo the admin
    uploaded for it on the new products form. Types only: the tool checks
    the values, naming the product's position."""
    productId: str = Field(..., description='The Products _id')
    imageUrl: str = Field(..., description="The uploaded photo's download "
                          "URL, the admin's own upload")


class UploadedProductPhotoRow(ResponseModel):
    """A product whose uploaded photo is now on it: the original, kept as
    one of the store's photos, and the prompt better photos are made from
    unless the admin edits it."""
    AI_READS: ClassVar[dict[str, str]] = {
        'productId': 'makeProductPhotoChoices takes it',
        'productName': 'names the product',
        'photoId': 'setProductPhoto takes it to put the original back',
    }
    productId: str = Field(..., min_length=1, description='The Products _id')
    productName: str = Field(..., min_length=1, description='Product name')
    photoId: str = Field(..., min_length=1,
                         description="The original's GeneratedPhotos _id")
    photoUrl: str = Field(..., min_length=1,
                          description="The original's download URL, now the "
                          "product's photo")
    defaultPrompt: str = Field(
        ..., min_length=1, max_length=PHOTO_PROMPT_MAX_LENGTH,
        description='The prompt better photos are made from unless the '
        'admin edits it')


class PhotoNotSavedRow(ResponseModel):
    """A product whose uploaded photo could not be put on it."""
    AI_READS: ClassVar[dict[str, str]] = {
        'productId': 'names the product',
        'productName': 'names the product',
        'reason': 'told to the admin',
    }
    productId: str = Field(..., min_length=1, description='The Products _id')
    productName: str = Field(..., min_length=1, description='Product name')
    reason: str = Field(..., min_length=1, description='Why it was not saved')


class PhotoShotRow(ResponseModel):
    """One of the fixed shots every photo call makes (PhotoActions.PHOTO_SHOTS),
    its line as the store's brand colors write it: the edit box shows it
    read-only under the base prompt."""
    label: str = Field(..., min_length=1, description="The shot's short name")
    line: str = Field(..., min_length=1,
                      description="The shot's line, added to the base prompt "
                      'for its photo')


class UploadedProductPhotosResponse(ResponseModel):
    """What setUploadedProductPhotos sends: the products whose uploaded
    photo is now on them, and those it could not be put on."""
    AI_READS: ClassVar[dict[str, str]] = {
        'products': 'the products whose photo is on',
        'notSaved': 'told to the admin',
    }
    products: list[UploadedProductPhotoRow] = Field(
        ..., max_length=PRODUCT_BATCH_MAX,
        description='The products whose photo is on, in the order sent')
    notSaved: list[PhotoNotSavedRow] = Field(
        ..., max_length=PRODUCT_BATCH_MAX,
        description='The products whose photo could not be saved; empty '
        'when all were')
    shots: list[PhotoShotRow] = Field(
        ..., max_length=PHOTOS_PER_CALL,
        description='The shots better photos are made in, one per photo, '
        'for the card')


class FoundProductRow(ResponseModel):
    """A product findProductsInPhoto found: its name and its cut-out
    photo."""
    AI_READS: ClassVar[dict[str, str]] = {
        'name': 'goes into showNewProductsForm',
        'imageUrl': 'goes into showNewProductsForm',
    }
    name: str = Field(..., min_length=1, description='Product name')
    imageUrl: str = Field(..., min_length=1,
                          description="The cut-out photo's URL, the "
                          "admin's own upload")


class FoundProductsResponse(ResponseModel):
    """What findProductsInPhoto sends: the products found, in the photo's
    order, and those named but not located (or whose cut-out could not be
    saved)."""
    AI_READS: ClassVar[dict[str, str]] = {
        'products': 'the products found',
        'notFound': 'told to the admin',
    }
    products: list[FoundProductRow] = Field(
        ..., max_length=PHOTO_FIND_ITEMS_MAX,
        description='The products found, left to right')
    notFound: list[str] = Field(
        ..., max_length=PHOTO_FIND_ITEMS_MAX,
        description='The names of products seen but not located, or '
        'whose cut-out could not be saved')


class PhotoColorRow(ResponseModel):
    """A color findColorsInPhoto read: its hex code and how much of the
    photo (or of the part it looked at) it covers."""
    AI_READS: ClassVar[dict[str, str]] = {
        'hex': 'the color saved as a brand color',
        'share': 'suggest the biggest',
        'neutral': 'suggest a non-neutral',
    }
    hex: str = Field(..., pattern=HEX_COLOR_PATTERN,
                     description='The color, like #1A73E8')
    share: float = Field(..., gt=0, le=1,
                         description='Its share of the pixels read, 0 to 1')
    neutral: bool = Field(..., description='White, black or grey: rarely a '
                          'brand color')


class PhotoColorsResponse(ResponseModel):
    """What findColorsInPhoto sends: the photo's main colors, biggest share
    first, and the part it looked at."""
    AI_READS: ClassVar[dict[str, str]] = {
        'colors': 'the colors read',
        'focus': 'the part read',
        'focusFound': 'false: say the focus was not found',
    }
    colors: list[PhotoColorRow] = Field(
        ..., max_length=PHOTO_COLORS_MAX,
        description='The main colors, biggest share first; empty when the '
        'photo has no opaque pixels')
    focus: str | None = Field(
        ..., max_length=PHOTO_COLOR_FOCUS_MAX_LENGTH,
        description='The part of the photo asked for ("green logo"); null '
        'for the whole photo')
    focusFound: bool | None = Field(
        ..., description='Whether the focus was found (false: the colors '
        'are the whole photo\'s); null without a focus')
