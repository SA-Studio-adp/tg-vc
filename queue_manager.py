"""
In-memory queue of media items per chat.
Simple, single-process — swap for Redis/DB if you need multi-worker scaling.
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
    thumbnail_url: Optional[str] = None
    duration: float = 0.0
    id: int = field(default_factory=lambda: next(_id_counter))
    elapsed: float = 0.0          # best-effort playback position, for seek math
    message_id: Optional[int] = None  # the channel message that shows the player card


class ChatQueue:
    def __init__(self):
        self.items: list[QueueItem] = []
        self.current_index: int = -1
        self.is_playing: bool = True  # bot-side flag toggled by Pause/Play button

    @property
    def current(self) -> Optional[QueueItem]:
        if 0 <= self.current_index < len(self.items):
            return self.items[self.current_index]
        return None

    def add(self, item: QueueItem):
        self.items.append(item)
        if self.current_index == -1:
            self.current_index = 0

    def next(self) -> Optional[QueueItem]:
        if self.current_index + 1 < len(self.items):
            self.current_index += 1
            self.is_playing = True
            return self.current
        return None

    def has_next(self) -> bool:
        return self.current_index + 1 < len(self.items)

    def remove(self, item_id: int):
        self.items = [i for i in self.items if i.id != item_id]

    def clear(self):
        self.items.clear()
        self.current_index = -1
        self.is_playing = True


# chat_id -> ChatQueue
_queues: dict[int, ChatQueue] = {}


def get_queue(chat_id: int) -> ChatQueue:
    if chat_id not in _queues:
        _queues[chat_id] = ChatQueue()
    return _queues[chat_id]
