"""数据模型定义."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any


class FileType(str, Enum):
    """文件类型枚举."""
    BANK_STATEMENT = "bank_statement"
    SYSTEM_RECEIPT = "system_receipt"
    MANUAL_ADJUSTMENT = "manual_adjustment"


class DiscrepancyType(str, Enum):
    """差异类型枚举."""
    MISSING_IN_BANK = "missing_in_bank"
    MISSING_IN_SYSTEM = "missing_in_system"
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE = "duplicate"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"


class DiscrepancyStatus(str, Enum):
    """差异处理状态."""
    OPEN = "open"
    CONFIRMED = "confirmed"
    IGNORED = "ignored"


class AdjustmentType(str, Enum):
    """手工调整类型."""
    TIMING_DIFF = "timing_diff"
    AMOUNT_ROUNDING = "amount_rounding"
    MANUAL_MATCH = "manual_match"
    WRITE_OFF = "write_off"


class BatchStatus(str, Enum):
    """批次状态."""
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class Transaction:
    """交易记录 - 可追溯到源文件."""
    txn_id: str
    amount: float
    date: str
    file_type: FileType
    source_file: str
    source_row: int
    counterparty: str = ""
    description: str = ""
    currency: str = "CNY"
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["file_type"] = self.file_type.value
        return d


@dataclass
class Discrepancy:
    """差异记录 - 带编号、复核人、备注、回滚."""
    discrepancy_id: str
    discrepancy_type: DiscrepancyType
    status: DiscrepancyStatus = DiscrepancyStatus.OPEN
    bank_txn: Optional[Transaction] = None
    system_txn: Optional[Transaction] = None
    adjustment_txn: Optional[Transaction] = None
    message: str = ""
    reviewer: Optional[str] = None
    note: str = ""
    rollback_history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def create(
        cls,
        discrepancy_type: DiscrepancyType,
        bank_txn: Optional[Transaction] = None,
        system_txn: Optional[Transaction] = None,
        adjustment_txn: Optional[Transaction] = None,
        message: str = "",
    ) -> "Discrepancy":
        return cls(
            discrepancy_id=cls._gen_id(),
            discrepancy_type=discrepancy_type,
            bank_txn=bank_txn,
            system_txn=system_txn,
            adjustment_txn=adjustment_txn,
            message=message,
        )

    @staticmethod
    def _gen_id() -> str:
        return "DISP-" + uuid.uuid4().hex[:10].upper()

    def mark(self, status: DiscrepancyStatus, reviewer: str, note: str = "") -> None:
        """标记差异状态，保存回滚记录."""
        self.rollback_history.append({
            "from_status": self.status.value,
            "to_status": status.value,
            "reviewer": reviewer,
            "note": self.note,
            "timestamp": datetime.now().isoformat(),
        })
        self.status = status
        self.reviewer = reviewer
        self.note = note
        self.updated_at = datetime.now().isoformat()

    def rollback(self) -> bool:
        """回滚到上一个状态."""
        if not self.rollback_history:
            return False
        last = self.rollback_history.pop()
        self.status = DiscrepancyStatus(last["from_status"])
        self.reviewer = None
        self.note = last["note"]
        self.updated_at = datetime.now().isoformat()
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "discrepancy_id": self.discrepancy_id,
            "discrepancy_type": self.discrepancy_type.value,
            "status": self.status.value,
            "message": self.message,
            "reviewer": self.reviewer,
            "note": self.note,
            "rollback_history": self.rollback_history,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "bank_txn": self.bank_txn.to_dict() if self.bank_txn else None,
            "system_txn": self.system_txn.to_dict() if self.system_txn else None,
            "adjustment_txn": self.adjustment_txn.to_dict() if self.adjustment_txn else None,
        }


@dataclass
class ImportedFile:
    """已导入文件记录."""
    file_type: FileType
    file_path: str
    imported_at: str = field(default_factory=lambda: datetime.now().isoformat())
    row_count: int = 0
    error_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_type": self.file_type.value,
            "file_path": self.file_path,
            "imported_at": self.imported_at,
            "row_count": self.row_count,
            "error_count": self.error_count,
        }


@dataclass
class Batch:
    """对账批次 - 所有状态的聚合根."""
    batch_id: str
    name: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: BatchStatus = BatchStatus.OPEN
    rule_file: Optional[str] = None
    imported_files: List[ImportedFile] = field(default_factory=list)
    bank_txns: List[Transaction] = field(default_factory=list)
    system_txns: List[Transaction] = field(default_factory=list)
    adjustment_txns: List[Transaction] = field(default_factory=list)
    discrepancies: List[Discrepancy] = field(default_factory=list)
    exports: List[Dict[str, Any]] = field(default_factory=list)
    manual_link_history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.status == BatchStatus.CLOSED

    @property
    def is_open(self) -> bool:
        return self.status == BatchStatus.OPEN

    def close(self) -> bool:
        """关闭批次（归档）. 返回 True 表示状态发生了变化."""
        if self.status == BatchStatus.CLOSED:
            return False
        self.status = BatchStatus.CLOSED
        self.touch()
        return True

    def reopen(self) -> bool:
        """重新打开批次. 返回 True 表示状态发生了变化."""
        if self.status == BatchStatus.OPEN:
            return False
        self.status = BatchStatus.OPEN
        self.touch()
        return True

    @classmethod
    def create(cls, name: str) -> "Batch":
        return cls(
            batch_id="BATCH-" + uuid.uuid4().hex[:8].upper(),
            name=name,
        )

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status.value,
            "rule_file": self.rule_file,
            "imported_files": [f.to_dict() for f in self.imported_files],
            "bank_txns": [t.to_dict() for t in self.bank_txns],
            "system_txns": [t.to_dict() for t in self.system_txns],
            "adjustment_txns": [t.to_dict() for t in self.adjustment_txns],
            "discrepancies": [d.to_dict() for d in self.discrepancies],
            "exports": self.exports,
            "manual_link_history": self.manual_link_history,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Batch":
        def txn_from_dict(d: Dict[str, Any]) -> Transaction:
            return Transaction(
                txn_id=d["txn_id"],
                amount=d["amount"],
                date=d["date"],
                file_type=FileType(d["file_type"]),
                source_file=d["source_file"],
                source_row=d["source_row"],
                counterparty=d.get("counterparty", ""),
                description=d.get("description", ""),
                currency=d.get("currency", "CNY"),
                raw_data=d.get("raw_data", {}),
            )

        def disp_from_dict(d: Dict[str, Any]) -> Discrepancy:
            return Discrepancy(
                discrepancy_id=d["discrepancy_id"],
                discrepancy_type=DiscrepancyType(d["discrepancy_type"]),
                status=DiscrepancyStatus(d["status"]),
                bank_txn=txn_from_dict(d["bank_txn"]) if d.get("bank_txn") else None,
                system_txn=txn_from_dict(d["system_txn"]) if d.get("system_txn") else None,
                adjustment_txn=txn_from_dict(d["adjustment_txn"]) if d.get("adjustment_txn") else None,
                message=d.get("message", ""),
                reviewer=d.get("reviewer"),
                note=d.get("note", ""),
                rollback_history=d.get("rollback_history", []),
                created_at=d.get("created_at", datetime.now().isoformat()),
                updated_at=d.get("updated_at", datetime.now().isoformat()),
            )

        return cls(
            batch_id=data["batch_id"],
            name=data["name"],
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            status=BatchStatus(data.get("status", BatchStatus.OPEN.value)),
            rule_file=data.get("rule_file"),
            imported_files=[
                ImportedFile(
                    file_type=FileType(f["file_type"]),
                    file_path=f["file_path"],
                    imported_at=f.get("imported_at", datetime.now().isoformat()),
                    row_count=f.get("row_count", 0),
                    error_count=f.get("error_count", 0),
                )
                for f in data.get("imported_files", [])
            ],
            bank_txns=[txn_from_dict(t) for t in data.get("bank_txns", [])],
            system_txns=[txn_from_dict(t) for t in data.get("system_txns", [])],
            adjustment_txns=[txn_from_dict(t) for t in data.get("adjustment_txns", [])],
            discrepancies=[disp_from_dict(d) for d in data.get("discrepancies", [])],
            exports=data.get("exports", []),
            manual_link_history=data.get("manual_link_history", []),
        )
