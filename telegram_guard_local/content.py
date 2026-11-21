"""Typed content accepted by the guard; no arbitrary MTProto constructors."""
from __future__ import annotations
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def text_units(value: str, maximum: int, *, empty=False):
    if "\x00" in value or (not empty and not value.strip()):
        raise ValueError("invalid text")
    if len(value.encode("utf-16-le")) // 2 > maximum:
        raise ValueError("text too long")
    return value


class Entity(Strict):
    kind: Literal["bold", "italic", "underline", "strike", "spoiler", "code", "pre",
                  "blockquote", "text_url", "custom_emoji"]
    offset: int = Field(ge=0)
    length: int = Field(gt=0)
    url: str | None = None
    language: str | None = None
    document_id: int | None = Field(default=None, gt=0, lt=2**63)

    @model_validator(mode="after")
    def appropriate_fields(self):
        if (self.url is not None) != (self.kind == "text_url"):
            raise ValueError("url only for text_url")
        if self.url is not None:
            text_units(self.url, 2048)
            if not self.url.startswith(("https://", "http://", "tg://", "mailto:")):
                raise ValueError("unsupported link scheme")
        if self.language is not None and self.kind != "pre":
            raise ValueError("language only for pre")
        if (self.document_id is not None) != (self.kind == "custom_emoji"):
            raise ValueError("document_id only for custom_emoji")
        return self


def validate_entities(text, entities):
    boundaries = {0}
    offset = 0
    for char in text:
        offset += len(char.encode("utf-16-le")) // 2
        boundaries.add(offset)
    for entity in entities:
        if entity.offset not in boundaries or entity.offset + entity.length not in boundaries:
            raise ValueError("entity outside text or inside surrogate pair")


class Options(Strict):
    reply_to: int | None = Field(default=None, gt=0, lt=2**31)
    top_msg_id: int | None = Field(default=None, gt=0, lt=2**31)
    silent: bool = False
    link_preview: bool = False
    entities: list[Entity] = Field(default_factory=list, max_length=100)


class FileContent(Strict):
    type: Literal["file"]
    path: str = Field(min_length=1, max_length=32767, description="Absolute local file path; protected files are refused")
    kind: Literal["document", "photo", "video", "animation", "audio", "voice", "video_note", "sticker"] = "document"
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    mime_type: str | None = Field(default=None, pattern=r"^[\w.+-]+/[\w.+-]+$")
    caption: str = ""
    entities: list[Entity] = Field(default_factory=list, max_length=100)
    thumbnail: str | None = None
    spoiler: bool = False
    ttl_seconds: int | None = Field(default=None, ge=1, le=60)
    duration: float = Field(default=0, ge=0, le=2**31-1)
    width: int = Field(default=0, ge=0, le=16384)
    height: int = Field(default=0, ge=0, le=16384)
    title: str | None = Field(default=None, max_length=256)
    performer: str | None = Field(default=None, max_length=256)
    emoji: str = Field(default="", max_length=32)

    @model_validator(mode="after")
    def media_options(self):
        text_units(self.caption, 4096, empty=True)
        validate_entities(self.caption, self.entities)
        if self.filename and (any(c in self.filename for c in '\\/:\x00') or self.filename in (".", "..")):
            raise ValueError("filename must be a basename")
        if self.kind in ("video", "video_note") and (not self.width or not self.height):
            raise ValueError("video requires width and height")
        if self.kind == "video_note" and self.width != self.height:
            raise ValueError("round video must be square")
        if self.kind in ("sticker", "video_note") and self.caption:
            raise ValueError("this media cannot have a caption")
        if self.spoiler and self.kind not in ("photo", "video", "animation"):
            raise ValueError("spoiler requires visual media")
        if self.ttl_seconds and self.kind not in ("photo", "video", "voice", "video_note"):
            raise ValueError("TTL unsupported for this media")
        if self.thumbnail and self.kind == "photo":
            raise ValueError("photos do not accept a separate thumbnail")
        return self


class AlbumContent(Strict):
    type: Literal["album"]
    items: list[FileContent] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def album_types(self):
        kinds = {item.kind for item in self.items}
        if not (kinds <= {"photo", "video"} or kinds == {"audio"} or kinds == {"document"}):
            raise ValueError("album must contain photos/videos, audio only, or documents only")
        if any(item.ttl_seconds for item in self.items):
            raise ValueError("albums cannot self-destruct")
        return self


