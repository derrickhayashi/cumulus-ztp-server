"""Durable, informational provisioning events; never controls provisioning."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


STAGES = (
    "upgrade_payload", "reachability", "os_install", "reboot",
    "configuration_payload", "prepare", "config_download", "config_apply",
    "config_save", "provisioning",
)


class ProgressEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    mgmt_mac: str | None = Field(default=None, pattern=r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
    serial: str | None = Field(default=None, min_length=1, max_length=128)
    stage: Literal["reachability", "os_install", "reboot", "prepare",
                   "config_download", "config_apply", "config_save", "provisioning"]
    result: Literal["started", "succeeded", "failed", "requested"]
    exit_code: int = Field(default=0, ge=0, le=255)

    @model_validator(mode="after")
    def validate_event(self):
        if not self.mgmt_mac and not self.serial:
            raise ValueError("Management MAC or serial is required")
        if (self.result == "failed") != (self.exit_code != 0):
            raise ValueError("Only failed events must have a nonzero exit code")
        if self.result == "requested" and self.stage != "reboot":
            raise ValueError("Only reboot can be requested")
        if self.stage == "provisioning" and self.result != "succeeded":
            raise ValueError("Provisioning completion must be succeeded")
        return self


class ProgressStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=0.2)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, hostname TEXT NOT NULL,
                    target_os TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, attempt_id TEXT NOT NULL,
                    stage TEXT NOT NULL, rank INTEGER NOT NULL,
                    result TEXT NOT NULL, exit_code INTEGER NOT NULL,
                    observed_os TEXT, received_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_host ON attempts(hostname);
                CREATE INDEX IF NOT EXISTS events_attempt ON events(attempt_id, id);
            """)
            with connection:
                yield connection
        finally:
            connection.close()

    def _append(self, connection, attempt_id, stage, result, exit_code=0, observed_os=None):
        connection.execute(
            "INSERT INTO events (attempt_id, stage, rank, result, exit_code, observed_os, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, stage, STAGES.index(stage), result, exit_code, observed_os,
             datetime.now(timezone.utc).isoformat()),
        )

    def payload(self, hostname, target_os, observed_os, upgrade):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM attempts WHERE hostname=? ORDER BY rowid DESC LIMIT 1",
                (hostname,),
            ).fetchone()
            # A configuration request after reboot continues the upgrade attempt.
            # Repeated payload requests otherwise start a fresh execution attempt.
            reusable = previous and not upgrade and previous["target_os"] == target_os
            if reusable:
                reusable = connection.execute(
                    "SELECT 1 FROM events WHERE attempt_id=? AND stage='upgrade_payload'",
                    (previous["id"],),
                ).fetchone() and not connection.execute(
                    "SELECT 1 FROM events WHERE attempt_id=? AND stage='configuration_payload'",
                    (previous["id"],),
                ).fetchone()
            attempt_id = previous["id"] if reusable else uuid4().hex
            if not reusable:
                connection.execute(
                    "INSERT INTO attempts VALUES (?, ?, ?, ?)",
                    (attempt_id, hostname, target_os, datetime.now(timezone.utc).isoformat()),
                )
            self._append(connection, attempt_id,
                         "upgrade_payload" if upgrade else "configuration_payload",
                         "requested", observed_os=observed_os)
            return attempt_id

    def report(self, hostname, event):
        with self.connection() as connection:
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE id=? AND hostname=?",
                (event.attempt_id, hostname),
            ).fetchone()
            if not attempt:
                raise KeyError("Unknown attempt for device")
            has_config = connection.execute(
                "SELECT 1 FROM events WHERE attempt_id=? AND stage='configuration_payload'",
                (event.attempt_id,),
            ).fetchone()
            if event.stage in ("os_install", "reboot", "reachability"):
                allowed = connection.execute(
                    "SELECT 1 FROM events WHERE attempt_id=? AND stage='upgrade_payload'",
                    (event.attempt_id,),
                ).fetchone()
            else:
                allowed = has_config
            if not allowed:
                raise ValueError("Stage does not match the issued payload")
            self._append(connection, event.attempt_id, event.stage, event.result, event.exit_code)

    def latest(self, hostname=None, limit=100, offset=0):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT a.*, e.stage, e.result, e.exit_code, e.received_at AS updated_at, "
                "(SELECT observed_os FROM events WHERE attempt_id=a.id AND observed_os IS NOT NULL "
                "ORDER BY id DESC LIMIT 1) AS observed_os FROM attempts a JOIN events e ON e.id="
                "(SELECT id FROM events WHERE attempt_id=a.id ORDER BY rank DESC, id DESC LIMIT 1) "
                "WHERE a.rowid=(SELECT MAX(rowid) FROM attempts WHERE hostname=a.hostname) "
                "AND (? IS NULL OR a.hostname=?) ORDER BY a.hostname LIMIT ? OFFSET ?",
                (hostname, hostname, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    def history(self, hostname, limit=100, offset=0):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT e.*, a.hostname, a.target_os FROM events e JOIN attempts a "
                "ON a.id=e.attempt_id WHERE a.hostname=? ORDER BY e.id DESC LIMIT ? OFFSET ?",
                (hostname, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]
