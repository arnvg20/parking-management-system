from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS parking_spaces (
    space_id             TEXT PRIMARY KEY,
    occupied             INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'EMPTY',
    vehicle_data         TEXT,
    decision_confidence  REAL,
    decision_reason      TEXT,
    source_detection_time TEXT,
    last_resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    data      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS commands (
    id           INTEGER PRIMARY KEY,
    device_id    TEXT NOT NULL,
    command      TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    requested_by TEXT NOT NULL DEFAULT 'operator',
    status       TEXT NOT NULL DEFAULT 'queued',
    created_at   TEXT NOT NULL,
    dispatched_at TEXT,
    completed_at TEXT,
    result       TEXT
);

CREATE TABLE IF NOT EXISTS uploads (
    id                TEXT PRIMARY KEY,
    device_id         TEXT NOT NULL,
    filename          TEXT NOT NULL,
    original_filename TEXT,
    path              TEXT NOT NULL,
    content_type      TEXT NOT NULL DEFAULT 'image/jpeg',
    metadata          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    url               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id         TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    filename   TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'jetson.telemetry',
    summary    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(zip(row.keys(), tuple(row)))


class ParkingDB:
    """SQLite-backed persistence layer.

    A single connection is shared across threads; callers must hold
    BackendState.lock for all write operations (the same lock that guards
    the in-memory state), so SQLite never sees concurrent writers.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Parking spaces                                                       #
    # ------------------------------------------------------------------ #

    def upsert_space(self, space_id: str, space: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO parking_spaces
               (space_id, occupied, status, vehicle_data,
                decision_confidence, decision_reason,
                source_detection_time, last_resolved_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                space_id,
                int(bool(space.get("occupied"))),
                space.get("status") or "EMPTY",
                json.dumps(space.get("vehicle_data")),
                space.get("decision_confidence"),
                space.get("decision_reason"),
                space.get("source_detection_time"),
                space.get("last_resolved_at"),
            ),
        )
        self._conn.commit()

    def load_spaces(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM parking_spaces").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = _row_to_dict(row)
            result[d["space_id"]] = {
                "occupied": bool(d["occupied"]),
                "status": d["status"],
                "vehicle_data": json.loads(d["vehicle_data"]) if d["vehicle_data"] else None,
                "decision_confidence": d["decision_confidence"],
                "decision_reason": d["decision_reason"],
                "source_detection_time": d["source_detection_time"],
                "last_resolved_at": d["last_resolved_at"],
            }
        return result

    # ------------------------------------------------------------------ #
    # Devices                                                              #
    # ------------------------------------------------------------------ #

    def upsert_device(self, device_id: str, device: dict[str, Any]) -> None:
        clean = copy.deepcopy({k: v for k, v in device.items() if k != "latest_frame_bytes"})
        for src in clean.get("latest_stream_by_source", {}).values():
            if isinstance(src, dict):
                src.pop("_frame_bytes", None)
        self._conn.execute(
            "INSERT OR REPLACE INTO devices (device_id, data) VALUES (?,?)",
            (device_id, json.dumps(clean, default=str)),
        )
        self._conn.commit()

    def load_devices(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM devices").fetchall()
        return {row["device_id"]: json.loads(row["data"]) for row in rows}

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    def insert_command(self, command: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO commands
               (id, device_id, command, payload, requested_by, status,
                created_at, dispatched_at, completed_at, result)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                command["id"],
                command["device_id"],
                command["command"],
                json.dumps(command.get("payload") or {}),
                command.get("requested_by") or "operator",
                command.get("status") or "queued",
                command["created_at"],
                command.get("dispatched_at"),
                command.get("completed_at"),
                json.dumps(command["result"]) if command.get("result") else None,
            ),
        )
        self._conn.commit()

    def update_command(self, command: dict[str, Any]) -> None:
        self._conn.execute(
            """UPDATE commands
               SET status=?, dispatched_at=?, completed_at=?, result=?
               WHERE id=?""",
            (
                command["status"],
                command.get("dispatched_at"),
                command.get("completed_at"),
                json.dumps(command["result"]) if command.get("result") else None,
                command["id"],
            ),
        )
        self._conn.commit()

    def load_commands(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM commands ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            d = _row_to_dict(row)
            result.append(
                {
                    "id": d["id"],
                    "device_id": d["device_id"],
                    "command": d["command"],
                    "payload": json.loads(d["payload"]) if d["payload"] else {},
                    "requested_by": d["requested_by"],
                    "status": d["status"],
                    "created_at": d["created_at"],
                    "dispatched_at": d["dispatched_at"],
                    "completed_at": d["completed_at"],
                    "result": json.loads(d["result"]) if d["result"] else None,
                }
            )
        return result

    # ------------------------------------------------------------------ #
    # Uploads                                                              #
    # ------------------------------------------------------------------ #

    def insert_upload(self, record: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO uploads
               (id, device_id, filename, original_filename,
                path, content_type, metadata, created_at, url)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                record["id"],
                record["device_id"],
                record["filename"],
                record.get("original_filename"),
                record["path"],
                record.get("content_type") or "image/jpeg",
                json.dumps(record.get("metadata") or {}),
                record["created_at"],
                record["url"],
            ),
        )
        self._conn.commit()

    def load_uploads(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM uploads").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = _row_to_dict(row)
            result[d["id"]] = {
                "id": d["id"],
                "device_id": d["device_id"],
                "filename": d["filename"],
                "original_filename": d["original_filename"],
                "path": d["path"],
                "content_type": d["content_type"],
                "metadata": json.loads(d["metadata"]) if d["metadata"] else {},
                "created_at": d["created_at"],
                "url": d["url"],
            }
        return result

    # ------------------------------------------------------------------ #
    # Observations                                                         #
    # ------------------------------------------------------------------ #

    def insert_observation(self, record: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO observations
               (id, device_id, filename, path, created_at, source, summary)
               VALUES (?,?,?,?,?,?,?)""",
            (
                record["id"],
                record["device_id"],
                record["filename"],
                record["path"],
                record["created_at"],
                record.get("source") or "jetson.telemetry",
                json.dumps(record.get("summary") or {}),
            ),
        )
        self._conn.commit()

    def delete_observation(self, observation_id: str) -> None:
        self._conn.execute("DELETE FROM observations WHERE id=?", (observation_id,))
        self._conn.commit()

    def load_observations(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM observations ORDER BY created_at ASC"
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = _row_to_dict(row)
            result[d["id"]] = {
                "id": d["id"],
                "device_id": d["device_id"],
                "filename": d["filename"],
                "path": d["path"],
                "created_at": d["created_at"],
                "source": d["source"],
                "summary": json.loads(d["summary"]) if d["summary"] else {},
            }
        return result

    # ------------------------------------------------------------------ #
    # Meta (key-value)                                                     #
    # ------------------------------------------------------------------ #

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Migration helper                                                     #
    # ------------------------------------------------------------------ #

    def is_empty(self) -> bool:
        """Return True when the DB has never been populated."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM devices"
        ).fetchone()
        return row["n"] == 0

    def purge_tables(self, tables: list[str]) -> None:
        for table in tables:
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()

    def bulk_load_from_snapshot(self, snapshot: dict[str, Any]) -> None:
        """One-shot migration from a state.json snapshot dict."""
        for space_id, space in (snapshot.get("parking_spaces") or {}).items():
            self.upsert_space(space_id, space)
        for device_id, device in (snapshot.get("devices") or {}).items():
            self.upsert_device(device_id, device)
        for command in snapshot.get("commands") or []:
            self.insert_command(command)
        for upload in (snapshot.get("uploads") or {}).values():
            self.insert_upload(upload)
        for obs in (snapshot.get("observations") or {}).values():
            self.insert_observation(obs)
        seq = snapshot.get("command_sequence")
        if seq is not None:
            self.set_meta("command_sequence", str(seq))