class ContactContent(Strict):
    type: Literal["contact"]
    phone_number: str = Field(min_length=1, max_length=32, pattern=r"^\+?[0-9 ()-]+$")
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str = Field(default="", max_length=64)
    vcard: str = Field(default="", max_length=8192)


class LocationContent(Strict):
    type: Literal["location"]
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_radius: int | None = Field(default=None, ge=0, le=1500)
    live_period: int | None = Field(default=None, ge=60, le=86400)
    heading: int | None = Field(default=None, ge=1, le=360)

    @model_validator(mode="after")
    def live_heading(self):
        if self.heading is not None and self.live_period is None:
            raise ValueError("heading requires a live location")
        return self


class VenueContent(Strict):
    type: Literal["venue"]
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    title: str = Field(min_length=1, max_length=256)
    address: str = Field(min_length=1, max_length=1024)
    provider: str = Field(default="", max_length=32)
    venue_id: str = Field(default="", max_length=256)
    venue_type: str = Field(default="", max_length=256)


class PollContent(Strict):
    type: Literal["poll"]
    question: str
    answers: list[str] = Field(min_length=2, max_length=10)
    anonymous: bool = True
    multiple_choice: bool = False
    quiz: bool = False
    correct_answers: list[int] = Field(default_factory=list, max_length=10)
    solution: str = ""
    close_period: int | None = Field(default=None, ge=5, le=600)

    @model_validator(mode="after")
    def poll_rules(self):
        text_units(self.question, 300)
        for answer in self.answers:
            text_units(answer, 100)
        text_units(self.solution, 200, empty=True)
        if self.quiz:
            if self.multiple_choice or len(self.correct_answers) != 1:
                raise ValueError("quiz requires one correct answer and single choice")
        elif self.correct_answers or self.solution:
            raise ValueError("answers and solution require quiz")
        if any(i < 0 or i >= len(self.answers) for i in self.correct_answers):
            raise ValueError("correct answer index out of range")
        return self


class DiceContent(Strict):
    type: Literal["dice"]
    emoticon: Literal["🎲", "🎯", "🏀", "⚽", "🎳", "🎰"] = "🎲"


class ForwardContent(Strict):
    type: Literal["forward"]
    from_chat_id: int = Field(ge=-(2**63)+1, le=2**63-1)
    message_ids: list[int] = Field(min_length=1, max_length=10)
    drop_author: bool = False
    drop_media_captions: bool = False

    @model_validator(mode="after")
    def forward_rules(self):
        if self.from_chat_id == 0 or len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("invalid source or duplicate message IDs")
        if any(not 0 < i < 2**31 for i in self.message_ids):
            raise ValueError("invalid message ID")
        if self.drop_media_captions and not self.drop_author:
            raise ValueError("dropping captions requires dropping author")
        return self


class ExistingMediaContent(Strict):
    type: Literal["existing_media"]
    from_chat_id: int
    message_id: int = Field(gt=0, lt=2**31)


class StoryContent(Strict):
    type: Literal["story"]
    from_chat_id: int
    story_id: int = Field(gt=0, lt=2**31)


class ChecklistContent(Strict):
    type: Literal["checklist"]
    title: str
    items: list[str] = Field(min_length=1, max_length=30)
    others_can_append: bool = False
    others_can_complete: bool = False

    @model_validator(mode="after")
    def checklist_text(self):
        text_units(self.title, 255)
        for item in self.items:
            text_units(item, 100)
        return self


Content = Annotated[Union[FileContent, AlbumContent, ContactContent, LocationContent,
                         VenueContent, PollContent, DiceContent, ForwardContent,
                         ExistingMediaContent, StoryContent, ChecklistContent], Field(discriminator="type")]
CONTENT = TypeAdapter(Content)


def parse_content(value):
    from .model import GuardError
    try:
        parsed = CONTENT.validate_python(value).model_dump()
        def strings(node):
            if isinstance(node, str):
                text_units(node, 32767, empty=True)
            elif isinstance(node, dict):
                for child in node.values():
                    strings(child)
            elif isinstance(node, list):
                for child in node:
                    strings(child)
        strings(parsed)
        return parsed
    except (ValueError, TypeError, UnicodeError):
        raise GuardError("invalid_content") from None


def parse_options(value, text):
    from .model import GuardError
    try:
        options = Options.model_validate(value)
        validate_entities(text, options.entities)
        return options.model_dump(exclude_defaults=True)
    except (ValueError, TypeError, UnicodeError):
        raise GuardError("invalid_options") from None


def content_files(content):
    if not content:
        return []
    if content["type"] == "file":
        return [content]
    if content["type"] == "album":
        return content["items"]
    return []
