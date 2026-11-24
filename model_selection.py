"""Per-controller Chat/Voice selection. No model loading, MCP or global mutation."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock


class SelectionBusy(RuntimeError):
    pass


class ModelSelection:
    def __init__(self, path, default):
        self.path = Path(path)
        self._lock, self._busy = RLock(), 0
        self._model = default
        try:
            if self.path.stat().st_size > 4096:
                raise ValueError('Model setting exceeds limit')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and self.valid(data.get('model')):
                self._model = data['model']
        except (OSError, ValueError, TypeError):
            pass  # Bad local settings never prevent ordinary Console startup.

    @staticmethod
    def valid(name):
        return (isinstance(name, str) and bool(name.strip()) and name == name.strip()
                and len(name) <= 512 and not any(ord(c) < 32 for c in name))

    def current(self):
        with self._lock:
            return self._model

    def status(self):
        with self._lock:
            return {'selected': self._model, 'busy': self._busy > 0}

    def select(self, name, installed):
        if not self.valid(name) or name not in installed:
            raise ValueError('Only an installed model may be selected')
        with self._lock:
            if self._busy:
                raise SelectionBusy('A Chat request is active')
            scratch = None
            try:
                with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                        prefix='.model-settings-', suffix='.tmp', delete=False) as output:
                    scratch = output.name
                    json.dump({'model': name}, output, ensure_ascii=False)
                os.replace(scratch, self.path)
            finally:
                if scratch and os.path.exists(scratch):
                    os.unlink(scratch)
            self._model = name
            return self.status()

    @contextmanager
    def use(self):
        with self._lock:
            model = self._model
            self._busy += 1
        try:
            yield model
        finally:
            with self._lock:
                self._busy -= 1
