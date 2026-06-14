"""状态存储模块 - 批次持久化, 支持 resume."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import List, Optional

from .models import Batch


DEFAULT_STORAGE_DIR = os.path.join(os.getcwd(), ".bank_reconcile")


class BatchStorage:
    """批次存储 - 基于本地 JSON 文件."""

    def __init__(self, storage_dir: Optional[str] = None):
        self.storage_dir = storage_dir or DEFAULT_STORAGE_DIR
        self.batches_dir = os.path.join(self.storage_dir, "batches")
        os.makedirs(self.batches_dir, exist_ok=True)

    def _batch_path(self, batch_id: str) -> str:
        return os.path.join(self.batches_dir, f"{batch_id}.json")

    def list_batches(self) -> List[dict]:
        """列出所有批次的摘要信息."""
        results = []
        if not os.path.isdir(self.batches_dir):
            return results
        for fname in sorted(os.listdir(self.batches_dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.batches_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                results.append({
                    "batch_id": data.get("batch_id"),
                    "name": data.get("name"),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "discrepancy_count": len(data.get("discrepancies", [])),
                    "imported_files": len(data.get("imported_files", [])),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return results

    def batch_exists(self, batch_id: str) -> bool:
        return os.path.isfile(self._batch_path(batch_id))

    def load(self, batch_id: str) -> Batch:
        """加载批次 - resume 时使用."""
        path = self._batch_path(batch_id)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"批次不存在: {batch_id}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Batch.from_dict(data)

    def save(self, batch: Batch) -> None:
        """保存批次到磁盘."""
        batch.touch()
        path = self._batch_path(batch.batch_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(batch.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def delete(self, batch_id: str) -> bool:
        path = self._batch_path(batch_id)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    def record_export(self, batch: Batch, export_path: str, export_type: str) -> None:
        """记录一次导出操作."""
        batch.exports.append({
            "export_type": export_type,
            "file_path": os.path.abspath(export_path),
            "exported_at": datetime.now().isoformat(),
            "discrepancy_count": len(batch.discrepancies),
        })
        self.save(batch)
