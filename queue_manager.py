"""
In-memory queue of media items for the voice-chat stream.
Single-process — swap for Redis/DB if you need multi-worker scaling.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import itertools

_id_counter = itertools.count(1)


class MediaType(Enum):
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"


@dataclass
class QueueItem:
    title: str
    url: str
    media_type: MediaType
    requested_by: int
    filepath: Optional[str] = None
    duration: float = 0.0
    # Actual detected content kind (from downloader.py's ffprobe check),
    # not just guessed from media_type — a /vfile link can turn out to
    # be audio-only (e.g. an mp3) even though MediaType.FILE doesn't
    # say so by itself. cover_path is a still image to loop as video
    # when is_video is False (embedded cover art or a thumbnail); None
    # means rtmp_streamer falls back to a black frame.
    is_video: bool = True
    cover_path: Optional[str] = None
    id: int = field(default_factory=lambda: next(_id_counter))


class StreamQueue:
    def __init__(self):
        self.items: list[QueueItem] = []
        self.current_index: int = -1
        self.is_playing: bool = True  # reflects real rtmp_streamer pause state

    @property
    def current(self) -> Optional[QueueItem]:
        if 0 <= self.current_index < len(self.items):
            return self.items[self.current_index]
        return None

    def add(self, item: QueueItem):
        self.items.append(item)

    def has_next(self) -> bool:
        return self.current_index + 1 < len(self.items)

    def advance(self) -> Optional[QueueItem]:
        if self.has_next():
            self.current_index += 1
            self.is_playing = True
            return self.current
        return None

    def remove(self, item_id: int):
        self.items = [i for i in self.items if i.id != item_id]

    def clear(self):
        self.items.clear()
        self.current_index = -1
        self.is_playing = True


# There's only ever one voice chat we stream into (CHAT_ID), so a single
# global queue is enough — unlike the old per-chat channel-post design.
queue = StreamQueue()
