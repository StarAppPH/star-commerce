"""Product photos the store's admin generates (PhotoActions).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field

from objects.base import MongoModel, ResponseModel


# ──────────────────────────────────────────────
# Generated product photos
# ──────────────────────────────────────────────

PHOTO_NOTES_MAX_LENGTH = 300
# The admin's reference photos per call, and the photos one call makes.
PHOTO_REFERENCES_MAX = 10
PHOTOS_PER_CALL_MAX = 4


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
    model: str = Field(..., description='The image model that made it')
    createdAt: datetime.datetime = Field(..., description='When it was saved')


class GeneratedPhotoRow(ResponseModel):
    id: str = Field(..., alias='_id', description='GeneratedPhotos _id')
    url: str = Field(..., min_length=1, description='Its download URL')


class GeneratedPhotosResponse(ResponseModel):
    """What generateProductPhotos sends: the photos it saved."""
    photos: list[GeneratedPhotoRow] = Field(
        ..., description='The photos made, in order')
