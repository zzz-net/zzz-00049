"""银行回单对账 CLI 入口."""
from __future__ import annotations

import os
import sys
from typing import Optional, Dict, List, Any

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .models import Batch, FileType, DiscrepancyStatus, DiscrepancyType, BatchStatus, AdjustmentType, Transaction, MatchLevel
from .parser import parse_csv, parse_xlsx, parse_file, ParseResult
from .rules import load_rules, RuleValidationError, validate_rules, export_rules, import_rules, MatchRules
from .matcher import run_matching, get_tolerance_match_records
from .storage import BatchStorage
from .report import export_discrepancies_csv, export_summary_csv, generate_summary
from .audit import AuditStorage
from .scheduler import (
    ScheduleStorage,
    ScheduleTask,
    ScheduleStep,
    ScheduleStatus,
    ScheduleRunStatus,
    ScheduleImportConfig,
    ScheduleReportConfig,
    Scheduler,
    BatchLock,
)
from .config import (
    load_config,
    save_config,
    set_column_alias,
    AliasConflictError,
    STANDARD_FIELDS,
    FILE_TYPE_ALIAS_KEYS,
)
from .snapshot import (
    create_snapshot,
    read_snapshot_info,
    restore_snapshot,
    ConflictStrategy,
    SnapshotError,
    SnapshotCorruptedError,
    SnapshotVersionError,
    SnapshotConflictError,
    SNAPSHOT_FILE_EXT,
)
from .health import (
    run_health_check,
    export_health_report_json,
    export_health_report_csv,
    HealthCheckError,
    HealthCheckCorruptedError,
    HealthReport,
    IssueLevel,
    CheckCategory,
)
from .claim import (
    ClaimStorage,
    ClaimStatus,
    ClaimRecord,
    ClaimError,
    BatchNotFoundError,
    DiscrepancyNotFoundError,
    AlreadyClaimedError,
    NotClaimantError,
    OperatorMissingError,
    ExportPathConflictError,
    ClaimPermissionDeniedError,
)
from .claim_service import (
    ClaimService,
    ClaimTakeResult,
    ClaimReleaseResult,
    ClaimListResult,
    ClaimExportResult,
    BatchValidationError,
    DiscrepancyValidationError,
)


console = Console(highlight=False, emoji=False, markup=True)


def _get_storage() -> BatchStorage:
    storage_dir = os.environ.get("BANK_RECONCILE_HOME")
    return BatchStorage(storage_dir)


def _get_audit(storage: BatchStorage) -> AuditStorage:
    return AuditStorage(storage.storage_dir)


def _get_claim(storage: BatchStorage) -> ClaimStorage:
    return ClaimStorage(storage.storage_dir)


def _get_claim_service() -> ClaimService:
    storage_dir = os.environ.get("BANK_RECONCILE_HOME")
    return ClaimService(storage_dir)


def _maybe_cleanup_audit(audit: AuditStorage, storage: BatchStorage) -> None:
    cfg = load_config(storage.storage_dir)
    days = cfg.get("audit_retention_days", 90)
    if days and days > 0:
        try:
            audit.cleanup(days)
        except Exception:
            pass


def _check_archived_write_block(batch: Batch, action: str) -> None:
    """如果批次已归档，阻断写操作并退出."""
    if batch.is_archived:
        console.print(
            f"[red]ERR[/] 批次已归档（archived），禁止{action}。\n"
            f"  请先执行 [cyan]bank-reconcile batch restore {batch.batch_id}[/] 恢复后再操作。"
        )
        sys.exit(1)


def _parse_col_map_raw(col_map_raw: str) -> Dict[str, str]:
    """解析 --col-map 参数，支持 YAML/JSON 文件或 KEY=VALUE 格式."""
    if not col_map_raw:
        return {}

    if os.path.isfile(col_map_raw):
        ext = os.path.splitext(col_map_raw)[1].lower()
        try:
            with open(col_map_raw, "r", encoding="utf-8") as f:
                content = f.read()
            if ext in (".yaml", ".yml"):
                import yaml
                data = yaml.safe_load(content)
            elif ext == ".json":
                import json
                data = json.loads(content)
            else:
                import yaml
                try:
                    data = yaml.safe_load(content)
                except Exception:
                    import json
                    data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("列映射文件内容必须是对象（键值对）")
            return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            raise ValueError(f"解析列映射文件 {col_map_raw} 失败: {e}")

    result: Dict[str, str] = {}
    for item in col_map_raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                f"无效的列映射项 '{item}'，应为 KEY=VALUE 格式，多个用逗号分隔，\n"
                f"或传 YAML/JSON 文件路径。"
            )
        k, v = item.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k or not v:
            raise ValueError(f"无效的列映射项 '{item}'，键和值都不能为空")
        result[k] = v
    return result


def _print_batch_info(batch: Batch) -> None:
    summary = generate_summary(batch)
    if batch.is_archived:
        status_style = "dim"
        status_text = f"[bold {status_style}]状态: {batch.status.value} (已归档)[/]"
    elif batch.status == BatchStatus.OPEN:
        status_style = "green"
        status_text = f"[bold {status_style}]状态: {batch.status.value}[/]"
    else:
        status_style = "yellow"
        status_text = f"[bold {status_style}]状态: {batch.status.value}[/]"
    console.print(Panel.fit(
        f"[bold cyan]批次:[/] {summary['batch_name']}  "
        f"[dim]({summary['batch_id']})[/]  "
        f"{status_text}\n"
        f"银行回单: {summary['bank_transactions']}  "
        f"系统流水: {summary['system_transactions']}  "
        f"手工调整: {summary['adjustment_transactions']}\n"
        f"差异总数: [bold yellow]{summary['total_discrepancies']}[/]",
        title="批次信息",
    ))


@click.group(help="银行回单对账 CLI - 导入、匹配、标记、导出")
@click.version_option(version="0.1.0")
def cli() -> None:
    pass


# ── create ────────────────────────────────────────────────
@cli.command(help="创建新的对账批次")
@click.argument("name")
def create(name: str) -> None:
    storage = _get_storage()
    batch = Batch.create(name)
    storage.save(batch)
    console.print(f"[green]OK[/] 批次已创建: [bold]{batch.name}[/] (ID: [cyan]{batch.batch_id}[/])")
    console.print(f"  存储位置: {storage.batches_dir}")


# ── list ──────────────────────────────────────────────────
@cli.command(name="list", help="列出所有批次")
@click.option("--all", "-a", "show_all", is_flag=True, help="同时列出归档目录中的批次")
def list_batches(show_all: bool) -> None:
    storage = _get_storage()
    if show_all:
        batches = storage.list_all_batches()
    else:
        batches = storage.list_batches()
    if not batches:
        console.print("[yellow]尚无批次[/], 使用 [cyan]bank-reconcile create <名称>[/] 创建")
        return

    table = Table(title="批次列表" + ("（含归档）" if show_all else ""))
    table.add_column("批次ID", style="cyan")
    table.add_column("名称", style="bold")
    table.add_column("状态", style="bold")
    if show_all:
        table.add_column("位置", style="dim")
    table.add_column("创建时间", style="dim")
    table.add_column("更新时间", style="dim")
    table.add_column("差异数", justify="right")
    table.add_column("导入文件数", justify="right")

    for b in batches:
        status = b.get("status", "open")
        if status == "archived":
            status_style = "dim"
            status_label = "archived(已归档)"
        elif status == "open":
            status_style = "green"
            status_label = status
        else:
            status_style = "yellow"
            status_label = status
        row_items = [
            b["batch_id"],
            b["name"],
            f"[{status_style}]{status_label}[/]",
        ]
        if show_all:
            row_items.append(b.get("location", "active"))
        row_items.extend([
            b["created_at"][:19].replace("T", " "),
            b["updated_at"][:19].replace("T", " "),
            str(b["discrepancy_count"]),
            str(b["imported_files"]),
        ])
        table.add_row(*row_items)
    console.print(table)


# ── import ────────────────────────────────────────────────
@cli.command("import", help="导入文件到指定批次")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--type", "-t", "file_type", required=True,
              type=click.Choice(["bank", "system", "adjustment"]),
              help="文件类型")
@click.option("--col-map", "col_map_raw", default=None,
              help="列名映射: 传 YAML/JSON 文件路径, 或 KEY=VALUE 用逗号分隔。如 --col-map 收入金额=amount,交易号=txn_id")
@click.argument("file_path")
def import_file(batch_id: str, file_type: str, file_path: str, col_map_raw: Optional[str]) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止导入。请先 reopen 后再操作。")
        sys.exit(1)
    _check_archived_write_block(batch, "导入")

    type_map = {
        "bank": FileType.BANK_STATEMENT,
        "system": FileType.SYSTEM_RECEIPT,
        "adjustment": FileType.MANUAL_ADJUSTMENT,
    }
    ft = type_map[file_type]

    try:
        extra_col_map = _parse_col_map_raw(col_map_raw) if col_map_raw else None
    except ValueError as e:
        console.print(f"[red]ERR[/] --col-map 参数错误: {e}")
        sys.exit(1)

    try:
        result, imported = parse_file(file_path, ft, storage_dir=storage.storage_dir, extra_col_map=extra_col_map)
    except FileNotFoundError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]ERR[/] 解析失败: {e}")
        sys.exit(1)

    if ft == FileType.BANK_STATEMENT:
        batch.bank_txns = result.transactions
    elif ft == FileType.SYSTEM_RECEIPT:
        batch.system_txns = result.transactions
    else:
        batch.adjustment_txns = result.transactions

    existing_names = {f.file_type for f in batch.imported_files}
    if ft in existing_names:
        batch.imported_files = [f for f in batch.imported_files if f.file_type != ft]
    batch.imported_files.append(imported)

    storage.save(batch)

    type_name = {
        FileType.BANK_STATEMENT: "银行回单",
        FileType.SYSTEM_RECEIPT: "系统流水",
        FileType.MANUAL_ADJUSTMENT: "手工调整",
    }[ft]

    console.print(f"[green]OK[/] 导入{type_name}文件成功")
    console.print(f"  有效记录: {result.row_count} 条")
    if result.errors:
        console.print(f"  [yellow]解析错误: {len(result.errors)} 条")
        for e in result.errors[:5]:
            console.print(f"    - 第{e.source_row}行: {e.message}")
        if len(result.errors) > 5:
            console.print(f"    ... 还有 {len(result.errors) - 5} 条错误")
    if result.duplicates:
        console.print(f"  [yellow]重复流水号: {len(result.duplicates)} 条")
        for d in result.duplicates[:5]:
            console.print(f"    - 第{d.source_row}行: {d.message}")
        if len(result.duplicates) > 5:
            console.print(f"    ... 还有 {len(result.duplicates) - 5} 条重复")

    file_basename = os.path.basename(file_path)
    summary_text = (
        f"导入 {file_basename}，{type_name} {result.row_count} 条"
    )
    audit = _get_audit(storage)
    audit.log("import", batch_id, result.row_count, summary_text)
    _maybe_cleanup_audit(audit, storage)


# ── rules group ───────────────────────────────────────────
@cli.group("rules", help="规则管理: 设置/校验/导出/导入")
def rules_group() -> None:
    pass


@rules_group.command("set", help="为批次设置规则文件")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.argument("rule_file")
def rules_set(batch_id: str, rule_file: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)
    _check_archived_write_block(batch, "设置规则")

    try:
        rules_obj = load_rules(rule_file)
    except RuleValidationError as e:
        console.print(f"[red]ERR[/] 规则文件错误: {e}")
        sys.exit(1)

    batch = storage.load(batch_id)
    batch.rule_file = os.path.abspath(rule_file)
    storage.save(batch)

    console.print(f"[green]OK[/] 规则文件已设置")
    console.print(f"  金额容差: {rules_obj.amount_tolerance}")
    console.print(f"  日期窗口: {rules_obj.date_window_days} 天")
    console.print(f"  人工复核关键词: {', '.join(rules_obj.manual_review_keywords)}")
    if rules_obj.tolerance.enabled:
        console.print(f"  [cyan]容忍匹配: 已启用[/]")
        if rules_obj.tolerance.amount_value > 0:
            if rules_obj.tolerance.amount_is_percent:
                console.print(f"    金额容差: {rules_obj.tolerance.amount_value * 100:.4g}%")
            else:
                console.print(f"    金额容差: ±{rules_obj.tolerance.amount_value:.2f}")
        if rules_obj.tolerance.date_tolerance_days > 0:
            console.print(f"    日期容差: ±{rules_obj.tolerance.date_tolerance_days} 天")
        if rules_obj.tolerance.txn_id_prefixes:
            console.print(f"    交易号前缀: {', '.join(rules_obj.tolerance.txn_id_prefixes)}")
        if rules_obj.tolerance.description_keywords:
            console.print(f"    备注关键字: {', '.join(rules_obj.tolerance.description_keywords)}")


