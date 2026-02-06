from enum import Enum
from typing import Any

ChannelSpec = str | tuple[str, dict[str, Any]]


class ChannelType(str, Enum):
    MAIN = "main"
    HOME_TIMELINE = "homeTimeline"
    LOCAL_TIMELINE = "localTimeline"
    HYBRID_TIMELINE = "hybridTimeline"
    GLOBAL_TIMELINE = "globalTimeline"
    ANTENNA = "antenna"
    CHAT_USER = "chatUser"


TIMELINE_CHANNELS = frozenset(
    {
        ChannelType.HOME_TIMELINE.value,
        ChannelType.LOCAL_TIMELINE.value,
        ChannelType.HYBRID_TIMELINE.value,
        ChannelType.GLOBAL_TIMELINE.value,
    }
)
NOTE_CHANNELS = frozenset({*TIMELINE_CHANNELS, ChannelType.ANTENNA.value})
CHAT_CHANNELS = frozenset({ChannelType.CHAT_USER.value})


__all__ = (
    "ChannelSpec",
    "ChannelType",
    "TIMELINE_CHANNELS",
    "NOTE_CHANNELS",
    "CHAT_CHANNELS",
)
