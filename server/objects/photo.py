"""Product photos the store's admin generates (PhotoActions).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime
from typing import Literal

from pydantic import Field

from objects.base import MongoModel, ResponseModel
from objects.store import HEX_COLOR_PATTERN


# ──────────────────────────────────────────────
# Generated product photos
# ──────────────────────────────────────────────

PHOTO_NOTES_MAX_LENGTH = 300
# The admin's reference photos per call.
PHOTO_REFERENCES_MAX = 10
# The photos every photo call makes (generateProductPhotos, and
# makeProductPhotoChoices next to the original): the first in the global
# prompt, the others in the global prompt plus the store's photo style.
PHOTOS_PER_CALL = 3
PHOTOS_THEMED = PHOTOS_PER_CALL - 1
# The most products findProductsInPhoto finds in one photo.
PHOTO_FIND_ITEMS_MAX = 12
# The most colors findColorsInPhoto reads from one photo, and the longest
# focus phrase it sends SAM.
PHOTO_COLORS_MAX = 6
PHOTO_COLOR_FOCUS_MAX_LENGTH = 60

# Where a store photo came from: the image model made it, or it is the
# admin's own photo, copied as it is (makeProductPhotoChoices).
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
        description="The object paths of the admin's reference photos")
    notes: str | None = Field(None, max_length=PHOTO_NOTES_MAX_LENGTH,
                              description="The admin's notes for this call")
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
    id: str = Field(..., alias='_id', description='GeneratedPhotos _id')
    url: str = Field(..., min_length=1, description='Its download URL')
    prompt: str | None = Field(
        None, description='The prompt the model was given for it; null on '
        'a card saved before each photo carried its own')


class GeneratedPhotosResponse(ResponseModel):
    """What generateProductPhotos sends: the photos it saved, each with
    the prompt it was made with."""
    photos: list[GeneratedPhotoRow] = Field(
        ..., max_length=PHOTOS_PER_CALL,
        description='The photos made, in order: the first in the global '
        'prompt, the others in the store\'s photo style too')
    notGenerated: str | None = Field(
        ..., description='Why fewer photos than asked were made (no photo '
        'style, a model call failed); null when all were')


class PhotoChoiceRow(ResponseModel):
    """One photo the admin may put on the product."""
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
    productId: str = Field(..., min_length=1, description='The Products _id')
    productName: str = Field(..., min_length=1, description='Product name')
    photos: list[PhotoChoiceRow] = Field(
        ..., min_length=1, max_length=1 + PHOTOS_PER_CALL,
        description='The original first, then the photos made')
    notGenerated: str | None = Field(
        ..., description='Why fewer photos than asked were made (no photo '
        "style, today's limit reached, generation unavailable); null when "
        'all were')


class FoundProductRow(ResponseModel):
    """A product findProductsInPhoto found: its name and its cut-out
    photo."""
    name: str = Field(..., min_length=1, description='Product name')
    imageUrl: str = Field(..., min_length=1,
                          description="The cut-out photo's URL, the "
                          "admin's own upload")


class FoundProductsResponse(ResponseModel):
    """What findProductsInPhoto sends: the products found, in the photo's
    order, and those named but not located."""
    products: list[FoundProductRow] = Field(
        ..., max_length=PHOTO_FIND_ITEMS_MAX,
        description='The products found, left to right')
    notFound: list[str] = Field(
        ..., max_length=PHOTO_FIND_ITEMS_MAX,
        description='The names of products seen but not located')


class PhotoColorRow(ResponseModel):
    """A color findColorsInPhoto read: its hex code and how much of the
    photo (or of the part it looked at) it covers."""
    hex: str = Field(..., pattern=HEX_COLOR_PATTERN,
                     description='The color, like #1A73E8')
    share: float = Field(..., gt=0, le=1,
                         description='Its share of the pixels read, 0 to 1')
    neutral: bool = Field(..., description='White, black or grey: rarely a '
                          'brand color')


class PhotoColorsResponse(ResponseModel):
    """What findColorsInPhoto sends: the photo's main colors, biggest share
    first, and the part it looked at."""
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