@rules_group.command("validate", help="校验规则文件格式")
@click.argument("rule_file")
def rules_validate(rule_file: str) -> None:
    ok, errors = validate_rules(rule_file)
    if ok:
        console.print(f"[green]OK[/] 规则文件有效: {rule_file}")
        try:
            rules_obj = load_rules(rule_file)
            console.print(f"  金额容差: {rules_obj.amount_tolerance}")
            console.print(f"  日期窗口: {rules_obj.date_window_days} 天")
            if rules_obj.tolerance.enabled:
                console.print(f"  [cyan]容忍匹配: 已启用[/]")
                if rules_obj.tolerance.amount_value > 0:
                    if rules_obj.tolerance.amount_is_percent:
                        console.print(f"    金额容差: {rules_obj.tolerance.amount_value * 100:.4g}%")
                    else:
                        console.print(f"    金额容差: ±{rules_obj.tolerance.amount_value:.2f}")
                if rules_obj.tolerance.date_tolerance_days > 0:
                    console.print(f"    日期容差: ±{rules_obj.tolerance.date_tolerance_days} 天")
        except Exception:
            pass
        sys.exit(0)
    else:
        console.print(f"[red]ERR[/] 规则文件校验失败: {rule_file}")
        for e in errors:
            console.print(f"  - {e}")
        sys.exit(1)


@rules_group.command("export", help="导出当前(或批次)规则到 YAML")
@click.option("--batch-id", "-b", default=None, help="批次ID (可选，导出该批次关联的规则)")
@click.option("--default", "use_default", is_flag=True, help="导出默认规则")
@click.argument("output_file")
def rules_export(batch_id: Optional[str], use_default: bool, output_file: str) -> None:
    rules_obj: MatchRules
    if use_default:
        rules_obj = MatchRules.default()
    elif batch_id:
        storage = _get_storage()
        if not storage.batch_exists_anywhere(batch_id):
            console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
            sys.exit(1)
        batch = storage.load(batch_id)
        rule_path = batch.rule_file
        if rule_path and os.path.isfile(rule_path):
            try:
                rules_obj = load_rules(rule_path)
            except RuleValidationError as e:
                console.print(f"[red]ERR[/] 批次关联规则文件错误: {e}")
                sys.exit(1)
        else:
            rules_obj = MatchRules.default()
            console.print(f"[yellow]WARN[/] 批次未关联有效规则文件，导出默认规则")
    else:
        rules_obj = MatchRules.default()

    try:
        export_rules(rules_obj, output_file)
    except Exception as e:
        console.print(f"[red]ERR[/] 导出失败: {e}")
        sys.exit(1)

    console.print(f"[green]OK[/] 规则已导出到: [cyan]{output_file}[/]")


@rules_group.command("import", help="从 YAML 导入规则，支持冲突检查")
@click.option("--batch-id", "-b", default=None, help="批次ID (可选，导入后关联到该批次)")
@click.option("--force", is_flag=True, help="忽略冲突警告直接导入")
@click.argument("rule_file")
def rules_import(batch_id: Optional[str], force: bool, rule_file: str) -> None:
    storage = _get_storage() if batch_id else None

    existing_rules: Optional[MatchRules] = None
    if batch_id:
        if not storage or not storage.batch_exists_anywhere(batch_id):
            console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
            sys.exit(1)
        batch = storage.load(batch_id)
        if batch.rule_file and os.path.isfile(batch.rule_file):
            try:
                existing_rules = load_rules(batch.rule_file)
            except RuleValidationError:
                existing_rules = None

    try:
        new_rules, conflicts = import_rules(rule_file, existing_rules, check_conflicts=not force)
    except RuleValidationError as e:
        console.print(f"[red]ERR[/] 规则文件错误: {e}")
        sys.exit(1)

    if conflicts and not force:
        console.print(f"[yellow]WARN[/] 检测到 {len(conflicts)} 处配置差异:")
        for c in conflicts:
            console.print(f"  - {c}")
        console.print(f"\n使用 [cyan]--force[/] 忽略冲突并导入。")
        sys.exit(1)

    if batch_id and storage:
        dest_path = os.path.abspath(rule_file)
        batch = storage.load(batch_id)
        _check_archived_write_block(batch, "设置规则文件关联")
        batch.rule_file = dest_path
        storage.save(batch)
        console.print(f"[green]OK[/] 规则已导入并关联到批次 {batch_id}")
    else:
        console.print(f"[green]OK[/] 规则文件校验通过")
        if conflicts:
            console.print(f"[dim]  (已忽略 {len(conflicts)} 处冲突)[/]")


# ── report group ──────────────────────────────────────────
@cli.group("report", help="报告生成: 汇总统计等")
def report_group() -> None:
    pass


@report_group.command("summary", help="输出批次汇总: 精确/容忍/手工匹配数及未匹配数")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--export", "export_path", default=None, help="导出 CSV 文件路径")
def report_summary(batch_id: str, export_path: Optional[str]) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)
    summary = generate_summary(batch)

    table = Table(title=f"批次汇总 - {summary['batch_name']}")
    table.add_column("指标", style="bold")
    table.add_column("值", justify="right")

    table.add_row("批次ID", summary["batch_id"])
    table.add_row("银行回单数", str(summary["bank_transactions"]))
    table.add_row("系统流水数", str(summary["system_transactions"]))
    table.add_row("手工调整数", str(summary["adjustment_transactions"]))
    table.add_row("精确匹配数", f"[green]{summary['exact_matches']}[/]")
    table.add_row("容忍匹配数", f"[cyan]{summary['tolerance_matches']}[/]")
    table.add_row("手工匹配数", f"[yellow]{summary['manual_matches']}[/]")
    table.add_row("未匹配数", f"[red]{summary['unmatched_count']}[/]")
    table.add_row("总差异数", str(summary["total_discrepancies"]))

    console.print(table)

    if summary["by_match_level"]:
        ml_table = Table(title="按匹配等级统计")
        ml_table.add_column("匹配等级", style="bold")
        ml_table.add_column("数量", justify="right")
        for k, v in sorted(summary["by_match_level"].items()):
            style = {"exact": "green", "tolerance": "cyan", "manual": "yellow"}.get(k, "")
            ml_table.add_row(f"[{style}]{k}[/]" if style else k, str(v))
        console.print(ml_table)

    if export_path:
        export_summary_csv(batch, export_path)
        console.print(f"[green]OK[/] 汇总已导出到 [cyan]{export_path}[/]")


# ── match ─────────────────────────────────────────────────
@cli.command(help="执行对账匹配，生成差异清单")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--rule-file", "-r", default=None, help="规则文件路径（可选，优先级高于批次已设置的规则）")
def match(batch_id: str, rule_file: Optional[str]) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止匹配。请先 reopen 后再操作。")
        sys.exit(1)
    _check_archived_write_block(batch, "匹配")

    rule_path = rule_file or batch.rule_file
    try:
        rules_obj = load_rules(rule_path)
    except RuleValidationError as e:
        console.print(f"[red]ERR[/] 规则文件错误: {e}")
        sys.exit(1)

    if not batch.bank_txns:
        console.print("[yellow]WARN[/] 尚未导入银行回单，跳过匹配")
        return
    if not batch.system_txns:
        console.print("[yellow]WARN[/] 尚未导入系统流水，跳过匹配")
        return

    discrepancies = run_matching(batch, rules_obj)
    batch.discrepancies = discrepancies
    if rule_file:
        batch.rule_file = os.path.abspath(rule_file)
    storage.save(batch)

    by_type = {}
    by_match_level = {}
    for d in discrepancies:
        t = d.discrepancy_type.value
        by_type[t] = by_type.get(t, 0) + 1
        ml = d.match_level.value
        by_match_level[ml] = by_match_level.get(ml, 0) + 1

    console.print(f"[green]OK[/] 匹配完成，共发现 [bold]{len(discrepancies)}[/] 条差异")
    for t, c in sorted(by_type.items()):
        console.print(f"  {t}: {c}")
    if by_match_level:
        console.print(f"[dim]  按匹配等级:[/]")
        for ml, c in sorted(by_match_level.items()):
            console.print(f"    {ml}: {c}")

    tol_records = get_tolerance_match_records(batch)
    if tol_records:
        console.print(f"[dim]  容忍匹配明细 ({len(tol_records)} 条):[/]")
        for rec in tol_records[:5]:
            console.print(f"    - 银行 {rec['bank_txn_id']}({rec['bank_amount']}) <-> "
                          f"系统 {rec['system_txn_id']}({rec['system_amount']})")
        if len(tol_records) > 5:
            console.print(f"    ... 还有 {len(tol_records) - 5} 条")

    bank_count = len(batch.bank_txns)
    sys_count = len(batch.system_txns)
    summary_text = (
        f"执行对账匹配，银行回单 {bank_count} 条，系统流水 {sys_count} 条，"
        f"差异 {len(discrepancies)} 条"
    )
    if tol_records:
        summary_text += f"，容忍匹配 {len(tol_records)} 条"
    audit = _get_audit(storage)
    audit.log("match", batch_id, len(discrepancies), summary_text)
    for i, rec in enumerate(tol_records):
        audit.log(
            "tolerance_match", batch_id, 1,
            f"容忍匹配 #{i + 1}: 银行 {rec['bank_txn_id']}({rec['bank_amount']}) <-> "
            f"系统 {rec['system_txn_id']}({rec['system_amount']}), {rec['message'][:100]}"
        )
    _maybe_cleanup_audit(audit, storage)


# ── discrepancies (list differences) ──────────────────────
@cli.command(name="discrepancies", help="列出差异清单")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--status", "-s", default=None,
              type=click.Choice(["open", "confirmed", "ignored"]),
              help="按状态过滤")
@click.option("--type", "-t", "disp_type", default=None,
              type=click.Choice([
                  "missing_in_bank", "missing_in_system",
                  "amount_mismatch", "duplicate", "needs_manual_review",
              ]),
              help="按类型过滤")
@click.option("--limit", "-n", default=20, help="显示条数")
def list_discrepancies(batch_id: str, status: Optional[str], disp_type: Optional[str], limit: int) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)
    items = batch.discrepancies

    if status:
        items = [d for d in items if d.status.value == status]
    if disp_type:
        items = [d for d in items if d.discrepancy_type.value == disp_type]

    if not items:
        console.print("[yellow]无符合条件的差异[/]")
        return

    table = Table(title=f"差异清单（共 {len(items)} 条，显示前 {min(limit, len(items))} 条）")
    table.add_column("ID", style="cyan", overflow="fold")
    table.add_column("类型", style="magenta")
    table.add_column("状态", style="yellow")
    table.add_column("说明", style="dim", overflow="fold")
    table.add_column("复核人", style="green")
    table.add_column("备注", style="dim", overflow="fold")

    for d in items[:limit]:
        table.add_row(
            d.discrepancy_id,
            d.discrepancy_type.value,
            d.status.value,
            d.message[:60] + ("..." if len(d.message) > 60 else ""),
            d.reviewer or "",
            d.note[:30] + ("..." if len(d.note) > 30 else ""),
        )
    console.print(table)


# ── mark ──────────────────────────────────────────────────
@cli.command(help="标记差异状态（确认/忽略）")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--discrepancy-id", "-d", required=True, help="差异ID")
@click.option("--status", "-s", required=True,
              type=click.Choice(["confirmed", "ignored"]),
              help="目标状态")
