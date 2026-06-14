"""报告模块 - 导出可追溯来源的差异清单."""
from __future__ import annotations

import csv
import os
from typing import List, Optional, Dict

from .models import Batch, Discrepancy, DiscrepancyStatus, DiscrepancyType, MatchLevel


DIFFERENCE_COLUMNS = [
    "discrepancy_id",
    "discrepancy_type",
    "match_level",
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
        "match_level": discrepancy.match_level.value,
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
    by_match_level = {
        MatchLevel.EXACT.value: 0,
        MatchLevel.TOLERANCE.value: 0,
        MatchLevel.MANUAL.value: 0,
    }

    for d in batch.discrepancies:
        t = d.discrepancy_type.value
        by_type[t] = by_type.get(t, 0) + 1
        s = d.status.value
        by_status[s] = by_status.get(s, 0) + 1
        ml = d.match_level.value
        by_match_level[ml] = by_match_level.get(ml, 0) + 1

    def _norm_id(txn_id: str) -> str:
        return txn_id.strip().lower() if txn_id else ""

    bank_by_id: Dict[str, List] = {}
    for t in batch.bank_txns:
        nid = _norm_id(t.txn_id)
        bank_by_id.setdefault(nid, []).append(t)

    system_by_id: Dict[str, List] = {}
    for t in batch.system_txns:
        nid = _norm_id(t.txn_id)
        system_by_id.setdefault(nid, []).append(t)

    disp_by_bank_id: Dict[str, List] = {}
    disp_by_system_id: Dict[str, List] = {}
    for d in batch.discrepancies:
        if d.bank_txn:
            disp_by_bank_id.setdefault(_norm_id(d.bank_txn.txn_id), []).append(d)
        if d.system_txn:
            disp_by_system_id.setdefault(_norm_id(d.system_txn.txn_id), []).append(d)

    matched_exact = 0
    matched_tolerance = 0
    matched_manual = 0

    paired_bank_ids: set = set()
    paired_system_ids: set = set()

    for nid in bank_by_id:
        if nid not in system_by_id:
            continue
        bank_list = bank_by_id[nid]
        sys_list = system_by_id[nid]
        pairs = min(len(bank_list), len(sys_list))

        for i in range(pairs):
            bt = bank_list[i]
            st = sys_list[i]
            paired_bank_ids.add(_norm_id(bt.txn_id))
            paired_system_ids.add(_norm_id(st.txn_id))

            best_level = None
            best_disp = None
            for d in batch.discrepancies:
                if d.bank_txn and d.system_txn:
                    if _norm_id(d.bank_txn.txn_id) == _norm_id(bt.txn_id) \
                       and _norm_id(d.system_txn.txn_id) == _norm_id(st.txn_id):
                        if d.discrepancy_type in (
                            DiscrepancyType.AMOUNT_MISMATCH,
                            DiscrepancyType.NEEDS_MANUAL_REVIEW,
                        ):
                            best_disp = d
                            best_level = d.match_level
                            break

            if best_level == MatchLevel.TOLERANCE:
                matched_tolerance += 1
            elif best_level == MatchLevel.MANUAL:
                matched_manual += 1
            else:
                matched_exact += 1

    for d in batch.discrepancies:
        if d.discrepancy_type == DiscrepancyType.NEEDS_MANUAL_REVIEW and d.bank_txn and d.system_txn:
            bnid = _norm_id(d.bank_txn.txn_id)
            snid = _norm_id(d.system_txn.txn_id)
            if bnid != snid:
                if d.match_level == MatchLevel.TOLERANCE:
                    matched_tolerance += 1
                elif d.match_level == MatchLevel.MANUAL:
                    matched_manual += 1
                paired_bank_ids.add(bnid)
                paired_system_ids.add(snid)

    for d in batch.discrepancies:
        if d.match_level == MatchLevel.MANUAL:
            if d.discrepancy_type in (
                DiscrepancyType.MISSING_IN_BANK,
                DiscrepancyType.MISSING_IN_SYSTEM,
            ):
                matched_manual += 1
                if d.bank_txn:
                    paired_bank_ids.add(_norm_id(d.bank_txn.txn_id))
                if d.system_txn:
                    paired_system_ids.add(_norm_id(d.system_txn.txn_id))

    for d in batch.discrepancies:
        if d.discrepancy_type == DiscrepancyType.DUPLICATE:
            if d.bank_txn:
                paired_bank_ids.add(_norm_id(d.bank_txn.txn_id))
            if d.system_txn:
                paired_system_ids.add(_norm_id(d.system_txn.txn_id))

    exact_count = len(batch.bank_txns)
    sys_count = len(batch.system_txns)

    def _count_paired_ids(txns, paired_ids):
        seen = set()
        for t in txns:
            if _norm_id(t.txn_id) in paired_ids:
                seen.add(_norm_id(t.txn_id))
        return len(seen)

    matched_bank_unique = _count_paired_ids(batch.bank_txns, paired_bank_ids)
    matched_system_unique = _count_paired_ids(batch.system_txns, paired_system_ids)

    unmatched_bank = exact_count - matched_bank_unique
    unmatched_system = sys_count - matched_system_unique

    total_unmatched = max(unmatched_bank, unmatched_system)

    return {
        "batch_id": batch.batch_id,
        "batch_name": batch.name,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "bank_transactions": exact_count,
        "system_transactions": sys_count,
        "adjustment_transactions": len(batch.adjustment_txns),
        "total_discrepancies": len(batch.discrepancies),
        "exact_matches": matched_exact,
        "tolerance_matches": matched_tolerance,
        "manual_matches": matched_manual,
        "unmatched_count": total_unmatched,
        "by_type": by_type,
        "by_status": by_status,
        "by_match_level": by_match_level,
        "exports": batch.exports,
        "imported_files": [f.to_dict() for f in batch.imported_files],
    }


SUMMARY_COLUMNS = [
    "指标", "值",
]


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
        writer.writerow(["精确匹配数", summary["exact_matches"]])
        writer.writerow(["容忍匹配数", summary["tolerance_matches"]])
        writer.writerow(["手工匹配数", summary["manual_matches"]])
        writer.writerow(["未匹配数", summary["unmatched_count"]])
        writer.writerow(["总差异数", summary["total_discrepancies"]])
        writer.writerow([])
        writer.writerow(["按差异类型统计"])
        for k, v in sorted(summary["by_type"].items()):
            writer.writerow([k, v])
        writer.writerow([])
        writer.writerow(["按状态统计"])
        for k, v in sorted(summary["by_status"].items()):
            writer.writerow([k, v])
        writer.writerow([])
        writer.writerow(["按匹配等级统计"])
        for k, v in sorted(summary["by_match_level"].items()):
            writer.writerow([k, v])
