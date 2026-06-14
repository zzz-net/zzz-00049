"""匹配模块 - 识别缺失、金额不符、重复和待人工确认项."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple, Optional

from .models import (
    Batch, Transaction, Discrepancy, DiscrepancyType, MatchLevel,
)
from .rules import MatchRules


def _index_by_normalized_id(
    txns: List[Transaction], rules: MatchRules
) -> Dict[str, List[Transaction]]:
    """按规范化的交易号建索引."""
    idx: Dict[str, List[Transaction]] = defaultdict(list)
    for t in txns:
        idx[rules.normalize_txn_id(t.txn_id)].append(t)
    return idx


def _find_cross_duplicates(
    bank_txns: List[Transaction],
    system_txns: List[Transaction],
    rules: MatchRules,
) -> List[Discrepancy]:
    """检测跨源的重复流水号."""
    discrepancies: List[Discrepancy] = []
    bank_map: Dict[str, List[Transaction]] = defaultdict(list)
    system_map: Dict[str, List[Transaction]] = defaultdict(list)

    for t in bank_txns:
        bank_map[rules.normalize_txn_id(t.txn_id)].append(t)
    for t in system_txns:
        system_map[rules.normalize_txn_id(t.txn_id)].append(t)

    for norm_id, txns in bank_map.items():
        if len(txns) > 1:
            for t in txns[1:]:
                first = txns[0]
                if rules.ignore_duplicate_if_amount_differs and not rules.amounts_match(t.amount, first.amount):
                    continue
                discrepancies.append(Discrepancy.create(
                    discrepancy_type=DiscrepancyType.DUPLICATE,
                    bank_txn=t,
                    match_level=MatchLevel.EXACT,
                    message=f"银行回单中存在重复流水号 {t.txn_id}, "
                            f"首笔金额 {first.amount}, 本笔金额 {t.amount}, "
                            f"首笔行号 {first.source_row}, 本笔行号 {t.source_row}",
                ))

    for norm_id, txns in system_map.items():
        if len(txns) > 1:
            for t in txns[1:]:
                first = txns[0]
                if rules.ignore_duplicate_if_amount_differs and not rules.amounts_match(t.amount, first.amount):
                    continue
                discrepancies.append(Discrepancy.create(
                    discrepancy_type=DiscrepancyType.DUPLICATE,
                    system_txn=t,
                    match_level=MatchLevel.EXACT,
                    message=f"系统流水中存在重复流水号 {t.txn_id}, "
                            f"首笔金额 {first.amount}, 本笔金额 {t.amount}, "
                            f"首笔行号 {first.source_row}, 本笔行号 {t.source_row}",
                ))

    return discrepancies


def _amounts_with_adj(
    bank_txn: Transaction,
    system_txn: Transaction,
    adjustments: Dict[str, List[Transaction]],
    rules: MatchRules,
) -> Tuple[bool, List[Transaction]]:
    """检查金额是否匹配，考虑手工调整."""
    if rules.amounts_match(bank_txn.amount, system_txn.amount):
        return True, []

    if not rules.consider_adjustments:
        return False, []

    norm_id = rules.normalize_txn_id(bank_txn.txn_id)
    adjs = adjustments.get(norm_id, [])
    if not adjs:
        return False, []

    diff = abs(bank_txn.amount - system_txn.amount)
    for adj in adjs:
        if rules.amounts_match(abs(adj.amount), diff):
            return True, [adj]
    return False, []


def _date_diff_days(date_a: str, date_b: str) -> Optional[int]:
    """计算两个日期的天数差，解析失败返回 None."""
    try:
        d1 = datetime.strptime(date_a, "%Y-%m-%d")
        d2 = datetime.strptime(date_b, "%Y-%m-%d")
        return abs((d1 - d2).days)
    except (ValueError, TypeError):
        return None


def _try_tolerance_match(
    bank_txn: Transaction,
    system_txn: Transaction,
    rules: MatchRules,
) -> Tuple[bool, str]:
    """尝试用容忍度规则匹配一对交易.

    返回 (是否匹配, 匹配原因描述).
    """
    tol = rules.tolerance
    if not tol.enabled:
        return False, ""

    reasons: List[str] = []

    amount_ok = tol.is_amount_within_tolerance(bank_txn.amount, system_txn.amount)
    if amount_ok:
        diff = abs(bank_txn.amount - system_txn.amount)
        if tol.amount_is_percent:
            reasons.append(f"金额差 {diff:.2f} 在 {tol.amount_value * 100:.4g}% 容差内")
        else:
            reasons.append(f"金额差 {diff:.2f} 在 ±{tol.amount_value:.2f} 容差内")

    date_ok = tol.is_date_within_tolerance(bank_txn.date, system_txn.date)
    if date_ok:
        dd = _date_diff_days(bank_txn.date, system_txn.date)
        if dd is not None:
            reasons.append(f"日期差 {dd} 天 在 ±{tol.date_tolerance_days} 天容差内")

    partial_ok = tol.is_partial_match(
        bank_txn.txn_id, system_txn.txn_id,
        bank_txn.description, system_txn.description,
    )
    if partial_ok:
        reasons.append("交易号前缀/备注关键字部分匹配")

    if not reasons:
        return False, ""

    if amount_ok and (date_ok or partial_ok):
        return True, "; ".join(reasons)
    if partial_ok and (amount_ok or date_ok):
        return True, "; ".join(reasons)
    if tol.txn_id_prefixes or tol.description_keywords:
        if partial_ok and amount_ok:
            return True, "; ".join(reasons)

    return False, ""


def _run_tolerance_second_pass(
    unmatched_bank: List[Transaction],
    unmatched_system: List[Transaction],
    rules: MatchRules,
) -> Tuple[List[Discrepancy], List[Tuple[Transaction, Transaction, str]]]:
    """第二轮: 容忍度匹配.

    返回 (容忍匹配产生的差异列表, 容忍匹配审计明细列表).
    审计明细: (bank_txn, system_txn, 匹配原因).
    """
    discrepancies: List[Discrepancy] = []
    audit_records: List[Tuple[Transaction, Transaction, str]] = []

    if not rules.tolerance.enabled:
        return discrepancies, audit_records

    used_bank: set = set()
    used_system: set = set()

    for bank_txn in unmatched_bank:
        if id(bank_txn) in used_bank:
            continue
        best_match = None
        best_reason = ""
        best_score = -1

        for system_txn in unmatched_system:
            if id(system_txn) in used_system:
                continue
            matched, reason = _try_tolerance_match(bank_txn, system_txn, rules)
            if matched:
                score = 0
                tol = rules.tolerance
                if tol.is_amount_within_tolerance(bank_txn.amount, system_txn.amount):
                    score += 2
                if tol.is_date_within_tolerance(bank_txn.date, system_txn.date):
                    score += 2
                if tol.is_partial_match(bank_txn.txn_id, system_txn.txn_id,
                                        bank_txn.description, system_txn.description):
                    score += 1
                if score > best_score:
                    best_score = score
                    best_match = system_txn
                    best_reason = reason

        if best_match is not None:
            used_bank.add(id(bank_txn))
            used_system.add(id(best_match))
            audit_records.append((bank_txn, best_match, best_reason))
            discrepancies.append(Discrepancy.create(
                discrepancy_type=DiscrepancyType.NEEDS_MANUAL_REVIEW,
                bank_txn=bank_txn,
                system_txn=best_match,
                match_level=MatchLevel.TOLERANCE,
                message=f"[容忍匹配] {best_reason}, "
                        f"银行 {bank_txn.txn_id} 金额 {bank_txn.amount} 日期 {bank_txn.date}, "
                        f"系统 {best_match.txn_id} 金额 {best_match.amount} 日期 {best_match.date}",
            ))

    return discrepancies, audit_records


def run_matching(batch: Batch, rules: MatchRules) -> List[Discrepancy]:
    """执行匹配，返回差异列表（不会自动写入 batch, 由调用方决定）.

    第一轮: 按交易号精确匹配.
    第二轮: 对未匹配交易执行容忍度匹配.
    """
    discrepancies: List[Discrepancy] = []

    discrepancies.extend(_find_cross_duplicates(batch.bank_txns, batch.system_txns, rules))

    bank_index = _index_by_normalized_id(batch.bank_txns, rules)
    system_index = _index_by_normalized_id(batch.system_txns, rules)
    adj_index = _index_by_normalized_id(batch.adjustment_txns, rules)

    matched_ids: set = set()
    unmatched_bank: List[Transaction] = []
    unmatched_system: List[Transaction] = []

    for norm_id, bank_list in bank_index.items():
        if norm_id in system_index:
            bank_txn = bank_list[0]
            system_list = system_index[norm_id]
            system_txn = system_list[0]
            matched_ids.add(norm_id)

            amounts_ok, used_adjs = _amounts_with_adj(bank_txn, system_txn, adj_index, rules)
            if not amounts_ok:
                tol_matched, tol_reason = _try_tolerance_match(bank_txn, system_txn, rules)
                if tol_matched:
                    discrepancies.append(Discrepancy.create(
                        discrepancy_type=DiscrepancyType.NEEDS_MANUAL_REVIEW,
                        bank_txn=bank_txn,
                        system_txn=system_txn,
                        match_level=MatchLevel.TOLERANCE,
                        message=f"[容忍匹配] {tol_reason}, "
                                f"银行 {bank_txn.txn_id} 金额 {bank_txn.amount} 日期 {bank_txn.date}, "
                                f"系统 {system_txn.txn_id} 金额 {system_txn.amount} 日期 {system_txn.date}",
                    ))
                else:
                    discrepancies.append(Discrepancy.create(
                        discrepancy_type=DiscrepancyType.AMOUNT_MISMATCH,
                        bank_txn=bank_txn,
                        system_txn=system_txn,
                        adjustment_txn=used_adjs[0] if used_adjs else None,
                        match_level=MatchLevel.EXACT,
                        message=f"金额不符: 银行 {bank_txn.amount} vs 系统 {system_txn.amount}, "
                                f"差额 {round(abs(bank_txn.amount - system_txn.amount), 2)}",
                    ))
            else:
                if rules.needs_manual_review(bank_txn.description) or rules.needs_manual_review(system_txn.description):
                    discrepancies.append(Discrepancy.create(
                        discrepancy_type=DiscrepancyType.NEEDS_MANUAL_REVIEW,
                        bank_txn=bank_txn,
                        system_txn=system_txn,
                        match_level=MatchLevel.EXACT,
                        message=f"摘要包含待人工确认关键词，银行: '{bank_txn.description}' / 系统: '{system_txn.description}'",
                    ))
        else:
            for bank_txn in bank_list:
                unmatched_bank.append(bank_txn)
                discrepancies.append(Discrepancy.create(
                    discrepancy_type=DiscrepancyType.MISSING_IN_SYSTEM,
                    bank_txn=bank_txn,
                    match_level=MatchLevel.EXACT,
                    message=f"银行有此流水 {bank_txn.txn_id} (金额 {bank_txn.amount})，但系统无记录",
                ))

    for norm_id, system_list in system_index.items():
        if norm_id in matched_ids:
            continue
        for system_txn in system_list:
            unmatched_system.append(system_txn)
            discrepancies.append(Discrepancy.create(
                discrepancy_type=DiscrepancyType.MISSING_IN_BANK,
                system_txn=system_txn,
                match_level=MatchLevel.EXACT,
                message=f"系统有此流水 {system_txn.txn_id} (金额 {system_txn.amount})，但银行无记录",
            ))

    if rules.tolerance.enabled and (unmatched_bank or unmatched_system):
        tol_discrepancies, tol_audit = _run_tolerance_second_pass(
            unmatched_bank, unmatched_system, rules
        )

        if tol_discrepancies:
            matched_bank_ids = {id(td.bank_txn) for td in tol_discrepancies if td.bank_txn}
            matched_system_ids = {id(td.system_txn) for td in tol_discrepancies if td.system_txn}

            discrepancies = [
                d for d in discrepancies
                if not (d.discrepancy_type == DiscrepancyType.MISSING_IN_SYSTEM
                        and d.bank_txn and id(d.bank_txn) in matched_bank_ids)
                and not (d.discrepancy_type == DiscrepancyType.MISSING_IN_BANK
                         and d.system_txn and id(d.system_txn) in matched_system_ids)
            ]

            discrepancies.extend(tol_discrepancies)

    return discrepancies


def get_tolerance_match_records(batch: Batch) -> List[Dict]:
    """从批次差异中提取容忍匹配的审计记录."""
    records: List[Dict] = []
    for d in batch.discrepancies:
        if d.match_level == MatchLevel.TOLERANCE:
            records.append({
                "bank_txn_id": d.bank_txn.txn_id if d.bank_txn else "",
                "bank_amount": d.bank_txn.amount if d.bank_txn else 0,
                "bank_date": d.bank_txn.date if d.bank_txn else "",
                "system_txn_id": d.system_txn.txn_id if d.system_txn else "",
                "system_amount": d.system_txn.amount if d.system_txn else 0,
                "system_date": d.system_txn.date if d.system_txn else "",
                "message": d.message,
            })
    return records