@click.option("--reviewer", "-r", required=True, help="复核人")
@click.option("--note", "-n", default="", help="备注")
def mark(batch_id: str, discrepancy_id: str, status: str, reviewer: str, note: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止标记。请先 reopen 后再操作。")
        sys.exit(1)
    _check_archived_write_block(batch, "标记")

    target_status = DiscrepancyStatus(status)

    found = None
    for d in batch.discrepancies:
        if d.discrepancy_id == discrepancy_id:
            found = d
            break

    if not found:
        console.print(f"[red]ERR[/] 差异不存在: {discrepancy_id}")
        sys.exit(1)

    claim = _get_claim(storage)
    try:
        claim.check_can_mark(batch_id, discrepancy_id, reviewer)
    except ClaimPermissionDeniedError as e:
        console.print(f"[red]ERR[/] {e}")
        audit = _get_audit(storage)
        audit.log(
            "mark_fail", batch_id, 0,
            f"标记 {discrepancy_id} 失败: {str(e)}"
        )
        sys.exit(1)

    found.mark(target_status, reviewer, note)
    storage.save(batch)

    summary_text = (
        f"标记 {discrepancy_id} 为 {status}，复核人 {reviewer}"
    )
    if note:
        summary_text += f"，备注: {note}"
    audit = _get_audit(storage)
    audit.log("mark", batch_id, 1, summary_text)
    _maybe_cleanup_audit(audit, storage)

    console.print(f"[green]OK[/] 已标记 {discrepancy_id} 为 [bold]{status}[/]")
    console.print(f"  复核人: {reviewer}")
    if note:
        console.print(f"  备注: {note}")


# ── rollback ──────────────────────────────────────────────
@cli.command(help="回滚差异状态到上一步")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--discrepancy-id", "-d", required=True, help="差异ID")
@click.option("--operator", "-r", required=True, help="操作人（用于认领权限校验）")
def rollback(batch_id: str, discrepancy_id: str, operator: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止回滚。请先 reopen 后再操作。")
        sys.exit(1)
    _check_archived_write_block(batch, "回滚")

    found = None
    for d in batch.discrepancies:
        if d.discrepancy_id == discrepancy_id:
            found = d
            break

    if not found:
        console.print(f"[red]ERR[/] 差异不存在: {discrepancy_id}")
        sys.exit(1)

    claim = _get_claim(storage)
    try:
        claim.check_can_mark(batch_id, discrepancy_id, operator)
    except ClaimPermissionDeniedError as e:
        console.print(f"[red]ERR[/] {e}")
        audit = _get_audit(storage)
        audit.log(
            "rollback_fail", batch_id, 0,
            f"回滚 {discrepancy_id} 失败: {str(e)}，操作人 {operator}"
        )
        sys.exit(1)

    if found.rollback():
        storage.save(batch)
        audit = _get_audit(storage)
        audit.log("rollback", batch_id, 1, f"回滚 {discrepancy_id}，当前状态 {found.status.value}")
        _maybe_cleanup_audit(audit, storage)
        console.print(f"[green]OK[/] 已回滚 {discrepancy_id}，当前状态: [bold]{found.status.value}[/]")
    else:
        console.print(f"[yellow]WARN[/] 无可回滚的历史记录")


# ── view (resume) ─────────────────────────────────────────
@cli.command(help="查看/恢复批次详情（resume）")
@click.argument("batch_id")
def resume(batch_id: str) -> None:
    """恢复批次 - 显示批次详情，确认状态完整保留."""
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)
    _print_batch_info(batch)

    if batch.imported_files:
        table = Table(title="已导入文件")
        table.add_column("类型", style="magenta")
        table.add_column("文件路径", style="dim")
        table.add_column("导入时间", style="dim")
        table.add_column("记录数", justify="right")
        table.add_column("错误数", justify="right")
        type_name = {
            FileType.BANK_STATEMENT: "银行回单",
            FileType.SYSTEM_RECEIPT: "系统流水",
            FileType.MANUAL_ADJUSTMENT: "手工调整",
        }
        for f in batch.imported_files:
            table.add_row(
                type_name.get(f.file_type, f.file_type.value),
                f.file_path,
                f.imported_at[:19].replace("T", " "),
                str(f.row_count),
                str(f.error_count),
            )
        console.print(table)

    if batch.discrepancies:
        by_status = {}
        by_type = {}
        for d in batch.discrepancies:
            s = d.status.value
            by_status[s] = by_status.get(s, 0) + 1
            t = d.discrepancy_type.value
            by_type[t] = by_type.get(t, 0) + 1

        console.print(f"\n[bold]差异统计:[/] 共 {len(batch.discrepancies)} 条")
        console.print("  按状态: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
        console.print("  按类型: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))

    if batch.exports:
        console.print(f"\n[bold]导出历史:[/] 共 {len(batch.exports)} 次")
        for e in batch.exports:
            console.print(f"  - {e['exported_at'][:19].replace('T', ' ')}  "
                          f"{e['export_type']}  {e['file_path']}  "
                          f"({e['discrepancy_count']} 条)")

    console.print(f"\n[dim]提示: 使用 discrepancies -b {batch_id} 查看差异列表[/]")


# ── export ────────────────────────────────────────────────
@cli.command(help="导出差异报告 CSV")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--output", "-o", required=True, help="输出文件路径")
@click.option("--status", "-s", default=None,
              type=click.Choice(["open", "confirmed", "ignored"]),
              help="按状态过滤（可选）")
@click.option("--type", "-t", "disp_type", default=None,
              type=click.Choice([
                  "missing_in_bank", "missing_in_system",
                  "amount_mismatch", "duplicate", "needs_manual_review",
              ]),
              help="按类型过滤（可选）")
@click.option("--with-summary", is_flag=True, help="同时导出摘要")
def export(batch_id: str, output: str, status: Optional[str], disp_type: Optional[str], with_summary: bool) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    status_filter = [DiscrepancyStatus(status)] if status else None
    type_filter = [DiscrepancyType(disp_type)] if disp_type else None

    count = export_discrepancies_csv(batch, output, status_filter, type_filter)
    storage.record_export(batch, output, "discrepancies")

    console.print(f"[green]OK[/] 已导出 {count} 条差异到 [cyan]{output}[/]")

    if with_summary:
        base, ext = os.path.splitext(output)
        summary_path = f"{base}_summary{ext}"
        export_summary_csv(batch, summary_path)
        console.print(f"[green]OK[/] 摘要已导出到 [cyan]{summary_path}[/]")


# ── close ─────────────────────────────────────────────────
@cli.command(help="关闭批次，关闭后禁止导入/匹配/标记/回滚")
@click.option("--batch-id", "-b", required=True, help="批次ID")
def close(batch_id: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)
    if batch.is_archived:
        console.print(f"[red]ERR[/] 批次已归档（archived），不能 close。\n"
                      f"  请先 [cyan]bank-reconcile batch restore {batch_id}[/] 后再操作。")
        sys.exit(1)
    changed = batch.close()
    storage.save(batch)

    audit = _get_audit(storage)
    audit.log("close", batch_id, 0, f"关闭批次 {batch.name}" if changed else f"批次 {batch.name} 已处于关闭状态")
    _maybe_cleanup_audit(audit, storage)

    if changed:
        console.print(f"[green]OK[/] 批次已关闭: [bold]{batch.name}[/] ({batch.batch_id})")
    else:
        console.print(f"[yellow]WARN[/] 批次已处于关闭状态: [bold]{batch.name}[/]")


# ── reopen ────────────────────────────────────────────────
@cli.command(help="重新打开已关闭的批次")
@click.option("--batch-id", "-b", required=True, help="批次ID")
def reopen(batch_id: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)
    if batch.is_archived:
        console.print(f"[red]ERR[/] 批次已归档（archived），不能直接 reopen。\n"
                      f"  请先 [cyan]bank-reconcile batch restore {batch_id}[/] 后再 reopen。")
        sys.exit(1)
    changed = batch.reopen()
    storage.save(batch)

    audit = _get_audit(storage)
    audit.log("reopen", batch_id, 0, f"重新打开批次 {batch.name}" if changed else f"批次 {batch.name} 已处于打开状态")
    _maybe_cleanup_audit(audit, storage)

    if changed:
        console.print(f"[green]OK[/] 批次已重新打开: [bold]{batch.name}[/] ({batch.batch_id})")
    else:
        console.print(f"[yellow]WARN[/] 批次已处于打开状态: [bold]{batch.name}[/]")


# ── audit-log ────────────────────────────────────────────
@cli.command("audit-log", help="查看/导出操作审计日志")
@click.option("--from", "from_date", default=None, help="起始日期 (YYYY-MM-DD)")
@click.option("--to", "to_date", default=None, help="截止日期 (YYYY-MM-DD)")
@click.option("--type", "-t", "op_type", default=None,
              type=click.Choice(["import", "match", "mark", "mark_fail", "rollback", "rollback_fail", "close", "reopen", "manual_link", "undo_manual_link", "tolerance_match", "schedule_add", "schedule_update", "schedule_delete", "schedule_run", "schedule_load", "snapshot_create", "snapshot_create_fail", "snapshot_restore", "snapshot_restore_fail", "health_check", "health_check_fail", "health_export", "health_export_fail", "claim_list", "claim_list_fail", "claim_take", "claim_take_fail", "claim_release", "claim_release_force", "claim_release_fail", "claim_export", "claim_export_fail", "archive", "restore"]),
              help="按操作类型过滤")
@click.option("--batch", "-b", "batch_id", default=None, help="按批次ID过滤")
@click.option("--output", "-o", default=None, help="导出文件路径（需配合 --format）")
@click.option("--format", "-f", "fmt", default=None,
              type=click.Choice(["csv", "json"]),
              help="导出格式（csv / json）")
def audit_log(from_date: Optional[str], to_date: Optional[str],
              op_type: Optional[str], batch_id: Optional[str],
              output: Optional[str], fmt: Optional[str]) -> None:
    storage = _get_storage()
    audit = _get_audit(storage)

    if from_date:
        from_date = from_date + "T00:00:00"
    if to_date:
        to_date = to_date + "T23:59:59"

    records = audit.query(
        from_date=from_date,
        to_date=to_date,
        op_type=op_type,
        batch_id=batch_id,
    )

    if output and fmt:
        if fmt == "csv":
            count = audit.export_csv(output, records)
        else:
            count = audit.export_json(output, records)
        console.print(f"[green]OK[/] 已导出 {count} 条审计记录到 [cyan]{output}[/]")
        return

    if not records:
        console.print("[yellow]无符合条件的审计记录[/]")
        return

    table = Table(title=f"审计日志（共 {len(records)} 条）")
    table.add_column("ID", justify="right", style="dim")
    table.add_column("时间", style="cyan")
    table.add_column("操作", style="magenta")
    table.add_column("批次ID", style="bold")
    table.add_column("影响数", justify="right")
    table.add_column("摘要", style="dim", overflow="fold")

    for r in records:
        ts = r["timestamp"][:19].replace("T", " ")
        table.add_row(
            str(r["id"]),
            ts,
            r["command"],
            r["batch_id"],
            str(r["affected"]),
            r["summary"][:80] + ("..." if len(r["summary"]) > 80 else ""),
        )
    console.print(table)


# ── diff ───────────────────────────────────────────────────
def _get_unmatched_txns(batch: Batch):
    """获取未匹配的银行和系统记录，以及金额不匹配的记录对."""
    from collections import defaultdict

    def _nid(txn_id: str) -> str:
        return txn_id.strip().lower() if txn_id else ""

    bank_by_id: dict = defaultdict(list)
    for t in batch.bank_txns:
        bank_by_id[_nid(t.txn_id)].append(t)

    system_by_id: dict = defaultdict(list)
    for t in batch.system_txns:
        system_by_id[_nid(t.txn_id)].append(t)

    common_ids = set(bank_by_id.keys()) & set(system_by_id.keys())

    amount_mismatch_pairs = []
    for d in batch.discrepancies:
        if d.discrepancy_type == DiscrepancyType.AMOUNT_MISMATCH:
            if d.bank_txn and d.system_txn:
                amount_mismatch_pairs.append((d.bank_txn, d.system_txn))

    unmatched_bank = []
    for t in batch.bank_txns:
        if _nid(t.txn_id) not in common_ids:
            unmatched_bank.append(t)

    unmatched_system = []
    for t in batch.system_txns:
        if _nid(t.txn_id) not in common_ids:
            unmatched_system.append(t)

    return unmatched_bank, unmatched_system, amount_mismatch_pairs


def _calc_date_diff(date1: str, date2: str) -> int:
    """计算两个日期之间的天数差."""
    from datetime import datetime
    try:
        d1 = datetime.strptime(date1, "%Y-%m-%d")
        d2 = datetime.strptime(date2, "%Y-%m-%d")
        return abs((d1 - d2).days)
    except (ValueError, TypeError):
        return -1


def _build_diff_rows(batch: Batch):
    """构建 diff 列表，包含未匹配记录和金额不匹配记录对."""
    unmatched_bank, unmatched_system, amount_mismatch_pairs = _get_unmatched_txns(batch)

    rows = []

    for bank_txn, system_txn in amount_mismatch_pairs:
        amount_diff = abs(bank_txn.amount - system_txn.amount)
        date_diff = _calc_date_diff(bank_txn.date, system_txn.date)
        reason = f"金额不符，差 {amount_diff:.2f} 元"
        if date_diff >= 0:
            reason += f"；日期差 {date_diff} 天"
        rows.append({
            "sort_key": amount_diff,
            "type": "amount_mismatch",
            "bank_txn": bank_txn,
            "system_txn": system_txn,
            "amount_diff": amount_diff,
            "date_diff": date_diff,
            "reason": reason,
        })

    for bank_txn in unmatched_bank:
        best_match = None
        min_diff = float("inf")
        for system_txn in batch.system_txns:
            diff = abs(bank_txn.amount - system_txn.amount)
            if diff < min_diff:
                min_diff = diff
                best_match = system_txn
        date_diff = _calc_date_diff(bank_txn.date, best_match.date) if best_match else -1
        if best_match:
            reason = f"银行有，系统无此交易号（最接近：{best_match.txn_id}，金额差 {min_diff:.2f} 元，日期差 {date_diff} 天）"
        else:
            reason = "银行有，系统无任何记录"
        rows.append({
            "sort_key": min_diff,
            "type": "missing_in_system",
            "bank_txn": bank_txn,
            "system_txn": best_match,
            "amount_diff": min_diff,
            "date_diff": date_diff,
            "reason": reason,
        })

    for system_txn in unmatched_system:
        best_match = None
        min_diff = float("inf")
        for bank_txn in batch.bank_txns:
            diff = abs(system_txn.amount - bank_txn.amount)
            if diff < min_diff:
                min_diff = diff
                best_match = bank_txn
        date_diff = _calc_date_diff(system_txn.date, best_match.date) if best_match else -1
        if best_match:
            reason = f"系统有，银行无此交易号（最接近：{best_match.txn_id}，金额差 {min_diff:.2f} 元，日期差 {date_diff} 天）"
        else:
            reason = "系统有，银行无任何记录"
        rows.append({
            "sort_key": min_diff,
            "type": "missing_in_bank",
            "bank_txn": best_match,
            "system_txn": system_txn,
            "amount_diff": min_diff,
            "date_diff": date_diff,
            "reason": reason,
        })

    rows.sort(key=lambda r: r["sort_key"])
    return rows


@cli.command(help="列出未匹配的银行和系统记录，按金额差排序")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--limit", "-n", default=50, help="显示条数")
@click.option("--export", "export_path", default=None, help="导出 CSV 文件路径")
def diff(batch_id: str, limit: int, export_path: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)
    rows = _build_diff_rows(batch)

    if not rows:
        console.print("[green]所有记录已匹配[/]")
        return

    if export_path:
        import csv
        out_dir = os.path.dirname(os.path.abspath(export_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(export_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "序号", "类型",
                "银行交易号", "银行金额", "银行日期", "银行对方",
                "系统交易号", "系统金额", "系统日期", "系统对方",
                "金额差异", "日期偏差(天)", "原因"
            ])
            for i, r in enumerate(rows, 1):
                bt = r["bank_txn"]
                st = r["system_txn"]
                writer.writerow([
                    i, r["type"],
                    bt.txn_id if bt else "",
                    f"{bt.amount:.2f}" if bt else "",
                    bt.date if bt else "",
                    bt.counterparty if bt else "",
                    st.txn_id if st else "",
                    f"{st.amount:.2f}" if st else "",
                    st.date if st else "",
                    st.counterparty if st else "",
                    f"{r['amount_diff']:.2f}",
                    r["date_diff"] if r["date_diff"] >= 0 else "",
                    r.get("reason", ""),
                ])
        console.print(f"[green]OK[/] 已导出 {len(rows)} 条记录到 [cyan]{export_path}[/]")
        return

    table = Table(title=f"未匹配记录（共 {len(rows)} 条，显示前 {min(limit, len(rows))} 条）")
    table.add_column("#", justify="right", style="dim")
    table.add_column("类型", style="magenta")
    table.add_column("银行交易号", style="cyan")
    table.add_column("系统交易号", style="green")
    table.add_column("金额差", justify="right", style="yellow")
    table.add_column("日期差", justify="right", style="yellow")
    table.add_column("原因", overflow="fold")

    for i, r in enumerate(rows[:limit], 1):
        bt = r["bank_txn"]
        st = r["system_txn"]
        type_label = {
            "amount_mismatch": "金额不符",
            "missing_in_system": "银行有系统无",
            "missing_in_bank": "系统有银行无",
        }.get(r["type"], r["type"])
        table.add_row(
            str(i),
            type_label,
            bt.txn_id if bt else "-",
            st.txn_id if st else "-",
            f"{r['amount_diff']:.2f}",
            str(r["date_diff"]) if r["date_diff"] >= 0 else "-",
            r.get("reason", ""),
        )
    console.print(table)


# ── manual-link ────────────────────────────────────────────
@cli.command("manual-link", help="手工关联一条银行和一条系统记录")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--bank-txn-id", required=True, help="银行交易号")
@click.option("--system-txn-id", required=True, help="系统交易号")
@click.option("--adj-type", "-t", required=True,
              type=click.Choice(["timing_diff", "amount_rounding", "manual_match", "write_off"]),
              help="调整类型")
@click.option("--reviewer", "-r", required=True, help="操作人")
@click.option("--note", "-n", default="", help="备注")
def manual_link(batch_id: str, bank_txn_id: str, system_txn_id: str,
                adj_type: str, reviewer: str, note: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止操作。请先 reopen 后再操作。")
        sys.exit(1)
    _check_archived_write_block(batch, "手工关联")

    bank_txn = None
    for t in batch.bank_txns:
        if t.txn_id == bank_txn_id:
            bank_txn = t
            break
    if not bank_txn:
        console.print(f"[red]ERR[/] 银行交易不存在: {bank_txn_id}")
        sys.exit(1)

    system_txn = None
    for t in batch.system_txns:
        if t.txn_id == system_txn_id:
            system_txn = t
            break
    if not system_txn:
        console.print(f"[red]ERR[/] 系统交易不存在: {system_txn_id}")
        sys.exit(1)

    adjustment_type = AdjustmentType(adj_type)
    amount_diff = system_txn.amount - bank_txn.amount

    from datetime import datetime
    import uuid
    adj_txn_id = "ADJ-" + uuid.uuid4().hex[:8].upper()
    adjustment_txn = Transaction(
        txn_id=adj_txn_id,
        amount=amount_diff,
        date=datetime.now().strftime("%Y-%m-%d"),
        file_type=FileType.MANUAL_ADJUSTMENT,
        source_file="manual_link",
        source_row=0,
        counterparty="手工关联",
        description=f"手工关联: {bank_txn_id} <-> {system_txn_id}, 类型: {adj_type}",
        currency="CNY",
        raw_data={
            "adjustment_type": adj_type,
            "bank_txn_id": bank_txn_id,
            "system_txn_id": system_txn_id,
            "reviewer": reviewer,
            "note": note,
        },
    )

    old_discrepancies = [d for d in batch.discrepancies]
    related_discrepancies = []
    for d in batch.discrepancies:
        if d.bank_txn and d.bank_txn.txn_id == bank_txn_id:
            related_discrepancies.append(d)
        elif d.system_txn and d.system_txn.txn_id == system_txn_id:
            related_discrepancies.append(d)

    for d in related_discrepancies:
        batch.discrepancies.remove(d)

    from .models import Discrepancy
    new_discrepancy = Discrepancy.create(
        discrepancy_type=DiscrepancyType.NEEDS_MANUAL_REVIEW,
        bank_txn=bank_txn,
        system_txn=system_txn,
        adjustment_txn=adjustment_txn,
        message=f"手工关联: 银行 {bank_txn_id} <-> 系统 {system_txn_id}, 调整类型 {adj_type}, 金额差 {amount_diff:.2f}",
        match_level=MatchLevel.MANUAL,
    )
    new_discrepancy.mark(DiscrepancyStatus.CONFIRMED, reviewer, note)
    batch.discrepancies.append(new_discrepancy)

    batch.adjustment_txns.append(adjustment_txn)

    history_entry = {
        "timestamp": datetime.now().isoformat(),
        "reviewer": reviewer,
        "note": note,
        "bank_txn_id": bank_txn_id,
        "system_txn_id": system_txn_id,
        "adjustment_type": adj_type,
        "adjustment_txn_id": adj_txn_id,
        "discrepancy_id": new_discrepancy.discrepancy_id,
        "amount_diff": amount_diff,
        "removed_discrepancy_ids": [d.discrepancy_id for d in related_discrepancies],
        "removed_discrepancies": [d.to_dict() for d in related_discrepancies],
    }
    batch.manual_link_history.append(history_entry)

    storage.save(batch)

    audit = _get_audit(storage)
    summary = (
        f"手工关联 {bank_txn_id} <-> {system_txn_id}, 调整类型 {adj_type}, "
        f"金额差 {amount_diff:.2f}, 操作人 {reviewer}"
    )
    if note:
        summary += f", 备注: {note}"
    audit.log("manual_link", batch_id, 1, summary)
    _maybe_cleanup_audit(audit, storage)

    console.print(f"[green]OK[/] 手工关联成功")
    console.print(f"  银行: [cyan]{bank_txn_id}[/] 金额 {bank_txn.amount:.2f} 日期 {bank_txn.date}")
    console.print(f"  系统: [green]{system_txn_id}[/] 金额 {system_txn.amount:.2f} 日期 {system_txn.date}")
    console.print(f"  金额差: [yellow]{amount_diff:.2f}[/]")
    console.print(f"  调整类型: [magenta]{adj_type}[/]")
    console.print(f"  调整单号: {adj_txn_id}")
    console.print(f"  差异ID: {new_discrepancy.discrepancy_id}")


# ── undo ───────────────────────────────────────────────────
@cli.command(help="撤销最近一次 manual-link 操作")
@click.option("--batch-id", "-b", required=True, help="批次ID")
def undo(batch_id: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止操作。请先 reopen 后再操作。")
        sys.exit(1)
    _check_archived_write_block(batch, "撤销手工关联")

    if not batch.manual_link_history:
        console.print("[yellow]没有可撤销的手工关联操作[/]")
        return

    last_entry = batch.manual_link_history.pop()

    adj_txn_id = last_entry["adjustment_txn_id"]
    batch.adjustment_txns = [
        t for t in batch.adjustment_txns if t.txn_id != adj_txn_id
    ]

    disp_id = last_entry["discrepancy_id"]
    batch.discrepancies = [
        d for d in batch.discrepancies if d.discrepancy_id != disp_id
    ]

    from .models import Discrepancy
    for disp_data in last_entry.get("removed_discrepancies", []):
        restored = Discrepancy(
            discrepancy_id=disp_data["discrepancy_id"],
            discrepancy_type=DiscrepancyType(disp_data["discrepancy_type"]),
            status=DiscrepancyStatus(disp_data["status"]),
            match_level=MatchLevel(disp_data.get("match_level", MatchLevel.EXACT.value)),
            bank_txn=Transaction(
                txn_id=disp_data["bank_txn"]["txn_id"],
                amount=disp_data["bank_txn"]["amount"],
                date=disp_data["bank_txn"]["date"],
                file_type=FileType(disp_data["bank_txn"]["file_type"]),
                source_file=disp_data["bank_txn"]["source_file"],
                source_row=disp_data["bank_txn"]["source_row"],
                counterparty=disp_data["bank_txn"].get("counterparty", ""),
                description=disp_data["bank_txn"].get("description", ""),
                currency=disp_data["bank_txn"].get("currency", "CNY"),
                raw_data=disp_data["bank_txn"].get("raw_data", {}),
            ) if disp_data.get("bank_txn") else None,
            system_txn=Transaction(
                txn_id=disp_data["system_txn"]["txn_id"],
                amount=disp_data["system_txn"]["amount"],
                date=disp_data["system_txn"]["date"],
                file_type=FileType(disp_data["system_txn"]["file_type"]),
                source_file=disp_data["system_txn"]["source_file"],
                source_row=disp_data["system_txn"]["source_row"],
                counterparty=disp_data["system_txn"].get("counterparty", ""),
                description=disp_data["system_txn"].get("description", ""),
                currency=disp_data["system_txn"].get("currency", "CNY"),
                raw_data=disp_data["system_txn"].get("raw_data", {}),
            ) if disp_data.get("system_txn") else None,
            adjustment_txn=Transaction(
                txn_id=disp_data["adjustment_txn"]["txn_id"],
                amount=disp_data["adjustment_txn"]["amount"],
                date=disp_data["adjustment_txn"]["date"],
                file_type=FileType(disp_data["adjustment_txn"]["file_type"]),
                source_file=disp_data["adjustment_txn"]["source_file"],
                source_row=disp_data["adjustment_txn"]["source_row"],
                counterparty=disp_data["adjustment_txn"].get("counterparty", ""),
                description=disp_data["adjustment_txn"].get("description", ""),
                currency=disp_data["adjustment_txn"].get("currency", "CNY"),
                raw_data=disp_data["adjustment_txn"].get("raw_data", {}),
            ) if disp_data.get("adjustment_txn") else None,
            message=disp_data.get("message", ""),
            reviewer=disp_data.get("reviewer"),
            note=disp_data.get("note", ""),
            rollback_history=disp_data.get("rollback_history", []),
            created_at=disp_data.get("created_at"),
            updated_at=disp_data.get("updated_at"),
        )
        batch.discrepancies.append(restored)

    storage.save(batch)

    audit = _get_audit(storage)
    summary = (
        f"撤销手工关联: {last_entry['bank_txn_id']} <-> {last_entry['system_txn_id']}, "
        f"操作人 {last_entry['reviewer']}"
    )
    audit.log("undo_manual_link", batch_id, 1, summary)
    _maybe_cleanup_audit(audit, storage)

    console.print(f"[green]OK[/] 已撤销最近一次手工关联")
    console.print(f"  银行: [cyan]{last_entry['bank_txn_id']}[/]")
    console.print(f"  系统: [green]{last_entry['system_txn_id']}[/]")
    console.print(f"  调整类型: [magenta]{last_entry['adjustment_type']}[/]")
    console.print(f"  操作时间: {last_entry['timestamp']}")


# ── claim group (list/take/release/export) ────────────────
@cli.group("claim", help="差异认领工作台: 多人协作认领、释放、导出交接清单")
def claim_group() -> None:
    pass


_STATUS_STYLE_MAP = {
    ClaimStatus.PENDING.value: "dim",
    ClaimStatus.CLAIMED.value: "green",
    ClaimStatus.RELEASED.value: "yellow",
}


def _render_claim_list(result: ClaimListResult) -> None:
    if not result.records:
        console.print("[yellow]无符合条件的认领记录[/]")
        return

    table = Table(
        title=f"认领记录（共 {result.total_count} 条，"
              f"显示前 {len(result.display_records)} 条）"
    )
    table.add_column("认领ID", style="cyan", overflow="fold")
    table.add_column("批次ID", style="bold")
    table.add_column("差异ID", style="green", overflow="fold")
    table.add_column("认领人", style="magenta")
    table.add_column("状态", style="yellow")
    table.add_column("认领时间", style="dim")
    table.add_column("过期时间", style="dim")
    table.add_column("备注", style="dim", overflow="fold")

    for r in result.display_records:
        status_style = _STATUS_STYLE_MAP.get(r.status.value, "")
        status_text = f"[{status_style}]{r.status.value}[/]" if status_style else r.status.value
        table.add_row(
            r.claim_id,
            r.batch_id,
            r.discrepancy_id,
            r.claimant or "-",
            status_text,
            r.claimed_at[:19].replace("T", " ") if r.claimed_at else "-",
            r.expires_at[:19].replace("T", " ") if r.expires_at else "-",
            r.note,
        )
    console.print(table)

    if result.by_status:
        sum_table = Table(title="认领汇总 - 按状态")
        sum_table.add_column("状态", style="bold")
        sum_table.add_column("数量", justify="right")
        for s, c in sorted(result.by_status.items()):
            st = _STATUS_STYLE_MAP.get(s, "")
            sum_table.add_row(f"[{st}]{s}[/]" if st else s, str(c))
        console.print(sum_table)

    if result.by_claimant:
        uc_table = Table(title="认领汇总 - 按认领人")
        uc_table.add_column("认领人", style="bold")
        uc_table.add_column("数量", justify="right")
        for u, c in sorted(result.by_claimant.items()):
            uc_table.add_row(u, str(c))
        console.print(uc_table)


def _render_claim_take(result: ClaimTakeResult) -> None:
    if result.successes:
        console.print(f"[green]OK[/] 成功认领 [bold]{len(result.successes)}[/] 条差异:")
        table = Table(title="成功认领清单")
        table.add_column("差异ID", style="green")
        table.add_column("认领人", style="magenta")
        table.add_column("过期时间", style="yellow")
        table.add_column("备注", style="dim", overflow="fold")
        for s in result.successes:
            table.add_row(
                s.discrepancy_id,
                s.claimant,
                s.expires_at[:19].replace("T", " ") if s.expires_at else "永不过期",
                s.note,
            )
        console.print(table)

    if result.failures:
        console.print(f"[yellow]WARN[/] 失败 [bold]{len(result.failures)}[/] 条:")
        table = Table(title="失败清单")
        table.add_column("差异ID", style="red")
        table.add_column("失败原因", style="yellow")
        for f in result.failures:
            table.add_row(f["discrepancy_id"], f["reason"])
        console.print(table)


def _render_claim_release(result: ClaimReleaseResult) -> None:
    release_type = "[magenta]管理员强制释放[/]" if result.force else "[green]本人释放[/]"

    if result.successes:
        console.print(f"[green]OK[/] {release_type}成功 [bold]{len(result.successes)}[/] 条差异:")
        table = Table(title="释放成功清单")
        table.add_column("差异ID", style="green")
        table.add_column("原认领人", style="magenta")
        table.add_column("释放方式", style="yellow")
        table.add_column("释放原因", style="dim", overflow="fold")
        for s in result.successes:
            table.add_row(
                s.discrepancy_id,
                s.claimant or "-",
                "强制释放" if s.is_force_release else "本人释放",
                s.release_reason or "-",
            )
        console.print(table)

    if result.failures:
        console.print(f"[yellow]WARN[/] 失败 [bold]{len(result.failures)}[/] 条:")
        table = Table(title="失败清单")
        table.add_column("差异ID", style="red")
        table.add_column("失败原因", style="yellow")
        for f in result.failures:
            table.add_row(f["discrepancy_id"], f["reason"])
        console.print(table)


@claim_group.command("list", help="按批次/处理人/状态筛选认领记录")
@click.option("--batch-id", "-b", default=None, help="批次ID (可选)")
@click.option("--claimant", "-u", default=None, help="按认领人筛选 (可选)")
@click.option("--status", "-s", default=None,
              type=click.Choice(["pending", "claimed", "released"]),
              help="按状态筛选: pending(待处理)/claimed(已认领)/released(已释放) (可选)")
@click.option("--limit", "-n", default=50, help="显示条数")
def claim_list(
    batch_id: Optional[str],
    claimant: Optional[str],
    status: Optional[str],
    limit: int,
) -> None:
    svc = _get_claim_service()
    status_enum = ClaimStatus(status) if status else None
    try:
        result = svc.do_list(
            batch_id=batch_id, claimant=claimant,
            status=status_enum, limit=limit,
        )
    except BatchValidationError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)

    _render_claim_list(result)


@claim_group.command("take", help="批量认领差异，支持过期时间和备注")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--discrepancy-ids", "-d", required=True,
              help="差异ID，多个用逗号分隔，如 DISP-XXX,DISP-YYY")
@click.option("--claimant", "-u", required=True, help="认领人")
@click.option("--expires-hours", "-e", default=None, type=int,
              help="过期时间（小时），到期后自动释放")
@click.option("--note", "-n", default="", help="认领备注")
def claim_take(
    batch_id: str,
    discrepancy_ids: str,
    claimant: str,
    expires_hours: Optional[int],
    note: str,
) -> None:
    svc = _get_claim_service()
    try:
        result = svc.do_take(
            batch_id=batch_id,
            discrepancy_ids_raw=discrepancy_ids,
            claimant=claimant,
            expires_hours=expires_hours,
            note=note,
        )
    except BatchValidationError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)
    except DiscrepancyValidationError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)
    except OperatorMissingError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)
    except ClaimError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)

    _render_claim_take(result)


