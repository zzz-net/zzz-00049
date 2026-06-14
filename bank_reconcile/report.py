"""报告模块 - 导出可追溯来源的差异清单."""
from __future__ import annotations

import csv
import os
from typing import List, Optional

from .models import Batch, Discrepancy, DiscrepancyStatus, DiscrepancyType


DIFFERENCE_COLUMNS = [
    "discrepancy_id",
    "discrepancy_type",
    "status",
    "message",
    "reviewer",
    "note",
    "created_at",
    "updated_at",
    "bank_txn_id",
    "bank_amount",
    "bank_date",
    "bank_counterparty",
    "bank_description",
    "bank_source_file",
    "bank_source_row",
    "system_txn_id",
    "system_amount",
    "system_date",
    "system_counterparty",
    "system_description",
    "system_source_file",
    "system_source_row",
    "adjustment_txn_id",
    "adjustment_amount",
    "adjustment_date",
    "adjustment_counterparty",
    "adjustment_description",
    "adjustment_source_file",
    "adjustment_source_row",
    "rollback_count",
]


def _txn_fields(txn, prefix: str) -> dict:
    if not txn:
        return {
            f"{prefix}_txn_id": "",
            f"{prefix}_amount": "",
            f"{prefix}_date": "",
            f"{prefix}_counterparty": "",
            f"{prefix}_description": "",
            f"{prefix}_source_file": "",
            f"{prefix}_source_row": "",
        }
    return {
        f"{prefix}_txn_id": txn.txn_id,
        f"{prefix}_amount": txn.amount,
        f"{prefix}_date": txn.date,
        f"{prefix}_counterparty": txn.counterparty,
        f"{prefix}_description": txn.description,
        f"{prefix}_source_file": txn.source_file,
        f"{prefix}_source_row": txn.source_row,
    }


def discrepancy_to_row(discrepancy: Discrepancy) -> dict:
    """将差异转为报告行，包含所有来源追溯字段."""
    row = {
        "discrepancy_id": discrepancy.discrepancy_id,
        "discrepancy_type": discrepancy.discrepancy_type.value,
        "status": discrepancy.status.value,
        "message": discrepancy.message,
        "reviewer": discrepancy.reviewer or "",
        "note": discrepancy.note or "",
        "created_at": discrepancy.created_at,
        "updated_at": discrepancy.updated_at,
        "rollback_count": len(discrepancy.rollback_history),
    }
    row.update(_txn_fields(discrepancy.bank_txn, "bank"))
    row.update(_txn_fields(discrepancy.system_txn, "system"))
    row.update(_txn_fields(discrepancy.adjustment_txn, "adjustment"))
    return row


def export_discrepancies_csv(
    batch: Batch,
    output_path: str,
    status_filter: Optional[List[DiscrepancyStatus]] = None,
    type_filter: Optional[List[DiscrepancyType]] = None,
) -> int:
    """导出差异清单 CSV，返回导出的条数."""
    discrepancies = batch.discrepancies
    if status_filter:
        s = set(status_filter)
        discrepancies = [d for d in discrepancies if d.status in s]
    if type_filter:
        t = set(type_filter)
        discrepancies = [d for d in discrepancies if d.discrepancy_type in t]

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DIFFERENCE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for d in discrepancies:
            writer.writerow(discrepancy_to_row(d))

    return len(discrepancies)


def generate_summary(batch: Batch) -> dict:
    """生成批次统计摘要."""
    by_type = {}
    by_status = {}
    for d in batch.discrepancies:
        t = d.discrepancy_type.value
        by_type[t] = by_type.get(t, 0) + 1
        s = d.status.value
        by_status[s] = by_status.get(s, 0) + 1

    return {
        "batch_id": batch.batch_id,
        "batch_name": batch.name,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "bank_transactions": len(batch.bank_txns),
        "system_transactions": len(batch.system_txns),
        "adjustment_transactions": len(batch.adjustment_txns),
        "total_discrepancies": len(batch.discrepancies),
        "by_type": by_type,
        "by_status": by_status,
        "exports": batch.exports,
        "imported_files": [f.to_dict() for f in batch.imported_files],
    }


def export_summary_csv(batch: Batch, output_path: str) -> None:
    """导出批次摘要 CSV."""
    summary = generate_summary(batch)
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["指标", "值"])
        writer.writerow(["批次ID", summary["batch_id"]])
        writer.writerow(["批次名称", summary["batch_name"]])
        writer.writerow(["创建时间", summary["created_at"]])
        writer.writerow(["更新时间", summary["updated_at"]])
        writer.writerow(["银行回单数", summary["bank_transactions"]])
        writer.writerow(["系统流水数", summary["system_transactions"]])
        writer.writerow(["手工调整数", summary["adjustment_transactions"]])
        writer.writerow(["总差异数", summary["total_discrepancies"]])
        writer.writerow([])
        writer.writerow(["按差异类型统计"])
        for k, v in sorted(summary["by_type"].items()):
            writer.writerow([k, v])
        writer.writerow([])
        writer.writerow(["按状态统计"])
        for k, v in sorted(summary["by_status"].items()):
            writer.writerow([k, v])
