"""全局配置模块 - 读取存储目录下的 config.yaml."""
from __future__ import annotations

import os
from typing import Any, Dict

import yaml


DEFAULT_CONFIG: Dict[str, Any] = {
    "audit_retention_days": 90,
}


def config_path(storage_dir: str) -> str:
    return os.path.join(storage_dir, "config.yaml")


def load_config(storage_dir: str) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    path = config_path(storage_dir)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            cfg.update(data)
    return cfg


def save_config(storage_dir: str, cfg: Dict[str, Any]) -> None:
    os.makedirs(storage_dir, exist_ok=True)
    path = config_path(storage_dir)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
