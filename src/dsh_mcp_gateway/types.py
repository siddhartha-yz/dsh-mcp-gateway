from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SessionPresence(str, Enum):
    LIVE = "live"
    PERSISTED = "persisted"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class SessionHandle:
    session_id: str
