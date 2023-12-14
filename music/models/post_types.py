import json
from datetime import datetime
from typing import Literal

from django.utils import timezone
from pydantic import BaseModel, Field
from activities.models.post_types import BasePostDataType

from core.ld import format_ld_date


class PlaylistData(BasePostDataType):
    type: Literal['Playlist']


class PlaylistItemData(BasePostDataType):
    type: Literal['PlaylistItem']
    name: str = Field(alias="http://buddhaflow.se/ns#name")
    artist_name: str = Field(alias="http://buddhaflow.se/ns#artist_name")
    release_name: str = Field(alias="http://buddhaflow.se/ns#release_name")
    isrc: str = Field(alias="http://buddhaflow.se/ns#isrc")
    upc: str = Field(alias="http://buddhaflow.se/ns#isrc")

    class Config:
        extra = "ignore"
        allow_population_by_field_name = True
