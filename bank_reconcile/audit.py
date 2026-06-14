"""操作审计模块 - SQLite 持久化审计日志."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any


AUDIT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    command     TEXT    NOT NULL,
    batch_id    TEXT    NOT NULL,
    affected    INTEGER NOT NULL DEFAULT 0,
    summary     TEXT    NOT NULL DEFAULT ''
);
"""

AUDIT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_command   ON audit_log(command);
CREATE INDEX IF NOT EXISTS idx_audit_batch_id  ON audit_log(batch_id);
"""


class AuditStorage:
    def __init__(self, storage_dir: str) -> None:
        os.makedirs(storage_dir, exist_ok=True)
        self.db_path = os.path.join(storage_dir, "audit.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(AUDIT_TABLE_DDL + AUDIT_INDEX_DDL)
            conn.commit()
        finally:
            conn.close()

    def log(
        self,
        command: str,
        batch_id: str,
        affected: int,
        summary: str,
    ) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO audit_log (timestamp, command, batch_id, affected, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), command, batch_id, affected, summary),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def query(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        op_type: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []

        if from_date:
            clauses.append("timestamp >= ?")
            params.append(from_date)
        if to_date:
            clauses.append("timestamp <= ?")
            params.append(to_date)
        if op_type:
            clauses.append("command = ?")
            params.append(op_type)
        if batch_id:
            clauses.append("batch_id = ?")
            params.append(batch_id)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM audit_log{where} ORDER BY timestamp DESC"

        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "command": r["command"],
                    "batch_id": r["batch_id"],
                    "affected": r["affected"],
                    "summary": r["summary"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def export_csv(self, output_path: str, records: List[Dict[str, Any]]) -> int:
        columns = ["id", "timestamp", "command", "batch_id", "affected", "summary"]
        out_dir = os.path.dirname(os.path.abspath(output_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for rec in records:
                writer.writerow(rec)
        return len(records)

    def export_json(self, output_path: str, records: List[Dict[str, Any]]) -> int:
        out_dir = os.path.dirname(os.path.abspath(output_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        return len(records)

    def cleanup(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM audit_log WHERE timestamp < ?", (cutoff,)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
