"""批次健康检查模块 - 一键检查批次数据完整性、规则可用性、匹配有效性等."""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional, Dict, Any

from .models import Batch, FileType, DiscrepancyStatus
from .storage import BatchStorage
from .rules import load_rules, RuleValidationError
from .audit import AuditStorage


class IssueLevel(str, Enum):
    """问题级别."""
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class CheckCategory(str, Enum):
    """检查类别."""
    FILES = "imported_files"
    RULES = "rules"
    MATCH = "matching"
    MARKERS = "markers_and_rollback"
    AUDIT = "audit_log"


@dataclass
class HealthIssue:
    """单个检查项问题/结果."""
    category: CheckCategory
    check_name: str
    level: IssueLevel
    description: str
    suggestion: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "check_name": self.check_name,
            "level": self.level.value,
            "description": self.description,
            "suggestion": self.suggestion,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HealthIssue":
        return cls(
            category=CheckCategory(data["category"]),
            check_name=data["check_name"],
            level=IssueLevel(data["level"]),
            description=data["description"],
            suggestion=data.get("suggestion", ""),
            details=data.get("details", {}),
        )


@dataclass
class HealthReport:
    """完整的健康检查报告."""
    batch_id: str
    batch_name: str
    batch_status: str
    generated_at: str
    overall_status: IssueLevel
    issues: List[HealthIssue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "batch_name": self.batch_name,
            "batch_status": self.batch_status,
            "generated_at": self.generated_at,
            "overall_status": self.overall_status.value,
            "issues": [i.to_dict() for i in self.issues],
            "summary": self._summary(),
        }

    def _summary(self) -> Dict[str, int]:
        counts = {l.value: 0 for l in IssueLevel}
        for issue in self.issues:
            counts[issue.level.value] += 1
        return counts

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HealthReport":
        return cls(
            batch_id=data["batch_id"],
            batch_name=data["batch_name"],
            batch_status=data["batch_status"],
            generated_at=data["generated_at"],
            overall_status=IssueLevel(data["overall_status"]),
            issues=[HealthIssue.from_dict(i) for i in data.get("issues", [])],
        )


class HealthCheckError(Exception):
    """健康检查错误."""
    pass


class HealthCheckCorruptedError(HealthCheckError):
    """审计库损坏等严重错误."""
    pass


MATCH_RESULT_EXPIRY_DAYS = 7


def _compute_overall_status(issues: List[HealthIssue]) -> IssueLevel:
    """根据所有问题计算总体状态."""
    if any(i.level == IssueLevel.CRITICAL for i in issues):
        return IssueLevel.CRITICAL
    if any(i.level == IssueLevel.WARNING for i in issues):
        return IssueLevel.WARNING
    if any(i.level == IssueLevel.INFO for i in issues):
        return IssueLevel.INFO
    return IssueLevel.OK


def _check_imported_files(batch: Batch) -> List[HealthIssue]:
    """检查导入文件是否齐全且源文件存在."""
    issues: List[HealthIssue] = []
    required_types = {FileType.BANK_STATEMENT, FileType.SYSTEM_RECEIPT}

    imported_types = {f.file_type for f in batch.imported_files}

    missing_required = required_types - imported_types
    if missing_required:
        type_names = {
            FileType.BANK_STATEMENT: "银行回单",
            FileType.SYSTEM_RECEIPT: "系统流水",
        }
        missing_names = [type_names.get(t, t.value) for t in missing_required]
        issues.append(HealthIssue(
            category=CheckCategory.FILES,
            check_name="required_files_present",
            level=IssueLevel.CRITICAL,
            description=f"缺少必要导入文件: {', '.join(missing_names)}",
            suggestion="请使用 import 命令导入缺失的文件类型",
            details={"missing_types": [t.value for t in missing_required]},
        ))
    else:
        issues.append(HealthIssue(
            category=CheckCategory.FILES,
            check_name="required_files_present",
            level=IssueLevel.OK,
            description="银行回单和系统流水均已导入",
        ))

    if FileType.MANUAL_ADJUSTMENT not in imported_types:
        issues.append(HealthIssue(
            category=CheckCategory.FILES,
            check_name="adjustment_file_optional",
            level=IssueLevel.INFO,
            description="未导入手工调整文件（可选）",
            suggestion="如需手工调账，请导入手工调整文件",
        ))

    for f in batch.imported_files:
        if f.file_path and not os.path.isfile(f.file_path):
            type_name = {
                FileType.BANK_STATEMENT: "银行回单",
                FileType.SYSTEM_RECEIPT: "系统流水",
                FileType.MANUAL_ADJUSTMENT: "手工调整",
            }.get(f.file_type, f.file_type.value)
            issues.append(HealthIssue(
                category=CheckCategory.FILES,
                check_name="source_file_exists",
                level=IssueLevel.WARNING,
                description=f"{type_name}源文件不存在: {f.file_path}",
                suggestion="请确认文件路径是否正确，或重新导入该文件",
                details={"file_type": f.file_type.value, "file_path": f.file_path},
            ))

    return issues