@claim_group.command("release", help="释放认领（本人释放 / 管理员 --force 强制释放）")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--discrepancy-ids", "-d", required=True,
              help="差异ID，多个用逗号分隔")
@click.option("--operator", "-u", required=True, help="操作人（认领人本人或管理员）")
@click.option("--reason", "-r", default="",
              help="释放原因（管理员强制释放时必填）")
@click.option("--force", is_flag=True, help="管理员强制释放（需提供 --reason）")
def claim_release(
    batch_id: str,
    discrepancy_ids: str,
    operator: str,
    reason: str,
    force: bool,
) -> None:
    svc = _get_claim_service()
    try:
        result = svc.do_release(
            batch_id=batch_id,
            discrepancy_ids_raw=discrepancy_ids,
            operator=operator,
            reason=reason,
            force=force,
        )
    except BatchValidationError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)
    except DiscrepancyValidationError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)
    except (OperatorMissingError, ClaimError) as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)

    _render_claim_release(result)


@claim_group.command("export", help="导出交接清单（JSON/CSV）")
@click.option("--batch-id", "-b", default=None, help="批次ID (可选)")
@click.option("--claimant", "-u", default=None, help="按认领人筛选 (可选)")
@click.option("--status", "-s", default=None,
              type=click.Choice(["pending", "claimed", "released"]),
              help="按状态筛选 (可选)")
