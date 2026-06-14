"""状态存储模块 - 批次持久化, 支持 resume / archive / restore."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from .models import Batch


DEFAULT_STORAGE_DIR = os.path.join(os.getcwd(), ".bank_reconcile")


class BatchStorage:
    """批次存储 - 基于本地 JSON 文件, 支持归档目录."""

    def __init__(self, storage_dir: Optional[str] = None):
        self.storage_dir = storage_dir or DEFAULT_STORAGE_DIR
        self.batches_dir = os.path.join(self.storage_dir, "batches")
        self.archive_dir = os.path.join(self.storage_dir, "archive")
        os.makedirs(self.batches_dir, exist_ok=True)
        os.makedirs(self.archive_dir, exist_ok=True)

    def _batch_path(self, batch_id: str) -> str:
        return os.path.join(self.batches_dir, f"{batch_id}.json")

    def _archive_path(self, batch_id: str) -> str:
        return os.path.join(self.archive_dir, f"{batch_id}.json")

    def list_batches(self) -> List[dict]:
        """列出所有批次的摘要信息（仅正常目录）."""
        results = []
        results.extend(self._list_dir(self.batches_dir))
        return results

    def list_archived_batches(self) -> List[dict]:
        """列出归档目录中的批次摘要信息."""
        return self._list_dir(self.archive_dir)

    def list_all_batches(self) -> List[dict]:
        """列出所有批次（含归档）."""
        results = self._list_dir(self.batches_dir)
        results.extend(self._list_dir(self.archive_dir))
        return results

    def _list_dir(self, directory: str) -> List[dict]:
        results = []
        if not os.path.isdir(directory):
            return results
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(directory, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                results.append({
                    "batch_id": data.get("batch_id"),
                    "name": data.get("name"),
                    "status": data.get("status", "open"),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "discrepancy_count": len(data.get("discrepancies", [])),
                    "imported_files": len(data.get("imported_files", [])),
                    "location": "archive" if directory == self.archive_dir else "active",
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return results

    def batch_exists(self, batch_id: str) -> bool:
        return os.path.isfile(self._batch_path(batch_id))

    def batch_is_archived(self, batch_id: str) -> bool:
        return os.path.isfile(self._archive_path(batch_id))

    def batch_exists_anywhere(self, batch_id: str) -> bool:
        return self.batch_exists(batch_id) or self.batch_is_archived(batch_id)

    def load(self, batch_id: str) -> Batch:
        """加载批次 - 优先正常目录，不存在则查归档目录."""
        path = self._batch_path(batch_id)
        if not os.path.isfile(path):
            path = self._archive_path(batch_id)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"批次不存在: {batch_id}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Batch.from_dict(data)

    def save(self, batch: Batch) -> None:
        """保存批次到磁盘（保存到它所在的目录）."""
        batch.touch()
        if batch.is_archived and self.batch_is_archived(batch.batch_id):
            path = self._archive_path(batch.batch_id)
        else:
            path = self._batch_path(batch.batch_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(batch.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def archive_batch(self, batch_id: str) -> Batch:
        """将批次从正常目录移到归档目录，并将状态置为 archived."""
        path = self._batch_path(batch_id)
        if not os.path.isfile(path):
            if self.batch_is_archived(batch_id):
                batch = self.load(batch_id)
                return batch
            raise FileNotFoundError(f"批次不存在: {batch_id}")

        batch = self.load(batch_id)
        batch.archive()
        batch.touch()

        archive_path = self._archive_path(batch_id)
        tmp = archive_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(batch.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, archive_path)
        os.remove(path)
        return batch

    def restore_batch(self, batch_id: str) -> Batch:
        """将批次从归档目录移回正常目录，恢复为 closed 状态."""
        path = self._archive_path(batch_id)
        if not os.path.isfile(path):
            if self.batch_exists(batch_id):
                batch = self.load(batch_id)
                return batch
            raise FileNotFoundError(f"归档批次不存在: {batch_id}")

        batch = self.load(batch_id)
        batch.unarchive()
        batch.touch()

        active_path = self._batch_path(batch_id)
        tmp = active_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(batch.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, active_path)
        os.remove(path)
        return batch

    def delete(self, batch_id: str) -> bool:
        path = self._batch_path(batch_id)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    def delete_archived(self, batch_id: str) -> bool:
        path = self._archive_path(batch_id)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    def list_expired_archives(self, retention_days: int) -> List[Dict[str, Any]]:
        """列出归档目录中超期的批次（仅预览，不删除）."""
        expired = []
        if retention_days <= 0:
            return expired
        cutoff = datetime.now() - timedelta(days=retention_days)
        if not os.path.isdir(self.archive_dir):
            return expired
        for fname in sorted(os.listdir(self.archive_dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.archive_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                updated_at_str = data.get("updated_at") or data.get("created_at")
                try:
                    updated_at = datetime.fromisoformat(updated_at_str)
                except (ValueError, TypeError):
                    mtime = datetime.fromtimestamp(os.path.getmtime(path))
                    updated_at = mtime
                if updated_at < cutoff:
                    expired.append({
                        "batch_id": data.get("batch_id"),
                        "name": data.get("name"),
                        "archived_at": updated_at_str,
                        "path": path,
                        "size_bytes": os.path.getsize(path),
                    })
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        return expired

    def cleanup_archives(self, retention_days: int, force: bool = False) -> List[Dict[str, Any]]:
        """清理超期归档批次，force=False 时仅预览."""
        expired = self.list_expired_archives(retention_days)
        if force:
            for item in expired:
                try:
                    os.remove(item["path"])
                except OSError:
                    pass
        return expired

    def record_export(self, batch: Batch, export_path: str, export_type: str) -> None:
        """记录一次导出操作."""
        batch.exports.append({
            "export_type": export_type,
            "file_path": os.path.abspath(export_path),
            "exported_at": datetime.now().isoformat(),
            "discrepancy_count": len(batch.discrepancies),
        })
        self.save(batch)
