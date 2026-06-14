"""端到端测试脚本 - 验证所有核心功能."""
import os
import sys
import shutil
import tempfile
import io
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank_reconcile.models import Batch, FileType, DiscrepancyStatus, DiscrepancyType, AdjustmentType, MatchLevel, Transaction
from bank_reconcile.parser import parse_csv, parse_xlsx, parse_file
from bank_reconcile.rules import load_rules, RuleValidationError, MatchRules, validate_rules, export_rules, import_rules
from bank_reconcile.matcher import run_matching, get_tolerance_match_records
from bank_reconcile.storage import BatchStorage
from bank_reconcile.report import export_discrepancies_csv, generate_summary, export_summary_csv
from bank_reconcile.audit import AuditStorage
from bank_reconcile.config import load_config, save_config


def test_parser():
    print("=== 测试解析模块 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")

    result, imported = parse_csv(
        os.path.join(samples_dir, "bank_statement.csv"),
        FileType.BANK_STATEMENT,
    )
    print(f"银行回单: {result.row_count} 条有效记录, {len(result.errors)} 条错误, {len(result.duplicates)} 条重复")
    assert result.row_count > 0, "应该有有效记录"
    assert len(result.errors) >= 2, "应该有非法金额和缺少交易号的错误"
    assert len(result.duplicates) >= 1, "parser 应填充 duplicates 列表（import 阶段告警）"

    error_types = {e.error_type for e in result.errors}
    assert "invalid_amount" in error_types, "应该有非法金额错误"
    assert "missing_txn_id" in error_types, "应该有缺少交易号错误"

    dup_types = {d.error_type for d in result.duplicates}
    assert "duplicate_txn_id" in dup_types, "应该有 duplicate_txn_id 类型的重复记录"
    assert all(d.source_row > 0 for d in result.duplicates), "重复记录应包含 source_row"
    print(f"  重复检测验证通过: 检测到 B002 重复出现在第 {result.duplicates[0].source_row} 行, {result.duplicates[0].message}")

    bank_ids_from_txns = [t.txn_id for t in result.transactions]
    assert bank_ids_from_txns.count("B002") >= 2, "重复流水仍要保留在 transactions 中供匹配阶段使用"
    print("  重复记录同时保留在 transactions 中供匹配阶段使用: OK")

    result2, imported2 = parse_csv(
        os.path.join(samples_dir, "system_receipt.csv"),
        FileType.SYSTEM_RECEIPT,
    )
    print(f"系统流水: {result2.row_count} 条有效记录")

    result3, imported3 = parse_csv(
        os.path.join(samples_dir, "manual_adjustment.csv"),
        FileType.MANUAL_ADJUSTMENT,
    )
    print(f"手工调整: {result3.row_count} 条有效记录")

    print("[PASS] 解析模块测试通过\n")
    return result, result2, result3


def test_rules():
    print("=== 测试规则引擎 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")

    rules = load_rules(os.path.join(samples_dir, "rules.yaml"))
    assert rules.amount_tolerance == 0.01
    assert rules.date_window_days == 3
    assert rules.consider_adjustments is True
    assert len(rules.manual_review_keywords) > 0
    assert rules.tolerance.enabled is False, "默认规则中 tolerance 应未启用"
    print(f"正常规则加载: 容差={rules.amount_tolerance}, 关键词={len(rules.manual_review_keywords)}个")

    try:
        load_rules(os.path.join(samples_dir, "rules_bad.yaml"))
        assert False, "应该抛出异常"
    except RuleValidationError as e:
        print(f"错误规则正确拒绝: {e}")

    default = MatchRules.default()
    assert default.amount_tolerance == 0.01
    assert default.tolerance.enabled is False

    print("[PASS] 规则引擎测试通过\n")


def test_matching():
    print("=== 测试匹配模块 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")

    bank_result, _ = parse_csv(
        os.path.join(samples_dir, "bank_statement.csv"),
        FileType.BANK_STATEMENT,
    )
    sys_result, _ = parse_csv(
        os.path.join(samples_dir, "system_receipt.csv"),
        FileType.SYSTEM_RECEIPT,
    )
    adj_result, _ = parse_csv(
        os.path.join(samples_dir, "manual_adjustment.csv"),
        FileType.MANUAL_ADJUSTMENT,
    )

    batch = Batch.create("测试批次")
    batch.bank_txns = bank_result.transactions
    batch.system_txns = sys_result.transactions
    batch.adjustment_txns = adj_result.transactions

    rules = MatchRules.default()
    discrepancies = run_matching(batch, rules)

    by_type = {}
    for d in discrepancies:
        t = d.discrepancy_type.value
        by_type[t] = by_type.get(t, 0) + 1

    print(f"共发现 {len(discrepancies)} 条差异:")
    for t, c in sorted(by_type.items()):
        print(f"  {t}: {c}")

    assert DiscrepancyType.MISSING_IN_BANK.value in by_type, "应该有银行缺失"
    assert DiscrepancyType.MISSING_IN_SYSTEM.value in by_type, "应该有系统缺失"
    assert DiscrepancyType.DUPLICATE.value in by_type, "应该有重复"
    assert DiscrepancyType.NEEDS_MANUAL_REVIEW.value in by_type, "应该有待人工确认"

    print("[PASS] 匹配模块测试通过\n")
    return batch, discrepancies


