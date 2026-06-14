"""批次快照与恢复模块 - 可迁移打包与恢复.

快照结构 (JSON):
{
  "snapshot_version": "1.0",
  "snapshot_id": "SNAP-XXXXXXXX",
  "created_at": "ISO timestamp",
  "checksum": "SHA-256 hex of payload",
  "batch_id": "...",
  "batch_name": "...",
  "payload": {
    "batch": { ...完整批次数据... },
    "rules_yaml": "规则文件YAML内容 (若批次关联了规则且可读)",
    "audit_records": [ ...该批次的所有审计记录... ],
    "config": {
      "column_aliases": { ... },
      "audit_retention_days": ...,
      "retention_days": ...
    },
    "rollback_history": {
      "discrepancies": { dis_id: [rollback_history] }
    },
    "manual_link_history": [ ... ],
    "exports": [ ... ]
  }
}
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .models import Batch, FileType
from .audit import AuditStorage
from .config import load_config, save_config, FILE_TYPE_ALIAS_KEYS
from .storage import BatchStorage


SNAPSHOT_VERSION = "1.0"
SNAPSHOT_FILE_EXT = ".brsnap"


class ConflictStrategy(str, Enum):
    """恢复冲突处理策略."""
    OVERWRITE = "overwrite"
    RENAME = "rename"
    SKIP = "skip"


class SnapshotError(Exception):
    """快照相关错误基类."""
    pass


class SnapshotCorruptedError(SnapshotError):
    """快照损坏（校验失败）错误."""
    pass


class SnapshotVersionError(SnapshotError):
    """快照版本不兼容错误."""
    pass


class SnapshotConflictError(SnapshotError):
    """恢复冲突错误."""
    pass


def _gen_snapshot_id() -> str:
    return "SNAP-" + uuid.uuid4().hex[:8].upper()


def _compute_checksum(payload: Dict[str, Any]) -> str:
    """计算 payload 的 SHA-256 校验和."""
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _read_rules_content(rule_path: Optional[str]) -> Optional[str]:
    """读取规则文件内容，若不可读返回 None."""
    if not rule_path:
        return None
    try:
        with open(rule_path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _collect_rollback_history(batch: Batch) -> Dict[str, List[Dict[str, Any]]]:
    """收集所有差异的回滚历史."""
    result: Dict[str, List[Dict[str, Any]]] = {}
    for d in batch.discrepancies:
        if d.rollback_history:
            result[d.discrepancy_id] = list(d.rollback_history)
    return result


def _build_payload(
    batch: Batch,
    storage: BatchStorage,
    audit: AuditStorage,
) -> Dict[str, Any]:
    """构建快照 payload（不含 checksum 和版本元数据）."""
    audit_records = audit.query(batch_id=batch.batch_id)
    cfg = load_config(storage.storage_dir)
    rules_content = _read_rules_content(batch.rule_file)

    return {
        "batch": batch.to_dict(),
        "rules_yaml": rules_content,
        "audit_records": audit_records,
        "config": {
            "audit_retention_days": cfg.get("audit_retention_days"),
            "retention_days": cfg.get("retention_days"),
            "column_aliases": {
                ft_key: dict(cfg.get("column_aliases", {}).get(ft_key, {}))
                for ft_key in FILE_TYPE_ALIAS_KEYS.values()
            },
        },
        "rollback_history": _collect_rollback_history(batch),
        "manual_link_history": list(batch.manual_link_history),
        "exports": list(batch.exports),
    }


def create_snapshot(
    batch_id: str,
    storage: BatchStorage,
    output_path: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """创建批次快照.

    Args:
        batch_id: 批次 ID
        storage: BatchStorage 实例
        output_path: 输出文件路径，若为 None 则根据批次信息自动生成

    Returns:
        (输出文件路径, 快照元数据字典)

    Raises:
        FileNotFoundError: 批次不存在
        SnapshotError: 创建过程中出错
    """
    if not storage.batch_exists_anywhere(batch_id):
        raise FileNotFoundError(f"批次不存在: {batch_id}")

    try:
        batch = storage.load(batch_id)
    except Exception as e:
        raise SnapshotError(f"加载批次失败: {e}") from e

    audit = AuditStorage(storage.storage_dir)

    payload = _build_payload(batch, storage, audit)
    checksum = _compute_checksum(payload)

    snapshot: Dict[str, Any] = {
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_id": _gen_snapshot_id(),
        "created_at": datetime.now().isoformat(),
        "checksum": checksum,
        "batch_id": batch.batch_id,
        "batch_name": batch.name,
        "batch_status": batch.status.value,
        "discrepancy_count": len(batch.discrepancies),
        "bank_txn_count": len(batch.bank_txns),
        "system_txn_count": len(batch.system_txns),
        "adjustment_txn_count": len(batch.adjustment_txns),
        "imported_file_count": len(batch.imported_files),
        "audit_record_count": len(payload["audit_records"]),
        "payload": payload,
    }

    if output_path is None:
        out_dir = os.getcwd()
        safe_name = "".join(
            c if c.isalnum() or c in ("-", "_") else "_"
            for c in batch.name
        )
        output_path = os.path.join(
            out_dir,
            f"{batch.batch_id}_{safe_name}{SNAPSHOT_FILE_EXT}"
        )

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    tmp_path = output_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, output_path)
    except Exception as e:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise SnapshotError(f"写入快照文件失败: {e}") from e

    return output_path, {
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_version": snapshot["snapshot_version"],
        "created_at": snapshot["created_at"],
        "checksum": checksum,
        "batch_id": snapshot["batch_id"],
        "batch_name": snapshot["batch_name"],
        "batch_status": snapshot.get("batch_status"),
        "discrepancy_count": snapshot.get("discrepancy_count", 0),
        "bank_txn_count": snapshot.get("bank_txn_count", 0),
        "system_txn_count": snapshot.get("system_txn_count", 0),
        "adjustment_txn_count": snapshot.get("adjustment_txn_count", 0),
        "imported_file_count": snapshot.get("imported_file_count", 0),
        "audit_record_count": snapshot.get("audit_record_count", 0),
        "output_path": os.path.abspath(output_path),
        "file_size_bytes": os.path.getsize(output_path),
    }


def read_snapshot_info(snapshot_path: str) -> Dict[str, Any]:
    """读取快照元信息（不展开 payload，快速查看）.

    同时进行完整性校验：
    - 文件是否是合法 JSON
    - 必需字段是否存在
    - checksum 是否匹配

    Returns:
        快照元信息字典（不含 payload 本身）

    Raises:
        FileNotFoundError: 文件不存在
        SnapshotCorruptedError: 文件损坏或校验失败
        SnapshotVersionError: 版本不兼容
    """
    if not os.path.isfile(snapshot_path):
        raise FileNotFoundError(f"快照文件不存在: {snapshot_path}")

    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except json.JSONDecodeError as e:
        raise SnapshotCorruptedError(f"快照文件不是合法 JSON: {e}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise SnapshotCorruptedError(f"读取快照文件失败: {e}") from e

    required_top = [
        "snapshot_version", "snapshot_id", "created_at",
        "checksum", "batch_id", "batch_name", "payload",
    ]
    for key in required_top:
        if key not in snapshot:
            raise SnapshotCorruptedError(f"快照缺少必需字段: {key}")

    version = snapshot["snapshot_version"]
    if not version.startswith("1."):
        raise SnapshotVersionError(
            f"快照版本 {version} 不兼容，当前工具仅支持 1.x 系列"
        )

    payload = snapshot["payload"]
    if not isinstance(payload, dict):
        raise SnapshotCorruptedError("快照 payload 不是对象")
    for key in ("batch", "audit_records", "config"):
        if key not in payload:
            raise SnapshotCorruptedError(f"快照 payload 缺少字段: {key}")

    actual_checksum = _compute_checksum(payload)
    if actual_checksum != snapshot["checksum"]:
        raise SnapshotCorruptedError(
            f"快照校验失败: 期望 {snapshot['checksum']}，实际 {actual_checksum}"
        )

    info = {k: v for k, v in snapshot.items() if k != "payload"}
    info["file_path"] = os.path.abspath(snapshot_path)
    info["file_size_bytes"] = os.path.getsize(snapshot_path)
    batch_data = payload["batch"]
    info["payload_summary"] = {
        "has_rules_yaml": payload.get("rules_yaml") is not None,
        "audit_record_count": len(payload.get("audit_records", [])),
        "config_keys": list(payload.get("config", {}).keys()),
        "rollback_history_discrepancies": len(payload.get("rollback_history", {})),
        "manual_link_history_count": len(payload.get("manual_link_history", [])),
        "exports_count": len(payload.get("exports", [])),
        "created_at": batch_data.get("created_at"),
        "updated_at": batch_data.get("updated_at"),
        "status": batch_data.get("status"),
        "rule_file": batch_data.get("rule_file"),
    }
    return info


def _validate_and_load_snapshot(snapshot_path: str) -> Dict[str, Any]:
    """校验并加载完整快照数据（供 restore 内部使用）.

    Returns:
        完整的 snapshot 字典

    Raises:
        FileNotFoundError, SnapshotCorruptedError, SnapshotVersionError
    """
    if not os.path.isfile(snapshot_path):
        raise FileNotFoundError(f"快照文件不存在: {snapshot_path}")

    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except json.JSONDecodeError as e:
        raise SnapshotCorruptedError(f"快照文件不是合法 JSON: {e}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise SnapshotCorruptedError(f"读取快照文件失败: {e}") from e

    required_top = [
        "snapshot_version", "snapshot_id", "created_at",
        "checksum", "batch_id", "batch_name", "payload",
    ]
    for key in required_top:
        if key not in snapshot:
            raise SnapshotCorruptedError(f"快照缺少必需字段: {key}")

    version = snapshot["snapshot_version"]
    if not version.startswith("1."):
        raise SnapshotVersionError(
            f"快照版本 {version} 不兼容，当前工具仅支持 1.x 系列"
        )

    payload = snapshot["payload"]
    if not isinstance(payload, dict):
        raise SnapshotCorruptedError("快照 payload 不是对象")
    for key in ("batch", "audit_records", "config"):
        if key not in payload:
            raise SnapshotCorruptedError(f"快照 payload 缺少字段: {key}")

    actual_checksum = _compute_checksum(payload)
    if actual_checksum != snapshot["checksum"]:
        raise SnapshotCorruptedError(
            f"快照校验失败: 期望 {snapshot['checksum']}，实际 {actual_checksum}"
        )

    return snapshot


def _merge_config_into_target(
    target_storage_dir: str,
    snapshot_config: Dict[str, Any],
) -> None:
    """将快照中的配置合并到目标目录.

    策略：列别名以目标现有为准（不覆盖），保留天数等基础配置仅
    在目标为默认值时才应用快照值（避免意外覆盖用户习惯配置）。
    """
    existing_cfg = load_config(target_storage_dir)
    has_changes = False
    new_cfg = {
        "audit_retention_days": existing_cfg["audit_retention_days"],
        "retention_days": existing_cfg["retention_days"],
        "column_aliases": {
            ft_key: dict(existing_cfg["column_aliases"].get(ft_key, {}))
            for ft_key in FILE_TYPE_ALIAS_KEYS.values()
        },
    }

    snap_audit_days = snapshot_config.get("audit_retention_days")
    if (snap_audit_days is not None
            and existing_cfg["audit_retention_days"] == 90
            and snap_audit_days != 90):
        new_cfg["audit_retention_days"] = snap_audit_days
        has_changes = True

    snap_retention = snapshot_config.get("retention_days")
    if (snap_retention is not None
            and existing_cfg["retention_days"] == 365
            and snap_retention != 365):
        new_cfg["retention_days"] = snap_retention
        has_changes = True

    snap_aliases = snapshot_config.get("column_aliases", {}) or {}
    for ft_key in FILE_TYPE_ALIAS_KEYS.values():
        snap_ft = snap_aliases.get(ft_key, {}) or {}
        existing_ft = new_cfg["column_aliases"][ft_key]
        for alias, std in snap_ft.items():
            if alias not in existing_ft:
                existing_ft[alias] = std
                has_changes = True

    if has_changes:
        save_config(target_storage_dir, new_cfg)


def _restore_rules_yaml(
    rules_yaml: Optional[str],
    target_storage_dir: str,
    batch_id: str,
) -> Optional[str]:
    """将快照中的 rules_yaml 内容写回目标目录的规则文件.

    Returns:
        恢复后的规则文件绝对路径，若没有规则内容则返回 None
    """
    if not rules_yaml:
        return None

    rules_dir = os.path.join(target_storage_dir, "restored_rules")
    os.makedirs(rules_dir, exist_ok=True)
    rules_path = os.path.join(rules_dir, f"{batch_id}_rules.yaml")

    with open(rules_path, "w", encoding="utf-8") as f:
        f.write(rules_yaml)

    return os.path.abspath(rules_path)


def _write_audit_records(
    target_audit: AuditStorage,
    records: List[Dict[str, Any]],
) -> int:
    """将快照中的审计记录批量写回目标审计库（不覆盖已存在 ID）."""
    if not records:
        return 0

    conn = target_audit._connect()
    try:
        existing_ids = set(
            r["id"] for r in conn.execute(
                "SELECT id FROM audit_log"
            ).fetchall()
        )
        inserted = 0
        for rec in records:
            if "id" in rec and rec["id"] in existing_ids:
                continue
            cur = conn.execute(
                "INSERT INTO audit_log (timestamp, command, batch_id, affected, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    rec.get("timestamp", datetime.now().isoformat()),
                    rec.get("command", "snapshot_restore"),
                    rec.get("batch_id", ""),
                    rec.get("affected", 0),
                    rec.get("summary", ""),
                ),
            )
            inserted += 1
            existing_ids.add(cur.lastrowid)
        conn.commit()
        return inserted
    finally:
        conn.close()


def _generate_new_batch_id() -> str:
    """生成新的批次 ID."""
    return "BATCH-" + uuid.uuid4().hex[:8].upper()


def restore_snapshot(
    snapshot_path: str,
    target_storage: BatchStorage,
    strategy: ConflictStrategy = ConflictStrategy.SKIP,
    new_name: Optional[str] = None,
) -> Dict[str, Any]:
    """恢复快照到目标存储目录.

    Args:
        snapshot_path: 快照文件路径
        target_storage: 目标 BatchStorage
        strategy: 冲突策略
        new_name: 当策略为 RENAME 时，指定新批次名（可选，自动追加后缀也可）

    Returns:
        恢复结果字典，包含 batch_id, batch_name, strategy,
        rules_restored, audit_inserted, config_merged 等信息

    Raises:
        FileNotFoundError: 快照文件不存在
        SnapshotCorruptedError: 快照损坏
        SnapshotVersionError: 版本不兼容
        SnapshotConflictError: 策略为 SKIP 时遇到冲突
    """
    snapshot = _validate_and_load_snapshot(snapshot_path)
    payload = snapshot["payload"]

    try:
        batch_data = payload["batch"]
        original_batch_id = batch_data["batch_id"]
        original_batch_name = batch_data["name"]
    except (KeyError, TypeError) as e:
        raise SnapshotCorruptedError(f"快照中批次数据损坏: {e}") from e

    final_batch_id = original_batch_id
    final_batch_name = new_name or original_batch_name
    applied_strategy = strategy

    conflict_id = target_storage.batch_exists_anywhere(original_batch_id)
    conflict_name = False
    for b in target_storage.list_all_batches():
        if b["name"] == final_batch_name and b["batch_id"] != original_batch_id:
            conflict_name = True
            break

    has_conflict = conflict_id or conflict_name

    if strategy == ConflictStrategy.RENAME:
        final_batch_id = _generate_new_batch_id()
        if new_name:
            final_batch_name = new_name
        else:
            suffix = datetime.now().strftime("%Y%m%d%H%M%S")
            final_batch_name = f"{original_batch_name}_restored_{suffix}"
        applied_strategy = ConflictStrategy.RENAME
    elif has_conflict:
        if strategy == ConflictStrategy.SKIP:
            raise SnapshotConflictError(
                f"冲突：目标存储中已存在 "
                f"{'同ID批次 ' + original_batch_id if conflict_id else ''}"
                f"{' 和 ' if conflict_id and conflict_name else ''}"
                f"{'同名批次 "' + final_batch_name + '"' if conflict_name else ''}"
                f"。使用 --strategy overwrite|rename 解决冲突。"
            )
        elif strategy == ConflictStrategy.OVERWRITE:
            if target_storage.batch_exists(original_batch_id):
                target_storage.delete(original_batch_id)
            if target_storage.batch_is_archived(original_batch_id):
                target_storage.delete_archived(original_batch_id)
    else:
        applied_strategy = ConflictStrategy.SKIP

    restored_batch_data = dict(batch_data)
    restored_batch_data["batch_id"] = final_batch_id
    restored_batch_data["name"] = final_batch_name

    try:
        batch_to_save = Batch.from_dict(restored_batch_data)
    except (KeyError, ValueError, TypeError) as e:
        raise SnapshotCorruptedError(f"批次数据反序列化失败: {e}") from e

    rules_restored = None
    if payload.get("rules_yaml"):
        rules_restored = _restore_rules_yaml(
            payload["rules_yaml"],
            target_storage.storage_dir,
            final_batch_id,
        )
        if rules_restored:
            batch_to_save.rule_file = rules_restored

    target_storage.save(batch_to_save)

    target_audit = AuditStorage(target_storage.storage_dir)
    snapshot_audit_records = payload.get("audit_records", []) or []
    if final_batch_id != original_batch_id:
        for rec in snapshot_audit_records:
            if rec.get("batch_id") == original_batch_id:
                rec["batch_id"] = final_batch_id
    audit_inserted = _write_audit_records(target_audit, snapshot_audit_records)

    _merge_config_into_target(target_storage.storage_dir, payload.get("config", {}) or {})

    result = {
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_path": os.path.abspath(snapshot_path),
        "original_batch_id": original_batch_id,
        "original_batch_name": original_batch_name,
        "batch_id": final_batch_id,
        "batch_name": final_batch_name,
        "strategy": applied_strategy.value,
        "rules_restored": rules_restored is not None,
        "rules_path": rules_restored,
        "audit_inserted": audit_inserted,
        "config_merged": True,
        "storage_dir": target_storage.storage_dir,
    }
    return result
