"""银行回单对账 CLI 入口."""
from __future__ import annotations

import os
import sys
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .models import Batch, FileType, DiscrepancyStatus, DiscrepancyType, BatchStatus, AdjustmentType, Transaction
from .parser import parse_csv, parse_xlsx, parse_file, ParseResult
from .rules import load_rules, RuleValidationError
from .matcher import run_matching
from .storage import BatchStorage
from .report import export_discrepancies_csv, export_summary_csv, generate_summary
from .audit import AuditStorage
from .config import (
    load_config,
    save_config,
    set_column_alias,
    AliasConflictError,
    STANDARD_FIELDS,
    FILE_TYPE_ALIAS_KEYS,
)


console = Console(highlight=False, emoji=False, markup=True)


def _get_storage() -> BatchStorage:
    storage_dir = os.environ.get("BANK_RECONCILE_HOME")
    return BatchStorage(storage_dir)


def _get_audit(storage: BatchStorage) -> AuditStorage:
    return AuditStorage(storage.storage_dir)


def _maybe_cleanup_audit(audit: AuditStorage, storage: BatchStorage) -> None:
    cfg = load_config(storage.storage_dir)
    days = cfg.get("audit_retention_days", 90)
    if days and days > 0:
        try:
            audit.cleanup(days)
        except Exception:
            pass


