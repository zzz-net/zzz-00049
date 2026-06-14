"""规则引擎 - 加载、校验和提供对账规则."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple

import yaml


class RuleValidationError(Exception):
    """规则文件校验错误."""
    pass


_PERCENT_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")


def _parse_amount_tolerance(value: Any) -> Tuple[float, bool]:
    """解析金额容差，返回 (容差值, 是否为百分比).

    支持:
      - 数字: 5, 0.01 → 绝对值
      - 字符串百分比: "5%", "0.5%" → 百分比
    """
    if isinstance(value, bool):
        raise RuleValidationError(f"amount_tolerance 不能是布尔值, 实际: {value}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise RuleValidationError(f"amount_tolerance 必须是非负数字, 实际: {value}")
        return float(value), False
    if isinstance(value, str):
        m = _PERCENT_PATTERN.match(value)
        if m:
            pct = float(m.group(1))
            if pct < 0:
                raise RuleValidationError(f"amount_tolerance 百分比必须非负, 实际: {value}")
            if pct > 100:
                raise RuleValidationError(f"amount_tolerance 百分比不能超过 100%, 实际: {value}")
            return pct / 100.0, True
        try:
            v = float(value)
            if v < 0:
                raise RuleValidationError(f"amount_tolerance 必须是非负数字, 实际: {value}")
            return v, False
        except (ValueError, TypeError):
            raise RuleValidationError(
                f"amount_tolerance 格式错误: '{value}'，"
                f"支持绝对值(如 5, 0.01)或百分比(如 '5%', '0.5%')"
            )
    raise RuleValidationError(
        f"amount_tolerance 类型错误: {type(value).__name__}，"
        f"支持数字或百分比字符串"
    )


def _format_amount_tolerance(value: float, is_percent: bool) -> Any:
    """格式化金额容差用于导出."""
    if is_percent:
        return f"{value * 100:.6g}%"
    return value


@dataclass
class ToleranceRules:
    """容忍度匹配规则."""
    enabled: bool = False
    amount_value: float = 0.0
    amount_is_percent: bool = False
    date_tolerance_days: int = 0
    txn_id_prefixes: List[str] = field(default_factory=list)
    description_keywords: List[str] = field(default_factory=list)

    @classmethod
    def default(cls) -> "ToleranceRules":
        return cls()

    def is_amount_within_tolerance(self, a: float, b: float) -> bool:
        """判断金额是否在容差范围内."""
        if not self.enabled:
            return False
        diff = abs(a - b)
        if self.amount_is_percent:
            base = max(abs(a), abs(b))
            if base == 0:
                return diff == 0
            return (diff / base) <= self.amount_value
        return diff <= self.amount_value

    def is_date_within_tolerance(self, date_a: str, date_b: str) -> bool:
        """判断日期是否在容差范围内."""
        if not self.enabled or self.date_tolerance_days <= 0:
            return False
        from datetime import datetime
        try:
            d1 = datetime.strptime(date_a, "%Y-%m-%d")
            d2 = datetime.strptime(date_b, "%Y-%m-%d")
            return abs((d1 - d2).days) <= self.date_tolerance_days
        except (ValueError, TypeError):
            return False

    def is_partial_match(self, txn_id_a: str, txn_id_b: str, desc_a: str = "", desc_b: str = "") -> bool:
        """判断是否部分匹配（交易号前缀或备注关键字）."""
        if not self.enabled:
            return False
        if self.txn_id_prefixes:
            norm_a = (txn_id_a or "").strip().upper()
            norm_b = (txn_id_b or "").strip().upper()
            for prefix in self.txn_id_prefixes:
                p = prefix.strip().upper()
                if p and (norm_a.startswith(p) or norm_b.startswith(p)):
                    if norm_a.startswith(p) and norm_b.startswith(p):
                        return True
        if self.description_keywords:
            t_a = (desc_a or "").lower()
            t_b = (desc_b or "").lower()
            for kw in self.description_keywords:
                k = kw.lower().strip()
                if k and k in t_a and k in t_b:
                    return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        if not self.enabled and not self.txn_id_prefixes and not self.description_keywords:
            return {}
        d: Dict[str, Any] = {
            "enabled": self.enabled,
        }
        if self.amount_value > 0 or self.amount_is_percent:
            d["amount_tolerance"] = _format_amount_tolerance(self.amount_value, self.amount_is_percent)
        if self.date_tolerance_days > 0:
            d["date_tolerance"] = self.date_tolerance_days
        if self.txn_id_prefixes:
            d["txn_id_prefixes"] = list(self.txn_id_prefixes)
        if self.description_keywords:
            d["description_keywords"] = list(self.description_keywords)
        return d

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ToleranceRules":
        if not data or not isinstance(data, dict):
            return cls.default()
        rules = cls()
        if "enabled" in data:
            v = data["enabled"]
            if not isinstance(v, bool):
                raise RuleValidationError(f"tolerance.enabled 必须是布尔值, 实际: {v}")
            rules.enabled = v
        if "amount_tolerance" in data:
            rules.amount_value, rules.amount_is_percent = _parse_amount_tolerance(data["amount_tolerance"])
            if rules.amount_value > 0:
                rules.enabled = True
        if "date_tolerance" in data:
            v = data["date_tolerance"]
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise RuleValidationError(f"tolerance.date_tolerance 必须是非负整数天数, 实际: {v}")
            rules.date_tolerance_days = v
            if v > 0:
                rules.enabled = True
        if "txn_id_prefixes" in data:
            v = data["txn_id_prefixes"]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise RuleValidationError("tolerance.txn_id_prefixes 必须是字符串列表")
            rules.txn_id_prefixes = list(v)
            if v:
                rules.enabled = True
        if "description_keywords" in data:
            v = data["description_keywords"]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise RuleValidationError("tolerance.description_keywords 必须是字符串列表")
            rules.description_keywords = list(v)
            if v:
                rules.enabled = True

        allowed = {"enabled", "amount_tolerance", "date_tolerance", "txn_id_prefixes", "description_keywords"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise RuleValidationError(
                f"tolerance 段包含未知字段: {sorted(unknown)}; 允许字段: {sorted(allowed)}"
            )
        return rules


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
    tolerance: ToleranceRules = field(default_factory=ToleranceRules.default)

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
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise RuleValidationError(f"amount_tolerance 必须是非负数字, 实际: {v}")
            rules.amount_tolerance = float(v)
        if "date_window_days" in data:
            v = data["date_window_days"]
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
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
        if "tolerance" in data:
            rules.tolerance = ToleranceRules.from_dict(data["tolerance"])
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
        d = {
            "amount_tolerance": self.amount_tolerance,
            "date_window_days": self.date_window_days,
            "require_exact_txn_id": self.require_exact_txn_id,
            "case_sensitive_txn_id": self.case_sensitive_txn_id,
            "manual_review_keywords": list(self.manual_review_keywords),
            "ignore_duplicate_if_amount_differs": self.ignore_duplicate_if_amount_differs,
            "consider_adjustments": self.consider_adjustments,
        }
        tol_dict = self.tolerance.to_dict()
        if tol_dict:
            d["tolerance"] = tol_dict
        return d


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
        "tolerance",
    }
    unknown = set(data.keys()) - allowed
    if unknown:
        raise RuleValidationError(f"规则文件包含未知字段: {sorted(unknown)}; 允许字段: {sorted(allowed)}")

    return MatchRules.from_dict(data)


def validate_rules(file_path: str) -> Tuple[bool, List[str]]:
    """校验规则文件，返回 (是否有效, 错误信息列表)."""
    errors: List[str] = []
    try:
        load_rules(file_path)
        return True, errors
    except RuleValidationError as e:
        errors.append(str(e))
        return False, errors
    except Exception as e:
        errors.append(f"未知错误: {e}")
        return False, errors


def export_rules(rules: MatchRules, file_path: str) -> None:
    """导出规则到 YAML 文件."""
    out_dir = os.path.dirname(os.path.abspath(file_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    data = rules.to_dict()
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def import_rules(file_path: str, existing_rules: Optional[MatchRules] = None,
                 check_conflicts: bool = True) -> Tuple[MatchRules, List[str]]:
    """从 YAML 文件导入规则，返回 (规则, 冲突/警告信息列表).

    如果 check_conflicts=True 且提供了 existing_rules，则对比并报告冲突字段.
    """
    warnings: List[str] = []
    new_rules = load_rules(file_path)

    if check_conflicts and existing_rules is not None:
        old_dict = existing_rules.to_dict()
        new_dict = new_rules.to_dict()
        all_keys = set(old_dict.keys()) | set(new_dict.keys())
        for key in sorted(all_keys):
            old_val = old_dict.get(key)
            new_val = new_dict.get(key)
            if key == "tolerance":
                old_tol = (existing_rules.tolerance.to_dict()
                           if existing_rules.tolerance else {})
                new_tol = (new_rules.tolerance.to_dict()
                           if new_rules.tolerance else {})
                tol_keys = set(old_tol.keys()) | set(new_tol.keys())
                for tk in sorted(tol_keys):
                    ov = old_tol.get(tk)
                    nv = new_tol.get(tk)
                    if ov != nv:
                        warnings.append(
                            f"tolerance.{tk}: {ov!r} -> {nv!r}"
                        )
            elif old_val != new_val:
                warnings.append(f"{key}: {old_val!r} -> {new_val!r}")

    return new_rules, warnings
