"""Short SQLite transactions. Never hold a DB connection during inference.

The Flask application owns per-session operation locks. Revision checks also
prevent a stale snapshot from overwriting a Reset/Delete committed elsewhere.
Multiple controller processes sharing a database are not supported.
"""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "local_ai_lab.sqlite3"
NEW_TITLE = "새 대화"


class StorageError(RuntimeError):
    code = "storage_error"
    status = 500


class StorageBusy(StorageError):
    code = "storage_busy"
    status = 503


class StorageUnavailable(StorageError):
    code = "storage_unavailable"
    status = 503


class StorageConflict(StorageError):
    code = "session_conflict"
    status = 409


class DeletedSession(StorageError):
    code = "session_deleted"
    status = 410


class SessionCapacity(StorageError):
    code = "session_limit_reached"
    status = 429


class SQLiteStore:
    def __init__(self, path=DEFAULT_DB_PATH, *, limit=60, timeout=3.0):
        self.path = Path(path).resolve()
        self.limit = limit
        self.timeout = timeout
        self.initialized = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise StorageUnavailable("Session storage is unavailable.") from None
        with self.connection(transaction=False) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                # An unrelated existing DB must not be silently repurposed.
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise StorageError("Unsupported session storage schema.")
                schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                db.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
            elif version != 1:
                raise StorageError("Unsupported session storage schema version.")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageError("Session storage integrity check failed.")
            db.execute("SELECT number,session_id,title,titled,explicit,system_prompt,"
                       "last_response_language,revision,created_at,updated_at FROM sessions LIMIT 0")
            db.execute("SELECT session_id,ordinal,role,content,language,created_at FROM messages LIMIT 0")
            db.execute("SELECT session_id,deleted_at FROM deleted_sessions LIMIT 0")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise StorageError("Session storage integrity check failed.")
            db.execute("PRAGMA journal_mode=WAL")
        self.initialized = True

    @contextmanager
    def connection(self, *, write=False, transaction=True):
        db = None
        try:
            # Runtime requests must not create an empty replacement if the
            # live database disappears. Only initial construction may create.
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
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise StorageBusy("Session storage is busy. No success was confirmed.") from None
            if code in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY,
                        sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_PERM):
                raise StorageUnavailable("Session storage is unavailable. No success was confirmed.") from None
            raise StorageError("Session storage operation failed. No success was confirmed.") from None
        finally:
            if db is not None:
                try:
                    if db.in_transaction:
                        db.rollback()
                except sqlite3.Error:
                    # Closing still releases the handle after an I/O failure;
                    # preserve the already sanitized operation error.
                    pass
                finally:
                    db.close()

    @staticmethod
    def check_deleted(db, session_id):
        if db.execute("SELECT 1 FROM deleted_sessions WHERE session_id=?", (session_id,)).fetchone():
            raise DeletedSession("This session was deleted. Create a new session.")

    def list_sessions(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sessions ORDER BY number")]

    def load(self, session_id):
        with self.connection() as db:
            self.check_deleted(db, session_id)
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["messages"] = [dict(item) for item in db.execute(
                "SELECT role,content,language FROM messages WHERE session_id=? ORDER BY ordinal", (session_id,))]
            return result

    def create(self, session_id, *, explicit=False):
        with self.connection(write=True) as db:
            self.check_deleted(db, session_id)
            if db.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone():
                raise StorageConflict("Session already exists.")
            if db.execute("SELECT count(*) FROM sessions").fetchone()[0] >= self.limit:
                raise SessionCapacity(f"At most {self.limit} stored sessions are allowed. Delete a session to free space.")
            db.execute("INSERT INTO sessions(session_id,title,explicit) VALUES (?,?,?)",
                       (session_id, NEW_TITLE, int(explicit)))
            return dict(db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone())

    def commit_turn(self, session_id, revision, *, user, answer, title, system_prompt, language):
        with self.connection(write=True) as db:
            self.check_deleted(db, session_id)
            updated = db.execute(
                "UPDATE sessions SET title=?,titled=1,system_prompt=?,last_response_language=?,"
                "revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE session_id=? AND revision=?",
                (title, system_prompt, language, session_id, revision))
            if updated.rowcount != 1:
                raise StorageConflict("Session changed before the answer could be saved.")
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal)+1,0) FROM messages WHERE session_id=?",
                                 (session_id,)).fetchone()[0]
            db.executemany("INSERT INTO messages(session_id,ordinal,role,content,language) VALUES (?,?,?,?,?)", [
                (session_id, ordinal, "user", user, None),
                (session_id, ordinal + 1, "assistant", answer, language),
            ])

    def reset(self, session_id, revision):
        with self.connection(write=True) as db:
            self.check_deleted(db, session_id)
            updated = db.execute("UPDATE sessions SET title=?,titled=0,system_prompt=NULL,"
                                 "last_response_language=NULL,revision=revision+1,"
                                 "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                                 "WHERE session_id=? AND revision=?", (NEW_TITLE, session_id, revision))
            if updated.rowcount != 1:
                raise StorageConflict("Session changed before Reset.")
            db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))

    def delete(self, session_id, revision):
        with self.connection(write=True) as db:
            self.check_deleted(db, session_id)
            removed = db.execute("DELETE FROM sessions WHERE session_id=? AND revision=?", (session_id, revision))
            if removed.rowcount != 1:
                raise StorageConflict("Session changed before Delete.")
            db.execute("INSERT INTO deleted_sessions(session_id) VALUES (?)", (session_id,))

    def discard_empty_implicit(self, session_id):
        # Preserve the pre-existing legacy /chat cleanup policy, without a
        # tombstone: a failed first Chat is not a user-requested Delete.
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE session_id=? AND explicit=0 AND titled=0 "
                       "AND NOT EXISTS (SELECT 1 FROM messages WHERE session_id=?)", (session_id, session_id))

    def delete_all(self) -> list[str]:
        """Delete live sessions and reset display numbering atomically.

        Tombstones survive numbering resets: UUID identity is independent of
        the display number. Any SQL/commit failure rolls back all three steps.
        """
        with self.connection(write=True) as db:
            ids = [row[0] for row in db.execute("SELECT session_id FROM sessions ORDER BY number")]
            db.execute("INSERT OR IGNORE INTO deleted_sessions(session_id) SELECT session_id FROM sessions")
            db.execute("DELETE FROM sessions")  # FK cascade removes messages.
            db.execute("DELETE FROM sqlite_sequence WHERE name='sessions'")
            return ids

    def backup(self, destination):
        """Consistent online backup; never copy a live .sqlite3 file alone."""
        destination = Path(destination).resolve()
        if destination == self.path or destination.exists():
            raise StorageError("Choose a new backup file outside the live database.")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Reserve exclusively, so concurrent backup commands cannot
            # overwrite the same destination between exists() and connect().
            with destination.open("xb"):
                pass
        except OSError:
            raise StorageUnavailable("The new backup file could not be created.") from None
        with self.connection() as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
