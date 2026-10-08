"""Session storage. In-memory and thread-safe; swap for Redis/DB in production."""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from app.conversation.models import Session


class SessionNotFound(KeyError):
    pass


class SessionClosed(RuntimeError):
    pass


class SessionStore(ABC):
    @abstractmethod
    def create(self, now: datetime) -> Session: ...

    @abstractmethod
    def get(self, session_id: str) -> Session: ...

    @abstractmethod
    def all(self) -> list[Session]: ...

    @abstractmethod
    def purge(self, now: datetime, retention_seconds: int) -> int: ...


class InMemorySessionStore(SessionStore):
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, now: datetime) -> Session:
        session = Session.new(now)
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def all(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def purge(self, now: datetime, retention_seconds: int) -> int:
        """Drop sessions with no activity for `retention_seconds`."""
        cutoff = now - timedelta(seconds=retention_seconds)
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if s.last_activity_at < cutoff]
            for sid in stale:
                del self._sessions[sid]
        return len(stale)