def _check_rules(batch: Batch) -> List[HealthIssue]:
    """检查规则文件是否存在且可加载."""
    issues: List[HealthIssue] = []

    if not batch.rule_file:
        issues.append(HealthIssue(
            category=CheckCategory.RULES,
            check_name="rules_configured",
            level=IssueLevel.WARNING,
            description="批次未设置规则文件，匹配时将使用默认规则",
            suggestion="使用 rules set 命令设置匹配规则文件",
        ))
    elif not os.path.isfile(batch.rule_file):
        issues.append(HealthIssue(
            category=CheckCategory.RULES,
            check_name="rules_file_exists",
            level=IssueLevel.CRITICAL,
            description=f"规则文件不存在: {batch.rule_file}",
            suggestion="请恢复规则文件或使用 rules set 命令重新设置",
            details={"rule_file": batch.rule_file},
        ))
    else:
        try:
            rules = load_rules(batch.rule_file)
            issues.append(HealthIssue(
                category=CheckCategory.RULES,
                check_name="rules_loadable",
                level=IssueLevel.OK,
                description=f"规则文件加载成功，金额容差 {rules.amount_tolerance}，日期窗口 {rules.date_window_days} 天",
            ))
        except RuleValidationError as e:
            issues.append(HealthIssue(
                category=CheckCategory.RULES,
                check_name="rules_loadable",
                level=IssueLevel.CRITICAL,
                description=f"规则文件加载失败: {e}",
                suggestion="请修复规则文件格式，使用 rules validate 校验",
                details={"rule_file": batch.rule_file, "error": str(e)},
            ))

    return issues


def _check_matching_results(batch: Batch) -> List[HealthIssue]:
    """检查匹配结果是否存在且未过期."""
    issues: List[HealthIssue] = []

    if not batch.discrepancies:
        issues.append(HealthIssue(
            category=CheckCategory.MATCH,
            check_name="matching_done",
            level=IssueLevel.WARNING,
            description="尚未执行匹配操作，无差异结果",
            suggestion="请使用 match 命令执行对账匹配",
        ))
        return issues

    issues.append(HealthIssue(
        category=CheckCategory.MATCH,
        check_name="matching_done",
        level=IssueLevel.OK,
        description=f"已完成匹配，共 {len(batch.discrepancies)} 条差异",
    ))

    updated_at_str = batch.updated_at
    try:
        updated_at = datetime.fromisoformat(updated_at_str)
        age_days = (datetime.now() - updated_at).days
        if age_days > MATCH_RESULT_EXPIRY_DAYS:
            issues.append(HealthIssue(
                category=CheckCategory.MATCH,
                check_name="matching_fresh",
                level=IssueLevel.WARNING,
                description=f"匹配结果已过期（{age_days} 天前更新）",
                suggestion=f"建议重新执行 match 命令，匹配结果有效期为 {MATCH_RESULT_EXPIRY_DAYS} 天",
                details={"age_days": age_days, "expiry_days": MATCH_RESULT_EXPIRY_DAYS},
            ))
        else:
            issues.append(HealthIssue(
                category=CheckCategory.MATCH,
                check_name="matching_fresh",
                level=IssueLevel.OK,
                description=f"匹配结果有效（{age_days} 天前更新）",
            ))
    except (ValueError, TypeError):
        issues.append(HealthIssue(
            category=CheckCategory.MATCH,
            check_name="matching_fresh",
            level=IssueLevel.WARNING,
            description="无法解析批次更新时间",
            suggestion="建议重新保存批次以刷新时间戳",
        ))

    open_count = sum(1 for d in batch.discrepancies if d.status == DiscrepancyStatus.OPEN)
    if open_count > 0:
        issues.append(HealthIssue(
            category=CheckCategory.MATCH,
            check_name="all_discrepancies_resolved",
            level=IssueLevel.INFO,
            description=f"仍有 {open_count} 条差异处于 open 状态，尚未处理",
            suggestion="请使用 mark 命令处理待确认差异",
            details={"open_count": open_count},
        ))
    else:
        issues.append(HealthIssue(
            category=CheckCategory.MATCH,
            check_name="all_discrepancies_resolved",
            level=IssueLevel.OK,
            description="所有差异均已处理",
        ))

    return issues


