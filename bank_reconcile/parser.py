"""文件解析模块 - 支持银行回单、系统流水、手工调整三类 CSV."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Any, Optional

from .models import FileType, Transaction, ImportedFile


DEFAULT_COLUMN_MAPS = {
    FileType.BANK_STATEMENT: {
        "txn_id": ["交易流水号", "流水号", "transaction_id", "txn_id"],
        "amount": ["金额", "交易金额", "amount"],
        "date": ["交易日期", "日期", "date", "transaction_date"],
        "counterparty": ["对方户名", "对方账户名", "交易对手", "counterparty"],
        "description": ["摘要", "备注", "用途", "description", "summary"],
        "currency": ["币种", "currency"],
    },
    FileType.SYSTEM_RECEIPT: {
        "txn_id": ["订单号", "交易号", "收款流水号", "transaction_id", "txn_id", "order_id"],
        "amount": ["收款金额", "金额", "amount"],
        "date": ["收款日期", "日期", "date", "receipt_date"],
        "counterparty": ["付款方", "客户", "customer", "counterparty"],
        "description": ["备注", "说明", "description"],
        "currency": ["币种", "currency"],
    },
    FileType.MANUAL_ADJUSTMENT: {
        "txn_id": ["调整编号", "流水号", "transaction_id", "txn_id", "adjustment_id"],
        "amount": ["调整金额", "金额", "amount"],
        "date": ["调整日期", "日期", "date"],
        "counterparty": ["对方", "counterparty"],
        "description": ["调整原因", "备注", "description", "reason"],
        "currency": ["币种", "currency"],
    },
}


@dataclass
class ParseError:
    """解析错误记录."""
    source_file: str
    source_row: int
    error_type: str
    message: str
    raw_row: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_row": self.source_row,
            "error_type": self.error_type,
            "message": self.message,
            "raw_row": self.raw_row,
        }


@dataclass
class ParseResult:
    """解析结果."""
    transactions: List[Transaction] = field(default_factory=list)
    errors: List[ParseError] = field(default_factory=list)
    duplicates: List[ParseError] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.transactions)

    @property
    def error_count(self) -> int:
        return len(self.errors) + len(self.duplicates)


def _detect_column(header: List[str], candidates: List[str]) -> Optional[str]:
    """根据候选列名列表自动检测实际列名."""
    header_lower = {h.strip().lower(): h for h in header}
    for cand in candidates:
        if cand in header:
            return cand
        if cand.lower() in header_lower:
            return header_lower[cand.lower()]
    return None


def _build_column_map(file_type: FileType, header: List[str]) -> Dict[str, str]:
    """构建字段名到CSV列名的映射."""
    defaults = DEFAULT_COLUMN_MAPS[file_type]
    mapping: Dict[str, str] = {}
    for field_name, candidates in defaults.items():
        detected = _detect_column(header, candidates)
        if detected:
            mapping[field_name] = detected
    return mapping


def _parse_amount(raw: str, source_file: str, source_row: int) -> Tuple[Optional[float], Optional[ParseError]]:
    """解析金额，处理千分位、正负号、货币符号等."""
    if raw is None:
        return None, ParseError(source_file, source_row, "invalid_amount", "金额为空")
    s = str(raw).strip()
    if not s:
        return None, ParseError(source_file, source_row, "invalid_amount", "金额为空")
    cleaned = s.replace(",", "").replace("，", "").replace("¥", "").replace("￥", "").replace(" ", "")
    try:
        val = float(cleaned)
        return val, None
    except ValueError:
        return None, ParseError(source_file, source_row, "invalid_amount", f"非法金额: {raw}")


def parse_csv(
    file_path: str,
    file_type: FileType,
    encoding: str = "utf-8-sig",
) -> Tuple[ParseResult, ImportedFile]:
    """解析CSV文件.

    Args:
        file_path: CSV 文件路径
        file_type: 文件类型
        encoding: 文件编码

    Returns:
        (解析结果, 导入文件记录)
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    result = ParseResult()
    imported_file = ImportedFile(
        file_type=file_type,
        file_path=os.path.abspath(file_path),
    )

    with open(file_path, "r", encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"CSV 文件无表头: {file_path}")

        header = list(reader.fieldnames)
        col_map = _build_column_map(file_type, header)

        if "txn_id" not in col_map:
            raise ValueError(
                f"文件 {file_path} 缺少交易号列. "
                f"支持的列名: {DEFAULT_COLUMN_MAPS[file_type]['txn_id']}"
            )
        if "amount" not in col_map:
            raise ValueError(
                f"文件 {file_path} 缺少金额列. "
                f"支持的列名: {DEFAULT_COLUMN_MAPS[file_type]['amount']}"
            )

        for idx, row in enumerate(reader, start=2):
            raw_row = dict(row)
            raw_txn_id = (row.get(col_map["txn_id"]) or "").strip()

            if not raw_txn_id:
                result.errors.append(ParseError(
                    source_file=file_path,
                    source_row=idx,
                    error_type="missing_txn_id",
                    message="缺少交易号",
                    raw_row=raw_row,
                ))
                continue

            amount, err = _parse_amount(row.get(col_map["amount"], ""), file_path, idx)
            if err is not None:
                err.raw_row = raw_row
                result.errors.append(err)
                continue

            txn = Transaction(
                txn_id=raw_txn_id,
                amount=amount,
                date=(row.get(col_map["date"], "") if "date" in col_map else "").strip(),
                file_type=file_type,
                source_file=file_path,
                source_row=idx,
                counterparty=(row.get(col_map["counterparty"], "") if "counterparty" in col_map else "").strip(),
                description=(row.get(col_map["description"], "") if "description" in col_map else "").strip(),
                currency=(row.get(col_map["currency"], "CNY") if "currency" in col_map else "CNY").strip() or "CNY",
                raw_data=raw_row,
            )
            result.transactions.append(txn)

    imported_file.row_count = result.row_count
    imported_file.error_count = result.error_count
    return result, imported_file
