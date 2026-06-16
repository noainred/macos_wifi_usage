"""SQLite 영속 계층: 샘플(시계열)과 카운터 기준값(baseline) 저장 + 집계 질의."""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,           -- Unix epoch(초)
    iface     TEXT    NOT NULL,
    conn_type TEXT    NOT NULL,           -- 'wifi' | 'ethernet' | 'other'
    network   TEXT    NOT NULL,           -- 대표 라벨(SSID/named/gateway)
    ssid      TEXT,
    gateway   TEXT,
    rx_bytes  INTEGER NOT NULL,           -- 이 구간에 받은 바이트(delta)
    tx_bytes  INTEGER NOT NULL            -- 이 구간에 보낸 바이트(delta)
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_samples_network ON samples(network);

CREATE TABLE IF NOT EXISTS counter_state (
    iface TEXT PRIMARY KEY,
    rx    INTEGER NOT NULL,
    tx    INTEGER NOT NULL,
    ts    INTEGER NOT NULL
);
"""

# group_by 키 -> (SELECT 라벨 표현식, GROUP BY 표현식)
# 시간 버킷은 localtime 기준으로 묶는다(사용자가 보기 좋게).
_GROUP_EXPR: Dict[str, Tuple[str, str]] = {
    "network": ("network", "network"),
    "ssid": ("ssid", "ssid"),
    "type": ("conn_type", "conn_type"),
    "iface": ("iface", "iface"),
    "gateway": ("gateway", "gateway"),
    "hour": ("strftime('%Y-%m-%d %H:00', ts, 'unixepoch', 'localtime')",
             "strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime')"),
    "day": ("strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')",
            "strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')"),
    "week": ("strftime('%Y-W%W', ts, 'unixepoch', 'localtime')",
             "strftime('%Y-W%W', ts, 'unixepoch', 'localtime')"),
    "month": ("strftime('%Y-%m', ts, 'unixepoch', 'localtime')",
              "strftime('%Y-%m', ts, 'unixepoch', 'localtime')"),
}

GROUP_KEYS = list(_GROUP_EXPR.keys())


class Storage:
    def __init__(self, db_path: str):
        self.db_path = os.path.expanduser(db_path)
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # 웹 포탈(요청 스레드)과 모니터 스레드가 같은 파일에 동시에 접근하므로
        # 잠금 대기 시간을 주고, 파일 DB 는 WAL 로 동시 읽기/쓰기를 매끄럽게 한다.
        self.conn.execute("PRAGMA busy_timeout=5000")
        if self.db_path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- 카운터 기준값 --------------------------------------------------------
    def get_last_counter(self, iface: str) -> Optional[Tuple[int, int]]:
        cur = self.conn.execute(
            "SELECT rx, tx FROM counter_state WHERE iface=?", (iface,)
        )
        row = cur.fetchone()
        return (row["rx"], row["tx"]) if row else None

    def set_last_counter(self, iface: str, rx: int, tx: int, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO counter_state(iface, rx, tx, ts) VALUES(?,?,?,?) "
            "ON CONFLICT(iface) DO UPDATE SET rx=excluded.rx, tx=excluded.tx, ts=excluded.ts",
            (iface, rx, tx, ts),
        )
        self.conn.commit()

    # -- 샘플 기록 ------------------------------------------------------------
    def insert_sample(
        self,
        ts: int,
        iface: str,
        conn_type: str,
        network: str,
        ssid: Optional[str],
        gateway: Optional[str],
        rx_bytes: int,
        tx_bytes: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO samples(ts, iface, conn_type, network, ssid, gateway, rx_bytes, tx_bytes) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (ts, iface, conn_type, network, ssid, gateway, rx_bytes, tx_bytes),
        )
        self.conn.commit()

    # -- 집계/조회 ------------------------------------------------------------
    def _where(self, since: Optional[int], until: Optional[int]) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        wsql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return wsql, params

    def aggregate(
        self,
        since: Optional[int] = None,
        until: Optional[int] = None,
        group_by: str = "network",
    ) -> List[Dict[str, Any]]:
        """group_by 기준으로 rx/tx 합계와 메타를 집계해 리스트(dict)로 반환한다."""
        label_expr, group_expr = _GROUP_EXPR.get(group_by, _GROUP_EXPR["network"])
        wsql, params = self._where(since, until)
        sql = (
            f"SELECT {label_expr} AS label, "
            "MAX(conn_type) AS conn_type, "
            "SUM(rx_bytes) AS rx, SUM(tx_bytes) AS tx, "
            "MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS samples "
            f"FROM samples {wsql} "
            f"GROUP BY {group_expr} "
            "ORDER BY (SUM(rx_bytes)+SUM(tx_bytes)) DESC"
        )
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def totals(
        self, since: Optional[int] = None, until: Optional[int] = None
    ) -> Tuple[int, int]:
        wsql, params = self._where(since, until)
        sql = f"SELECT SUM(rx_bytes) AS rx, SUM(tx_bytes) AS tx FROM samples {wsql}"
        row = self.conn.execute(sql, params).fetchone()
        return (row["rx"] or 0, row["tx"] or 0)

    def iter_samples(
        self, since: Optional[int] = None, until: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """원본 샘플을 시간 순으로 반환한다(CSV/JSON export 용)."""
        wsql, params = self._where(since, until)
        sql = (
            "SELECT ts, iface, conn_type, network, ssid, gateway, rx_bytes, tx_bytes "
            f"FROM samples {wsql} ORDER BY ts ASC"
        )
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
