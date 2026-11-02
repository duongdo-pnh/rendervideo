from __future__ import annotations

import threading

from .session import StreamSession


class StreamManager:
    """Thread-safe single-GPU session registry (one active stream in MVP)."""

    def __init__(self):
        self._sessions: dict[str, StreamSession] = {}
        self._lock = threading.RLock()

    def add(self, session: StreamSession) -> None:
        with self._lock:
            active = [
                item for item in self._sessions.values()
                if item.get_status()["status"] not in {"stopped", "failed"}
            ]
            if active:
                raise RuntimeError("MVP supports one active stream per GPU")
            if session.config.session_id in self._sessions:
                raise ValueError("session_id already exists")
            self._sessions[session.config.session_id] = session

    def replace(self, session: StreamSession) -> None:
        """Stop and remove any previous stream before registering a new one."""
        with self._lock:
            previous = list(self._sessions.values())
            self._sessions.clear()
        for item in previous:
            item.stop()
        with self._lock:
            self._sessions[session.config.session_id] = session

    def get(self, session_id: str) -> StreamSession:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError:
                raise KeyError(f"unknown stream session: {session_id}") from None

    def stop(self, session_id: str) -> None:
        session = self.get(session_id)
        session.stop()

    def statuses(self) -> list[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [session.get_status() for session in sessions]