def _check_markers_and_rollback(batch: Batch) -> List[HealthIssue]:
    """检查人工标记和回滚链完整性."""
    issues: List[HealthIssue] = []
    broken_chains = 0
    missing_reviewers = 0

    for d in batch.discrepancies:
        if d.status != DiscrepancyStatus.OPEN:
            if not d.reviewer:
                missing_reviewers += 1
            if d.rollback_history:
                for i, entry in enumerate(d.rollback_history):
                    required_keys = {"from_status", "to_status", "timestamp"}
                    if not required_keys.issubset(entry.keys()):
                        broken_chains += 1
                        break

    if missing_reviewers > 0:
        issues.append(HealthIssue(
            category=CheckCategory.MARKERS,
            check_name="reviewer_recorded",
            level=IssueLevel.WARNING,
            description=f"有 {missing_reviewers} 条已标记差异缺少复核人信息",
            suggestion="请确保所有标记操作都通过 mark 命令完成",
            details={"missing_reviewer_count": missing_reviewers},
        ))
    else:
        issues.append(HealthIssue(
            category=CheckCategory.MARKERS,
            check_name="reviewer_recorded",
            level=IssueLevel.OK,
            description="所有已标记差异均有复核人记录",
        ))

    if broken_chains > 0:
        issues.append(HealthIssue(
            category=CheckCategory.MARKERS,
            check_name="rollback_chain_integrity",
            level=IssueLevel.CRITICAL,
            description=f"有 {broken_chains} 条差异的回滚链损坏",
            suggestion="回滚历史数据已损坏，建议检查批次 JSON 文件完整性",
            details={"broken_chain_count": broken_chains},
        ))
    else:
        issues.append(HealthIssue(
            category=CheckCategory.MARKERS,
            check_name="rollback_chain_integrity",
            level=IssueLevel.OK,
            description="所有回滚链完整",
        ))

    marked_count = sum(1 for d in batch.discrepancies if d.status != DiscrepancyStatus.OPEN)
    issues.append(HealthIssue(
        category=CheckCategory.MARKERS,
        check_name="markers_summary",
        level=IssueLevel.OK,
        description=f"共 {marked_count} 条已标记差异，"
                    f"{sum(1 for d in batch.discrepancies if d.status == DiscrepancyStatus.CONFIRMED)} 条确认，"
                    f"{sum(1 for d in batch.discrepancies if d.status == DiscrepancyStatus.IGNORED)} 条忽略",
    ))

    return issues