@click.option("--output", "-o", required=True, help="输出文件路径")
@click.option("--format", "-f", "fmt", required=True,
              type=click.Choice(["json", "csv"]),
              help="导出格式: json / csv")
def claim_export(
    batch_id: Optional[str],
    claimant: Optional[str],
    status: Optional[str],
    output: str,
    fmt: str,
) -> None:
    svc = _get_claim_service()
    status_enum = ClaimStatus(status) if status else None
    try:
        result = svc.do_export(
            output=output, fmt=fmt,
            batch_id=batch_id, claimant=claimant,
            status=status_enum,
        )
    except BatchValidationError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)
    except ExportPathConflictError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)
    except OSError as e:
        console.print(f"[red]ERR[/] 导出失败: {e}")
        sys.exit(1)

    console.print(
        f"[green]OK[/] 已导出 [bold]{result.count}[/] 条认领记录到 [cyan]{result.output_path}[/]"
        f" (格式: {fmt.upper()})"
    )


# ── batch group (archive/restore/cleanup) ─────────────────
@cli.group("batch", help="批次归档/恢复/清理管理")
def batch_group() -> None:
    pass


@batch_group.command("archive", help="归档批次（移到 archive 目录，禁写仅读）")
@click.argument("batch_id")
@click.option("--operator", "-o", default="", help="操作人（会写入审计）")
@click.option("--note", "-n", default="", help="备注（会写入审计）")
def batch_archive(batch_id: str, operator: str, note: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    if storage.batch_is_archived(batch_id):
        batch = storage.load(batch_id)
        console.print(f"[yellow]WARN[/] 批次已处于归档状态: [bold]{batch.name}[/] ({batch_id})")
        return

    try:
        batch = storage.archive_batch(batch_id)
    except FileNotFoundError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)

    audit = _get_audit(storage)
    audit.log_archive("archive", batch_id, batch.name, operator, note)
    audit.log("archive", batch_id, 0,
              f"归档批次 {batch.name}" + (f", 操作人: {operator}" if operator else "")
              + (f", 备注: {note}" if note else ""))
    _maybe_cleanup_audit(audit, storage)

    console.print(f"[green]OK[/] 批次已归档: [bold]{batch.name}[/] ({batch_id})")
    console.print(f"  位置: {storage.archive_dir}")
    console.print(f"  状态: archived（禁写，仅读）")
    console.print(f"  [dim]恢复请执行: bank-reconcile batch restore {batch_id}[/]")


@batch_group.command("restore", help="从归档恢复批次（移回正常目录，恢复为 closed）")
@click.argument("batch_id")
@click.option("--operator", "-o", default="", help="操作人（会写入审计）")
@click.option("--note", "-n", default="", help="备注（会写入审计）")
def batch_restore(batch_id: str, operator: str, note: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    if not storage.batch_is_archived(batch_id):
        batch = storage.load(batch_id)
        console.print(f"[yellow]WARN[/] 批次不在归档目录: [bold]{batch.name}[/] ({batch_id}), 当前状态: {batch.status.value}")
        return

    try:
        batch = storage.restore_batch(batch_id)
    except FileNotFoundError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)

    audit = _get_audit(storage)
    audit.log_archive("restore", batch_id, batch.name, operator, note)
    audit.log("restore", batch_id, 0,
              f"恢复归档批次 {batch.name}（状态 {batch.status.value}）"
              + (f", 操作人: {operator}" if operator else "")
              + (f", 备注: {note}" if note else ""))
    _maybe_cleanup_audit(audit, storage)

    console.print(f"[green]OK[/] 批次已从归档恢复: [bold]{batch.name}[/] ({batch_id})")
    console.print(f"  当前状态: {batch.status.value}（如需导入/匹配，请再执行 reopen）")
    if batch.status.value == "closed":
        console.print(f"  [dim]重新打开: bank-reconcile reopen -b {batch_id}[/]")


