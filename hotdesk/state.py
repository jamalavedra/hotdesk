import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager


def validate_agent(owner, task):
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 100:
        raise ValueError("owner must contain 1-100 characters")
    if not isinstance(task, str) or len(task) > 200:
        raise ValueError("task must contain at most 200 characters")


class ControlConflict(ValueError):
    pass


class State:
    def __init__(self, config):
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = config.state_dir / "state.sqlite3"
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.path.chmod(0o600)
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 1:
            self.db.close()
            raise ValueError("This state database requires a newer Hot Desk version.")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS desks (
                name TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'idle',
                agent TEXT, owner TEXT, task TEXT, expires REAL,
                generation INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner TEXT NOT NULL,
                task TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, agent TEXT,
                tool TEXT NOT NULL, started REAL NOT NULL, finished REAL,
                outcome TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, workspace TEXT, event TEXT NOT NULL, at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_running_operation
                ON operations(workspace) WHERE outcome='running';
        """)
        if "last_heartbeat" not in {
            row["name"] for row in self.db.execute("PRAGMA table_info(desks)")
        }:
            self.db.execute("ALTER TABLE desks ADD COLUMN last_heartbeat REAL")
        self.db.execute("PRAGMA user_version=1")
        for name in config.desktops:
            self.db.execute("INSERT OR IGNORE INTO desks(name) VALUES (?)", (name,))
        with self.transaction():
            self.db.execute(
                "UPDATE desks SET state='recovery', agent=NULL WHERE name IN "
                "(SELECT workspace FROM operations WHERE outcome='running')"
            )
            self.db.execute(
                "UPDATE operations SET outcome='unknown', finished=? WHERE outcome='running'",
                (time.time(),),
            )
            self.db.execute(
                "UPDATE desks SET state='idle', agent=NULL, owner=NULL, task=NULL, "
                "expires=NULL, generation=generation+1 WHERE state='reserved'"
            )
            self.db.execute("UPDATE desks SET state='recovery' WHERE state='takeover'")
            self.db.execute("DELETE FROM agents")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, name, event):
        self.db.execute(
            "INSERT INTO events(workspace,event,at) VALUES (?,?,?)", (name, event, time.time())
        )
        self.db.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)"
        )

    def row(self, name):
        row = self.db.execute("SELECT * FROM desks WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValueError("Unknown workspace")
        if row["state"] == "reserved" and row["expires"] <= time.time():
            active = self.db.execute(
                "SELECT 1 FROM operations WHERE workspace=? AND outcome='running'", (name,)
            ).fetchone()
            self.db.execute(
                "UPDATE desks SET state=?,agent=NULL,owner=NULL,task=NULL,"
                "expires=NULL,generation=generation+1 WHERE name=?",
                ("recovery" if active else "idle", name),
            )
            self.event(name, "reservation expired")
            return self.row(name)
        return dict(row)

    def register(self, name, owner, task):
        self.row(name)
        validate_agent(owner, task)
        token = secrets.token_urlsafe(32)
        identity = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute("INSERT INTO agents VALUES (?,?,?,?)", (identity, name, owner, task))
        return token

    def authenticate(self, token, name):
        identity = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.execute(
            "SELECT * FROM agents WHERE id=? AND workspace=?", (identity, name)
        ).fetchone()
        if row is None:
            raise ControlConflict(
                "Invalid or expired workspace credential. Use a new agent session name."
            )
        return dict(row)

    def acquire(self, agent, seconds=300):
        if type(seconds) is not int or not 10 <= seconds <= 3600:
            raise ValueError("Reservation duration must be 10-3600 seconds")
        name = agent["workspace"]
        with self.transaction():
            row = self.row(name)
            if self.unsettled(name):
                raise ControlConflict("A previous operation has not settled; recovery is required.")
            if row["state"] != "idle" and not (
                row["state"] == "reserved" and row["agent"] == agent["id"]
            ):
                raise ControlConflict(
                    f"Workspace is {row['state']}; owner: {row['owner'] or 'none'}"
                )
            self.db.execute(
                "UPDATE desks SET state='reserved',agent=?,owner=?,task=?,expires=?,"
                "generation=generation+1,last_heartbeat=? WHERE name=?",
                (
                    agent["id"],
                    agent["owner"],
                    agent["task"],
                    time.time() + seconds,
                    time.time(),
                    name,
                ),
            )
            self.event(name, "reservation acquired")
        return self.public(name)

    def check(self, agent):
        row = self.row(agent["workspace"])
        if row["state"] != "reserved" or row["agent"] != agent["id"]:
            raise ControlConflict(
                f"Workspace is {row['state']}. Acquire a reservation before acting."
            )
        return row

    def renew(self, agent, seconds=300):
        self.check(agent)
        if type(seconds) is not int or not 10 <= seconds <= 3600:
            raise ValueError("Reservation duration must be 10-3600 seconds")
        self.db.execute(
            "UPDATE desks SET expires=?,last_heartbeat=? WHERE name=?",
            (time.time() + seconds, time.time(), agent["workspace"]),
        )
        return self.public(agent["workspace"])

    def release(self, agent):
        if self.row(agent["workspace"])["state"] == "idle" and not self.unsettled(
            agent["workspace"]
        ):
            return self.public(agent["workspace"])
        self.check(agent)
        if self.unsettled(agent["workspace"]):
            raise ControlConflict("An operation is still running")
        self.set_mode(agent["workspace"], "idle")
        return self.public(agent["workspace"])

    def set_mode(self, name, mode):
        self.row(name)
        self.db.execute(
            "UPDATE desks SET state=?,agent=NULL,owner=NULL,task=NULL,expires=NULL,"
            "generation=generation+1 WHERE name=?",
            (mode, name),
        )
        self.event(name, mode)

    def begin(self, agent, tool):
        with self.transaction():
            self.check(agent)
            if self.unsettled(agent["workspace"]):
                raise ControlConflict("A previous operation has not settled; recovery is required.")
            operation = secrets.token_hex(16)
            self.db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,NULL,'running')",
                (operation, agent["workspace"], agent["id"], tool, time.time()),
            )
        return operation

    def unsettled(self, name):
        return (
            self.db.execute(
                "SELECT 1 FROM operations WHERE workspace=? AND outcome='running'", (name,)
            ).fetchone()
            is not None
        )

    def finish(self, operation, outcome):
        with self.transaction():
            row = self.db.execute(
                "SELECT workspace FROM operations WHERE id=?", (operation,)
            ).fetchone()
            self.db.execute(
                "UPDATE operations SET outcome=?,finished=? WHERE id=?",
                (outcome, time.time(), operation),
            )
            if outcome == "unknown":
                self.set_mode(row["workspace"], "recovery")
            self.db.execute(
                "DELETE FROM operations WHERE outcome!='running' AND id NOT IN "
                "(SELECT id FROM operations ORDER BY started DESC LIMIT 500)"
            )

    def public(self, name):
        row = self.row(name)
        return {
            key: row[key]
            for key in ("name", "state", "owner", "task", "expires", "generation", "last_heartbeat")
        }

    def history(self):
        return {
            "events": [
                dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")
            ],
            "operations": [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,workspace,tool,started,finished,outcome FROM operations ORDER BY started DESC LIMIT 100"
                )
            ],
        }