def _check_audit_log(batch: Batch, audit: AuditStorage) -> List[HealthIssue]:
    """检查审计日志中关键操作是否存在且可对应."""
    issues: List[HealthIssue] = []

    try:
        records = audit.query(batch_id=batch.batch_id)
    except Exception as e:
        issues.append(HealthIssue(
            category=CheckCategory.AUDIT,
            check_name="audit_db_accessible",
            level=IssueLevel.CRITICAL,
            description=f"审计数据库访问失败: {e}",
            suggestion="审计库可能已损坏，请检查 audit.db 文件或恢复备份",
            details={"error": str(e)},
        ))
        return issues

    issues.append(HealthIssue(
        category=CheckCategory.AUDIT,
        check_name="audit_db_accessible",
        level=IssueLevel.OK,
        description=f"审计日志可访问，共 {len(records)} 条记录",
    ))

    command_types = {r["command"] for r in records}

    if batch.imported_files:
        expected_imports = {f.file_type.value for f in batch.imported_files}
        if "import" not in command_types:
            issues.append(HealthIssue(
                category=CheckCategory.AUDIT,
                check_name="audit_import_recorded",
                level=IssueLevel.WARNING,
                description="存在导入文件但审计日志中无 import 操作记录",
                suggestion="请确认审计日志完整性，可能存在未记录的操作",
            ))
        else:
            issues.append(HealthIssue(
                category=CheckCategory.AUDIT,
                check_name="audit_import_recorded",
                level=IssueLevel.OK,
                description="导入操作均有审计记录",
            ))
    else:
        issues.append(HealthIssue(
            category=CheckCategory.AUDIT,
            check_name="audit_import_recorded",
            level=IssueLevel.INFO,
            description="尚未导入任何文件",
        ))

    if batch.discrepancies:
        if "match" not in command_types:
            issues.append(HealthIssue(
                category=CheckCategory.AUDIT,
                check_name="audit_match_recorded",
                level=IssueLevel.WARNING,
                description="存在匹配结果但审计日志中无 match 操作记录",
                suggestion="请确认审计日志完整性，可能存在未记录的操作",
            ))
        else:
            issues.append(HealthIssue(
                category=CheckCategory.AUDIT,
                check_name="audit_match_recorded",
                level=IssueLevel.OK,
                description="匹配操作有审计记录",
            ))

        marked_discrepancies = [d for d in batch.discrepancies if d.status != DiscrepancyStatus.OPEN]
        if marked_discrepancies:
            if "mark" not in command_types:
                issues.append(HealthIssue(
                    category=CheckCategory.AUDIT,
                    check_name="audit_mark_recorded",
                    level=IssueLevel.WARNING,
                    description=f"存在 {len(marked_discrepancies)} 条已标记差异但审计日志中无 mark 操作记录",
                    suggestion="请确认审计日志完整性，可能存在未记录的操作",
                ))
            else:
                issues.append(HealthIssue(
                    category=CheckCategory.AUDIT,
                    check_name="audit_mark_recorded",
                    level=IssueLevel.OK,
                    description="标记操作有审计记录",
                ))

    if batch.status.value == "closed":
        if "close" not in command_types:
            issues.append(HealthIssue(
                category=CheckCategory.AUDIT,
                check_name="audit_close_recorded",
                level=IssueLevel.WARNING,
                description="批次已关闭但审计日志中无 close 操作记录",
                suggestion="请确认审计日志完整性",
            ))
        else:
            issues.append(HealthIssue(
                category=CheckCategory.AUDIT,
                check_name="audit_close_recorded",
                level=IssueLevel.OK,
                description="关闭操作有审计记录",
            ))

    return issues


def run_health_check(
    storage: BatchStorage,
    batch_id: str,
) -> HealthReport:
    """执行批次健康检查，返回完整报告.

    Raises:
        HealthCheckError: 批次不存在、配置缺失、审计库损坏等
    """
    if not storage.batch_exists_anywhere(batch_id):
        raise HealthCheckError(f"批次不存在: {batch_id}")

    try:
        batch = storage.load(batch_id)
    except FileNotFoundError:
        raise HealthCheckError(f"批次不存在: {batch_id}")
    except json.JSONDecodeError as e:
        raise HealthCheckError(f"批次数据损坏，无法解析: {e}")

    audit = AuditStorage(storage.storage_dir)
    try:
        _ = audit.query(batch_id=batch_id)
    except Exception as e:
        raise HealthCheckCorruptedError(f"审计数据库损坏: {e}")

    issues: List[HealthIssue] = []
    issues.extend(_check_imported_files(batch))
    issues.extend(_check_rules(batch))
    issues.extend(_check_matching_results(batch))
    issues.extend(_check_markers_and_rollback(batch))
    issues.extend(_check_audit_log(batch, audit))

    overall_status = _compute_overall_status(issues)

    return HealthReport(
        batch_id=batch.batch_id,
        batch_name=batch.name,
        batch_status=batch.status.value,
        generated_at=datetime.now().isoformat(),
        overall_status=overall_status,
        issues=issues,
    )


def export_health_report_json(report: HealthReport, output_path: str) -> None:
    """导出健康检查报告为 JSON 格式."""
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)


def export_health_report_csv(report: HealthReport, output_path: str) -> None:
    """导出健康检查报告为 CSV 格式."""
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    columns = [
        "batch_id", "batch_name", "batch_status", "generated_at",
        "overall_status", "category", "check_name", "level",
        "description", "suggestion",
    ]

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for issue in report.issues:
            writer.writerow({
                "batch_id": report.batch_id,
                "batch_name": report.batch_name,
                "batch_status": report.batch_status,
                "generated_at": report.generated_at,
                "overall_status": report.overall_status.value,
                "category": issue.category.value,
                "check_name": issue.check_name,
                "level": issue.level.value,
                "description": issue.description,
                "suggestion": issue.suggestion,
            })
