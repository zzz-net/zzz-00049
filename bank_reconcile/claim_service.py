"""认领工作台服务层 - 校验、导出检查、列表格式化、审计日志的统一入口.

保证 CLI 入口和测试入口走同一套逻辑，外部命令名和基本用法不变。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple

from .claim import (
    ClaimStorage,
    ClaimStatus,
    ClaimRecord,
    ClaimError,
    AlreadyClaimedError,
    OperatorMissingError,
    ExportPathConflictError,
)
from .storage import BatchStorage
from .audit import AuditStorage
from .config import load_config
from .models import Batch, BatchStatus


@dataclass
class ClaimTakeResult:
    successes: List[ClaimRecord] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    batch_id: str = ""
    claimant: str = ""
    audit_ids: List[int] = field(default_factory=list)


@dataclass
class ClaimReleaseResult:
    successes: List[ClaimRecord] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    batch_id: str = ""
    operator: str = ""
    force: bool = False
    audit_ids: List[int] = field(default_factory=list)


@dataclass
class ClaimListResult:
    records: List[ClaimRecord] = field(default_factory=list)
    display_records: List[ClaimRecord] = field(default_factory=list)
    total_count: int = 0
    by_status: Dict[str, int] = field(default_factory=dict)
    by_claimant: Dict[str, int] = field(default_factory=dict)
    batch_id: Optional[str] = None
    claimant: Optional[str] = None
    status: Optional[str] = None
    audit_id: int = 0


@dataclass
class ClaimExportResult:
    count: int = 0
    output_path: str = ""
    fmt: str = ""
    batch_id: Optional[str] = None
    claimant: Optional[str] = None
    status: Optional[str] = None
    audit_ids: List[int] = field(default_factory=list)


class BatchValidationError(ClaimError):
    """批次校验失败（不存在/已关闭/已归档）."""
    pass


class DiscrepancyValidationError(ClaimError):
    """差异ID校验失败."""
    pass


class ClaimService:
    """认领工作台统一服务层.

    收拢认领校验、导出前检查、列表展示格式、审计日志，
    CLI 入口和测试入口共用此层。
    """

    def __init__(self, storage_dir: str) -> None:
        self.storage_dir = storage_dir
        self.storage = BatchStorage(storage_dir)
        self.claim = ClaimStorage(storage_dir)
        self.audit = AuditStorage(storage_dir)

    def _log_audit(self, command: str, batch_id: str, affected: int, summary: str) -> int:
        return self.audit.log(command, batch_id, affected, summary)

    def _maybe_cleanup_audit(self) -> None:
        cfg = load_config(self.storage_dir)
        days = cfg.get("audit_retention_days", 90)
        if days and days > 0:
            try:
                self.audit.cleanup(days)
            except Exception:
                pass

    def validate_batch_writable(self, batch_id: str, action: str) -> Batch:
        """校验批次存在且可写（非关闭、非归档）.

        Raises:
            BatchValidationError: 批次不存在 / 已关闭 / 已归档
        """
        if not self.storage.batch_exists_anywhere(batch_id):
            raise BatchValidationError(f"批次不存在: {batch_id}")
        batch = self.storage.load(batch_id)
        if batch.is_closed:
            raise BatchValidationError(f"批次已关闭（closed），禁止{action}。请先 reopen 后再操作。")
        if batch.is_archived:
            raise BatchValidationError(
                f"批次已归档（archived），禁止{action}。\n"
                f"  请先执行 bank-reconcile batch restore {batch_id} 恢复后再操作。"
            )
        return batch

    def validate_batch_readable(self, batch_id: str) -> Batch:
        """校验批次存在（仅读操作用，不检查关闭/归档）.

        Raises:
            BatchValidationError: 批次不存在
        """
        if not self.storage.batch_exists_anywhere(batch_id):
            raise BatchValidationError(f"批次不存在: {batch_id}")
        return self.storage.load(batch_id)

    @staticmethod
    def parse_id_list(raw: str) -> List[str]:
        """解析逗号分隔的ID列表."""
        return [s.strip() for s in raw.split(",") if s.strip()]

    @staticmethod
    def validate_discrepancy_ids(
        batch: Batch, discrepancy_ids: List[str]
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """校验差异ID是否存在于批次中，返回(有效ID列表, 失败列表)."""
        existing = {d.discrepancy_id for d in batch.discrepancies}
        valid: List[str] = []
        invalid: List[Dict[str, Any]] = []
        for did in discrepancy_ids:
            did = did.strip()
            if not did:
                continue
            if did in existing:
                valid.append(did)
            else:
                invalid.append({"discrepancy_id": did, "reason": "差异不存在于该批次"})
        return valid, invalid

    def do_take(
        self,
        batch_id: str,
        discrepancy_ids_raw: str,
        claimant: str,
        expires_hours: Optional[int] = None,
        note: str = "",
    ) -> ClaimTakeResult:
        """批量认领差异 - 含完整校验、业务逻辑、审计.

        Returns:
            ClaimTakeResult 包含成功/失败列表和审计ID
        """
        result = ClaimTakeResult(batch_id=batch_id, claimant=claimant)

        try:
            batch = self.validate_batch_writable(batch_id, "认领")
        except BatchValidationError as e:
            self._log_audit("claim_take_fail", batch_id, 0, f"认领失败: {e}")
            raise

        disp_ids = self.parse_id_list(discrepancy_ids_raw)
        if not disp_ids:
            self._log_audit("claim_take_fail", batch_id, 0, "认领失败: 未提供差异ID")
            raise DiscrepancyValidationError("请提供至少一个有效的差异ID")

        valid_ids, invalid = self.validate_discrepancy_ids(batch, disp_ids)
        if not valid_ids:
            for f in invalid:
                pass
            self._log_audit(
                "claim_take_fail", batch_id, 0,
                f"认领失败: 无有效差异ID, 认领人 {claimant}"
            )
            raise DiscrepancyValidationError(
                "没有有效的差异ID: " + "; ".join(
                    f["discrepancy_id"] + ": " + f["reason"] for f in invalid
                )
            )

        try:
            successes, failures = self.claim.take(
                batch_id, valid_ids, claimant,
                expires_hours=expires_hours, note=note,
            )
        except OperatorMissingError as e:
            self._log_audit("claim_take_fail", batch_id, 0, f"认领失败: {e}")
            raise
        except ClaimError as e:
            self._log_audit("claim_take_fail", batch_id, 0, f"认领失败: {e}")
            raise

        result.successes = successes
        result.failures = invalid + failures

        if successes:
            success_ids = ",".join(s.discrepancy_id for s in successes)
            summary = (
                f"成功认领 {len(successes)} 条: {success_ids}, 认领人 {claimant}"
                + (f", 过期 {expires_hours}h" if expires_hours else "")
                + (f", 备注: {note}" if note else "")
            )
            aid = self._log_audit("claim_take", batch_id, len(successes), summary)
            result.audit_ids.append(aid)

        if result.failures:
            fail_ids = ",".join(
                f"{f['discrepancy_id']}({f['reason']})" for f in result.failures
            )
            aid = self._log_audit(
                "claim_take_fail", batch_id, len(result.failures),
                f"认领失败 {len(result.failures)} 条: {fail_ids}, 认领人 {claimant}"
            )
            result.audit_ids.append(aid)

        self._maybe_cleanup_audit()
        return result

    def do_release(
        self,
        batch_id: str,
        discrepancy_ids_raw: str,
        operator: str,
        reason: str = "",
        force: bool = False,
    ) -> ClaimReleaseResult:
        """释放认领 - 含完整校验、业务逻辑、审计."""
        result = ClaimReleaseResult(
            batch_id=batch_id, operator=operator, force=force
        )

        try:
            batch = self.validate_batch_writable(batch_id, "释放认领")
        except BatchValidationError as e:
            self._log_audit("claim_release_fail", batch_id, 0, f"释放失败: {e}")
            raise

        disp_ids = self.parse_id_list(discrepancy_ids_raw)
        if not disp_ids:
            self._log_audit("claim_release_fail", batch_id, 0, "释放失败: 未提供差异ID")
            raise DiscrepancyValidationError("请提供至少一个有效的差异ID")

        valid_ids, invalid = self.validate_discrepancy_ids(batch, disp_ids)
        if not valid_ids:
            self._log_audit(
                "claim_release_fail", batch_id, 0,
                f"释放失败: 无有效差异ID, 操作人 {operator}"
            )
            raise DiscrepancyValidationError(
                "没有有效的差异ID: " + "; ".join(
                    f["discrepancy_id"] + ": " + f["reason"] for f in invalid
                )
            )

        try:
            successes, failures = self.claim.release(
                batch_id, valid_ids, operator, reason=reason, force=force
            )
        except (OperatorMissingError, ClaimError) as e:
            self._log_audit("claim_release_fail", batch_id, 0, f"释放失败: {e}")
            raise

        result.successes = successes
        result.failures = invalid + failures

        if successes:
            success_ids = ",".join(s.discrepancy_id for s in successes)
            cmd_type = "claim_release_force" if force else "claim_release"
            summary = (
                f"释放成功 {len(successes)} 条: {success_ids}, 操作人 {operator}"
                f", 类型={'强制' if force else '本人'}"
                + (f", 原因: {reason}" if reason else "")
            )
            aid = self._log_audit(cmd_type, batch_id, len(successes), summary)
            result.audit_ids.append(aid)

        if result.failures:
            fail_ids = ",".join(
                f"{f['discrepancy_id']}({f['reason']})" for f in result.failures
            )
            aid = self._log_audit(
                "claim_release_fail", batch_id, len(result.failures),
                f"释放失败 {len(result.failures)} 条: {fail_ids}, 操作人 {operator}"
            )
            result.audit_ids.append(aid)

        self._maybe_cleanup_audit()
        return result

    def do_list(
        self,
        batch_id: Optional[str] = None,
        claimant: Optional[str] = None,
        status: Optional[ClaimStatus] = None,
        limit: int = 50,
    ) -> ClaimListResult:
        """查询认领记录 - 含校验、ensure_pending、汇总统计、审计."""
        result = ClaimListResult(
            batch_id=batch_id, claimant=claimant,
            status=status.value if status else None,
        )

        if batch_id:
            try:
                batch = self.validate_batch_readable(batch_id)
            except BatchValidationError as e:
                self._log_audit(
                    "claim_list_fail", batch_id, 0,
                    f"列表查询失败: {e}"
                )
                raise
            all_disp_ids = [d.discrepancy_id for d in batch.discrepancies]
            self.claim.ensure_pending_for_batch(batch_id, all_disp_ids)

        records = self.claim.list(batch_id=batch_id, claimant=claimant, status=status)

        result.records = records
        result.total_count = len(records)
        result.display_records = records[:limit]

        by_status: Dict[str, int] = {}
        by_claimant: Dict[str, int] = {}
        for r in records:
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
            if r.claimant:
                by_claimant[r.claimant] = by_claimant.get(r.claimant, 0) + 1
        result.by_status = by_status
        result.by_claimant = by_claimant

        status_label = status.value if status else "全部"
        summary = (
            f"查询认领记录: 批次={batch_id or '全部'}, "
            f"认领人={claimant or '全部'}, "
            f"状态={status_label}, "
            f"共 {len(records)} 条"
        )
        result.audit_id = self._log_audit(
            "claim_list", batch_id or "ALL", len(records), summary
        )

        self._maybe_cleanup_audit()
        return result

    def do_export(
        self,
        output: str,
        fmt: str,
        batch_id: Optional[str] = None,
        claimant: Optional[str] = None,
        status: Optional[ClaimStatus] = None,
    ) -> ClaimExportResult:
        """导出交接清单 - 含路径冲突检查、校验、审计.

        Raises:
            ExportPathConflictError: 输出路径是目录或文件已存在
            BatchValidationError: 批次不存在
        """
        result = ClaimExportResult(
            output_path=output, fmt=fmt,
            batch_id=batch_id, claimant=claimant,
            status=status.value if status else None,
        )

        if batch_id:
            try:
                batch = self.validate_batch_readable(batch_id)
            except BatchValidationError as e:
                self._log_audit(
                    "claim_export_fail", batch_id or "ALL", 0,
                    f"导出失败: {e}"
                )
                raise

        abs_output = os.path.abspath(output)
        if os.path.isdir(abs_output):
            self._log_audit(
                "claim_export_fail", batch_id or "ALL", 0,
                f"导出失败: 路径是目录 {output}"
            )
            raise ExportPathConflictError(f"导出路径冲突: '{output}' 是一个目录，请指定文件路径")

        out_dir = os.path.dirname(abs_output)
        if out_dir and not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as e:
                self._log_audit(
                    "claim_export_fail", batch_id or "ALL", 0,
                    f"导出失败: 目录创建失败 {e}"
                )
                raise

        if os.path.isfile(abs_output):
            self._log_audit(
                "claim_export_fail", batch_id or "ALL", 0,
                f"导出失败: 文件已存在 {abs_output}"
            )
            raise ExportPathConflictError(
                f"导出路径冲突: 文件已存在 '{abs_output}'，请指定新的输出路径"
            )

        if batch_id and self.storage.batch_exists(batch_id):
            batch = self.storage.load(batch_id)
            all_disp_ids = [d.discrepancy_id for d in batch.discrepancies]
            self.claim.ensure_pending_for_batch(batch_id, all_disp_ids)

        try:
            if fmt == "json":
                count = self.claim.export_json(abs_output, batch_id, claimant, status)
            else:
                count = self.claim.export_csv(abs_output, batch_id, claimant, status)
        except OSError as e:
            self._log_audit(
                "claim_export_fail", batch_id or "ALL", 0,
                f"导出失败: {e}, 路径 {output}"
            )
            raise

        result.count = count
        result.output_path = abs_output

        summary = (
            f"导出交接清单: {count} 条, 格式={fmt.upper()}, 路径={abs_output}"
            f", 批次={batch_id or '全部'}, 认领人={claimant or '全部'}, "
            f"状态={status.value if status else '全部'}"
        )
        aid = self._log_audit("claim_export", batch_id or "ALL", count, summary)
        result.audit_ids.append(aid)

        self._maybe_cleanup_audit()
        return result