@batch_group.command("cleanup", help="清理超期归档批次（读 config.yaml 的 retention_days，默认 365 天）")
@click.option("--force", is_flag=True, help="真正删除（默认只预览不删除）")
def batch_cleanup(force: bool) -> None:
    storage = _get_storage()
    cfg = load_config(storage.storage_dir)
    retention_days = cfg.get("retention_days", 365)

    if force:
        expired = storage.cleanup_archives(retention_days, force=True)
        if not expired:
            console.print(f"[green]OK[/] 无超期归档批次（保留期 {retention_days} 天）")
            return

        table = Table(title=f"已删除超期归档批次（保留期 {retention_days} 天）")
        table.add_column("批次ID", style="cyan")
        table.add_column("名称", style="bold")
        table.add_column("归档/更新时间", style="dim")
        table.add_column("大小", justify="right", style="dim")

        total_size = 0
        for item in expired:
            size_kb = item["size_bytes"] / 1024
            total_size += item["size_bytes"]
            ts = (item["archived_at"] or "")[:19].replace("T", " ")
            table.add_row(
                item["batch_id"],
                item["name"],
                ts,
                f"{size_kb:.1f} KB",
            )
        console.print(table)
        console.print(f"[green]OK[/] 共删除 {len(expired)} 个超期批次，释放 {total_size/1024:.1f} KB")
    else:
        expired = storage.cleanup_archives(retention_days, force=False)
        if not expired:
            console.print(f"[green]OK[/] 无超期归档批次（保留期 {retention_days} 天）")
            return

        table = Table(title=f"预览: 超期归档批次（保留期 {retention_days} 天，加 --force 才真正删除）")
        table.add_column("批次ID", style="cyan")
        table.add_column("名称", style="bold")
        table.add_column("归档/更新时间", style="dim")
        table.add_column("大小", justify="right", style="dim")

        total_size = 0
        for item in expired:
            size_kb = item["size_bytes"] / 1024
            total_size += item["size_bytes"]
            ts = (item["archived_at"] or "")[:19].replace("T", " ")
            table.add_row(
                item["batch_id"],
                item["name"],
                ts,
                f"{size_kb:.1f} KB",
            )
        console.print(table)
        console.print(f"[yellow]预览模式[/]: 共 {len(expired)} 个超期批次，预计释放 {total_size/1024:.1f} KB")
        console.print(f"  [dim]加 --force 真正删除: bank-reconcile batch cleanup --force[/]")


def main() -> None:
    cli()


# ── config group ───────────────────────────────────────────
@cli.group("config", help="查看/修改全局配置（列名别名等）")
def config_group() -> None:
    pass


FILE_TYPE_CHOICES = ["bank", "system", "adjustment"]
_FILE_TYPE_MAP = {
    "bank": FileType.BANK_STATEMENT,
    "system": FileType.SYSTEM_RECEIPT,
    "adjustment": FileType.MANUAL_ADJUSTMENT,
}
_FILE_TYPE_LABEL = {
    FileType.BANK_STATEMENT: "银行回单",
    FileType.SYSTEM_RECEIPT: "系统流水",
    FileType.MANUAL_ADJUSTMENT: "手工调整",
}


@config_group.command("set", help="设置列名别名: config set --type bank 交易单号 txn_id")
@click.option("--type", "-t", "file_type", required=True,
              type=click.Choice(FILE_TYPE_CHOICES),
              help="文件类型")
@click.argument("alias_name")
@click.argument("standard_field")
def config_set(file_type: str, alias_name: str, standard_field: str) -> None:
    storage = _get_storage()
    ft = _FILE_TYPE_MAP[file_type]

    if standard_field not in STANDARD_FIELDS:
        console.print(
            f"[red]ERR[/] 标准字段 '{standard_field}' 无效。\n"
            f"可用标准字段: [cyan]{', '.join(sorted(STANDARD_FIELDS))}[/]"
        )
        sys.exit(1)

    try:
        cfg = set_column_alias(storage.storage_dir, ft, alias_name, standard_field)
    except AliasConflictError as e:
        console.print(f"[red]ERR[/] 别名冲突: {e}")
        sys.exit(1)

    ft_key = FILE_TYPE_ALIAS_KEYS[ft]
    console.print(
        f"[green]OK[/] 已为 [bold]{_FILE_TYPE_LABEL[ft]}[/] 设置别名: "
        f"[cyan]{alias_name}[/] → [magenta]{standard_field}[/]"
    )
    aliases = cfg["column_aliases"].get(ft_key, {})
    if aliases:
        console.print(f"  当前别名映射 ({len(aliases)} 条):")
        for a, s in sorted(aliases.items()):
            console.print(f"    [cyan]{a}[/] → [magenta]{s}[/]")


@config_group.command("show", help="显示完整配置（含列名别名）")
def config_show() -> None:
    storage = _get_storage()
    cfg = load_config(storage.storage_dir)

    console.print(Panel.fit(
        f"[bold]存储目录:[/] {storage.storage_dir}\n"
        f"[bold]审计保留天数:[/] {cfg.get('audit_retention_days', 90)}\n"
        f"[bold]归档保留天数:[/] {cfg.get('retention_days', 365)}",
        title="全局配置",
    ))

    ca = cfg.get("column_aliases", {})
    has_any = False
    for choice in FILE_TYPE_CHOICES:
        ft = _FILE_TYPE_MAP[choice]
        ft_key = FILE_TYPE_ALIAS_KEYS[ft]
        aliases = ca.get(ft_key, {})
        if aliases:
            has_any = True
            table = Table(title=f"列名别名 - {_FILE_TYPE_LABEL[ft]} ({choice})")
            table.add_column("别名列名", style="cyan")
            table.add_column("标准字段", style="magenta")
            for a, s in sorted(aliases.items()):
                table.add_row(a, s)
            console.print(table)

    if not has_any:
        console.print("[yellow]尚未配置任何列名别名。[/]")
        console.print(f"使用示例: [cyan]bank-reconcile config set -t bank 交易单号 txn_id[/]")

    console.print(f"\n[dim]可用标准字段: {', '.join(sorted(STANDARD_FIELDS))}[/]")
    console.print(f"[dim]可用文件类型: {', '.join(FILE_TYPE_CHOICES)}[/]")


def _get_schedule_storage() -> ScheduleStorage:
    storage_dir = os.environ.get("BANK_RECONCILE_HOME")
    return ScheduleStorage(storage_dir or os.path.join(os.getcwd(), ".bank_reconcile"))


def _parse_steps_raw(steps_raw: str) -> List[ScheduleStep]:
    step_map = {
        "import": ScheduleStep.IMPORT,
        "match": ScheduleStep.MATCH,
        "report": ScheduleStep.REPORT,
    }
    steps = []
    for s in steps_raw.split(","):
        s = s.strip().lower()
        if not s:
            continue
        if s not in step_map:
            raise ValueError(f"未知步骤: {s}，可选: import/match/report")
        steps.append(step_map[s])
    if not steps:
        raise ValueError("至少需要指定一个步骤")
    return steps


# ── schedule group ─────────────────────────────────────────
@cli.group("schedule", help="定时任务调度管理")
def schedule_group() -> None:
    pass


