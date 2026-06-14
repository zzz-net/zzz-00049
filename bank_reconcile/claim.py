"""差异认领工作台模块 - 多人协作认领、持久化、权限控制."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional, Dict, Any, Tuple


class ClaimStatus(str, Enum):
    """认领状态枚举."""
    PENDING = "pending"
    CLAIMED = "claimed"
    RELEASED = "released"


CLAIM_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS claim_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id        TEXT    NOT NULL UNIQUE,
    batch_id        TEXT    NOT NULL,
    discrepancy_id  TEXT    NOT NULL,
    claimant        TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending',
    claimed_at      TEXT    NOT NULL DEFAULT '',
    expires_at      TEXT    NOT NULL DEFAULT '',
    released_at     TEXT    NOT NULL DEFAULT '',
    release_reason  TEXT    NOT NULL DEFAULT '',
    release_operator TEXT   NOT NULL DEFAULT '',
    note            TEXT    NOT NULL DEFAULT '',
    is_force_release INTEGER NOT NULL DEFAULT 0
);
"""

CLAIM_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_claim_batch_id      ON claim_records(batch_id);
CREATE INDEX IF NOT EXISTS idx_claim_discrepancy_id ON claim_records(discrepancy_id);
CREATE INDEX IF NOT EXISTS idx_claim_claimant      ON claim_records(claimant);
CREATE INDEX IF NOT EXISTS idx_claim_status        ON claim_records(status);
CREATE INDEX IF NOT EXISTS idx_claim_batch_disp    ON claim_records(batch_id, discrepancy_id);
"""


class ClaimError(Exception):
    """认领模块通用异常."""
    pass


class BatchNotFoundError(ClaimError):
    """批次不存在."""
    pass


class DiscrepancyNotFoundError(ClaimError):
    """差异不存在."""
    pass


class AlreadyClaimedError(ClaimError):
    """重复认领异常."""
    pass


class NotClaimantError(ClaimError):
    """非认领人操作异常."""
    pass


class OperatorMissingError(ClaimError):
    """操作者缺失异常."""
    pass


class ExportPathConflictError(ClaimError):
    """导出路径冲突异常."""
    pass


class ClaimPermissionDeniedError(ClaimError):
    """认领权限拒绝（用于 mark/rollback 联动）."""
    pass


@dataclass
class ClaimRecord:
    """认领记录."""
    claim_id: str
    batch_id: str
    discrepancy_id: str
    claimant: str = ""
    status: ClaimStatus = ClaimStatus.PENDING
    claimed_at: str = ""
    expires_at: str = ""
    released_at: str = ""
    release_reason: str = ""
    release_operator: str = ""
    note: str = ""
    is_force_release: bool = False

    @classmethod
    def create_pending(cls, batch_id: str, discrepancy_id: str) -> "ClaimRecord":
        return cls(
            claim_id="CLAIM-" + uuid.uuid4().hex[:10].upper(),
            batch_id=batch_id,
            discrepancy_id=discrepancy_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["is_force_release"] = 1 if self.is_force_release else 0
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ClaimRecord":
        return cls(
            claim_id=row["claim_id"],
            batch_id=row["batch_id"],
            discrepancy_id=row["discrepancy_id"],
            claimant=row["claimant"] or "",
            status=ClaimStatus(row["status"]),
            claimed_at=row["claimed_at"] or "",
            expires_at=row["expires_at"] or "",
            released_at=row["released_at"] or "",
            release_reason=row["release_reason"] or "",
            release_operator=row["release_operator"] or "",
            note=row["note"] or "",
            is_force_release=bool(row["is_force_release"]),
        )


class ClaimStorage:
    """认领记录存储 - SQLite 持久化."""

    def __init__(self, storage_dir: str) -> None:
        os.makedirs(storage_dir, exist_ok=True)
        self.db_path = os.path.join(storage_dir, "claims.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(CLAIM_TABLE_DDL + CLAIM_INDEX_DDL)
            conn.commit()
        finally:
            conn.close()

    def _get_or_create_record(
        self, conn: sqlite3.Connection, batch_id: str, discrepancy_id: str
    ) -> ClaimRecord:
        row = conn.execute(
            "SELECT * FROM claim_records WHERE batch_id = ? AND discrepancy_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (batch_id, discrepancy_id),
        ).fetchone()
        if row:
            rec = ClaimRecord.from_row(row)
            if rec.status == ClaimStatus.CLAIMED and rec.expires_at:
                try:
                    exp_dt = datetime.fromisoformat(rec.expires_at)
                    if datetime.now() > exp_dt:
                        rec.status = ClaimStatus.RELEASED
                        rec.released_at = datetime.now().isoformat()
                        rec.release_reason = "自动过期"
                        rec.is_force_release = False
                        conn.execute(
                            "UPDATE claim_records SET status = ?, released_at = ?, "
                            "release_reason = ?, is_force_release = 0 WHERE claim_id = ?",
                            (rec.status.value, rec.released_at, rec.release_reason, rec.claim_id),
                        )
                        conn.commit()
                        new_rec = ClaimRecord.create_pending(batch_id, discrepancy_id)
                        conn.execute(
                            "INSERT INTO claim_records "
                            "(claim_id, batch_id, discrepancy_id, claimant, status, "
                            "claimed_at, expires_at, released_at, release_reason, "
                            "release_operator, note, is_force_release) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                new_rec.claim_id, new_rec.batch_id, new_rec.discrepancy_id,
                                new_rec.claimant, new_rec.status.value,
                                new_rec.claimed_at, new_rec.expires_at,
                                new_rec.released_at, new_rec.release_reason,
                                new_rec.release_operator, new_rec.note,
                                0,
                            ),
                        )
                        conn.commit()
                        return new_rec
                except (ValueError, TypeError):
                    pass
            return rec
        new_rec = ClaimRecord.create_pending(batch_id, discrepancy_id)
        conn.execute(
            "INSERT INTO claim_records "
            "(claim_id, batch_id, discrepancy_id, claimant, status, "
            "claimed_at, expires_at, released_at, release_reason, "
            "release_operator, note, is_force_release) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_rec.claim_id, new_rec.batch_id, new_rec.discrepancy_id,
                new_rec.claimant, new_rec.status.value,
                new_rec.claimed_at, new_rec.expires_at,
                new_rec.released_at, new_rec.release_reason,
                new_rec.release_operator, new_rec.note,
                0,
            ),
        )
        conn.commit()
        return new_rec

    def take(
        self,
        batch_id: str,
        discrepancy_ids: List[str],
        claimant: str,
        expires_hours: Optional[int] = None,
        note: str = "",
    ) -> Tuple[List[ClaimRecord], List[Dict[str, Any]]]:
        """
        批量认领差异.
        返回 (成功认领列表, 失败列表[{discrepancy_id, reason}])
        """
        if not claimant or not claimant.strip():
            raise OperatorMissingError("认领人（claimant）不能为空")

        successes: List[ClaimRecord] = []
        failures: List[Dict[str, Any]] = []

        expires_at = ""
        if expires_hours is not None and expires_hours > 0:
            expires_at = (datetime.now() + timedelta(hours=expires_hours)).isoformat()

        conn = self._connect()
        try:
            for disp_id in discrepancy_ids:
                disp_id = disp_id.strip()
                if not disp_id:
                    failures.append({"discrepancy_id": disp_id, "reason": "差异ID为空"})
                    continue
                try:
                    rec = self._get_or_create_record(conn, batch_id, disp_id)
                    if rec.status == ClaimStatus.CLAIMED:
                        if rec.claimant == claimant.strip():
                            failures.append({
                                "discrepancy_id": disp_id,
                                "reason": f"您已认领该差异，请勿重复提交",
                            })
                            continue
                        failures.append({
                            "discrepancy_id": disp_id,
                            "reason": f"已被 {rec.claimant} 认领",
                        })
                        continue

                    now = datetime.now().isoformat()
                    conn.execute(
                        "UPDATE claim_records SET claimant = ?, status = ?, "
                        "claimed_at = ?, expires_at = ?, note = ? WHERE claim_id = ?",
                        (
                            claimant.strip(),
                            ClaimStatus.CLAIMED.value,
                            now,
                            expires_at,
                            note,
                            rec.claim_id,
                        ),
                    )
                    conn.commit()
                    row = conn.execute(
                        "SELECT * FROM claim_records WHERE claim_id = ?",
                        (rec.claim_id,),
                    ).fetchone()
                    successes.append(ClaimRecord.from_row(row))
                except Exception as e:
                    failures.append({
                        "discrepancy_id": disp_id,
                        "reason": str(e),
                    })
            return successes, failures
        finally:
            conn.close()

    def release(
        self,
        batch_id: str,
        discrepancy_ids: List[str],
        operator: str,
        reason: str = "",
        force: bool = False,
    ) -> Tuple[List[ClaimRecord], List[Dict[str, Any]]]:
        """
        释放认领.
        force=True 时管理员可强制释放他人认领的差异（需记录原因）.
        """
        if not operator or not operator.strip():
            raise OperatorMissingError("操作者（operator）不能为空")

        if force and (not reason or not reason.strip()):
            raise ClaimError("强制释放必须提供原因（reason）")

        successes: List[ClaimRecord] = []
        failures: List[Dict[str, Any]] = []

        conn = self._connect()
        try:
            for disp_id in discrepancy_ids:
                disp_id = disp_id.strip()
                if not disp_id:
                    failures.append({"discrepancy_id": disp_id, "reason": "差异ID为空"})
                    continue
                try:
                    rec = self._get_or_create_record(conn, batch_id, disp_id)
                    if rec.status != ClaimStatus.CLAIMED:
                        failures.append({
                            "discrepancy_id": disp_id,
                            "reason": f"当前状态为 {rec.status.value}，无需释放",
                        })
                        continue

                    if not force and rec.claimant != operator.strip():
                        failures.append({
                            "discrepancy_id": disp_id,
                            "reason": f"该差异由 {rec.claimant} 认领，仅本人或管理员(加 --force)可释放",
                        })
                        continue

                    now = datetime.now().isoformat()
                    conn.execute(
                        "UPDATE claim_records SET status = ?, released_at = ?, "
                        "release_reason = ?, release_operator = ?, "
                        "is_force_release = ? WHERE claim_id = ?",
                        (
                            ClaimStatus.RELEASED.value,
                            now,
                            reason.strip(),
                            operator.strip(),
                            1 if force else 0,
                            rec.claim_id,
                        ),
                    )
                    conn.commit()

                    new_rec = ClaimRecord.create_pending(batch_id, disp_id)
                    conn.execute(
                        "INSERT INTO claim_records "
                        "(claim_id, batch_id, discrepancy_id, claimant, status, "
                        "claimed_at, expires_at, released_at, release_reason, "
                        "release_operator, note, is_force_release) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_rec.claim_id, new_rec.batch_id, new_rec.discrepancy_id,
                            new_rec.claimant, new_rec.status.value,
                            new_rec.claimed_at, new_rec.expires_at,
                            new_rec.released_at, new_rec.release_reason,
                            new_rec.release_operator, new_rec.note,
                            0,
                        ),
                    )
                    conn.commit()
                    row = conn.execute(
                        "SELECT * FROM claim_records WHERE claim_id = ?",
                        (rec.claim_id,),
                    ).fetchone()
                    successes.append(ClaimRecord.from_row(row))
                except Exception as e:
                    failures.append({
                        "discrepancy_id": disp_id,
                        "reason": str(e),
                    })
            return successes, failures
        finally:
            conn.close()

    def ensure_pending_for_batch(
        self, batch_id: str, discrepancy_ids: List[str]
    ) -> None:
        """
        为批次中指定的所有差异ID确保存在 PENDING 认领记录.
        用于 list/export 前初始化，避免从未被认领过的差异不出现在列表中.
        """
        if not discrepancy_ids:
            return
        conn = self._connect()
        try:
            for disp_id in discrepancy_ids:
                self._get_or_create_record(conn, batch_id, disp_id)
            conn.commit()
        finally:
            conn.close()

    def list(
        self,
        batch_id: Optional[str] = None,
        claimant: Optional[str] = None,
        status: Optional[ClaimStatus] = None,
    ) -> List[ClaimRecord]:
        """查询认领记录（最新有效记录）."""
        clauses: List[str] = []
        params: List[Any] = []

        if batch_id:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if claimant:
            clauses.append("claimant = ?")
            params.append(claimant)
        if status:
            clauses.append("status = ?")
            params.append(status.value)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = (
            "SELECT cr.* FROM claim_records cr "
            "INNER JOIN ( "
            "  SELECT batch_id, discrepancy_id, MAX(id) as max_id "
            "  FROM claim_records "
            f"{where} "
            "  GROUP BY batch_id, discrepancy_id "
            ") latest ON cr.id = latest.max_id "
            "ORDER BY cr.id DESC"
        )

        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            results: List[ClaimRecord] = []
            for row in rows:
                rec = ClaimRecord.from_row(row)
                if rec.status == ClaimStatus.CLAIMED and rec.expires_at:
                    try:
                        exp_dt = datetime.fromisoformat(rec.expires_at)
                        if datetime.now() > exp_dt:
                            continue
                    except (ValueError, TypeError):
                        pass
                results.append(rec)
            return results
        finally:
            conn.close()

    def list_history(
        self,
        batch_id: Optional[str] = None,
        discrepancy_id: Optional[str] = None,
    ) -> List[ClaimRecord]:
        """查询认领历史记录（含已释放记录，用于审计和交接）."""
        clauses: List[str] = []
        params: List[Any] = []

        if batch_id:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if discrepancy_id:
            clauses.append("discrepancy_id = ?")
            params.append(discrepancy_id)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM claim_records{where} ORDER BY id DESC"

        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [ClaimRecord.from_row(r) for r in rows]
        finally:
            conn.close()

    def get_current(self, batch_id: str, discrepancy_id: str) -> Optional[ClaimRecord]:
        """获取指定差异的当前认领状态."""
        conn = self._connect()
        try:
            rec = self._get_or_create_record(conn, batch_id, discrepancy_id)
            if rec.status == ClaimStatus.CLAIMED and rec.expires_at:
                try:
                    exp_dt = datetime.fromisoformat(rec.expires_at)
                    if datetime.now() > exp_dt:
                        return None
                except (ValueError, TypeError):
                    pass
            if rec.status == ClaimStatus.CLAIMED:
                return rec
            return None
        finally:
            conn.close()

    def check_can_mark(
        self, batch_id: str, discrepancy_id: str, operator: str
    ) -> None:
        """
        检查操作者是否有权标记该差异（用于 mark/rollback 联动）.
        规则：
        - 未被认领：任何人均可标记
        - 已被认领给本人：可标记
        - 已被认领给他人：普通用户不可标记（抛 ClaimPermissionDeniedError）
        """
        current = self.get_current(batch_id, discrepancy_id)
        if current is None:
            return
        if current.claimant != operator.strip():
            raise ClaimPermissionDeniedError(
                f"差异 {discrepancy_id} 已由 {current.claimant} 认领，"
                f"仅认领人可标记。如需操作请先由认领人释放或管理员强制释放。"
            )

    def export_json(
        self,
        output_path: str,
        batch_id: Optional[str] = None,
        claimant: Optional[str] = None,
        status: Optional[ClaimStatus] = None,
    ) -> int:
        """导出 JSON 交接清单.

        Raises:
            ExportPathConflictError: 输出文件已存在
        """
        if os.path.isfile(output_path):
            raise ExportPathConflictError(
                f"导出路径冲突: 文件已存在 '{output_path}'，请指定新的输出路径"
            )
        records = self.list(batch_id=batch_id, claimant=claimant, status=status)
        history = self.list_history(batch_id=batch_id)
        data = {
            "exported_at": datetime.now().isoformat(),
            "filters": {
                "batch_id": batch_id,
                "claimant": claimant,
                "status": status.value if status else None,
            },
            "current_claims": [r.to_dict() for r in records],
            "claim_history": [r.to_dict() for r in history],
            "summary": {
                "total_current": len(records),
                "total_history": len(history),
                "by_status": self._count_by_status(records),
                "by_claimant": self._count_by_claimant(records),
            },
        }
        out_dir = os.path.dirname(os.path.abspath(output_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return len(records)

    def export_csv(
        self,
        output_path: str,
        batch_id: Optional[str] = None,
        claimant: Optional[str] = None,
        status: Optional[ClaimStatus] = None,
    ) -> int:
        """导出 CSV 交接清单（当前认领记录）.

        Raises:
            ExportPathConflictError: 输出文件已存在
        """
        if os.path.isfile(output_path):
            raise ExportPathConflictError(
                f"导出路径冲突: 文件已存在 '{output_path}'，请指定新的输出路径"
            )
        records = self.list(batch_id=batch_id, claimant=claimant, status=status)
        columns = [
            "claim_id", "batch_id", "discrepancy_id", "claimant",
            "status", "claimed_at", "expires_at", "note",
        ]
        out_dir = os.path.dirname(os.path.abspath(output_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for rec in records:
                d = rec.to_dict()
                writer.writerow({k: d.get(k, "") for k in columns})
        return len(records)

    def _count_by_status(self, records: List[ClaimRecord]) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for r in records:
            result[r.status.value] = result.get(r.status.value, 0) + 1
        return result

    def _count_by_claimant(self, records: List[ClaimRecord]) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for r in records:
            if r.claimant:
                result[r.claimant] = result.get(r.claimant, 0) + 1
        return result
