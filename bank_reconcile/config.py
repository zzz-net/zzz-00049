"""全局配置模块 - 读取存储目录下的 config.yaml."""
from __future__ import annotations

import os
from typing import Any, Dict

import yaml

from .models import FileType


STANDARD_FIELDS = {"txn_id", "amount", "date", "counterparty", "description", "currency"}

FILE_TYPE_ALIAS_KEYS = {
    FileType.BANK_STATEMENT: "bank_statement",
    FileType.SYSTEM_RECEIPT: "system_receipt",
    FileType.MANUAL_ADJUSTMENT: "manual_adjustment",
}


DEFAULT_CONFIG: Dict[str, Any] = {
    "audit_retention_days": 90,
    "column_aliases": {
        "bank_statement": {},
        "system_receipt": {},
        "manual_adjustment": {},
    },
}


class AliasConflictError(ValueError):
    """列名别名冲突异常."""
    pass


def config_path(storage_dir: str) -> str:
    return os.path.join(storage_dir, "config.yaml")


def load_config(storage_dir: str) -> Dict[str, Any]:
    cfg = {
        "audit_retention_days": DEFAULT_CONFIG["audit_retention_days"],
        "column_aliases": {
            "bank_statement": {},
            "system_receipt": {},
            "manual_adjustment": {},
        },
    }
    path = config_path(storage_dir)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            if "audit_retention_days" in data:
                cfg["audit_retention_days"] = data["audit_retention_days"]
            if "column_aliases" in data and isinstance(data["column_aliases"], dict):
                for ft_key in FILE_TYPE_ALIAS_KEYS.values():
                    if ft_key in data["column_aliases"] and isinstance(data["column_aliases"][ft_key], dict):
                        cfg["column_aliases"][ft_key] = dict(data["column_aliases"][ft_key])
    return cfg


def save_config(storage_dir: str, cfg: Dict[str, Any]) -> None:
    os.makedirs(storage_dir, exist_ok=True)
    path = config_path(storage_dir)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


def validate_column_aliases(aliases: Dict[str, str]) -> None:
    """校验同一文件类型内的别名映射是否存在冲突（两个别名指向同一标准字段）。

    Args:
        aliases: {别名: 标准字段} 映射字典

    Raises:
        AliasConflictError: 当多个别名映射到同一个标准字段时
    """
    reverse: Dict[str, str] = {}
    for alias, std_field in aliases.items():
        if std_field not in STANDARD_FIELDS:
            raise AliasConflictError(
                f"别名 '{alias}' 指向了未知的标准字段 '{std_field}'。"
                f"有效的标准字段: {sorted(STANDARD_FIELDS)}"
            )
        if std_field in reverse:
            raise AliasConflictError(
                f"冲突：别名 '{reverse[std_field]}' 和 '{alias}' 都指向标准字段 "
                f"'{std_field}'，同一文件类型内每个标准字段只能有一个别名。"
            )
        reverse[std_field] = alias


def set_column_alias(
    storage_dir: str,
    file_type: FileType,
    alias_name: str,
    standard_field: str,
) -> Dict[str, Any]:
    """设置单个列名别名，写入 config.yaml。冲突时抛 AliasConflictError。"""
    if standard_field not in STANDARD_FIELDS:
        raise AliasConflictError(
            f"标准字段 '{standard_field}' 无效。有效的标准字段: {sorted(STANDARD_FIELDS)}"
        )

    cfg = load_config(storage_dir)
    ft_key = FILE_TYPE_ALIAS_KEYS[file_type]
    aliases = dict(cfg["column_aliases"][ft_key])

    existing = aliases.get(alias_name)
    if existing == standard_field:
        return cfg

    aliases[alias_name] = standard_field
    validate_column_aliases(aliases)

    cfg["column_aliases"][ft_key] = aliases
    save_config(storage_dir, cfg)
    return cfg


def get_column_aliases(cfg: Dict[str, Any], file_type: FileType) -> Dict[str, str]:
    """从配置中取出指定文件类型的列名别名映射 {别名: 标准字段}."""
    ft_key = FILE_TYPE_ALIAS_KEYS[file_type]
    ca = cfg.get("column_aliases", {})
    result = ca.get(ft_key, {})
    return dict(result) if isinstance(result, dict) else {}
