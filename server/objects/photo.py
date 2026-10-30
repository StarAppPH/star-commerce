"""Product photos the store's admin generates (PhotoActions).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime
from typing import Literal

from pydantic import Field

from objects.base import MongoModel, ResponseModel


# ──────────────────────────────────────────────
# Generated product photos
# ──────────────────────────────────────────────

PHOTO_NOTES_MAX_LENGTH = 300
# The admin's reference photos per call, and the photos one call makes.
PHOTO_REFERENCES_MAX = 10
PHOTOS_PER_CALL_MAX = 4
# The styled photos makeProductPhotoChoices makes next to the original.
PHOTO_CHOICES_GENERATED = 3
# The most products findProductsInPhoto finds in one photo.
PHOTO_FIND_ITEMS_MAX = 12

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


class GeneratedPhotosResponse(ResponseModel):
    """What generateProductPhotos sends: the photos it saved and the prompt
    they were made with."""
    photos: list[GeneratedPhotoRow] = Field(
        ..., description='The photos made, in order')
    prompt: str = Field(..., min_length=1,
                        description='The prompt the model was given')


class PhotoChoiceRow(ResponseModel):
    """One photo the admin may put on the product."""
    id: str = Field(..., alias='_id', description='GeneratedPhotos _id')
    url: str = Field(..., min_length=1, description='Its download URL')
    source: PhotoSource = Field(..., description='Styled, or the original')


class ProductPhotoChoicesResponse(ResponseModel):
    """What makeProductPhotoChoices sends: the product, its original photo
    first, then the styled ones made."""
    productId: str = Field(..., min_length=1, description='The Products _id')
    productName: str = Field(..., min_length=1, description='Product name')
    photos: list[PhotoChoiceRow] = Field(
        ..., min_length=1, max_length=1 + PHOTO_CHOICES_GENERATED,
        description='The original first, then the styled photos')
    prompt: str | None = Field(
        ..., description='The prompt the styled photos were made with; null '
        'when none were made')
    notGenerated: str | None = Field(
        ..., description='Why no styled photos were made (no photo style, '
        "today's limit reached, generation unavailable); null when they "
        'were')


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
