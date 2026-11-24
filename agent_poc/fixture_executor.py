"""C-3A execution endpoint: fixed JSON fixtures only, no transport or callbacks."""
import asyncio
import json
from threading import Event, RLock

from mcp_manager.registry import canonical_json


class FixtureExecutor:
    def __init__(self, results):
        self._results = {key: canonical_json(value) for key, value in results.items()}
        if any(len(value.encode('utf-8')) > 65536 for value in self._results.values()):
            raise ValueError('Fixture exceeds result limit')
        self._lock = RLock()
        self._seen = set()
        self.started, self.gate = Event(), Event()
        self.gate.set()
        self.fail = False

    @property
    def calls(self):
        with self._lock:
            return len(self._seen)

    async def execute(self, operation_id, server_id, tool_name):
        with self._lock:
            if operation_id in self._seen:
                raise RuntimeError('Duplicate fixture operation')
            self._seen.add(operation_id)
            result = self._results[(server_id, tool_name)]
            self.started.set()
        # A controllable fixture simulates a transport that outlives its caller.
        # No subprocess, socket, filesystem or actual MCPService execution exists.
        while not self.gate.is_set():
            await asyncio.sleep(.002)
        if self.fail:
            raise RuntimeError('Fixture failure')
        return json.loads(result)
