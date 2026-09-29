"""Check history in a single SQLite file (stdlib sqlite3)."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from .checks import Result

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_results_check_ts ON results (check_id, ts);
"""


@dataclass(frozen=True)
class DayStat:
    total: int
    down: int
    degraded: int

    @property
    def uptime_pct(self) -> float:
        """Share of probes that were not 'down' (degraded still counts as reachable)."""
        return 100.0 * (self.total - self.down) / self.total if self.total else 0.0


class History:
    def __init__(self, path: str):
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "History":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def append(self, results: list[Result]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT INTO results (check_id, ts, status, latency_ms, detail) VALUES (?,?,?,?,?)",
                [(r.check_id, r.ts, r.status, r.latency_ms, r.detail) for r in results],
            )

    def read(self, check_id: str, limit: int | None = None) -> list[Result]:
        """Results of one check, newest first."""
        sql = "SELECT check_id, ts, status, latency_ms, detail FROM results WHERE check_id=? ORDER BY id DESC"
        params: list = [check_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [Result(*row) for row in self._db.execute(sql, params)]

    def latest(self) -> dict[str, Result]:
        """The most recent result of every check."""
        rows = self._db.execute(
            "SELECT check_id, ts, status, latency_ms, detail FROM results "
            "WHERE id IN (SELECT MAX(id) FROM results GROUP BY check_id)"
        )
        return {row[0]: Result(*row) for row in rows}

    def daily(self, since_day: str) -> dict[str, dict[str, DayStat]]:
        """Per check and per UTC day ('YYYY-MM-DD') probe counts from since_day on."""
        rows = self._db.execute(
            "SELECT check_id, substr(ts,1,10) AS d, COUNT(*), "
            "SUM(status='down'), SUM(status='degraded') "
            "FROM results WHERE ts >= ? GROUP BY check_id, d",
            (since_day,),
        )
        out: dict[str, dict[str, DayStat]] = {}
        for cid, day, total, down, degraded in rows:
            out.setdefault(cid, {})[day] = DayStat(total, down or 0, degraded or 0)
        return out

    def prune(self, before_ts: str) -> int:
        with self._db:
            cur = self._db.execute("DELETE FROM results WHERE ts < ?", (before_ts,))
        return cur.rowcount
