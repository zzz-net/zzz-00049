"""规则引擎 - 加载、校验和提供对账规则."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

import yaml


class RuleValidationError(Exception):
    """规则文件校验错误."""
    pass


@dataclass
class MatchRules:
    """对账匹配规则."""
    amount_tolerance: float = 0.01
    date_window_days: int = 3
    require_exact_txn_id: bool = True
    case_sensitive_txn_id: bool = False
    manual_review_keywords: List[str] = field(default_factory=lambda: [
        "调账", "手续费", "利息", "汇率", "退款", "红冲", "待确认",
    ])
    ignore_duplicate_if_amount_differs: bool = False
    consider_adjustments: bool = True

    @classmethod
    def default(cls) -> "MatchRules":
        return cls()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MatchRules":
        if not isinstance(data, dict):
            raise RuleValidationError("规则文件根节点必须是字典")
        rules = cls()
        if "amount_tolerance" in data:
            v = data["amount_tolerance"]
            if not isinstance(v, (int, float)) or v < 0:
                raise RuleValidationError(f"amount_tolerance 必须是非负数字, 实际: {v}")
            rules.amount_tolerance = float(v)
        if "date_window_days" in data:
            v = data["date_window_days"]
            if not isinstance(v, int) or v < 0:
                raise RuleValidationError(f"date_window_days 必须是非负整数, 实际: {v}")
            rules.date_window_days = v
        if "require_exact_txn_id" in data:
            v = data["require_exact_txn_id"]
            if not isinstance(v, bool):
                raise RuleValidationError(f"require_exact_txn_id 必须是布尔值, 实际: {v}")
            rules.require_exact_txn_id = v
        if "case_sensitive_txn_id" in data:
            v = data["case_sensitive_txn_id"]
            if not isinstance(v, bool):
                raise RuleValidationError(f"case_sensitive_txn_id 必须是布尔值, 实际: {v}")
            rules.case_sensitive_txn_id = v
        if "manual_review_keywords" in data:
            v = data["manual_review_keywords"]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise RuleValidationError("manual_review_keywords 必须是字符串列表")
            rules.manual_review_keywords = list(v)
        if "ignore_duplicate_if_amount_differs" in data:
            v = data["ignore_duplicate_if_amount_differs"]
            if not isinstance(v, bool):
                raise RuleValidationError(f"ignore_duplicate_if_amount_differs 必须是布尔值, 实际: {v}")
            rules.ignore_duplicate_if_amount_differs = v
        if "consider_adjustments" in data:
            v = data["consider_adjustments"]
            if not isinstance(v, bool):
                raise RuleValidationError(f"consider_adjustments 必须是布尔值, 实际: {v}")
            rules.consider_adjustments = v
        return rules

    def normalize_txn_id(self, txn_id: str) -> str:
        if not self.case_sensitive_txn_id:
            return txn_id.strip().upper()
        return txn_id.strip()

    def amounts_match(self, a: float, b: float) -> bool:
        return abs(a - b) <= self.amount_tolerance

    def needs_manual_review(self, text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        return any(kw.lower() in t for kw in self.manual_review_keywords)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "amount_tolerance": self.amount_tolerance,
            "date_window_days": self.date_window_days,
            "require_exact_txn_id": self.require_exact_txn_id,
            "case_sensitive_txn_id": self.case_sensitive_txn_id,
            "manual_review_keywords": list(self.manual_review_keywords),
            "ignore_duplicate_if_amount_differs": self.ignore_duplicate_if_amount_differs,
            "consider_adjustments": self.consider_adjustments,
        }


def load_rules(file_path: Optional[str]) -> MatchRules:
    """加载规则文件. 文件不存在或路径为 None 时返回默认规则."""
    if not file_path:
        return MatchRules.default()
    if not os.path.isfile(file_path):
        raise RuleValidationError(f"规则文件不存在: {file_path}")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise RuleValidationError(f"规则文件 YAML 解析失败: {e}")
    except UnicodeDecodeError:
        raise RuleValidationError(f"规则文件编码错误，请使用 UTF-8: {file_path}")

    if data is None:
        return MatchRules.default()
    if not isinstance(data, dict):
        raise RuleValidationError("规则文件必须是 YAML 映射 (字典) 格式")

    allowed = {
        "amount_tolerance", "date_window_days", "require_exact_txn_id",
        "case_sensitive_txn_id", "manual_review_keywords",
        "ignore_duplicate_if_amount_differs", "consider_adjustments",
    }
    unknown = set(data.keys()) - allowed
    if unknown:
        raise RuleValidationError(f"规则文件包含未知字段: {sorted(unknown)}; 允许字段: {sorted(allowed)}")

    return MatchRules.from_dict(data)