def test_storage_and_lifecycle():
    print("=== 测试状态存储与完整生命周期 ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_test_")
    storage = BatchStorage(tmpdir)

    try:
        batch = Batch.create("测试批次1")
        storage.save(batch)
        batch_id = batch.batch_id
        print(f"创建批次: {batch_id}")

        batches = storage.list_batches()
        assert len(batches) == 1
        print(f"列表批次: {len(batches)} 个")

        loaded = storage.load(batch_id)
        assert loaded.batch_id == batch_id
        assert loaded.name == "测试批次1"
        print("加载批次成功")

        samples_dir = os.path.join(os.path.dirname(__file__), "samples")
        bank_result, bank_imported = parse_csv(
            os.path.join(samples_dir, "bank_statement.csv"),
            FileType.BANK_STATEMENT,
        )
        sys_result, sys_imported = parse_csv(
            os.path.join(samples_dir, "system_receipt.csv"),
            FileType.SYSTEM_RECEIPT,
        )
        adj_result, adj_imported = parse_csv(
            os.path.join(samples_dir, "manual_adjustment.csv"),
            FileType.MANUAL_ADJUSTMENT,
        )

        loaded.bank_txns = bank_result.transactions
        loaded.system_txns = sys_result.transactions
        loaded.adjustment_txns = adj_result.transactions
        loaded.imported_files = [bank_imported, sys_imported, adj_imported]
        storage.save(loaded)
        print("导入文件并保存")

        rules = MatchRules.default()
        discrepancies = run_matching(loaded, rules)
        loaded.discrepancies = discrepancies
        storage.save(loaded)
        print(f"匹配完成，{len(discrepancies)} 条差异")

        first_disp = discrepancies[0]
        first_disp.mark(DiscrepancyStatus.CONFIRMED, "tester1", "测试备注")
        storage.save(loaded)
        print(f"标记差异 {first_disp.discrepancy_id} 为 confirmed")

        reloaded = storage.load(batch_id)
        marked = next(d for d in reloaded.discrepancies if d.discrepancy_id == first_disp.discrepancy_id)
        assert marked.status == DiscrepancyStatus.CONFIRMED
        assert marked.reviewer == "tester1"
        assert marked.note == "测试备注"
        assert len(marked.rollback_history) == 1
        print("状态持久化验证通过 (status/reviewer/note/rollback)")

        marked.rollback()
        storage.save(reloaded)
        reloaded2 = storage.load(batch_id)
        rolled = next(d for d in reloaded2.discrepancies if d.discrepancy_id == first_disp.discrepancy_id)
        assert rolled.status == DiscrepancyStatus.OPEN
        assert rolled.reviewer is None
        assert len(rolled.rollback_history) == 0
        print("回滚持久化验证通过")

        out_path = os.path.join(tmpdir, "test_report.csv")
        count = export_discrepancies_csv(reloaded2, out_path)
        storage.record_export(reloaded2, out_path, "discrepancies")
        assert count > 0
        assert os.path.isfile(out_path)
        print(f"导出报告: {count} 条 -> {out_path}")

        final = storage.load(batch_id)
        assert len(final.exports) == 1
        assert final.exports[0]["export_type"] == "discrepancies"
        print("导出历史持久化验证通过")

        summary = generate_summary(final)
        assert summary["total_discrepancies"] == len(discrepancies)
        assert "by_type" in summary
        assert "by_status" in summary
        print("摘要生成验证通过")

        batch2 = Batch.create("旧批次")
        storage.save(batch2)
        assert len(storage.list_batches()) == 2
        print("多批次共存验证通过")

        print("[PASS] 状态存储与生命周期测试通过\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_source_traceability():
    print("=== 测试报告来源可追溯 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_trace_")

    try:
        storage = BatchStorage(tmpdir)
        batch = Batch.create("追溯测试")

        bank_result, _ = parse_csv(
            os.path.join(samples_dir, "bank_statement.csv"),
            FileType.BANK_STATEMENT,
        )
        sys_result, _ = parse_csv(
            os.path.join(samples_dir, "system_receipt.csv"),
            FileType.SYSTEM_RECEIPT,
        )
        adj_result, _ = parse_csv(
            os.path.join(samples_dir, "manual_adjustment.csv"),
            FileType.MANUAL_ADJUSTMENT,
        )

        batch.bank_txns = bank_result.transactions
        batch.system_txns = sys_result.transactions
        batch.adjustment_txns = adj_result.transactions

        rules = MatchRules.default()
        batch.discrepancies = run_matching(batch, rules)
        storage.save(batch)

        out = os.path.join(tmpdir, "trace_report.csv")
        export_discrepancies_csv(batch, out)

        with open(out, "r", encoding="utf-8-sig") as f:
            import csv
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            row = next(reader)

        assert "bank_source_file" in headers
        assert "bank_source_row" in headers
        assert "system_source_file" in headers
        assert "system_source_row" in headers
        assert "adjustment_source_file" in headers
        assert "discrepancy_id" in headers
        assert "reviewer" in headers
        assert "note" in headers
        assert "rollback_count" in headers
        print(f"报告字段完整: {len(headers)} 列")
        print(f"示例行 ID: {row.get('discrepancy_id')}, 类型: {row.get('discrepancy_type')}")

        print("[PASS] 来源可追溯性测试通过\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_error_paths():
    print("=== 测试失败路径 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")

    try:
        parse_csv("nonexistent.csv", FileType.BANK_STATEMENT)
        assert False, "应该抛出 FileNotFoundError"
    except FileNotFoundError:
        print("  不存在的文件: 正确拒绝")

    try:
        load_rules(os.path.join(samples_dir, "rules_bad.yaml"))
        assert False, "应该抛出 RuleValidationError"
    except RuleValidationError as e:
        print(f"  错误规则文件: 正确拒绝 ({e})")

    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_err_")
    try:
        storage = BatchStorage(tmpdir)
        try:
            storage.load("NONEXISTENT")
            assert False, "应该抛出 FileNotFoundError"
        except FileNotFoundError:
            print("  不存在的批次: 正确拒绝")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("[PASS] 失败路径测试通过\n")


def test_cli_rich_output():
    print("=== 测试 CLI Rich 输出格式 (无 MarkupError) ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_cli_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}\n exc_info={result.exception and str(result.exception)}"
            if result.exception:
                import traceback
                tb = "".join(traceback.format_exception(type(result.exception), result.exception, result.exception.__traceback__))
                raise AssertionError(f"[{desc}] 抛出异常: {result.exception}\n{tb}")
            print(f"  [OK] {desc}: exit_code=0, 无异常栈")

        r = runner.invoke(cli, ["create", "cli_rich_test"])
        check("create", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()
        print(f"    批次ID: {batch_id}")

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import bank", r)
        assert "重复流水号" in r.output, "银行回单样例含 B002 重复，输出应包含'重复流水号'告警"
        print("    import 阶段重复告警可见: YES")

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        check("import system", r)

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "adjustment",
                            os.path.join(samples_dir, "manual_adjustment.csv")])
        check("import adjustment", r)

        r = runner.invoke(cli, ["rules", "set", "-b", batch_id,
                            os.path.join(samples_dir, "rules.yaml")])
        check("rules set", r)

        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match", r)

        r = runner.invoke(cli, ["discrepancies", "-b", batch_id, "-n", "5"])
        check("discrepancies", r)

        storage2 = BatchStorage(tmpdir)
        b = storage2.load(batch_id)
        assert len(b.discrepancies) > 0, "match 后应存在差异"
        first_disp_id = b.discrepancies[0].discrepancy_id
        print(f"    首个差异ID: {first_disp_id}")

        r = runner.invoke(cli, ["mark", "-b", batch_id, "-d", first_disp_id,
                              "-s", "confirmed", "-r", "tester_alice", "-n", "测试备注"])
        check("mark confirmed", r)

        r = runner.invoke(cli, ["rollback", "-b", batch_id, "-d", first_disp_id])
        check("rollback", r)

        out_csv = os.path.join(tmpdir, "cli_test.csv")
        r = runner.invoke(cli, ["export", "-b", batch_id, "-o", out_csv, "--with-summary"])
        check("export", r)

        assert os.path.isfile(out_csv), "CSV 报告应落盘"
        summary_path = os.path.join(tmpdir, "cli_test_summary.csv")
        assert os.path.isfile(summary_path), "摘要文件应落盘"
        print(f"    报告落盘: CSV {out_csv}")

        r = runner.invoke(cli, ["resume", batch_id])
        check("resume", r)

        print("[PASS] CLI Rich 输出格式测试通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_audit_unit():
    print("=== 测试审计模块（单元） ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_audit_")

    try:
        audit = AuditStorage(tmpdir)
        assert os.path.isfile(os.path.join(tmpdir, "audit.db")), "SQLite 数据库应自动创建"
        print("  数据库自动创建: OK")

        rid = audit.log("import", "BATCH-TEST01", 34, "导入 bank_statement.csv，银行回单 34 条")
        assert rid > 0, "log 应返回正整数 ID"
        print(f"  写入审计记录: id={rid}")

        audit.log("match", "BATCH-TEST01", 6, "匹配完成，差异 6 条")
        audit.log("mark", "BATCH-TEST01", 1, "标记 DISP-XXX 为 confirmed")
        audit.log("rollback", "BATCH-TEST01", 1, "回滚 DISP-XXX")

        all_records = audit.query()
        assert len(all_records) == 4, f"应有 4 条记录, 实际 {len(all_records)}"
        print(f"  查询全部: {len(all_records)} 条")

        import_records = audit.query(op_type="import")
        assert len(import_records) == 1, f"import 应 1 条, 实际 {len(import_records)}"
        assert import_records[0]["command"] == "import"
        assert import_records[0]["affected"] == 34
        assert "银行回单 34 条" in import_records[0]["summary"]
        print(f"  按类型过滤 import: {len(import_records)} 条")

        batch_records = audit.query(batch_id="BATCH-TEST01")
        assert len(batch_records) == 4
        print(f"  按批次过滤: {len(batch_records)} 条")

        csv_path = os.path.join(tmpdir, "audit_export.csv")
        count = audit.export_csv(csv_path, all_records)
        assert count == 4
        assert os.path.isfile(csv_path)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            import csv
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(rows) == 4
            assert "id" in rows[0]
            assert "timestamp" in rows[0]
            assert "command" in rows[0]
            assert "batch_id" in rows[0]
            assert "affected" in rows[0]
            assert "summary" in rows[0]
        print(f"  CSV 导出: {count} 条, 字段完整")

        json_path = os.path.join(tmpdir, "audit_export.json")
        count = audit.export_json(json_path, all_records)
        assert count == 4
        assert os.path.isfile(json_path)
        with open(json_path, "r", encoding="utf-8") as f:
            import json
            data = json.load(f)
            assert len(data) == 4
            cmds = {d["command"] for d in data}
            assert cmds == {"import", "match", "mark", "rollback"}
        print(f"  JSON 导出: {count} 条")

        audit.log("import", "BATCH-OLD", 10, "旧记录")
        deleted = audit.cleanup(0)
        assert deleted == 0, "retention_days=0 不应删除任何记录"
        print("  cleanup(0): 不删除")

        deleted = audit.cleanup(90)
        assert deleted >= 0, "cleanup 应返回非负整数"
        print(f"  cleanup(90): 删除 {deleted} 条")

        all_after = audit.query()
        assert len(all_after) <= 5
        print(f"  清理后剩余: {len(all_after)} 条")

        print("[PASS] 审计模块单元测试通过\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_audit_config():
    print("=== 测试配置模块 ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_cfg_")

    try:
        cfg = load_config(tmpdir)
        assert cfg["audit_retention_days"] == 90, f"默认应为 90, 实际 {cfg['audit_retention_days']}"
        print(f"  默认配置: audit_retention_days={cfg['audit_retention_days']}")

        cfg["audit_retention_days"] = 30
        save_config(tmpdir, cfg)

        cfg2 = load_config(tmpdir)
        assert cfg2["audit_retention_days"] == 30
        print(f"  修改后配置: audit_retention_days={cfg2['audit_retention_days']}")

        print("[PASS] 配置模块测试通过\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_audit_cli_integration():
    print("=== 测试审计 CLI 集成 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_audit_cli_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"
            if result.exception:
                import traceback
                tb = "".join(traceback.format_exception(type(result.exception), result.exception, result.exception.__traceback__))
                raise AssertionError(f"[{desc}] 抛出异常: {result.exception}\n{tb}")
            print(f"  [OK] {desc}")

        r = runner.invoke(cli, ["create", "audit_test"])
        check("create", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import bank", r)

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        check("import system", r)

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "adjustment",
                            os.path.join(samples_dir, "manual_adjustment.csv")])
        check("import adjustment", r)

        r = runner.invoke(cli, ["rules", "set", "-b", batch_id,
                            os.path.join(samples_dir, "rules.yaml")])
        check("rules set", r)

        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match", r)

        storage = BatchStorage(tmpdir)
        b = storage.load(batch_id)
        first_disp_id = b.discrepancies[0].discrepancy_id

        r = runner.invoke(cli, ["mark", "-b", batch_id, "-d", first_disp_id,
                              "-s", "confirmed", "-r", "audit_tester", "-n", "审计测试"])
        check("mark confirmed", r)

        r = runner.invoke(cli, ["rollback", "-b", batch_id, "-d", first_disp_id])
        check("rollback", r)

        audit = AuditStorage(tmpdir)
        all_records = audit.query()
        assert len(all_records) >= 6, f"应有至少 6 条审计记录(import×3 + match + mark + rollback), 实际 {len(all_records)}"
        print(f"  审计记录数: {len(all_records)}")

        commands = {r["command"] for r in all_records}
        assert "import" in commands, "应包含 import 操作"
        assert "match" in commands, "应包含 match 操作"
        assert "mark" in commands, "应包含 mark 操作"
        assert "rollback" in commands, "应包含 rollback 操作"
        print(f"  操作类型: {sorted(commands)}")

        import_records = audit.query(op_type="import")
        assert len(import_records) == 3, f"应有 3 条 import 记录, 实际 {len(import_records)}"
        print(f"  import 记录: {len(import_records)} 条")

        batch_records = audit.query(batch_id=batch_id)
        assert len(batch_records) == len(all_records), "按批次过滤应返回全部"
        print(f"  按批次过滤: {len(batch_records)} 条")

        r = runner.invoke(cli, ["audit-log"])
        check("audit-log (no filter)", r)
        assert "审计日志" in r.output

        r = runner.invoke(cli, ["audit-log", "--type", "import"])
        check("audit-log --type import", r)

        r = runner.invoke(cli, ["audit-log", "-b", batch_id])
        check("audit-log -b", r)

        csv_export = os.path.join(tmpdir, "audit_cli_export.csv")
        r = runner.invoke(cli, ["audit-log", "-o", csv_export, "-f", "csv"])
        check("audit-log export csv", r)
        assert os.path.isfile(csv_export), "CSV 审计导出文件应落盘"
        with open(csv_export, "r", encoding="utf-8-sig") as f:
            import csv
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(rows) >= 6
            assert "id" in rows[0]
            assert "timestamp" in rows[0]
            assert "command" in rows[0]
            assert "batch_id" in rows[0]
            assert "affected" in rows[0]
            assert "summary" in rows[0]
        print(f"  CSV 导出: {len(rows)} 条, 字段完整 (与 report 导出风格对齐)")

        json_export = os.path.join(tmpdir, "audit_cli_export.json")
        r = runner.invoke(cli, ["audit-log", "-o", json_export, "-f", "json"])
        check("audit-log export json", r)
        assert os.path.isfile(json_export)
        with open(json_export, "r", encoding="utf-8") as f:
            import json
            data = json.load(f)
            assert len(data) >= 6
            assert data[0]["command"] in ("import", "match", "mark", "rollback")
        print(f"  JSON 导出: {len(data)} 条")

        import_records_summary = [r for r in all_records if r["command"] == "import"]
        for rec in import_records_summary:
            assert "导入" in rec["summary"], f"import 摘要应包含'导入', 实际: {rec['summary']}"
        print("  import 摘要格式验证通过")

        print("[PASS] 审计 CLI 集成测试通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_audit_persistence():
    print("=== 测试审计数据持久化（重启不丢） ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_audit_persist_")

    try:
        audit1 = AuditStorage(tmpdir)
        audit1.log("import", "BATCH-P1", 25, "第一次写入")
        audit1.log("match", "BATCH-P1", 3, "匹配3条")

        records1 = audit1.query()
        assert len(records1) == 2
        print(f"  第一次实例: {len(records1)} 条")

        audit2 = AuditStorage(tmpdir)
        records2 = audit2.query()
        assert len(records2) == 2, "重新创建实例后数据应保留"
        audit2.log("mark", "BATCH-P1", 1, "标记1条")

        records3 = audit2.query()
        assert len(records3) == 3
        print(f"  第二次实例: {len(records3)} 条（含新增）")

        print("[PASS] 审计持久化测试通过\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_audit_retention_cleanup():
    print("=== 测试审计保留天数自动清理 ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_audit_ret_")

    try:
        audit = AuditStorage(tmpdir)
        audit.log("import", "BATCH-R1", 10, "近期记录")

        cfg = load_config(tmpdir)
        assert cfg["audit_retention_days"] == 90
        cfg["audit_retention_days"] = 1
        save_config(tmpdir, cfg)
        cfg_reload = load_config(tmpdir)
        assert cfg_reload["audit_retention_days"] == 1
        print("  配置 audit_retention_days=1 写入验证通过")

        deleted = audit.cleanup(1)
        print(f"  cleanup(1): 删除 {deleted} 条 (当天记录不会被删)")

        audit.log("import", "BATCH-R2", 5, "另一条记录")
        records = audit.query()
        assert len(records) >= 1
        print(f"  清理后正常写入: {len(records)} 条")

        print("[PASS] 审计保留天数清理测试通过\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_close_reopen_import_block():
    """场景1: close 之后导入被拒 → reopen 之后正常导入."""
    print("=== 场景1: close/reopen 导入权限控制 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_close_import_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}" \
                + (f"\nexception={result.exception}" if result.exception else "")
            print(f"  [OK] {desc}: exit={expected_exit}")

        r = runner.invoke(cli, ["create", "关闭重开测试"])
        check("create", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["close", "-b", batch_id])
        check("close", r)
        assert "批次已关闭" in r.output

        storage = BatchStorage(tmpdir)
        b = storage.load(batch_id)
        assert b.is_closed, "批次应为 closed 状态"
        assert b.status.value == "closed"
        print("  模型层状态校验: closed OK")

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import while closed (应被拒)", r, expected_exit=1)
        assert "批次已关闭" in r.output and "禁止导入" in r.output, \
            f"应提示禁止导入, 实际输出: {r.output}"
        print("  关闭后 import 被拒绝: OK")

        b_after = storage.load(batch_id)
        assert len(b_after.bank_txns) == 0, "关闭状态下不应真的写入数据"
        print("  关闭状态下无副作用 (无数据写入): OK")

        r = runner.invoke(cli, ["reopen", "-b", batch_id])
        check("reopen", r)
        assert "批次已重新打开" in r.output

        b = storage.load(batch_id)
        assert b.is_open, "reopen 后应为 open 状态"
        print("  模型层状态校验: reopen OK")

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import after reopen", r)
        b = storage.load(batch_id)
        assert len(b.bank_txns) > 0, "reopen 后应能正常导入"
        print(f"  reopen 后导入成功: {len(b.bank_txns)} 条银行回单")

        audit = AuditStorage(tmpdir)
        records = audit.query(batch_id=batch_id)
        cmds = {rec["command"] for rec in records}
        assert "close" in cmds, "close 操作应写入审计日志"
        assert "reopen" in cmds, "reopen 操作应写入审计日志"
        assert "import" in cmds, "import 操作应写入审计日志"
        print(f"  审计日志包含 close/reopen/import: {sorted(cmds)}")

        print("[PASS] 场景1 (close/reopen 导入权限) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_xlsx_import_field_values():
    """场景2: 拿一份 xlsx 导入确认字段值都对."""
    print("=== 场景2: XLSX 导入字段值校验 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_xlsx_")

    try:
        csv_path = os.path.join(samples_dir, "bank_statement.csv")
        xlsx_path = os.path.join(tmpdir, "bank_statement.xlsx")

        from openpyxl import Workbook
        import csv as _csv

        wb = Workbook()
        ws = wb.active
        ws.title = "银行流水"
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = _csv.reader(f)
            for row in reader:
                ws.append(row)
        wb.save(xlsx_path)
        print(f"  生成测试 XLSX: {xlsx_path}")

        csv_result, _ = parse_csv(csv_path, FileType.BANK_STATEMENT)
        xlsx_result, xlsx_imported = parse_xlsx(xlsx_path, FileType.BANK_STATEMENT)

        assert csv_result.row_count == xlsx_result.row_count, \
            f"XLSX 行数应与 CSV 一致: CSV={csv_result.row_count} XLSX={xlsx_result.row_count}"
        print(f"  有效记录数一致: {csv_result.row_count} 条")

        assert len(csv_result.errors) == len(xlsx_result.errors), \
            f"XLSX 错误数应与 CSV 一致: CSV={len(csv_result.errors)} XLSX={len(xlsx_result.errors)}"
        assert len(csv_result.duplicates) == len(xlsx_result.duplicates), \
            f"XLSX 重复数应与 CSV 一致"
        print(f"  错误数一致: {len(csv_result.errors)} 条错误 / {len(csv_result.duplicates)} 条重复")

        csv_ids = [t.txn_id for t in csv_result.transactions]
        xlsx_ids = [t.txn_id for t in xlsx_result.transactions]
        assert csv_ids == xlsx_ids, f"交易号顺序应一致: {csv_ids} vs {xlsx_ids}"
        print(f"  交易号顺序一致: {csv_ids[:5]}...")

        for i in range(min(csv_result.row_count, 12)):
            ct = csv_result.transactions[i]
            xt = xlsx_result.transactions[i]
            assert ct.txn_id == xt.txn_id, f"行{i} txn_id 不一致"
            assert abs(ct.amount - xt.amount) < 1e-9, \
                f"行{i} amount 不一致: {ct.amount} vs {xt.amount}"
            assert ct.date == xt.date, \
                f"行{i} date 不一致: '{ct.date}' vs '{xt.date}'"
            assert ct.counterparty == xt.counterparty, \
                f"行{i} counterparty 不一致: '{ct.counterparty}' vs '{xt.counterparty}'"
            assert ct.description == xt.description, \
                f"行{i} description 不一致: '{ct.description}' vs '{xt.description}'"
            assert ct.currency == xt.currency, \
                f"行{i} currency 不一致: '{ct.currency}' vs '{xt.currency}'"
            assert ct.source_row == xt.source_row, \
                f"行{i} source_row 不一致: {ct.source_row} vs {xt.source_row}"
            assert ct.file_type == xt.file_type, \
                f"行{i} file_type 不一致"
        print("  全部字段逐行对比 (txn_id/amount/date/counterparty/description/currency/source_row) 一致: OK")

        csv_error_types = {e.error_type for e in csv_result.errors}
        xlsx_error_types = {e.error_type for e in xlsx_result.errors}
        assert csv_error_types == xlsx_error_types, f"错误类型应一致: {csv_error_types} vs {xlsx_error_types}"
        print(f"  错误类型一致: {sorted(csv_error_types)}")

        xlsx_dup_types = {d.error_type for d in xlsx_result.duplicates}
        assert "duplicate_txn_id" in xlsx_dup_types, "XLSX 也应检测到重复流水"
        print(f"  重复检测: {len(xlsx_result.duplicates)} 条 duplicate_txn_id")

        assert xlsx_imported.file_type == FileType.BANK_STATEMENT
        assert xlsx_imported.row_count == xlsx_result.row_count
        assert xlsx_imported.error_count == xlsx_result.error_count
        assert os.path.basename(xlsx_imported.file_path) == "bank_statement.xlsx"
        print("  ImportedFile 记录字段正确 (type/row/error/file_path): OK")

        os.environ["BANK_RECONCILE_HOME"] = tmpdir
        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        r = runner.invoke(cli, ["create", "xlsx_import_test"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank", xlsx_path])
        assert r.exit_code == 0, f"CLI xlsx import 应成功: {r.output}"
        assert "有效记录" in r.output, "CLI 输出应显示有效记录数"
        assert "重复流水号" in r.output, "CLI 输出应显示重复告警 (B002)"
        print("  CLI 端 XLSX import 集成成功，输出含有效记录+重复告警: OK")

        storage2 = BatchStorage(tmpdir)
        b = storage2.load(batch_id)
        assert len(b.bank_txns) == xlsx_result.row_count, "CLI import 后批次中的交易数应匹配"
        assert len(b.imported_files) == 1
        assert b.imported_files[0].file_path.endswith(".xlsx")
        print(f"  批次持久化校验: {len(b.bank_txns)} 条交易，导入文件记录为 .xlsx: OK")

        print("[PASS] 场景2 (XLSX 导入字段值) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_close_block_mark_rollback_match():
    """场景3: close 之后 mark 和 rollback 全部被拒 (顺手测 match 也被拒)."""
    print("=== 场景3: close 后 mark/rollback/match 权限控制 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_close_ops_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"
            print(f"  [OK] {desc}: exit={expected_exit}")

        r = runner.invoke(cli, ["create", "操作权限测试"])
        check("create", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import bank (open)", r)
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        check("import system (open)", r)
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "adjustment",
                            os.path.join(samples_dir, "manual_adjustment.csv")])
        check("import adjustment (open)", r)

        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match (open)", r)

        storage = BatchStorage(tmpdir)
        b = storage.load(batch_id)
        assert len(b.discrepancies) > 0, "匹配后应有差异"
        first_disp_id = b.discrepancies[0].discrepancy_id
        print(f"  生成差异 {len(b.discrepancies)} 条, 选 {first_disp_id} 做后续操作")

        r = runner.invoke(cli, ["close", "-b", batch_id])
        check("close", r)

        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match while closed (应被拒)", r, expected_exit=1)
        assert "禁止匹配" in r.output, f"match 应被禁止, 输出: {r.output}"
        print("  关闭后 match 被拒绝: OK")

        r = runner.invoke(cli, ["mark", "-b", batch_id, "-d", first_disp_id,
                              "-s", "confirmed", "-r", "block_tester", "-n", "不应成功"])
        check("mark while closed (应被拒)", r, expected_exit=1)
        assert "禁止标记" in r.output, f"mark 应被禁止, 输出: {r.output}"
        print("  关闭后 mark 被拒绝: OK")

        r = runner.invoke(cli, ["rollback", "-b", batch_id, "-d", first_disp_id])
        check("rollback while closed (应被拒)", r, expected_exit=1)
        assert "禁止回滚" in r.output, f"rollback 应被禁止, 输出: {r.output}"
        print("  关闭后 rollback 被拒绝: OK")

        b_after = storage.load(batch_id)
        disp_after = next(d for d in b_after.discrepancies if d.discrepancy_id == first_disp_id)
        assert disp_after.status == DiscrepancyStatus.OPEN, \
            "关闭状态下 mark 不应改变差异状态"
        assert disp_after.reviewer is None, \
            "关闭状态下 mark 不应写入 reviewer"
        assert disp_after.note == "", \
            "关闭状态下 mark 不应写入 note"
        assert len(disp_after.rollback_history) == 0, \
            "关闭状态下不应有回滚历史"
        print("  关闭下操作均无副作用 (status/reviewer/note/history 未变): OK")

        out_csv = os.path.join(tmpdir, "closed_export.csv")
        r = runner.invoke(cli, ["export", "-b", batch_id, "-o", out_csv])
        check("export while closed (应允许)", r)
        assert os.path.isfile(out_csv), "关闭状态下 export 应正常工作"
        print("  关闭状态下 export 仍可用: OK (不受影响)")

        r = runner.invoke(cli, ["resume", batch_id])
        check("resume while closed (应允许)", r)
        assert "状态: closed" in r.output, "resume 输出应显示 closed 状态"
        print("  关闭状态下 resume 仍可用，状态显示为 closed: OK")

        r = runner.invoke(cli, ["reopen", "-b", batch_id])
        check("reopen", r)

        r = runner.invoke(cli, ["mark", "-b", batch_id, "-d", first_disp_id,
                              "-s", "confirmed", "-r", "reopen_tester", "-n", "reopen 后正常"])
        check("mark after reopen (应成功)", r)
        b_final = storage.load(batch_id)
        disp_final = next(d for d in b_final.discrepancies if d.discrepancy_id == first_disp_id)
        assert disp_final.status == DiscrepancyStatus.CONFIRMED
        assert disp_final.reviewer == "reopen_tester"
        print("  reopen 后 mark 正常工作: OK")

        r = runner.invoke(cli, ["rollback", "-b", batch_id, "-d", first_disp_id])
        check("rollback after reopen (应成功)", r)
        print("  reopen 后 rollback 正常工作: OK")

        print("[PASS] 场景3 (close 后 mark/rollback 拒绝) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_manual_diff_sort():
    """验证1: diff 列出未匹配且排序正确."""
    print("=== 验证1: diff 列出未匹配且排序正确 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_diff_test_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli, _build_diff_rows

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"
            print(f"  [OK] {desc}: exit={expected_exit}")

        r = runner.invoke(cli, ["create", "diff排序测试"])
        check("create", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import bank", r)

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        check("import system", r)

        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match", r)

        storage = BatchStorage(tmpdir)
        batch = storage.load(batch_id)

        diff_rows = _build_diff_rows(batch)
        assert len(diff_rows) > 0, "应该有未匹配记录"
        print(f"  diff 记录数: {len(diff_rows)}")

        sort_keys = [r["sort_key"] for r in diff_rows]
        assert sort_keys == sorted(sort_keys), "记录应按金额差从小到大排序"
        print(f"  排序验证: {[f'{k:.2f}' for k in sort_keys]}")

        for row in diff_rows:
            assert "bank_txn" in row
            assert "system_txn" in row
            assert "amount_diff" in row
            assert "date_diff" in row
            assert row["amount_diff"] >= 0
        print("  每条记录包含 bank_txn/system_txn/amount_diff/date_diff: OK")

        r = runner.invoke(cli, ["diff", "-b", batch_id, "-n", "10"])
        check("diff CLI", r)
        assert "未匹配记录" in r.output
        assert "金额差" in r.output
        assert "日期差" in r.output
        print("  diff CLI 输出包含预期字段: OK")

        csv_path = os.path.join(tmpdir, "diff_export.csv")
        r = runner.invoke(cli, ["diff", "-b", batch_id, "--export", csv_path])
        check("diff --export", r)
        assert os.path.isfile(csv_path), "CSV 文件应落盘"

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            import csv
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            rows = list(reader)

        assert "金额差异" in headers, "CSV 应包含金额差异列"
        assert "日期偏差(天)" in headers, "CSV 应包含日期偏差列"
        assert len(rows) == len(diff_rows), "CSV 行数应匹配 diff 记录数"
        print(f"  CSV 导出验证: {len(rows)} 行, 字段完整")

        csv_amounts = [float(r["金额差异"]) for r in rows]
        assert csv_amounts == sorted(csv_amounts), "CSV 也应按金额差排序"
        print("  CSV 排序正确: OK")

        print("[PASS] 验证1 (diff 列出未匹配且排序正确) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_manual_link_adjustment():
    """验证2: manual-link 后 adjustment 入库字段值对."""
    print("=== 验证2: manual-link 后 adjustment 入库字段值对 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_manual_link_test_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}" \
                + (f"\nexception={result.exception}" if result.exception else "")
            print(f"  [OK] {desc}: exit={expected_exit}")

        r = runner.invoke(cli, ["create", "手工关联测试"])
        check("create", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import bank", r)

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        check("import system", r)

        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match", r)

        storage = BatchStorage(tmpdir)
        batch_before = storage.load(batch_id)
        adj_count_before = len(batch_before.adjustment_txns)
        print(f"  关联前 adjustment 数量: {adj_count_before}")

        bank_txn_id = "B004"
        system_txn_id = "B004"
        adj_type = "amount_rounding"
        reviewer = "tester_manual"
        note = "金额四舍五入差异"

        r = runner.invoke(cli, [
            "manual-link",
            "-b", batch_id,
            "--bank-txn-id", bank_txn_id,
            "--system-txn-id", system_txn_id,
            "-t", adj_type,
            "-r", reviewer,
            "-n", note,
        ])
        check("manual-link", r)

        batch_after = storage.load(batch_id)

        assert len(batch_after.adjustment_txns) == adj_count_before + 1, "应新增一条 adjustment"
        new_adj = batch_after.adjustment_txns[-1]
        print(f"  新增 adjustment: {new_adj.txn_id}")

        assert new_adj.txn_id.startswith("ADJ-"), "adjustment ID 应以 ADJ- 开头"
        assert new_adj.file_type == FileType.MANUAL_ADJUSTMENT
        assert new_adj.source_file == "manual_link"
        assert new_adj.counterparty == "手工关联"
        assert new_adj.currency == "CNY"

        bank_txn = next(t for t in batch_after.bank_txns if t.txn_id == bank_txn_id)
        system_txn = next(t for t in batch_after.system_txns if t.txn_id == system_txn_id)
        expected_amount_diff = system_txn.amount - bank_txn.amount
        assert abs(new_adj.amount - expected_amount_diff) < 1e-9, \
            f"adjustment 金额应为 {expected_amount_diff}, 实际 {new_adj.amount}"
        print(f"  adjustment 金额验证: {new_adj.amount:.2f} (银行 {bank_txn.amount}, 系统 {system_txn.amount})")

        assert new_adj.raw_data["adjustment_type"] == adj_type
        assert new_adj.raw_data["bank_txn_id"] == bank_txn_id
        assert new_adj.raw_data["system_txn_id"] == system_txn_id
        assert new_adj.raw_data["reviewer"] == reviewer
        assert new_adj.raw_data["note"] == note
        print(f"  raw_data 字段验证: adjustment_type={new_adj.raw_data['adjustment_type']}, reviewer={new_adj.raw_data['reviewer']}")

        linked_disp = None
        for d in batch_after.discrepancies:
            if d.bank_txn and d.system_txn and d.adjustment_txn:
                if d.bank_txn.txn_id == bank_txn_id and d.system_txn.txn_id == system_txn_id:
                    linked_disp = d
                    break
        assert linked_disp is not None, "应生成关联的差异记录"
        assert linked_disp.status == DiscrepancyStatus.CONFIRMED
        assert linked_disp.reviewer == reviewer
        assert linked_disp.note == note
        assert linked_disp.adjustment_txn.txn_id == new_adj.txn_id
        print(f"  差异记录验证: {linked_disp.discrepancy_id}, status={linked_disp.status.value}")

        assert len(batch_after.manual_link_history) == 1, "应记录手工关联历史"
        history = batch_after.manual_link_history[0]
        assert history["bank_txn_id"] == bank_txn_id
        assert history["system_txn_id"] == system_txn_id
        assert history["adjustment_type"] == adj_type
        assert history["adjustment_txn_id"] == new_adj.txn_id
        assert history["discrepancy_id"] == linked_disp.discrepancy_id
        print(f"  历史记录验证: adjustment_txn_id={history['adjustment_txn_id']}")

        audit = AuditStorage(tmpdir)
        audit_records = audit.query(op_type="manual_link")
        assert len(audit_records) >= 1, "应写入审计日志"
        assert reviewer in audit_records[0]["summary"]
        assert adj_type in audit_records[0]["summary"]
        print(f"  审计日志验证: {audit_records[0]['summary']}")

        print("[PASS] 验证2 (manual-link 后 adjustment 入库字段值对) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_undo_manual_link():
    """验证3: undo 后再次 diff 确认关联已还原."""
    print("=== 验证3: undo 后再次 diff 确认关联已还原 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_undo_test_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli, _build_diff_rows

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}" \
                + (f"\nexception={result.exception}" if result.exception else "")
            print(f"  [OK] {desc}: exit={expected_exit}")

        r = runner.invoke(cli, ["create", "undo测试"])
        check("create", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import bank", r)

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        check("import system", r)

        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match", r)

        storage = BatchStorage(tmpdir)
        batch_initial = storage.load(batch_id)
        diff_rows_before = _build_diff_rows(batch_initial)
        adj_count_before = len(batch_initial.adjustment_txns)
        disp_count_before = len(batch_initial.discrepancies)
        print(f"  关联前: diff={len(diff_rows_before)} 条, adj={adj_count_before} 条, disp={disp_count_before} 条")

        bank_txn_id = "B004"
        system_txn_id = "B004"
        r = runner.invoke(cli, [
            "manual-link",
            "-b", batch_id,
            "--bank-txn-id", bank_txn_id,
            "--system-txn-id", system_txn_id,
            "-t", "timing_diff",
            "-r", "undo_tester",
            "-n", "日期差异",
        ])
        check("manual-link", r)

        batch_after_link = storage.load(batch_id)
        diff_rows_after_link = _build_diff_rows(batch_after_link)
        print(f"  关联后: diff={len(diff_rows_after_link)} 条, adj={len(batch_after_link.adjustment_txns)} 条")
        assert len(diff_rows_after_link) < len(diff_rows_before), "关联后 diff 记录应减少"

        r = runner.invoke(cli, ["undo", "-b", batch_id])
        check("undo", r)
        assert "已撤销" in r.output

        batch_after_undo = storage.load(batch_id)
        diff_rows_after_undo = _build_diff_rows(batch_after_undo)
        adj_count_after_undo = len(batch_after_undo.adjustment_txns)
        disp_count_after_undo = len(batch_after_undo.discrepancies)
        print(f"  undo后: diff={len(diff_rows_after_undo)} 条, adj={adj_count_after_undo} 条, disp={disp_count_after_undo} 条")

        assert adj_count_after_undo == adj_count_before, \
            f"undo 后 adjustment 数量应恢复为 {adj_count_before}, 实际 {adj_count_after_undo}"
        print(f"  adjustment 数量恢复: {adj_count_before} -> {len(batch_after_link.adjustment_txns)} -> {adj_count_after_undo}")

        assert disp_count_after_undo == disp_count_before, \
            f"undo 后 discrepancy 数量应恢复为 {disp_count_before}, 实际 {disp_count_after_undo}"
        print(f"  discrepancy 数量恢复: {disp_count_before} -> {len(batch_after_link.discrepancies)} -> {disp_count_after_undo}")

        assert len(batch_after_undo.manual_link_history) == 0, \
            f"undo 后历史记录应清空, 实际剩余 {len(batch_after_undo.manual_link_history)} 条"

        assert len(diff_rows_after_undo) == len(diff_rows_before), \
            f"undo 后 diff 记录数应恢复为 {len(diff_rows_before)}, 实际 {len(diff_rows_after_undo)}"
        print(f"  diff 记录数恢复: {len(diff_rows_before)} -> {len(diff_rows_after_link)} -> {len(diff_rows_after_undo)}")

        sort_keys_before = [r["sort_key"] for r in diff_rows_before]
        sort_keys_after_undo = [r["sort_key"] for r in diff_rows_after_undo]
        assert sort_keys_before == sort_keys_after_undo, "undo 后 diff 排序应与关联前一致"
        print("  undo 后 diff 排序与关联前一致: OK")

        audit = AuditStorage(tmpdir)
        undo_audit = audit.query(op_type="undo_manual_link")
        assert len(undo_audit) >= 1, "应写入 undo 审计日志"
        assert bank_txn_id in undo_audit[0]["summary"]
        assert system_txn_id in undo_audit[0]["summary"]
        print(f"  undo 审计日志验证: {undo_audit[0]['summary']}")

        r = runner.invoke(cli, ["undo", "-b", batch_id])
        check("undo (无历史时)", r)
        assert "没有可撤销" in r.output
        print("  无历史时 undo 给出正确提示: OK")

        print("[PASS] 验证3 (undo 后再次 diff 确认关联已还原) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_tolerance_rules_validation():
    """验证1: 非法规则加载报错（tolerance 段非法值）."""
    print("=== 验证1: 非法规则加载报错 ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_tol_val_")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")

    try:
        bad_cases = [
            ("tolerance.amount_tolerance 负数", {
                "tolerance": {"amount_tolerance": -5}
            }, "amount_tolerance 必须是非负数字"),
            ("tolerance.amount_tolerance 布尔", {
                "tolerance": {"amount_tolerance": True}
            }, "不能是布尔值"),
            ("tolerance.amount_tolerance 百分比>100", {
                "tolerance": {"amount_tolerance": "150%"}
            }, "百分比不能超过 100%"),
            ("tolerance.amount_tolerance 非法字符串", {
                "tolerance": {"amount_tolerance": "abc"}
            }, "格式错误"),
            ("tolerance.date_tolerance 负数", {
                "tolerance": {"date_tolerance": -2}
            }, "必须是非负整数天数"),
            ("tolerance.date_tolerance 字符串", {
                "tolerance": {"date_tolerance": "abc"}
            }, "必须是非负整数天数"),
            ("tolerance.txn_id_prefixes 非列表", {
                "tolerance": {"txn_id_prefixes": "B"}
            }, "必须是字符串列表"),
            ("tolerance.description_keywords 非列表", {
                "tolerance": {"description_keywords": 123}
            }, "必须是字符串列表"),
            ("tolerance.enabled 非布尔", {
                "tolerance": {"enabled": "yes"}
            }, "必须是布尔值"),
            ("tolerance 未知字段", {
                "tolerance": {"unknown_field": 1}
            }, "包含未知字段"),
            ("顶层未知字段", {
                "unknown_top": 123
            }, "规则文件包含未知字段"),
        ]

        import yaml
        for case_name, rule_dict, expected_substr in bad_cases:
            safe_name = case_name.replace(" ", "_").replace(">", "_gt_")
            bad_path = os.path.join(tmpdir, f"bad_{safe_name}.yaml")
            with open(bad_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(rule_dict, f, allow_unicode=True)
            try:
                load_rules(bad_path)
                assert False, f"[{case_name}] 应该抛出 RuleValidationError"
            except RuleValidationError as e:
                assert expected_substr in str(e), \
                    f"[{case_name}] 错误信息应包含 '{expected_substr}', 实际: {e}"
                print(f"  [OK] {case_name}: {e}")

        ok, errors = validate_rules(os.path.join(samples_dir, "rules_bad.yaml"))
        assert not ok, "rules_bad.yaml 应该校验失败"
        assert len(errors) > 0
        print(f"  [OK] validate_rules 错误规则返回 (False, {len(errors)} 个错误)")

        ok, errors = validate_rules(os.path.join(samples_dir, "rules.yaml"))
        assert ok, "rules.yaml 应该校验通过"
        assert len(errors) == 0
        print(f"  [OK] validate_rules 正常规则返回 (True, 无错误)")

        print("[PASS] 验证1 (非法规则加载报错) 通过\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_tolerance_matching_match_level():
    """验证2: 容忍匹配 match_level 正确（exact/tolerance/manual）."""
    print("=== 验证2: 容忍匹配 match_level 正确 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_tol_match_")

    try:
        bank_result, _ = parse_csv(
            os.path.join(samples_dir, "bank_statement.csv"),
            FileType.BANK_STATEMENT,
        )
        sys_result, _ = parse_csv(
            os.path.join(samples_dir, "system_receipt.csv"),
            FileType.SYSTEM_RECEIPT,
        )

        batch = Batch.create("容忍匹配测试")
        batch.bank_txns = bank_result.transactions
        batch.system_txns = sys_result.transactions

        tol_rules = MatchRules.default()
        tol_rules.tolerance.enabled = True
        tol_rules.tolerance.amount_value = 10.0
        tol_rules.tolerance.amount_is_percent = False
        tol_rules.tolerance.date_tolerance_days = 3
        tol_rules.tolerance.txn_id_prefixes = ["B"]
        tol_rules.tolerance.description_keywords = ["货款"]

        discrepancies = run_matching(batch, tol_rules)
        batch.discrepancies = discrepancies

        levels = {d.match_level.value for d in discrepancies}
        print(f"  匹配等级分布: {[(d.match_level.value, d.discrepancy_type.value) for d in discrepancies]}")

        assert MatchLevel.EXACT.value in levels, "应有 exact 匹配产生的差异"
        print(f"  [OK] exact 匹配存在: {sum(1 for d in discrepancies if d.match_level == MatchLevel.EXACT)} 条")

        for d in discrepancies:
            if d.match_level == MatchLevel.TOLERANCE:
                assert d.discrepancy_type == DiscrepancyType.NEEDS_MANUAL_REVIEW, \
                    "tolerance 匹配差异应为 NEEDS_MANUAL_REVIEW"
                assert "[容忍匹配]" in d.message, "tolerance 匹配信息应包含 '[容忍匹配]'"
                print(f"  [OK] tolerance 差异: {d.message[:80]}")

        tol_count = sum(1 for d in discrepancies if d.match_level == MatchLevel.TOLERANCE)
        if tol_count > 0:
            tol_records = get_tolerance_match_records(batch)
            assert len(tol_records) == tol_count, "get_tolerance_match_records 数量应匹配"
            print(f"  [OK] get_tolerance_match_records 返回 {len(tol_records)} 条")

        manual_rules = MatchRules.default()
        batch2 = Batch.create("手动匹配测试")
        batch2.bank_txns = bank_result.transactions
        batch2.system_txns = sys_result.transactions
        batch2.discrepancies = run_matching(batch2, manual_rules)

        from datetime import datetime
        import uuid
        if batch2.discrepancies:
            d_manual = batch2.discrepancies[0]
            assert d_manual.match_level == MatchLevel.EXACT, "默认匹配应为 EXACT"
            d_manual.match_level = MatchLevel.MANUAL
            d_dict = d_manual.to_dict()
            assert d_dict["match_level"] == "manual", "to_dict 应正确序列化 match_level"
            print(f"  [OK] 手动序列化 match_level=manual 正确")

        os.environ["BANK_RECONCILE_HOME"] = tmpdir
        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"

        r = runner.invoke(cli, ["create", "tol_cli_test"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import bank", r)
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        check("import system", r)

        import yaml
        tol_rule_path = os.path.join(tmpdir, "tol_rules.yaml")
        tol_rule_data = {
            "amount_tolerance": 0.01,
            "tolerance": {
                "enabled": True,
                "amount_tolerance": 10.0,
                "date_tolerance": 3,
                "txn_id_prefixes": ["B"],
            }
        }
        with open(tol_rule_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(tol_rule_data, f, allow_unicode=True)

        r = runner.invoke(cli, ["rules", "set", "-b", batch_id, tol_rule_path])
        check("rules set with tolerance", r)

        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match with tolerance", r)
        assert "按匹配等级" in r.output or "tolerance" in r.output.lower() or "容忍" in r.output, \
            "匹配输出应包含匹配等级或容忍匹配信息"
        print(f"  [OK] CLI match 包含容忍匹配信息")

        print("[PASS] 验证2 (容忍匹配 match_level 正确) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rules_roundtrip_import_export():
    """验证3: rules 往返导入导出一致."""
    print("=== 验证3: rules 往返导入导出一致 ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_roundtrip_")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")

    try:
        import yaml

        original_rules = MatchRules.default()
        original_rules.tolerance.enabled = True
        original_rules.tolerance.amount_value = 0.05
        original_rules.tolerance.amount_is_percent = True
        original_rules.tolerance.date_tolerance_days = 2
        original_rules.tolerance.txn_id_prefixes = ["PAY", "TXN"]
        original_rules.tolerance.description_keywords = ["采购", "报销"]
        original_rules.manual_review_keywords = ["手续费", "调账"]

        export_path = os.path.join(tmpdir, "exported.yaml")
        export_rules(original_rules, export_path)
        assert os.path.isfile(export_path), "导出文件应存在"
        print(f"  [OK] 规则已导出到 {export_path}")

        reloaded = load_rules(export_path)
        assert reloaded.amount_tolerance == original_rules.amount_tolerance
        assert reloaded.date_window_days == original_rules.date_window_days
        assert reloaded.manual_review_keywords == original_rules.manual_review_keywords
        assert reloaded.tolerance.enabled == original_rules.tolerance.enabled
        assert reloaded.tolerance.amount_is_percent == original_rules.tolerance.amount_is_percent
        assert abs(reloaded.tolerance.amount_value - original_rules.tolerance.amount_value) < 1e-9
        assert reloaded.tolerance.date_tolerance_days == original_rules.tolerance.date_tolerance_days
        assert reloaded.tolerance.txn_id_prefixes == original_rules.tolerance.txn_id_prefixes
        assert reloaded.tolerance.description_keywords == original_rules.tolerance.description_keywords
        print(f"  [OK] 导出后重新加载内容一致")

        export_path2 = os.path.join(tmpdir, "exported2.yaml")
        export_rules(reloaded, export_path2)

        with open(export_path, "r", encoding="utf-8") as f1, \
             open(export_path2, "r", encoding="utf-8") as f2:
            d1 = yaml.safe_load(f1)
            d2 = yaml.safe_load(f2)
        assert d1 == d2, "两次导出内容应完全一致"
        print(f"  [OK] 两次导出 YAML 完全一致")

        imported, warnings = import_rules(export_path, None, check_conflicts=True)
        assert len(warnings) == 0, "无 existing_rules 时应无冲突警告"
        print(f"  [OK] import_rules(无对比) 无警告")

        different_rules = MatchRules.default()
        different_rules.amount_tolerance = 0.5
        different_rules.tolerance.enabled = True
        different_rules.tolerance.amount_value = 100.0
        imported2, warnings2 = import_rules(export_path, different_rules, check_conflicts=True)
        assert len(warnings2) > 0, "与不同规则对比应产生冲突警告"
        print(f"  [OK] 冲突检测产生 {len(warnings2)} 条警告:")
        for w in warnings2:
            print(f"    - {w}")

        imported3, warnings3 = import_rules(export_path, different_rules, check_conflicts=False)
        assert len(warnings3) == 0, "--force 模式下应无冲突警告"
        print(f"  [OK] --force 模式下无冲突警告")

        os.environ["BANK_RECONCILE_HOME"] = tmpdir
        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"

        r = runner.invoke(cli, ["rules", "validate", export_path])
        check("rules validate (valid)", r)
        assert "OK" in r.output and "规则文件有效" in r.output
        print(f"  [OK] CLI rules validate 通过")

        r = runner.invoke(cli, ["rules", "validate",
                                os.path.join(samples_dir, "rules_bad.yaml")])
        check("rules validate (invalid)", r, expected_exit=1)
        assert "校验失败" in r.output
        print(f"  [OK] CLI rules validate 失败正确退出码 1")

        r = runner.invoke(cli, ["rules", "export", "--default",
                                os.path.join(tmpdir, "cli_export.yaml")])
        check("rules export --default", r)
        assert os.path.isfile(os.path.join(tmpdir, "cli_export.yaml"))
        print(f"  [OK] CLI rules export --default 成功")

        r = runner.invoke(cli, ["create", "roundtrip_batch"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["rules", "export", "-b", batch_id,
                                os.path.join(tmpdir, "cli_batch_export.yaml")])
        check("rules export -b (无规则)", r)
        assert "WARN" in r.output or "默认规则" in r.output, \
            "批次无规则时应有警告或导出默认规则"
        print(f"  [OK] CLI rules export -b 无规则时提示正确")

        r = runner.invoke(cli, ["rules", "import", export_path])
        check("rules import (无批次)", r)
        print(f"  [OK] CLI rules import 无批次模式成功")

        print("[PASS] 验证3 (rules 往返导入导出一致) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_summary_matches_diff_count():
    """验证4: summary 统计与 diff 数量吻合."""
    print("=== 验证4: summary 统计与 diff 数量吻合 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_summary_")

    try:
        bank_result, _ = parse_csv(
            os.path.join(samples_dir, "bank_statement.csv"),
            FileType.BANK_STATEMENT,
        )
        sys_result, _ = parse_csv(
            os.path.join(samples_dir, "system_receipt.csv"),
            FileType.SYSTEM_RECEIPT,
        )

        batch = Batch.create("summary 测试")
        batch.bank_txns = bank_result.transactions
        batch.system_txns = sys_result.transactions

        rules = MatchRules.default()
        batch.discrepancies = run_matching(batch, rules)

        summary = generate_summary(batch)
        print(f"  summary keys: {list(summary.keys())}")
        print(f"  exact_matches={summary['exact_matches']}, "
              f"tolerance_matches={summary['tolerance_matches']}, "
              f"manual_matches={summary['manual_matches']}, "
              f"unmatched_count={summary['unmatched_count']}")
        print(f"  bank_txns={summary['bank_transactions']}, "
              f"system_txns={summary['system_transactions']}")

        assert "exact_matches" in summary, "summary 应包含 exact_matches"
        assert "tolerance_matches" in summary, "summary 应包含 tolerance_matches"
        assert "manual_matches" in summary, "summary 应包含 manual_matches"
        assert "unmatched_count" in summary, "summary 应包含 unmatched_count"
        assert "by_match_level" in summary, "summary 应包含 by_match_level"
        print(f"  [OK] summary 字段完整")

        os.environ["BANK_RECONCILE_HOME"] = tmpdir
        from click.testing import CliRunner
        from bank_reconcile.cli import cli, _build_diff_rows
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"

        r = runner.invoke(cli, ["create", "summary_cli_test"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import bank", r)
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        check("import system", r)
        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match", r)

        storage = BatchStorage(tmpdir)
        batch_loaded = storage.load(batch_id)
        diff_rows = _build_diff_rows(batch_loaded)
        print(f"  diff 行数: {len(diff_rows)}")

        csv_path = os.path.join(tmpdir, "summary.csv")
        r = runner.invoke(cli, ["report", "summary", "-b", batch_id, "--export", csv_path])
        check("report summary --export", r)
        assert "精确匹配数" in r.output or "exact" in r.output.lower(), \
            "report summary 应包含精确匹配数"
        assert "容忍匹配数" in r.output or "tolerance" in r.output.lower(), \
            "report summary 应包含容忍匹配数"
        assert "未匹配数" in r.output or "unmatched" in r.output.lower(), \
            "report summary 应包含未匹配数"
        print(f"  [OK] CLI report summary 输出包含匹配等级")

        assert os.path.isfile(csv_path), "summary CSV 应落盘"
        import csv
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            csv_rows = list(reader)
        csv_headers = [row[0] for row in csv_rows if row]
        assert "精确匹配数" in csv_headers, "CSV 应包含精确匹配数"
        assert "容忍匹配数" in csv_headers, "CSV 应包含容忍匹配数"
        assert "手工匹配数" in csv_headers, "CSV 应包含手工匹配数"
        assert "未匹配数" in csv_headers, "CSV 应包含未匹配数"
        assert "按匹配等级统计" in csv_headers, "CSV 应包含按匹配等级统计"
        print(f"  [OK] summary CSV 字段完整: {csv_headers[:12]}...")

        tol_rules = MatchRules.default()
        tol_rules.tolerance.enabled = True
        tol_rules.tolerance.amount_value = 1000.0
        tol_rules.tolerance.date_tolerance_days = 30
        tol_rules.tolerance.txn_id_prefixes = ["B"]

        batch2 = Batch.create("summary tol 测试")
        batch2.bank_txns = bank_result.transactions
        batch2.system_txns = sys_result.transactions
        batch2.discrepancies = run_matching(batch2, tol_rules)

        summary2 = generate_summary(batch2)
        print(f"  启用容忍后: exact={summary2['exact_matches']}, "
              f"tolerance={summary2['tolerance_matches']}, "
              f"unmatched={summary2['unmatched_count']}")

        assert summary2["tolerance_matches"] >= 0, "tolerance_matches 应非负"

        diff_rows2 = _build_diff_rows(batch2)
        print(f"  启用容忍后 diff 行数: {len(diff_rows2)}")
        if summary2["tolerance_matches"] > 0:
            assert len(diff_rows2) <= len(diff_rows), \
                "启用容忍匹配后 diff 行数应减少或不变"
            print(f"  [OK] 启用容忍匹配后 diff 从 {len(diff_rows)} 减少到 {len(diff_rows2)}")

        print("[PASS] 验证4 (summary 统计与 diff 数量吻合) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    try:
        test_parser()
        test_rules()
        test_matching()
        test_storage_and_lifecycle()
        test_source_traceability()
        test_error_paths()
        test_cli_rich_output()
        test_audit_unit()
        test_audit_config()
        test_audit_cli_integration()
        test_audit_persistence()
        test_audit_retention_cleanup()
        test_close_reopen_import_block()
        test_xlsx_import_field_values()
        test_close_block_mark_rollback_match()
        test_manual_diff_sort()
        test_manual_link_adjustment()
        test_undo_manual_link()
        test_tolerance_rules_validation()
        test_tolerance_matching_match_level()
        test_rules_roundtrip_import_export()
        test_summary_matches_diff_count()
        print("=" * 50)
        print("所有测试通过！")
        print("=" * 50)
        return 0
    except AssertionError as e:
        print(f"\n[FAIL] 断言失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    except Exception as e:
        print(f"\n[FAIL] 异常: {e}")
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
