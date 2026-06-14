"""匹配模块 - 识别缺失、金额不符、重复和待人工确认项."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Tuple

from .models import (
    Batch, Transaction, Discrepancy, DiscrepancyType,
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


def run_matching(batch: Batch, rules: MatchRules) -> List[Discrepancy]:
    """执行匹配，返回差异列表（不会自动写入 batch, 由调用方决定）."""
    discrepancies: List[Discrepancy] = []

    discrepancies.extend(_find_cross_duplicates(batch.bank_txns, batch.system_txns, rules))

    bank_index = _index_by_normalized_id(batch.bank_txns, rules)
    system_index = _index_by_normalized_id(batch.system_txns, rules)
    adj_index = _index_by_normalized_id(batch.adjustment_txns, rules)

    matched_ids: set = set()

    for norm_id, bank_list in bank_index.items():
        if norm_id in system_index:
            bank_txn = bank_list[0]
            system_list = system_index[norm_id]
            system_txn = system_list[0]
            matched_ids.add(norm_id)

            amounts_ok, used_adjs = _amounts_with_adj(bank_txn, system_txn, adj_index, rules)
            if not amounts_ok:
                discrepancies.append(Discrepancy.create(
                    discrepancy_type=DiscrepancyType.AMOUNT_MISMATCH,
                    bank_txn=bank_txn,
                    system_txn=system_txn,
                    adjustment_txn=used_adjs[0] if used_adjs else None,
                    message=f"金额不符: 银行 {bank_txn.amount} vs 系统 {system_txn.amount}, "
                            f"差额 {round(abs(bank_txn.amount - system_txn.amount), 2)}",
                ))
            else:
                if rules.needs_manual_review(bank_txn.description) or rules.needs_manual_review(system_txn.description):
                    discrepancies.append(Discrepancy.create(
                        discrepancy_type=DiscrepancyType.NEEDS_MANUAL_REVIEW,
                        bank_txn=bank_txn,
                        system_txn=system_txn,
                        message=f"摘要包含待人工确认关键词，银行: '{bank_txn.description}' / 系统: '{system_txn.description}'",
                    ))
        else:
            for bank_txn in bank_list:
                discrepancies.append(Discrepancy.create(
                    discrepancy_type=DiscrepancyType.MISSING_IN_SYSTEM,
                    bank_txn=bank_txn,
                    message=f"银行有此流水 {bank_txn.txn_id} (金额 {bank_txn.amount})，但系统无记录",
                ))

    for norm_id, system_list in system_index.items():
        if norm_id in matched_ids:
            continue
        for system_txn in system_list:
            discrepancies.append(Discrepancy.create(
                discrepancy_type=DiscrepancyType.MISSING_IN_BANK,
                system_txn=system_txn,
                message=f"系统有此流水 {system_txn.txn_id} (金额 {system_txn.amount})，但银行无记录",
            ))

    return discrepancies
