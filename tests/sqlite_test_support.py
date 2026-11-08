"""Keep all API tests, including the entry point, away from personal data."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def isolated_database(test):
    directory = TemporaryDirectory(prefix="local-ai-test-")
    test.addCleanup(directory.cleanup)
    path = Path(directory.name) / "sessions.sqlite3"
    environment = patch.dict(os.environ, {
        "LOCAL_AI_DB_PATH": str(path),
        "LOCAL_AI_MCP_DB_PATH": str(Path(directory.name) / "mcp.sqlite3"),
    })
    environment.start()
    test.addCleanup(environment.stop)
    return path
