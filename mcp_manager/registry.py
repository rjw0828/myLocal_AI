"""Independent MCP SQLite registry. Never opens or migrates the Session DB."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .errors import MCPServiceError

DEFAULT_MCP_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "mcp_registry.sqlite3"
APPLICATION_ID = 1296257074


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def config_data(config):
    return {"executable": config.executable, "args": list(config.args), "cwd": config.cwd}


class MCPRegistry:
    def __init__(self, path, *, protected_path, timeout=3.0):
        self.path = Path(path).resolve()
        protected = Path(protected_path).resolve()
        protected_paths = [Path(str(protected) + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
        protected_names = {os.path.normcase(str(item)) for item in protected_paths}
        same_file = self.path.exists() and any(item.exists() and self.path.samefile(item)
                                               for item in protected_paths)
        if os.path.normcase(str(self.path)) in protected_names or same_file:
            raise MCPServiceError("mcp_registry_path_conflict", "MCP storage must be separate from Session storage.", 503)
        self.timeout, self.initialized = timeout, False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise MCPServiceError("mcp_registry_unavailable", "MCP storage is unavailable.", 503) from None
        with self.connection(transaction=False) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise MCPServiceError("mcp_registry_schema", "Unsupported MCP storage schema.", 503)
                schema = Path(__file__).with_name("registry.sql").read_text(encoding="utf-8")
                db.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
            elif version != 1 or db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise MCPServiceError("mcp_registry_schema", "Unsupported MCP storage schema.", 503)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MCPServiceError("mcp_registry_corrupt", "MCP storage integrity check failed.", 503)
            db.execute("SELECT server_id,profile_id,name,config_json,config_hash,created_at FROM mcp_servers LIMIT 0")
            db.execute("SELECT server_id,tool_name,schema_hash,allowed,updated_at FROM mcp_tool_permissions LIMIT 0")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise MCPServiceError("mcp_registry_corrupt", "MCP storage integrity check failed.", 503)
            db.execute("PRAGMA journal_mode=WAL")
        self.initialized = True

    @contextmanager
    def connection(self, *, write=False, transaction=True):
        db = None
        try:
            mode = "rw" if self.initialized else "rwc"
            db = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True,
                                 timeout=self.timeout, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            if transaction:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            if db.in_transaction:
                db.commit()
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xff
            name = getattr(error, "sqlite_errorname", "")
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise MCPServiceError("mcp_registry_busy", "MCP storage is busy.", 503) from None
            if name in {"SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"}:
                raise MCPServiceError("mcp_registry_conflict", "This execution profile is already registered.", 409) from None
            raise MCPServiceError("mcp_registry_unavailable", "MCP storage operation failed; no success was confirmed.", 503) from None
        finally:
            if db is not None:
                try:
                    if db.in_transaction:
                        db.rollback()
                except sqlite3.Error:
                    pass  # Preserve the sanitized original error on I/O failure.
                finally:
                    db.close()

    @staticmethod
    def _decode(row):
        if row is None:
            raise MCPServiceError("mcp_server_not_found", "MCP server was not found.", 404)
        result = dict(row)
        try:
            result["config"] = json.loads(result.pop("config_json"))
            if not isinstance(result["config"], dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise MCPServiceError("mcp_registry_corrupt", "MCP registration data is invalid.", 503) from None
        return result

    def list_servers(self):
        with self.connection() as db:
            return [self._decode(row) for row in db.execute("SELECT * FROM mcp_servers ORDER BY created_at,server_id")]

    def get(self, server_id):
        with self.connection() as db:
            return self._decode(db.execute("SELECT * FROM mcp_servers WHERE server_id=?", (server_id,)).fetchone())

    def create(self, profile_id, name, config):
        server_id = "mcp-" + uuid4().hex
        with self.connection(write=True) as db:
            db.execute("INSERT INTO mcp_servers(server_id,profile_id,name,config_json,config_hash) VALUES (?,?,?,?,?)",
                       (server_id, profile_id, name, canonical_json(config), fingerprint(config)))
            return self._decode(db.execute("SELECT * FROM mcp_servers WHERE server_id=?", (server_id,)).fetchone())

    def delete(self, server_id):
        with self.connection(write=True) as db:
            if db.execute("DELETE FROM mcp_servers WHERE server_id=?", (server_id,)).rowcount != 1:
                raise MCPServiceError("mcp_server_not_found", "MCP server was not found.", 404)

    def permission(self, server_id, tool_name):
        with self.connection() as db:
            row = db.execute("SELECT * FROM mcp_tool_permissions WHERE server_id=? AND tool_name=?",
                             (server_id, tool_name)).fetchone()
            return dict(row) if row else None

    def list_permissions(self, server_id):
        with self.connection() as db:
            self._decode(db.execute("SELECT * FROM mcp_servers WHERE server_id=?", (server_id,)).fetchone())
            return [dict(row) for row in db.execute(
                "SELECT tool_name,schema_hash,allowed,updated_at FROM mcp_tool_permissions "
                "WHERE server_id=? ORDER BY tool_name", (server_id,))]

    def set_permission(self, server_id, tool_name, schema_hash, allowed):
        with self.connection(write=True) as db:
            self._decode(db.execute("SELECT * FROM mcp_servers WHERE server_id=?", (server_id,)).fetchone())
            db.execute("INSERT INTO mcp_tool_permissions(server_id,tool_name,schema_hash,allowed) VALUES (?,?,?,?) "
                       "ON CONFLICT(server_id,tool_name) DO UPDATE SET schema_hash=excluded.schema_hash,"
                       "allowed=excluded.allowed,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                       (server_id, tool_name, schema_hash, int(allowed)))