@schedule_group.command("add", help="创建定时任务: schedule add --name 日常对账 --batch BATCH-XXX --cron '02:00' --steps import,match,report --config task.yaml")
@click.option("--name", "-n", required=True, help="任务名称")
@click.option("--batch-id", "-b", "batch_id", required=True, help="批次ID")
@click.option("--cron", required=True, help="触发时间: 'HH:MM'（每天）或 'every N minutes'")
@click.option("--steps", "steps_raw", required=True, help="执行步骤，逗号分隔: import,match,report（可只选几项）")
@click.option("--config", "config_file", default=None, help="YAML 配置文件路径（包含 import_configs/report_config 等）")
@click.option("--expires-at", default=None, help="到期时间 ISO 格式，如 2026-12-31T23:59:59")
@click.option("--max-retries", default=3, type=int, help="失败最大重试次数")
def schedule_add(name: str, batch_id: str, cron: str, steps_raw: str,
                 config_file: Optional[str], expires_at: Optional[str],
                 max_retries: int) -> None:
    storage = _get_storage()
    sched_storage = _get_schedule_storage()
    audit = _get_audit(storage)

    if not storage.batch_exists_anywhere(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    try:
        steps = _parse_steps_raw(steps_raw)
    except ValueError as e:
        console.print(f"[red]ERR[/] --steps 参数错误: {e}")
        sys.exit(1)

    import_configs: List[ScheduleImportConfig] = []
    rule_file: Optional[str] = None
    report_config: Optional[ScheduleReportConfig] = None

    if config_file:
        if not os.path.isfile(config_file):
            console.print(f"[red]ERR[/] 配置文件不存在: {config_file}")
            sys.exit(1)
        try:
            import yaml
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if not isinstance(cfg, dict):
                raise ValueError("配置文件必须是对象（键值对）")

            if "import_configs" in cfg and isinstance(cfg["import_configs"], list):
                for ic in cfg["import_configs"]:
                    if not isinstance(ic, dict) or "file_type" not in ic or "file_path" not in ic:
                        raise ValueError("import_configs 每项必须包含 file_type 和 file_path")
                    import_configs.append(ScheduleImportConfig(
                        file_type=ic["file_type"],
                        file_path=ic["file_path"],
                        col_map=ic.get("col_map"),
                    ))

            if "rule_file" in cfg:
                rule_file = cfg["rule_file"]

            if "report_config" in cfg and isinstance(cfg["report_config"], dict):
                rc = cfg["report_config"]
                if "output_path" not in rc:
                    raise ValueError("report_config 必须包含 output_path")
                report_config = ScheduleReportConfig(
                    output_path=rc["output_path"],
                    with_summary=rc.get("with_summary", False),
                )
        except Exception as e:
            console.print(f"[red]ERR[/] 解析配置文件失败: {e}")
            sys.exit(1)

    task = ScheduleTask.create(
        name=name,
        batch_id=batch_id,
        cron=cron,
        steps=steps,
        import_configs=import_configs,
        rule_file=rule_file,
        report_config=report_config,
        expires_at=expires_at,
        max_retries=max_retries,
    )
    sched_storage.save(task)

    audit.log("schedule_add", task.task_id, 0,
              f"创建定时任务 {name}，批次 {batch_id}，cron={cron}，步骤={','.join(s.value for s in steps)}")
    _maybe_cleanup_audit(audit, storage)

    console.print(f"[green]OK[/] 定时任务已创建: [bold]{task.name}[/] (ID: [cyan]{task.task_id}[/])")
    console.print(f"  批次: {batch_id}")
    console.print(f"  触发: {cron}")
    console.print(f"  步骤: {', '.join(s.value for s in steps)}")
    if expires_at:
        console.print(f"  到期: {expires_at}")
    if import_configs:
        console.print(f"  import 配置: {len(import_configs)} 个文件")
    if report_config:
        console.print(f"  report 输出: {report_config.output_path}")


@schedule_group.command("list", help="列出所有定时任务")
def schedule_list() -> None:
    sched_storage = _get_schedule_storage()
    tasks = sched_storage.list_tasks()
    if not tasks:
        console.print("[yellow]尚无定时任务[/], 使用 [cyan]bank-reconcile schedule add ...[/] 创建")
        return

    table = Table(title="定时任务列表")
    table.add_column("任务ID", style="cyan")
    table.add_column("名称", style="bold")
    table.add_column("批次ID", style="green")
    table.add_column("触发", style="yellow")
    table.add_column("步骤", style="magenta")
    table.add_column("状态", style="bold")
    table.add_column("上次运行", style="dim")
    table.add_column("重试", justify="right")

    for t in tasks:
        status = t.get("status", "active")
        status_style = {"active": "green", "paused": "yellow", "completed": "cyan", "expired": "dim"}.get(status, "")
        last_run = t.get("last_run_at") or "-"
        if last_run != "-":
            last_run = last_run[:19].replace("T", " ")
            lr_status = t.get("last_run_status") or ""
            if lr_status:
                last_run += f" ({lr_status})"
        table.add_row(
            t["task_id"],
            t["name"],
            t["batch_id"],
            t["cron"],
            ", ".join(t.get("steps", [])),
            f"[{status_style}]{status}[/]" if status_style else status,
            last_run,
            str(t.get("retry_count", 0)),
        )
    console.print(table)


@schedule_group.command("show", help="查看定时任务详情")
@click.argument("task_id")
def schedule_show(task_id: str) -> None:
    sched_storage = _get_schedule_storage()
    if not sched_storage.task_exists(task_id):
        console.print(f"[red]ERR[/] 任务不存在: {task_id}")
        sys.exit(1)

    task = sched_storage.load(task_id)
    status_style = {"active": "green", "paused": "yellow", "completed": "cyan", "expired": "dim"}.get(task.status.value, "")

    lines = [
        f"[bold cyan]任务ID:[/] {task.task_id}",
        f"[bold]名称:[/] {task.name}",
        f"[bold]批次ID:[/] {task.batch_id}",
        f"[bold]触发:[/] {task.cron}",
        f"[bold]步骤:[/] {', '.join(s.value for s in task.steps)}",
        f"[bold]状态:[/] [{status_style}]{task.status.value}[/]" if status_style else f"[bold]状态:[/] {task.status.value}",
        f"[bold]创建时间:[/] {task.created_at[:19].replace('T', ' ')}",
        f"[bold]更新时间:[/] {task.updated_at[:19].replace('T', ' ')}",
    ]
    if task.expires_at:
        lines.append(f"[bold]到期时间:[/] {task.expires_at}")
    if task.last_run_at:
        lines.append(f"[bold]上次运行:[/] {task.last_run_at[:19].replace('T', ' ')} ({task.last_run_status.value if task.last_run_status else 'unknown'})")
    lines.append(f"[bold]重试次数:[/] {task.retry_count}/{task.max_retries}")

    if task.import_configs:
        lines.append("")
        lines.append("[bold]Import 配置:[/]")
        for i, ic in enumerate(task.import_configs, 1):
            lines.append(f"  {i}. {ic.file_type}: {ic.file_path}")
            if ic.col_map:
                lines.append(f"     col_map: {ic.col_map}")

    if task.rule_file:
        lines.append(f"[bold]规则文件:[/] {task.rule_file}")

    if task.report_config:
        lines.append("")
        lines.append(f"[bold]Report 配置:[/]")
        lines.append(f"  输出路径: {task.report_config.output_path}")
        lines.append(f"  附带摘要: {'是' if task.report_config.with_summary else '否'}")

    console.print(Panel.fit("\n".join(lines), title="任务详情"))


@schedule_group.command("update", help="更新定时任务配置")
@click.argument("task_id")
@click.option("--name", default=None, help="任务名称")
@click.option("--cron", default=None, help="触发时间")
@click.option("--steps", "steps_raw", default=None, help="执行步骤，逗号分隔")
@click.option("--config", "config_file", default=None, help="YAML 配置文件路径")
@click.option("--status", default=None, type=click.Choice(["active", "paused"]), help="状态: active/paused")
@click.option("--expires-at", default=None, help="到期时间 ISO 格式")
def schedule_update(task_id: str, name: Optional[str], cron: Optional[str],
                    steps_raw: Optional[str], config_file: Optional[str],
                    status: Optional[str], expires_at: Optional[str]) -> None:
    storage = _get_storage()
    sched_storage = _get_schedule_storage()
    audit = _get_audit(storage)

    if not sched_storage.task_exists(task_id):
        console.print(f"[red]ERR[/] 任务不存在: {task_id}")
        sys.exit(1)

    task = sched_storage.load(task_id)
    changed_fields = []

    if name:
        task.name = name
        changed_fields.append(f"name={name}")
    if cron:
        task.cron = cron
        changed_fields.append(f"cron={cron}")
    if steps_raw:
        try:
            steps = _parse_steps_raw(steps_raw)
            task.steps = steps
            changed_fields.append(f"steps={','.join(s.value for s in steps)}")
        except ValueError as e:
            console.print(f"[red]ERR[/] --steps 参数错误: {e}")
            sys.exit(1)
    if status:
        task.status = ScheduleStatus(status)
        changed_fields.append(f"status={status}")
    if expires_at is not None:
        task.expires_at = expires_at or None
        changed_fields.append(f"expires_at={expires_at or '(removed)'}")

    if config_file:
        if not os.path.isfile(config_file):
            console.print(f"[red]ERR[/] 配置文件不存在: {config_file}")
            sys.exit(1)
        try:
            import yaml
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if not isinstance(cfg, dict):
                raise ValueError("配置文件必须是对象")

            if "import_configs" in cfg and isinstance(cfg["import_configs"], list):
                task.import_configs = []
                for ic in cfg["import_configs"]:
                    if not isinstance(ic, dict) or "file_type" not in ic or "file_path" not in ic:
                        raise ValueError("import_configs 每项必须包含 file_type 和 file_path")
                    task.import_configs.append(ScheduleImportConfig(
                        file_type=ic["file_type"],
                        file_path=ic["file_path"],
                        col_map=ic.get("col_map"),
                    ))
                changed_fields.append("import_configs updated")

            if "rule_file" in cfg:
                task.rule_file = cfg["rule_file"] or None
                changed_fields.append(f"rule_file={cfg['rule_file'] or '(removed)'}")

            if "report_config" in cfg:
                rc = cfg["report_config"]
                if rc is None:
                    task.report_config = None
                    changed_fields.append("report_config (removed)")
                elif isinstance(rc, dict):
                    if "output_path" not in rc:
                        raise ValueError("report_config 必须包含 output_path")
                    task.report_config = ScheduleReportConfig(
                        output_path=rc["output_path"],
                        with_summary=rc.get("with_summary", False),
                    )
                    changed_fields.append(f"report_config output={rc['output_path']}")
        except Exception as e:
            console.print(f"[red]ERR[/] 解析配置文件失败: {e}")
            sys.exit(1)

    if not changed_fields:
        console.print("[yellow]未指定任何更新项[/]")
        return

    sched_storage.save(task)
    audit.log("schedule_update", task_id, 0,
              f"更新定时任务 {task.name}: {', '.join(changed_fields)}")
    _maybe_cleanup_audit(audit, storage)

    console.print(f"[green]OK[/] 任务已更新: [bold]{task.name}[/] ({task_id})")
    for cf in changed_fields:
        console.print(f"  - {cf}")


@schedule_group.command("delete", help="删除定时任务")
@click.argument("task_id")
@click.option("--force", "-f", is_flag=True, help="跳过确认")
def schedule_delete(task_id: str, force: bool) -> None:
    storage = _get_storage()
    sched_storage = _get_schedule_storage()
    audit = _get_audit(storage)

    if not sched_storage.task_exists(task_id):
        console.print(f"[red]ERR[/] 任务不存在: {task_id}")
        sys.exit(1)

    task = sched_storage.load(task_id)
    if not force:
        console.print(f"即将删除任务: [bold]{task.name}[/] ({task_id})")
        confirm = input("确认删除? (y/N): ").strip().lower()
        if confirm not in ("y", "yes"):
            console.print("[yellow]已取消[/]")
            return

    if sched_storage.delete(task_id):
        audit.log("schedule_delete", task_id, 0, f"删除定时任务 {task.name}")
        _maybe_cleanup_audit(audit, storage)
        console.print(f"[green]OK[/] 任务已删除: {task.name} ({task_id})")
    else:
        console.print(f"[red]ERR[/] 删除失败: {task_id}")
        sys.exit(1)


@schedule_group.command("run", help="触发定时任务执行")
@click.argument("task_id")
@click.option("--now", is_flag=True, required=True, help="立即手动触发一次")
def schedule_run(task_id: str, now: bool) -> None:
    storage = _get_storage()
    sched_storage = _get_schedule_storage()
    audit = _get_audit(storage)

    if not sched_storage.task_exists(task_id):
        console.print(f"[red]ERR[/] 任务不存在: {task_id}")
        sys.exit(1)

    scheduler = Scheduler(storage.storage_dir)
    try:
        result = scheduler.run_task_now(task_id)
    except FileNotFoundError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)

    if result.get("skipped"):
        console.print(f"[yellow]跳过[/] {result.get('reason')}: {result.get('error')}")
        return

    if result["success"]:
        console.print(f"[green]OK[/] 任务执行成功: {task_id}")
        for step_name, sr in result.get("steps", {}).items():
            console.print(f"  {step_name}: {sr}")
    else:
        console.print(f"[red]ERR[/] 任务执行失败: {result.get('error')}")
        for step_name, sr in result.get("steps", {}).items():
            console.print(f"  {step_name}: {sr}")
        sys.exit(1)


# ── snapshot group ────────────────────────────────────────
@cli.group("snapshot", help="批次快照与恢复（可迁移打包）: create / info / restore")
def snapshot_group() -> None:
    pass


@snapshot_group.command("create", help="创建批次快照：打包数据、规则、审计、配置为可迁移文件")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--output", "-o", "output_path", default=None,
              help=f"输出文件路径（默认: 当前目录/<批次ID>_<名称>{SNAPSHOT_FILE_EXT}）")
def snapshot_create(batch_id: str, output_path: Optional[str]) -> None:
    storage = _get_storage()
    audit = _get_audit(storage)

    if not storage.batch_exists_anywhere(batch_id):
        err_msg = f"批次不存在: {batch_id}"
        console.print(f"[red]ERR[/] {err_msg}")
        audit.log("snapshot_create_fail", batch_id, 0, err_msg)
        _maybe_cleanup_audit(audit, storage)
        sys.exit(1)

    try:
        out_path, info = create_snapshot(batch_id, storage, output_path)
    except (FileNotFoundError, SnapshotError) as e:
        err_msg = f"创建快照失败: {e}"
        console.print(f"[red]ERR[/] {err_msg}")
        audit.log("snapshot_create_fail", batch_id, 0, err_msg)
        _maybe_cleanup_audit(audit, storage)
        sys.exit(1)
    except Exception as e:
        err_msg = f"创建快照时发生未知错误: {type(e).__name__}: {e}"
        console.print(f"[red]ERR[/] {err_msg}")
        audit.log("snapshot_create_fail", batch_id, 0, err_msg)
        _maybe_cleanup_audit(audit, storage)
        sys.exit(1)

    size_kb = info["file_size_bytes"] / 1024
    summary = (
        f"创建快照成功: {info['snapshot_id']}, "
        f"批次 {info['batch_name']}({info['batch_id']}), "
        f"大小 {size_kb:.1f} KB, 文件 {out_path}"
    )
    audit.log(
        "snapshot_create",
        batch_id,
        1,
        summary,
    )
    _maybe_cleanup_audit(audit, storage)

    console.print(Panel.fit(
        f"[bold green]快照创建成功[/]\n\n"
        f"[bold cyan]快照ID:[/] {info['snapshot_id']}\n"
        f"[bold cyan]版本:[/]   {info['snapshot_version']}\n"
        f"[bold cyan]创建时间:[/] {info['created_at'][:19].replace('T', ' ')}\n"
        f"[bold cyan]校验摘要:[/] {info['checksum'][:16]}...\n"
        f"[bold cyan]批次ID:[/]  {info['batch_id']}\n"
        f"[bold cyan]批次名:[/]  {info['batch_name']}\n"
        f"[bold cyan]文件大小:[/] {size_kb:.1f} KB\n"
        f"[bold cyan]输出路径:[/] [bold]{out_path}[/]",
        title="快照创建结果",
    ))


@snapshot_group.command("info", help="查看快照文件信息（会做完整性校验）")
@click.argument("snapshot_path")
def snapshot_info(snapshot_path: str) -> None:
    try:
        info = read_snapshot_info(snapshot_path)
    except FileNotFoundError as e:
        console.print(f"[red]ERR[/] {e}")
        sys.exit(1)
    except SnapshotCorruptedError as e:
        console.print(f"[red]ERR[/] 快照损坏或校验失败: {e}")
        sys.exit(1)
    except SnapshotVersionError as e:
        console.print(f"[red]ERR[/] 快照版本不兼容: {e}")
        sys.exit(1)
    except SnapshotError as e:
        console.print(f"[red]ERR[/] 读取快照失败: {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]ERR[/] 未知错误: {type(e).__name__}: {e}")
        sys.exit(1)

    ps = info.get("payload_summary", {})
    size_kb = info["file_size_bytes"] / 1024

    table = Table(title=f"快照信息 - {info['snapshot_id']}")
    table.add_column("字段", style="bold")
    table.add_column("值")

    table.add_row("快照ID", info["snapshot_id"])
    table.add_row("快照版本", info["snapshot_version"])
    table.add_row("创建时间", info["created_at"][:19].replace("T", " "))
    table.add_row("校验摘要 (SHA-256)", info["checksum"])
    table.add_row("原批次ID", info["batch_id"])
    table.add_row("原批次名称", info["batch_name"])
    table.add_row("原批次状态", info.get("batch_status", "未知"))
    table.add_row("差异数", str(info.get("discrepancy_count", 0)))
    table.add_row("银行交易数", str(info.get("bank_txn_count", 0)))
    table.add_row("系统交易数", str(info.get("system_txn_count", 0)))
    table.add_row("手工调整数", str(info.get("adjustment_txn_count", 0)))
    table.add_row("已导入文件数", str(info.get("imported_file_count", 0)))
    table.add_row("审计记录数", str(info.get("audit_record_count", 0)))
    table.add_row("文件大小", f"{size_kb:.2f} KB")
    table.add_row("文件路径", info["file_path"])

    ps_table = Table(title="Payload 概要")
    ps_table.add_column("字段", style="bold")
    ps_table.add_column("值")

    ps_table.add_row("内嵌规则", "是" if ps.get("has_rules_yaml") else "否")
    ps_table.add_row("审计记录", str(ps.get("audit_record_count", 0)))
    ps_table.add_row("配置键", ", ".join(ps.get("config_keys", [])) or "-")
    ps_table.add_row("有回滚历史的差异", str(ps.get("rollback_history_discrepancies", 0)))
    ps_table.add_row("手工关联历史", str(ps.get("manual_link_history_count", 0)))
    ps_table.add_row("导出历史", str(ps.get("exports_count", 0)))
    ps_table.add_row("批次创建时间", (ps.get("created_at") or "")[:19].replace("T", " ") or "-")
    ps_table.add_row("批次更新时间", (ps.get("updated_at") or "")[:19].replace("T", " ") or "-")
    ps_table.add_row("批次状态", ps.get("status") or "-")
    ps_table.add_row("原规则文件路径", ps.get("rule_file") or "-")

    console.print(Panel.fit(
        f"[green]快照完整性校验通过[/]\n",
        title="校验结果",
    ))
    console.print(table)
    console.print(ps_table)


