"""银行回单对账 CLI 入口."""
from __future__ import annotations

import os
import sys
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .models import Batch, FileType, DiscrepancyStatus, DiscrepancyType
from .parser import parse_csv, ParseResult
from .rules import load_rules, RuleValidationError
from .matcher import run_matching
from .storage import BatchStorage
from .report import export_discrepancies_csv, export_summary_csv, generate_summary
from .audit import AuditStorage
from .config import load_config


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
    console.print(Panel.fit(
        f"[bold cyan]批次:[/] {summary['batch_name']}  "
        f"[dim]({summary['batch_id']})[/]\n"
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
    table.add_column("创建时间", style="dim")
    table.add_column("更新时间", style="dim")
    table.add_column("差异数", justify="right")
    table.add_column("导入文件数", justify="right")

    for b in batches:
        table.add_row(
            b["batch_id"],
            b["name"],
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

    type_map = {
        "bank": FileType.BANK_STATEMENT,
        "system": FileType.SYSTEM_RECEIPT,
        "adjustment": FileType.MANUAL_ADJUSTMENT,
    }
    ft = type_map[file_type]

    try:
        result, imported = parse_csv(file_path, ft)
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


# ── audit-log ────────────────────────────────────────────
@cli.command("audit-log", help="查看/导出操作审计日志")
@click.option("--from", "from_date", default=None, help="起始日期 (YYYY-MM-DD)")
@click.option("--to", "to_date", default=None, help="截止日期 (YYYY-MM-DD)")
@click.option("--type", "-t", "op_type", default=None,
              type=click.Choice(["import", "match", "mark", "rollback"]),
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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
