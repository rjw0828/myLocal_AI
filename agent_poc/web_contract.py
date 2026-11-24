"""Session arbitration interface retained from MCP-5C; no Agent loop."""
from contextlib import contextmanager
from threading import Lock
from uuid import uuid4

class ToolChatError(RuntimeError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)

class SessionReservations:
    """Shared arbitration interface for future ordinary Chat/Reset adapters."""
    def __init__(self):
        self._lock, self._owners = Lock(), {}

    def reserve(self, session_id, owner):
        with self._lock:
            if session_id in self._owners:
                raise ToolChatError("session_reserved")
            self._owners[session_id] = owner

    def release(self, session_id, owner):
        with self._lock:
            if self._owners.get(session_id) == owner:
                del self._owners[session_id]

    def transfer(self, session_id, old_owner, new_owner):
        with self._lock:
            if self._owners.get(session_id) != old_owner:
                raise ToolChatError("session_reserved")
            self._owners[session_id] = new_owner

    def owners(self):
        with self._lock:
            return dict(self._owners)

    def owner(self, session_id):
        with self._lock:
            return self._owners.get(session_id)

    @contextmanager
    def ordinary(self, session_id):
        owner = uuid4().hex
        self.reserve(session_id, owner)
        try:
            yield
        finally:
            self.release(session_id, owner)


class DisabledBackend:
    def __init__(self):self.reservations=SessionReservations()
    def cancel_session(self,sid):pass
    def cancel_all(self):pass
    def shutdown(self):pass
