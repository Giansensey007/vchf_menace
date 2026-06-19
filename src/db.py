from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from enum import Enum

from src.config_loader import db_path


def _json_default(obj):
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(type(obj))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cycles (
                id TEXT PRIMARY KEY,
                direction TEXT,
                size_vchf REAL,
                state TEXT,
                net_profit_usd REAL,
                payload TEXT,
                error TEXT,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS cycle_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT,
                step TEXT,
                payload TEXT,
                created_at REAL
            );
            """
        )


def save_cycle(record) -> None:
    payload = {
        "simulation": record.simulation,
        "tx_hashes": record.tx_hashes,
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO cycles
            (id, direction, size_vchf, state, net_profit_usd, payload, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.direction,
                record.size_vchf,
                record.state.value if hasattr(record.state, "value") else str(record.state),
                getattr(record.simulation, "net_profit_usd", 0) if record.simulation else 0,
                json.dumps(payload, default=_json_default),
                record.error,
                time.time(),
            ),
        )


def log_cycle_step(cycle_id: str, step: str, payload: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO cycle_steps (cycle_id, step, payload, created_at) VALUES (?, ?, ?, ?)",
            (cycle_id, step, json.dumps(payload), time.time()),
        )


def recent_cycles(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cycles ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