@snapshot_group.command("restore", help="恢复快照到目标存储目录（默认当前工作目录的 .bank_reconcile）")
@click.argument("snapshot_path")
@click.option("--target-dir", "-d", default=None,
              help="目标存储目录（默认: 当前目录/.bank_reconcile 或 $BANK_RECONCILE_HOME）")
@click.option("--strategy", "-s", default="skip",
              type=click.Choice(["overwrite", "rename", "skip"]),
              help="冲突策略: skip（默认，遇冲突报错）/ overwrite（覆盖同ID批次）/ rename（新ID新名称）")
@click.option("--new-name", default=None, help="配合 --strategy rename 指定新批次名")
def snapshot_restore(snapshot_path: str, target_dir: Optional[str],
                     strategy: str, new_name: Optional[str]) -> None:
    if target_dir:
        target_storage = BatchStorage(target_dir)
    else:
        target_storage = _get_storage()

    target_audit = AuditStorage(target_storage.storage_dir)

    strategy_enum = ConflictStrategy(strategy)

    try:
        result = restore_snapshot(
            snapshot_path=snapshot_path,
            target_storage=target_storage,
            strategy=strategy_enum,
            new_name=new_name,
        )
    except FileNotFoundError as e:
        err_msg = f"恢复失败: {e}"
        console.print(f"[red]ERR[/] {err_msg}")
        target_audit.log("snapshot_restore_fail", "", 0, err_msg)
        sys.exit(1)
    except SnapshotCorruptedError as e:
        err_msg = f"恢复失败: 快照损坏或校验失败: {e}"
        console.print(f"[red]ERR[/] {err_msg}")
        target_audit.log("snapshot_restore_fail", "", 0, err_msg)
        sys.exit(1)
    except SnapshotVersionError as e:
        err_msg = f"恢复失败: 快照版本不兼容: {e}"
        console.print(f"[red]ERR[/] {err_msg}")
        target_audit.log("snapshot_restore_fail", "", 0, err_msg)
        sys.exit(1)
    except SnapshotConflictError as e:
        err_msg = f"恢复失败: 冲突 - {e}"
        console.print(f"[red]ERR[/] {err_msg}")
        target_audit.log("snapshot_restore_fail", "", 0, err_msg)
        sys.exit(1)
    except SnapshotError as e:
        err_msg = f"恢复失败: {e}"
        console.print(f"[red]ERR[/] {err_msg}")
        target_audit.log("snapshot_restore_fail", "", 0, err_msg)
        sys.exit(1)
    except Exception as e:
        err_msg = f"恢复失败: 未知错误 {type(e).__name__}: {e}"
        console.print(f"[red]ERR[/] {err_msg}")
        target_audit.log("snapshot_restore_fail", "", 0, err_msg)
        sys.exit(1)

    strategy_label = {
        "overwrite": "[red]overwrite[/] 覆盖",
        "rename": "[cyan]rename[/] 改名",
        "skip": "[green]skip[/] 无冲突直接导入",
    }.get(result["strategy"], result["strategy"])

    summary = (
        f"恢复快照 {result['snapshot_id']} 成功: 原批次 "
        f"{result['original_batch_name']}({result['original_batch_id']}) -> "
        f"{result['batch_name']}({result['batch_id']}), "
        f"策略 {result['strategy']}, 目标目录 {result['storage_dir']}"
    )
    target_audit.log("snapshot_restore", result["batch_id"], 1, summary)

    console.print(Panel.fit(
        f"[bold green]快照恢复成功[/]\n\n"
        f"[bold cyan]快照ID:[/]        {result['snapshot_id']}\n"
        f"[bold cyan]原批次ID:[/]      {result['original_batch_id']}\n"
        f"[bold cyan]原批次名:[/]      {result['original_batch_name']}\n"
        f"[bold cyan]新批次ID:[/]      {result['batch_id']}\n"
        f"[bold cyan]新批次名:[/]      [bold]{result['batch_name']}[/]\n"
        f"[bold cyan]冲突策略:[/]      {strategy_label}\n"
        f"[bold cyan]规则恢复:[/]      {'[green]是[/] ' + (result.get('rules_path') or '') if result['rules_restored'] else '[yellow]否[/]（原批次未关联规则）'}\n"
        f"[bold cyan]审计记录写回:[/]  {result['audit_inserted']} 条\n"
        f"[bold cyan]配置合并:[/]      {'[green]已执行[/]' if result['config_merged'] else '无'}\n"
        f"[bold cyan]目标目录:[/]      {result['storage_dir']}\n\n"
        f"[dim]提示: 使用 resume {result['batch_id']} 查看详情[/]\n"
        f"[dim]提示: 使用 export -b {result['batch_id']} -o <文件> 导出报告[/]\n"
        f"[dim]提示: 使用 diff -b {result['batch_id']} 查询差异[/]\n"
        f"[dim]提示: 使用 audit-log -b {result['batch_id']} 查询审计[/]",
        title="快照恢复结果",
    ))





# ── health group ──────────────────────────────────────────
@cli.group("health", help="批次健康检查: 一键检查批次数据完整性并导出报告")
def health_group() -> None:
    pass


@health_group.command("check", help="执行批次健康检查")
@click.option("--batch-id", "-b", required=True, help="批次ID")
def health_check(batch_id: str) -> None:
    storage = _get_storage()
    audit = _get_audit(storage)

    try:
        report = run_health_check(storage, batch_id)
    except HealthCheckCorruptedError as e:
        console.print(f"[red]ERR[/] 审计库损坏: {e}")
        try:
            audit.log("health_check_fail", batch_id, 0, f"健康检查失败: 审计库损坏 - {e}")
        except Exception:
            pass
        _maybe_cleanup_audit(audit, storage)
        sys.exit(1)
    except HealthCheckError as e:
        console.print(f"[red]ERR[/] {e}")
        try:
            audit.log("health_check_fail", batch_id, 0, f"健康检查失败: {e}")
        except Exception:
            pass
        _maybe_cleanup_audit(audit, storage)
        sys.exit(1)

    summary = report._summary()
    level_style = {
        IssueLevel.OK: "green",
        IssueLevel.INFO: "cyan",
        IssueLevel.WARNING: "yellow",
        IssueLevel.CRITICAL: "red",
    }
    overall_style = level_style.get(report.overall_status, "white")

    console.print(Panel.fit(
        f"[bold cyan]批次:[/] {report.batch_name}  [dim]({report.batch_id})[/]\n"
        f"[bold]状态:[/] [{overall_style}]{report.overall_status.value.upper()}[/]  "
        f"[bold]批次状态:[/] {report.batch_status}\n"
        f"[bold]检查时间:[/] {report.generated_at[:19].replace('T', ' ')}\n"
        f"[bold]问题汇总:[/] "
        f"[green]OK: {summary['ok']}[/]  "
        f"[cyan]INFO: {summary['info']}[/]  "
        f"[yellow]WARNING: {summary['warning']}[/]  "
        f"[red]CRITICAL: {summary['critical']}[/]",
        title="批次健康检查结果",
    ))

    category_labels = {
        CheckCategory.FILES: "导入文件",
        CheckCategory.RULES: "规则配置",
        CheckCategory.MATCH: "匹配结果",
        CheckCategory.MARKERS: "标记与回滚",
        CheckCategory.AUDIT: "审计日志",
    }

    for category in CheckCategory:
        category_issues = [i for i in report.issues if i.category == category]
        if not category_issues:
            continue
        table = Table(title=f"{category_labels.get(category, category.value)}")
        table.add_column("级别", style="bold")
        table.add_column("检查项", style="cyan")
        table.add_column("说明", overflow="fold")
        table.add_column("建议", overflow="fold")

        for issue in category_issues:
            style = level_style.get(issue.level, "white")
            table.add_row(
                f"[{style}]{issue.level.value.upper()}[/]",
                issue.check_name,
                issue.description,
                issue.suggestion or "-",
            )
        console.print(table)

    issue_count = len([i for i in report.issues if i.level in (IssueLevel.WARNING, IssueLevel.CRITICAL)])
    try:
        audit.log(
            "health_check", batch_id, issue_count,
            f"健康检查完成: 总体 {report.overall_status.value}, "
            f"OK={summary['ok']} INFO={summary['info']} "
            f"WARNING={summary['warning']} CRITICAL={summary['critical']}"
        )
    except Exception:
        pass
    _maybe_cleanup_audit(audit, storage)

    if report.overall_status == IssueLevel.CRITICAL:
        console.print("\n[red]存在严重问题，不建议继续导出或交接。[/]")
        sys.exit(2)
    elif report.overall_status == IssueLevel.WARNING:
        console.print("\n[yellow]存在警告问题，建议处理后再继续。[/]")


@health_group.command("export", help="导出健康检查报告 (JSON/CSV)")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--output", "-o", required=True, help="输出文件路径")
@click.option("--format", "-f", "fmt", required=True,
              type=click.Choice(["json", "csv"]),
              help="导出格式: json / csv")
@click.option("--force", is_flag=True, help="输出文件已存在时强制覆盖")
def health_export(batch_id: str, output: str, fmt: str, force: bool) -> None:
    storage = _get_storage()
    audit = _get_audit(storage)

    abs_output = os.path.abspath(output)
    if os.path.exists(abs_output) and not force:
        console.print(
            f"[red]ERR[/] 输出文件已存在: {abs_output}\n"
            f"  使用 [cyan]--force[/] 覆盖，或选择其他路径。"
        )
        try:
            audit.log(
                "health_export_fail", batch_id, 0,
                f"健康报告导出失败: 输出路径冲突 - {abs_output}"
            )
        except Exception:
            pass
        _maybe_cleanup_audit(audit, storage)
        sys.exit(1)

    try:
        report = run_health_check(storage, batch_id)
    except HealthCheckCorruptedError as e:
        console.print(f"[red]ERR[/] 审计库损坏: {e}")
        try:
            audit.log("health_export_fail", batch_id, 0, f"健康报告导出失败: 审计库损坏 - {e}")
        except Exception:
            pass
        _maybe_cleanup_audit(audit, storage)
        sys.exit(1)
    except HealthCheckError as e:
        console.print(f"[red]ERR[/] {e}")
        try:
            audit.log("health_export_fail", batch_id, 0, f"健康报告导出失败: {e}")
        except Exception:
            pass
        _maybe_cleanup_audit(audit, storage)
        sys.exit(1)

    try:
        if fmt == "json":
            export_health_report_json(report, abs_output)
        else:
            export_health_report_csv(report, abs_output)
    except OSError as e:
        console.print(f"[red]ERR[/] 写入文件失败: {e}")
        try:
            audit.log(
                "health_export_fail", batch_id, 0,
                f"健康报告导出失败: 写入错误 - {e}"
            )
        except Exception:
            pass
        _maybe_cleanup_audit(audit, storage)
        sys.exit(1)

    summary = report._summary()
    console.print(f"[green]OK[/] 健康报告已导出到 [cyan]{abs_output}[/]")
    console.print(
        f"  格式: {fmt.upper()}  "
        f"总体状态: {report.overall_status.value.upper()}  "
        f"问题: OK={summary['ok']} INFO={summary['info']} "
        f"WARNING={summary['warning']} CRITICAL={summary['critical']}"
    )

    try:
        audit.log(
            "health_export", batch_id, len(report.issues),
            f"健康报告导出成功: {abs_output} ({fmt.upper()}), "
            f"总体 {report.overall_status.value}"
        )
    except Exception:
        pass
    _maybe_cleanup_audit(audit, storage)


if __name__ == "__main__":
    main()