def _print_batch_info(batch: Batch) -> None:
    summary = generate_summary(batch)
    status_style = "green" if batch.status == BatchStatus.OPEN else "dim"
    console.print(Panel.fit(
        f"[bold cyan]批次:[/] {summary['batch_name']}  "
        f"[dim]({summary['batch_id']})[/]  "
        f"[bold {status_style}]状态: {batch.status.value}[/]\n"
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
def list_batches() -> None:
    storage = _get_storage()
    batches = storage.list_batches()
    if not batches:
        console.print("[yellow]尚无批次[/], 使用 [cyan]bank-reconcile create <名称>[/] 创建")
        return

    table = Table(title="批次列表")
    table.add_column("批次ID", style="cyan")
    table.add_column("名称", style="bold")
    table.add_column("状态", style="bold")
    table.add_column("创建时间", style="dim")
    table.add_column("更新时间", style="dim")
    table.add_column("差异数", justify="right")
    table.add_column("导入文件数", justify="right")

    for b in batches:
        status = b.get("status", "open")
        status_style = "green" if status == "open" else "dim"
        table.add_row(
            b["batch_id"],
            b["name"],
            f"[{status_style}]{status}[/]",
            b["created_at"][:19].replace("T", " "),
            b["updated_at"][:19].replace("T", " "),
            str(b["discrepancy_count"]),
            str(b["imported_files"]),
        )
    console.print(table)


# ── import ────────────────────────────────────────────────
@cli.command("import", help="导入文件到指定批次")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--type", "-t", "file_type", required=True,
              type=click.Choice(["bank", "system", "adjustment"]),
              help="文件类型")
@click.argument("file_path")
def import_file(batch_id: str, file_type: str, file_path: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止导入。请先 reopen 后再操作。")
        sys.exit(1)

    type_map = {
        "bank": FileType.BANK_STATEMENT,
        "system": FileType.SYSTEM_RECEIPT,
        "adjustment": FileType.MANUAL_ADJUSTMENT,
    }
    ft = type_map[file_type]

    try:
        result, imported = parse_file(file_path, ft, storage_dir=storage.storage_dir)
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


# ── rules ─────────────────────────────────────────────────
@cli.command(help="为批次设置规则文件")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.argument("rule_file")
def rules(batch_id: str, rule_file: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

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


# ── match ─────────────────────────────────────────────────
@cli.command(help="执行对账匹配，生成差异清单")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--rule-file", "-r", default=None, help="规则文件路径（可选，优先级高于批次已设置的规则）")
def match(batch_id: str, rule_file: Optional[str]) -> None:
    storage = _get_storage()
    if not storage.batch_exists(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止匹配。请先 reopen 后再操作。")
        sys.exit(1)

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
    for d in discrepancies:
        t = d.discrepancy_type.value
        by_type[t] = by_type.get(t, 0) + 1

    console.print(f"[green]OK[/] 匹配完成，共发现 [bold]{len(discrepancies)}[/] 条差异")
    for t, c in sorted(by_type.items()):
        console.print(f"  {t}: {c}")

    bank_count = len(batch.bank_txns)
    sys_count = len(batch.system_txns)
    summary_text = (
        f"执行对账匹配，银行回单 {bank_count} 条，系统流水 {sys_count} 条，"
        f"差异 {len(discrepancies)} 条"
    )
    audit = _get_audit(storage)
    audit.log("match", batch_id, len(discrepancies), summary_text)
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
    if not storage.batch_exists(batch_id):
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
    if not storage.batch_exists(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止标记。请先 reopen 后再操作。")
        sys.exit(1)

    target_status = DiscrepancyStatus(status)

    found = None
    for d in batch.discrepancies:
        if d.discrepancy_id == discrepancy_id:
            found = d
            break

    if not found:
        console.print(f"[red]ERR[/] 差异不存在: {discrepancy_id}")
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
def rollback(batch_id: str, discrepancy_id: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止回滚。请先 reopen 后再操作。")
        sys.exit(1)

    found = None
    for d in batch.discrepancies:
        if d.discrepancy_id == discrepancy_id:
            found = d
            break

    if not found:
        console.print(f"[red]ERR[/] 差异不存在: {discrepancy_id}")
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
    if not storage.batch_exists(batch_id):
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
    if not storage.batch_exists(batch_id):
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
@cli.command(help="关闭批次（归档），关闭后禁止导入/匹配/标记/回滚")
@click.option("--batch-id", "-b", required=True, help="批次ID")
def close(batch_id: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)
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
    if not storage.batch_exists(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)
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
              type=click.Choice(["import", "match", "mark", "rollback", "close", "reopen", "manual_link", "undo_manual_link"]),
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
    matched_bank_ids = set()
    matched_system_ids = set()
    amount_mismatch_pairs = []

    for d in batch.discrepancies:
        if d.discrepancy_type == DiscrepancyType.AMOUNT_MISMATCH:
            if d.bank_txn and d.system_txn:
                matched_bank_ids.add(d.bank_txn.txn_id)
                matched_system_ids.add(d.system_txn.txn_id)
                amount_mismatch_pairs.append((d.bank_txn, d.system_txn))
        elif d.discrepancy_type == DiscrepancyType.NEEDS_MANUAL_REVIEW:
            if d.bank_txn:
                matched_bank_ids.add(d.bank_txn.txn_id)
            if d.system_txn:
                matched_system_ids.add(d.system_txn.txn_id)

    for d in batch.discrepancies:
        if d.discrepancy_type == DiscrepancyType.DUPLICATE:
            if d.bank_txn:
                matched_bank_ids.add(d.bank_txn.txn_id)
            if d.system_txn:
                matched_system_ids.add(d.system_txn.txn_id)

    bank_norm_ids = set()
    system_norm_ids = set()
    for d in batch.discrepancies:
        if d.discrepancy_type in (DiscrepancyType.MISSING_IN_BANK, DiscrepancyType.MISSING_IN_SYSTEM):
            continue
        if d.bank_txn:
            bank_norm_ids.add(d.bank_txn.txn_id)
        if d.system_txn:
            system_norm_ids.add(d.system_txn.txn_id)

    unmatched_bank = [t for t in batch.bank_txns if t.txn_id not in matched_bank_ids and t.txn_id not in bank_norm_ids]
    unmatched_system = [t for t in batch.system_txns if t.txn_id not in matched_system_ids and t.txn_id not in system_norm_ids]

    for t in batch.bank_txns:
        for d in batch.discrepancies:
            if d.discrepancy_type == DiscrepancyType.MISSING_IN_SYSTEM and d.bank_txn and d.bank_txn.txn_id == t.txn_id:
                if t not in unmatched_bank:
                    unmatched_bank.append(t)

    for t in batch.system_txns:
        for d in batch.discrepancies:
            if d.discrepancy_type == DiscrepancyType.MISSING_IN_BANK and d.system_txn and d.system_txn.txn_id == t.txn_id:
                if t not in unmatched_system:
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
        rows.append({
            "sort_key": amount_diff,
            "type": "amount_mismatch",
            "bank_txn": bank_txn,
            "system_txn": system_txn,
            "amount_diff": amount_diff,
            "date_diff": date_diff,
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
        rows.append({
            "sort_key": min_diff,
            "type": "missing_in_system",
            "bank_txn": bank_txn,
            "system_txn": best_match,
            "amount_diff": min_diff,
            "date_diff": date_diff,
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
        rows.append({
            "sort_key": min_diff,
            "type": "missing_in_bank",
            "bank_txn": best_match,
            "system_txn": system_txn,
            "amount_diff": min_diff,
            "date_diff": date_diff,
        })

    rows.sort(key=lambda r: r["sort_key"])
    return rows


@cli.command(help="列出未匹配的银行和系统记录，按金额差排序")
@click.option("--batch-id", "-b", required=True, help="批次ID")
@click.option("--limit", "-n", default=50, help="显示条数")
@click.option("--export", "export_path", default=None, help="导出 CSV 文件路径")
def diff(batch_id: str, limit: int, export_path: str) -> None:
    storage = _get_storage()
    if not storage.batch_exists(batch_id):
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
                "金额差异", "日期偏差(天)"
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
                ])
        console.print(f"[green]OK[/] 已导出 {len(rows)} 条记录到 [cyan]{export_path}[/]")
        return

    table = Table(title=f"未匹配记录（共 {len(rows)} 条，显示前 {min(limit, len(rows))} 条）")
    table.add_column("#", justify="right", style="dim")
    table.add_column("类型", style="magenta")
    table.add_column("银行交易号", style="cyan")
    table.add_column("银行金额", justify="right")
    table.add_column("银行日期", style="dim")
    table.add_column("系统交易号", style="green")
    table.add_column("系统金额", justify="right")
    table.add_column("系统日期", style="dim")
    table.add_column("金额差", justify="right", style="yellow")
    table.add_column("日期差", justify="right", style="yellow")

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
            f"{bt.amount:.2f}" if bt else "-",
            bt.date if bt else "-",
            st.txn_id if st else "-",
            f"{st.amount:.2f}" if st else "-",
            st.date if st else "-",
            f"{r['amount_diff']:.2f}",
            str(r["date_diff"]) if r["date_diff"] >= 0 else "-",
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
    if not storage.batch_exists(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止操作。请先 reopen 后再操作。")
        sys.exit(1)

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
    if not storage.batch_exists(batch_id):
        console.print(f"[red]ERR[/] 批次不存在: {batch_id}")
        sys.exit(1)

    batch = storage.load(batch_id)

    if batch.is_closed:
        console.print(f"[red]ERR[/] 批次已关闭（closed），禁止操作。请先 reopen 后再操作。")
        sys.exit(1)

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
        f"[bold]审计保留天数:[/] {cfg.get('audit_retention_days', 90)}",
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


if __name__ == "__main__":
    main()
