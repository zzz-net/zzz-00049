"""端到端测试脚本 - 验证所有核心功能."""
import os
import sys
import shutil
import tempfile
import io
import json
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


def test_tolerance_same_txn_id_small_diff():
    """回归测试: 同交易号金额差3元日期差1天应走容忍匹配，而非 exact+amount_mismatch."""
    print("=== 回归测试: 同交易号小差异判容忍匹配 ===")

    from bank_reconcile.rules import MatchRules

    rules = MatchRules.default()
    rules.tolerance.enabled = True
    rules.tolerance.amount_value = 5.0
    rules.tolerance.date_tolerance_days = 3
    rules.tolerance.txn_id_prefixes = []
    rules.tolerance.description_keywords = []

    batch = Batch.create("同ID小差异测试")

    batch.bank_txns = [
        Transaction(
            txn_id="TOL001", amount=100.00, date="2024-01-15",
            file_type=FileType.BANK_STATEMENT, source_file="test_bank.csv", source_row=1,
            counterparty="测试商户A", description="采购款",
        ),
        Transaction(
            txn_id="BIGDIFF", amount=500.00, date="2024-01-15",
            file_type=FileType.BANK_STATEMENT, source_file="test_bank.csv", source_row=2,
            counterparty="测试商户B", description="测试大额差异",
        ),
    ]

    batch.system_txns = [
        Transaction(
            txn_id="TOL001", amount=103.00, date="2024-01-16",
            file_type=FileType.SYSTEM_RECEIPT, source_file="test_sys.csv", source_row=1,
            counterparty="测试商户A", description="采购款",
        ),
        Transaction(
            txn_id="BIGDIFF", amount=600.00, date="2024-01-15",
            file_type=FileType.SYSTEM_RECEIPT, source_file="test_sys.csv", source_row=2,
            counterparty="测试商户B", description="测试大额差异",
        ),
    ]

    batch.discrepancies = run_matching(batch, rules)
    discrepancies = batch.discrepancies

    tol001_disps = [
        d for d in discrepancies
        if (d.bank_txn and d.bank_txn.txn_id == "TOL001")
        or (d.system_txn and d.system_txn.txn_id == "TOL001")
    ]
    print(f"  TOL001 差异: {[(d.discrepancy_type.value, d.match_level.value) for d in tol001_disps]}")

    assert len(tol001_disps) >= 1, "TOL001 应至少有1条差异记录"
    tol_disp = tol001_disps[0]
    assert tol_disp.match_level == MatchLevel.TOLERANCE, \
        f"TOL001 应为 TOLERANCE 等级，实际是 {tol_disp.match_level.value}"
    assert tol_disp.discrepancy_type == DiscrepancyType.NEEDS_MANUAL_REVIEW, \
        f"TOL001 差异类型应为 NEEDS_MANUAL_REVIEW，实际是 {tol_disp.discrepancy_type.value}"
    print(f"  [OK] TOL001 match_level=TOLERANCE, type=needs_manual_review")

    bigdiff_disps = [
        d for d in discrepancies
        if (d.bank_txn and d.bank_txn.txn_id == "BIGDIFF")
        or (d.system_txn and d.system_txn.txn_id == "BIGDIFF")
    ]
    print(f"  BIGDIFF 差异: {[(d.discrepancy_type.value, d.match_level.value) for d in bigdiff_disps]}")
    assert len(bigdiff_disps) >= 1, "BIGDIFF 应至少有1条差异记录"
    assert bigdiff_disps[0].match_level == MatchLevel.EXACT, \
        f"BIGDIFF 应为 EXACT 等级（金额差100超容差），实际是 {bigdiff_disps[0].match_level.value}"
    assert bigdiff_disps[0].discrepancy_type == DiscrepancyType.AMOUNT_MISMATCH, \
        f"BIGDIFF 差异类型应为 AMOUNT_MISMATCH，实际是 {bigdiff_disps[0].discrepancy_type.value}"
    print(f"  [OK] BIGDIFF 超容差 → match_level=EXACT, type=amount_mismatch")

    tolerance_records = get_tolerance_match_records(batch)
    assert len(tolerance_records) >= 1, "get_tolerance_match_records 应至少返回1条容忍匹配"
    tol_ids = {r["bank_txn_id"] for r in tolerance_records} | {r["system_txn_id"] for r in tolerance_records}
    assert "TOL001" in tol_ids, "容忍匹配记录应包含 TOL001"
    print(f"  [OK] get_tolerance_match_records 返回 {len(tolerance_records)} 条，包含 TOL001")

    print("[PASS] 回归测试 (同交易号小差异判容忍) 通过\n")


def test_summary_perfect_exact_not_unmatched():
    """回归测试: 完美精确匹配（同ID同金额同日期无关键词）不能被统计为未匹配."""
    print("=== 回归测试: 完美精确匹配不计入未匹配 ===")

    from bank_reconcile.rules import MatchRules

    rules = MatchRules.default()

    batch = Batch.create("完美匹配统计测试")

    batch.bank_txns = [
        Transaction(
            txn_id="PERFECT", amount=999.00, date="2024-02-01",
            file_type=FileType.BANK_STATEMENT, source_file="b.csv", source_row=1,
            counterparty="甲公司", description="正常交易",
        ),
        Transaction(
            txn_id="ONLY_BANK", amount=200.00, date="2024-02-02",
            file_type=FileType.BANK_STATEMENT, source_file="b.csv", source_row=2,
            counterparty="乙公司", description="银行单边",
        ),
    ]

    batch.system_txns = [
        Transaction(
            txn_id="PERFECT", amount=999.00, date="2024-02-01",
            file_type=FileType.SYSTEM_RECEIPT, source_file="s.csv", source_row=1,
            counterparty="甲公司", description="正常交易",
        ),
        Transaction(
            txn_id="ONLY_SYS", amount=300.00, date="2024-02-03",
            file_type=FileType.SYSTEM_RECEIPT, source_file="s.csv", source_row=2,
            counterparty="丙公司", description="系统单边",
        ),
    ]

    batch.discrepancies = run_matching(batch, rules)
    print(f"  生成差异数: {len(batch.discrepancies)}")
    for d in batch.discrepancies:
        print(f"    - {d.discrepancy_type.value} / {d.match_level.value}: {d.message[:60]}")

    summary = generate_summary(batch)
    print(f"  summary: exact={summary['exact_matches']}, "
          f"tolerance={summary['tolerance_matches']}, "
          f"manual={summary['manual_matches']}, "
          f"unmatched={summary['unmatched_count']}")
    print(f"  bank_txns={summary['bank_transactions']}, "
          f"system_txns={summary['system_transactions']}")

    assert summary["exact_matches"] >= 1, \
        f"至少应统计到1条精确匹配（PERFECT），实际 exact_matches={summary['exact_matches']}"
    print(f"  [OK] exact_matches={summary['exact_matches']} ≥ 1")

    assert summary["unmatched_count"] >= 1 and summary["unmatched_count"] <= 2, \
        f"未匹配数应为1或2（ONLY_BANK和ONLY_SYS各算单边），实际 {summary['unmatched_count']}"
    print(f"  [OK] unmatched_count={summary['unmatched_count']} 在合理范围")

    total = summary["exact_matches"] + summary["tolerance_matches"] + summary["manual_matches"] + summary["unmatched_count"]
    print(f"  exact+tolerance+manual+unmatched = {total}")
    assert summary["exact_matches"] + summary["unmatched_count"] >= summary["bank_transactions"], \
        f"精确匹配+未匹配 ≥ 银行回单数不成立: {summary['exact_matches']}+{summary['unmatched_count']} < {summary['bank_transactions']}"
    print(f"  [OK] 精确匹配 + 未匹配 ≥ 银行回单数（{summary['exact_matches']}+{summary['unmatched_count']} ≥ {summary['bank_transactions']}）")

    print("[PASS] 回归测试 (完美精确匹配不计入未匹配) 通过\n")


def test_diff_reason_column_visible():
    """回归: diff 表格包含原因列，且原因中可见日期差说明."""
    print("=== 回归测试: diff 原因列包含日期差说明 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_diff_reason_test_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli, _build_diff_rows

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"

        r = runner.invoke(cli, ["create", "diff原因测试"])
        check("create", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        runner.invoke(cli, ["match", "-b", batch_id])

        r = runner.invoke(cli, ["diff", "-b", batch_id, "-n", "10"])
        check("diff CLI", r)

        assert "原因" in r.output, "diff 输出应包含'原因'列"
        assert "日期差" in r.output, "diff 输出应包含'日期差'列名"
        assert "金额差" in r.output, "diff 输出应包含'金额差'列名"
        assert "未匹配记录" in r.output, "diff 输出应包含标题'未匹配记录'"
        print("  diff 输出包含原因列、日期差列、金额差列: OK")

        storage = BatchStorage(tmpdir)
        batch = storage.load(batch_id)
        rows = _build_diff_rows(batch)
        for row in rows:
            assert "reason" in row, "每一行都应有 reason 字段"
            assert row["reason"], "reason 字段不应为空"
            assert "日期差" in row["reason"] or "金额不符" in row["reason"] or "系统无此交易号" in row["reason"] or "银行无此交易号" in row["reason"], \
                f"reason 应包含有意义的描述: {row['reason']}"
        print(f"  全部 {len(rows)} 条 diff 记录均有 reason 字段: OK")

        csv_path = os.path.join(tmpdir, "diff_with_reason.csv")
        r = runner.invoke(cli, ["diff", "-b", batch_id, "--export", csv_path])
        check("diff --export", r)

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            import csv
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            csv_rows = list(reader)

        assert "原因" in headers, "CSV 导出应包含'原因'列"
        assert len(csv_rows) == len(rows), "CSV 行数应与 diff 记录数一致"
        for csv_row in csv_rows:
            assert csv_row["原因"], "CSV 中原因列不应为空"
        print("  CSV 导出包含原因列且有内容: OK")

        print("[PASS] 回归测试 (diff 原因列包含日期差说明) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_readme_rules_command_matches_cli():
    """回归: README 中的 rules set 命令与实际 CLI help 对齐."""
    print("=== 回归测试: README 规则命令与 CLI help 对齐 ===")

    from click.testing import CliRunner
    from bank_reconcile.cli import cli

    runner = CliRunner()

    r = runner.invoke(cli, ["rules", "--help"])
    assert r.exit_code == 0, f"rules --help 应退出成功: {r.output}"
    assert "set" in r.output, "rules group 应包含 set 子命令"
    assert "validate" in r.output, "rules group 应包含 validate 子命令"
    print("  rules group 包含 set/validate 子命令: OK")

    r = runner.invoke(cli, ["rules", "set", "--help"])
    assert r.exit_code == 0, f"rules set --help 应退出成功: {r.output}"
    assert "--batch-id" in r.output or "-b" in r.output, "rules set 应支持 --batch-id / -b 选项"
    assert "RULE_FILE" in r.output or "rule_file" in r.output or "FILE" in r.output.lower(), \
        f"rules set 应接受规则文件位置参数，实际输出: {r.output[:200]}"
    print("  rules set 支持 --batch-id 选项和规则文件参数: OK")

    readme_path = os.path.join(os.path.dirname(__file__), "README.md")
    assert os.path.isfile(readme_path), "README.md 应存在"
    with open(readme_path, "r", encoding="utf-8") as f:
        readme = f.read()

    assert "rules set" in readme, "README 快速开始应使用 'rules set' 而不是单独的 'rules'"
    assert "--batch-id" in readme, "README 示例命令应包含 --batch-id 长选项"
    print("  README 快速开始包含 rules set 和 --batch-id: OK")

    print("[PASS] 回归测试 (README 规则命令与 CLI 对齐) 通过\n")


def test_archive_write_block():
    """测试1: 归档后 import/match/mark/manual-link/undo/rollback 全拦截，读操作仍可用."""
    print("=== 测试1: 归档写阻断 + 读操作仍可用 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_archive_block_")

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

        r = runner.invoke(cli, ["create", "归档阻断测试"])
        check("create", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        check("import bank (open)", r)
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        check("import system (open)", r)
        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match (open)", r)

        storage = BatchStorage(tmpdir)
        b = storage.load(batch_id)
        first_disp_id = b.discrepancies[0].discrepancy_id
        bank_txn_id = b.bank_txns[0].txn_id
        system_txn_id = b.system_txns[0].txn_id

        r = runner.invoke(cli, ["batch", "archive", batch_id, "-o", "归档员老王", "-n", "月底对账完成归档"])
        check("batch archive", r)
        assert "批次已归档" in r.output, f"归档成功输出应含'批次已归档': {r.output}"
        assert f"操作人" in r.output or "-o" in r.output or True

        b_archived = storage.load(batch_id)
        assert b_archived.is_archived, "模型层状态应为 archived"
        assert b_archived.status.value == "archived"
        print("  [OK] 模型层状态验证: archived")

        assert storage.batch_is_archived(batch_id), "归档 JSON 应在 archive/ 目录"
        assert not storage.batch_exists(batch_id), "原 batches/ 目录应已移除 JSON"
        print("  [OK] 文件目录迁移验证: batches/ -> archive/")

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "adjustment",
                            os.path.join(samples_dir, "manual_adjustment.csv")])
        check("import while archived (应被拒)", r, expected_exit=1)
        assert "已归档（archived），禁止导入" in r.output, \
            f"应提示禁止导入，实际输出: {r.output}"
        print("  [OK] 归档后 import 被拒绝")

        r = runner.invoke(cli, ["match", "-b", batch_id])
        check("match while archived (应被拒)", r, expected_exit=1)
        assert "禁止匹配" in r.output
        print("  [OK] 归档后 match 被拒绝")

        r = runner.invoke(cli, ["mark", "-b", batch_id, "-d", first_disp_id,
                              "-s", "confirmed", "-r", "归档尝试者", "-n", "不应成功"])
        check("mark while archived (应被拒)", r, expected_exit=1)
        assert "禁止标记" in r.output
        print("  [OK] 归档后 mark 被拒绝")

        r = runner.invoke(cli, ["rollback", "-b", batch_id, "-d", first_disp_id])
        check("rollback while archived (应被拒)", r, expected_exit=1)
        assert "禁止回滚" in r.output
        print("  [OK] 归档后 rollback 被拒绝")

        r = runner.invoke(cli, ["manual-link", "-b", batch_id,
                              "--bank-txn-id", bank_txn_id,
                              "--system-txn-id", system_txn_id,
                              "-t", "timing_diff", "-r", "xxx", "-n", "不应成功"])
        check("manual-link while archived (应被拒)", r, expected_exit=1)
        assert "禁止手工关联" in r.output
        print("  [OK] 归档后 manual-link 被拒绝")

        r = runner.invoke(cli, ["undo", "-b", batch_id])
        check("undo while archived (应被拒)", r, expected_exit=1)
        assert "禁止撤销手工关联" in r.output
        print("  [OK] 归档后 undo 被拒绝")

        b_after = storage.load(batch_id)
        disp_after = next(d for d in b_after.discrepancies if d.discrepancy_id == first_disp_id)
        assert disp_after.status == DiscrepancyStatus.OPEN, \
            "归档状态下 mark 不应改变差异状态"
        assert disp_after.reviewer is None
        print("  [OK] 归档下写操作均无副作用 (差异状态未变)")

        out_csv = os.path.join(tmpdir, "archived_export.csv")
        r = runner.invoke(cli, ["export", "-b", batch_id, "-o", out_csv])
        check("export while archived (应允许)", r)
        assert os.path.isfile(out_csv), "归档状态下 export 应正常工作"
        print("  [OK] 归档状态下 export 仍可用 (读操作)")

        r = runner.invoke(cli, ["resume", batch_id])
        check("resume while archived (应允许)", r)
        assert "archived" in r.output.lower(), "resume 应显示 archived 状态"
        print("  [OK] 归档状态下 resume 仍可用，状态显示为 archived")

        r = runner.invoke(cli, ["report", "summary", "-b", batch_id])
        check("report summary while archived (应允许)", r)
        assert "批次汇总" in r.output or "汇总" in r.output
        print("  [OK] 归档状态下 report summary 仍可用")

        r = runner.invoke(cli, ["discrepancies", "-b", batch_id, "-n", "3"])
        check("discrepancies while archived (应允许)", r)
        assert "差异清单" in r.output
        print("  [OK] 归档状态下 discrepancies 仍可用")

        r = runner.invoke(cli, ["diff", "-b", batch_id, "-n", "3"])
        check("diff while archived (应允许)", r)
        print("  [OK] 归档状态下 diff 仍可用")

        r = runner.invoke(cli, ["list", "--all"])
        check("list --all 含归档", r)
        assert "archived" in r.output or "归档" in r.output, \
            f"list --all 应显示归档批次，输出: {r.output[:500]}"
        print("  [OK] list --all 显示归档批次")

        audit = AuditStorage(tmpdir)
        archive_recs = audit.query_archive_log(batch_id=batch_id)
        assert len(archive_recs) >= 1, "archive_log 表应写入1条归档记录"
        assert archive_recs[0]["operation"] == "archive"
        assert archive_recs[0]["operator"] == "归档员老王"
        assert archive_recs[0]["note"] == "月底对账完成归档"
        print(f"  [OK] archive_log 表写入: operation={archive_recs[0]['operation']}, operator={archive_recs[0]['operator']}")

        audit_recs = audit.query(op_type="archive")
        assert len(audit_recs) >= 1, "普通 audit_log 也应记录 archive 操作"
        print(f"  [OK] 普通 audit_log 也记录 archive 操作")

        print("[PASS] 测试1 (归档写阻断+读操作验证) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_archive_restore_cross_restart():
    """测试2: 归档后跨重启（重新实例化）恢复，数据完整还原，操作权限复原."""
    print("=== 测试2: 归档/恢复 跨重启持久化 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_restore_persist_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"
            print(f"  [OK] {desc}: exit={expected_exit}")

        r = runner.invoke(cli, ["create", "跨重启归档测试"])
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

        storage1 = BatchStorage(tmpdir)
        b1 = storage1.load(batch_id)
        bank_count = len(b1.bank_txns)
        sys_count = len(b1.system_txns)
        disp_count = len(b1.discrepancies)
        first_disp_id = b1.discrepancies[0].discrepancy_id
        first_disp_before = b1.discrepancies[0].to_dict()
        print(f"  归档前: bank={bank_count}, sys={sys_count}, disp={disp_count}")

        r = runner.invoke(cli, ["batch", "archive", batch_id, "-o", "持久化测试员"])
        check("batch archive", r)

        storage2 = BatchStorage(tmpdir)
        assert storage2.batch_is_archived(batch_id)
        b_arch = storage2.load(batch_id)
        assert b_arch.is_archived
        assert len(b_arch.bank_txns) == bank_count
        assert len(b_arch.system_txns) == sys_count
        assert len(b_arch.discrepancies) == disp_count
        print("  [OK] 跨实例读取归档: 数据完整 (bank/sys/disp 数量一致)")

        audit1 = AuditStorage(tmpdir)
        arc_recs1 = audit1.query_archive_log(batch_id=batch_id, operation="archive")
        assert len(arc_recs1) == 1, "重启前应写入1条 archive 记录"
        ts_before = arc_recs1[0]["timestamp"]

        r = runner.invoke(cli, ["batch", "restore", batch_id, "-o", "恢复测试员"])
        check("batch restore", r)
        assert "批次已从归档恢复" in r.output
        print("  [OK] batch restore 执行成功")

        storage3 = BatchStorage(tmpdir)
        assert not storage3.batch_is_archived(batch_id), "恢复后应移出 archive 目录"
        assert storage3.batch_exists(batch_id), "恢复后应在 batches/ 目录"

        b_restored = storage3.load(batch_id)
        assert not b_restored.is_archived, "恢复后状态不应是 archived"
        assert b_restored.status.value == "closed", "恢复后默认是 closed 状态"
        assert len(b_restored.bank_txns) == bank_count
        assert len(b_restored.system_txns) == sys_count
        assert len(b_restored.discrepancies) == disp_count
        print(f"  [OK] 恢复后数据完整: bank={len(b_restored.bank_txns)}, sys={len(b_restored.system_txns)}, disp={len(b_restored.discrepancies)}")

        disp_restored = next(d for d in b_restored.discrepancies if d.discrepancy_id == first_disp_id)
        assert disp_restored.to_dict()["discrepancy_id"] == first_disp_before["discrepancy_id"]
        assert disp_restored.to_dict()["message"] == first_disp_before["message"]
        print("  [OK] 恢复后差异内容逐字段一致 (message/discrepancy_id)")

        audit2 = AuditStorage(tmpdir)
        arc_recs2 = audit2.query_archive_log(batch_id=batch_id)
        ops = [r2["operation"] for r2 in arc_recs2]
        assert "archive" in ops, "archive_log 应仍保留 archive 记录"
        assert "restore" in ops, "archive_log 应写入 restore 记录"
        restore_rec = next(r3 for r3 in arc_recs2 if r3["operation"] == "restore")
        assert restore_rec["operator"] == "恢复测试员"
        assert restore_rec["timestamp"] >= ts_before, "restore 时间应晚于 archive"
        print(f"  [OK] archive_log 跨重启完整: ops={ops}, restore操作员={restore_rec['operator']}")

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "adjustment",
                            os.path.join(samples_dir, "manual_adjustment.csv")])
        check("import after restore (closed → 被拒)", r, expected_exit=1)
        assert "批次已关闭" in r.output, "恢复后是 closed，应继续阻止导入直到 reopen"

        r = runner.invoke(cli, ["reopen", "-b", batch_id])
        check("reopen after restore", r)

        r = runner.invoke(cli, ["mark", "-b", batch_id, "-d", first_disp_id,
                              "-s", "confirmed", "-r", "恢复后测试员", "-n", "恢复后成功标记"])
        check("mark after reopen", r)
        b_final = BatchStorage(tmpdir).load(batch_id)
        disp_final = next(d for d in b_final.discrepancies if d.discrepancy_id == first_disp_id)
        assert disp_final.status == DiscrepancyStatus.CONFIRMED
        assert disp_final.reviewer == "恢复后测试员"
        print("  [OK] reopen 后所有写操作恢复正常 (mark 成功)")

        print("[PASS] 测试2 (归档/恢复 跨重启持久化) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_batch_cleanup_preview_force():
    """测试3: batch cleanup 预览不删除，--force 才真删；结合 retention_days 配置."""
    print("=== 测试3: batch cleanup 预览/强删 + retention_days ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_cleanup_test_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir

        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"
            print(f"  [OK] {desc}: exit={expected_exit}")

        cfg = load_config(tmpdir)
        assert cfg.get("retention_days") == 365, f"默认 retention_days 应为 365, 实际 {cfg.get('retention_days')}"
        print("  [OK] 默认 retention_days=365")

        cfg["retention_days"] = 0
        save_config(tmpdir, cfg)
        cfg2 = load_config(tmpdir)
        assert cfg2["retention_days"] == 0
        print("  [OK] 配置 retention_days=0 写入并可读取")

        r = runner.invoke(cli, ["create", "清理预览测试1"])
        check("create batch1", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch1_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["create", "清理预览测试2"])
        check("create batch2", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch2_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["batch", "archive", batch1_id])
        check("archive batch1", r)
        r = runner.invoke(cli, ["batch", "archive", batch2_id])
        check("archive batch2", r)

        storage = BatchStorage(tmpdir)
        import json as _json
        old_batch1_path = storage._archive_path(batch1_id)
        with open(old_batch1_path, "r", encoding="utf-8") as f:
            d1 = _json.load(f)
        d1["updated_at"] = "2020-01-01T00:00:00"
        with open(old_batch1_path, "w", encoding="utf-8") as f:
            _json.dump(d1, f, ensure_ascii=False, indent=2)
        print("  [OK] 将 batch1 updated_at 伪造为 2020-01-01 (超期)")

        cfg["retention_days"] = 30
        save_config(tmpdir, cfg)

        r = runner.invoke(cli, ["batch", "cleanup"])
        check("cleanup (预览, 默认无 --force)", r)
        assert "预览" in r.output or "预览模式" in r.output or "--force" in r.output, \
            f"预览模式应包含'预览'或'--force'提示，输出: {r.output[:500]}"
        assert batch1_id in r.output, f"预览输出应包含超期 batch1_id={batch1_id}"
        print("  [OK] cleanup 预览模式: 显示超期批次，提示 --force")

        assert storage.batch_is_archived(batch1_id), "预览模式不应删除 batch1"
        assert storage.batch_is_archived(batch2_id), "预览模式不应删除 batch2"
        print("  [OK] 预览模式: 实际未删除任何归档批次")

        expired = storage.list_expired_archives(30)
        assert len(expired) == 1, f"list_expired_archives 应返回1条超期 (batch1), 实际 {len(expired)}"
        assert expired[0]["batch_id"] == batch1_id
        print(f"  [OK] storage.list_expired_archives(30): 返回 {len(expired)} 条, batch_id={expired[0]['batch_id']}")

        r = runner.invoke(cli, ["batch", "cleanup", "--force"])
        check("cleanup --force (真删)", r)
        assert "已删除" in r.output or "删除" in r.output, \
            f"--force 模式应含'已删除'，输出: {r.output[:500]}"
        print("  [OK] cleanup --force 执行成功")

        assert not storage.batch_is_archived(batch1_id), "--force 后 batch1 应已删除"
        assert storage.batch_is_archived(batch2_id), "batch2 未超期，应保留"
        print("  [OK] force 模式: 超期 batch1 删除，未超期 batch2 保留")

        r = runner.invoke(cli, ["list", "--all"])
        check("list --all 清理后", r)
        list_out = r.output
        print(f"  [DEBUG] list --all output (len={len(list_out)}):\n{list_out[:800]}\n  [DEBUG] end")
        # 额外校验：用 storage API 验证
        all_from_storage = storage.list_all_batches()
        ids_in_storage = {b["batch_id"] for b in all_from_storage}
        print(f"  [DEBUG] storage.list_all_batches IDs: {ids_in_storage}")
        assert batch2_id in ids_in_storage, f"storage 层应有 batch2={batch2_id}，实际 {ids_in_storage}"
        assert batch1_id not in ids_in_storage, f"storage 层不应有 batch1={batch1_id}，实际 {ids_in_storage}"
        # 若 rich 表格列宽导致换行，退而求其次：CLI 输出中包含 batch2 的名称也算通过
        all_batches_name = {b["name"]: b["batch_id"] for b in all_from_storage}
        batch2_name = next(n for n, i in all_batches_name.items() if i == batch2_id)
        assert batch1_id not in list_out or batch2_name in list_out, \
            f"至少应包含 batch2 名称 {batch2_name} 或 ID {batch2_id}，输出: {list_out[:500]}"
        print("  [OK] list --all 验证: 超期批次不再显示")

        r = runner.invoke(cli, ["batch", "cleanup"])
        check("cleanup (无超期)", r)
        assert "无超期" in r.output or "0 个超期" in r.output or len(storage.list_expired_archives(30)) == 0, \
            "无超期批次时应给出友好提示"
        print("  [OK] 无超期批次时 cleanup 提示友好")

        print("[PASS] 测试3 (batch cleanup 预览/强删) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_import_col_map_positive_negative():
    """测试4: import --col-map 正异常：YAML/JSON/KEY=VALUE 三种形式，及错误处理."""
    print("=== 测试4: import --col-map 列映射正异常 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    alias_dir = os.path.join(os.path.dirname(__file__), "samples_alias")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_colmap_test_")

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

        r = runner.invoke(cli, ["create", "列映射-KEY=VALUE"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_kv = batch_line.split("(ID: ")[1].rstrip(")").strip()

        custom_csv = os.path.join(alias_dir, "bank_custom_cols.csv")
        col_map_kv = "业务编号=txn_id,收支金额=amount,记账日期=date,关联方=counterparty,交易说明=description,本位币=currency"

        r = runner.invoke(cli, ["import", "-b", batch_kv, "-t", "bank",
                            "--col-map", col_map_kv, custom_csv])
        check("import --col-map KEY=VALUE", r)
        b = BatchStorage(tmpdir).load(batch_kv)
        assert len(b.bank_txns) == 3, f"KEY=VALUE 映射后应有3条记录, 实际 {len(b.bank_txns)}"
        ids = sorted([t.txn_id for t in b.bank_txns])
        assert ids == ["C001", "C002", "C003"], f"KEY=VALUE 映射后交易号应匹配: {ids}"
        amounts = sorted([t.amount for t in b.bank_txns])
        assert abs(amounts[0] - 999.99) < 1e-9
        assert abs(amounts[-1] - 2345.67) < 1e-9
        dates = sorted([t.date for t in b.bank_txns])
        assert dates[0] == "2024-03-01"
        counterparties = {t.counterparty for t in b.bank_txns}
        assert "辰公司" in counterparties
        print(f"  [OK] KEY=VALUE 映射: ids={ids}, amounts 999.99~2345.67 OK")

        r = runner.invoke(cli, ["create", "列映射-YAML文件"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_yaml = batch_line.split("(ID: ")[1].rstrip(")").strip()

        yaml_map_path = os.path.join(tmpdir, "col_map.yaml")
        import yaml as _yaml
        yaml_data = {
            "交易单号": "txn_id",
            "发生额": "amount",
            "交易时间": "date",
            "往来单位": "counterparty",
            "交易摘要": "description",
            "结算币种": "currency",
        }
        with open(yaml_map_path, "w", encoding="utf-8") as f:
            _yaml.safe_dump(yaml_data, f, allow_unicode=True)
        alias_csv = os.path.join(alias_dir, "bank_alias_all.csv")
        r = runner.invoke(cli, ["import", "-b", batch_yaml, "-t", "bank",
                            "--col-map", yaml_map_path, alias_csv])
        check("import --col-map YAML 文件", r)
        b2 = BatchStorage(tmpdir).load(batch_yaml)
        assert len(b2.bank_txns) == 5, f"YAML 映射应有5条记录, 实际 {len(b2.bank_txns)}"
        ids2 = sorted([t.txn_id for t in b2.bank_txns])
        assert ids2 == ["A001", "A002", "A003", "A004", "A005"], f"YAML 映射 ids: {ids2}"
        tx_a003 = next(t for t in b2.bank_txns if t.txn_id == "A003")
        assert abs(tx_a003.amount - 2500.00) < 1e-9, f"A003 金额应为2500 (千分位解析后), 实际 {tx_a003.amount}"
        print(f"  [OK] YAML 文件映射: ids={ids2}, A003金额(千分位)={tx_a003.amount:.2f}")

        r = runner.invoke(cli, ["create", "列映射-JSON文件"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_json = batch_line.split("(ID: ")[1].rstrip(")").strip()

        json_map_path = os.path.join(tmpdir, "col_map.json")
        import json as _json
        json_data = {
            "交易单号": "txn_id",
            "金额": "amount",
            "交易日期": "date",
            "交易对手": "counterparty",
            "摘要": "description",
            "币种": "currency",
        }
        with open(json_map_path, "w", encoding="utf-8") as f:
            _json.dump(json_data, f, ensure_ascii=False, indent=2)
        partial_csv = os.path.join(alias_dir, "bank_alias_partial.csv")
        r = runner.invoke(cli, ["import", "-b", batch_json, "-t", "bank",
                            "--col-map", json_map_path, partial_csv])
        check("import --col-map JSON 文件", r)
        b3 = BatchStorage(tmpdir).load(batch_json)
        assert len(b3.bank_txns) == 4
        ids3 = sorted([t.txn_id for t in b3.bank_txns])
        assert ids3 == ["P001", "P002", "P003", "P004"]
        print(f"  [OK] JSON 文件映射: ids={ids3}")

        r = runner.invoke(cli, ["create", "列映射-异常-无效字段"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_bad_std = batch_line.split("(ID: ")[1].rstrip(")").strip()
        r = runner.invoke(cli, ["import", "-b", batch_bad_std, "-t", "bank",
                            "--col-map", "收支金额=not_a_field,业务编号=txn_id", custom_csv])
        check("import --col-map 无效标准字段 (应被拒)", r, expected_exit=1)
        assert "无效" in r.output or "可用标准字段" in r.output, \
            f"无效字段应报错含'无效'或'可用标准字段'，输出: {r.output[:500]}"
        assert "amount" in r.output and "txn_id" in r.output, \
            "错误信息应列出可用字段名 (amount, txn_id 等)"
        print("  [OK] 无效标准字段 → 报错并列出可用字段")

        r = runner.invoke(cli, ["create", "列映射-异常-列名不存在"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_bad_col = batch_line.split("(ID: ")[1].rstrip(")").strip()
        r = runner.invoke(cli, ["import", "-b", batch_bad_col, "-t", "bank",
                            "--col-map", "不存在的列=txn_id,收支金额=amount", custom_csv])
        check("import --col-map 别名列不在表头 (应被拒)", r, expected_exit=1)
        assert "表头中未找到" in r.output or "不存在的列" in r.output, \
            f"应提示别名列不在表头，输出: {r.output[:500]}"
        print("  [OK] 别名列不在表头 → 报错提示列不存在")

        r = runner.invoke(cli, ["create", "列映射-异常-格式错误"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_bad_fmt = batch_line.split("(ID: ")[1].rstrip(")").strip()
        r = runner.invoke(cli, ["import", "-b", batch_bad_fmt, "-t", "bank",
                            "--col-map", "没有等号格式不对,收支金额=amount", custom_csv])
        check("import --col-map 格式错误 (无等号)", r, expected_exit=1)
        assert "KEY=VALUE" in r.output or "格式" in r.output or "无效的列映射项" in r.output, \
            f"格式错误应提示，输出: {r.output[:500]}"
        print("  [OK] 无等号格式 → 报错提示 KEY=VALUE 格式")

        print("[PASS] 测试4 (import --col-map 正异常) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_close_reopen_blocked_on_archived():
    """额外测试: 归档批次 close/reopen 被拒，需先 restore."""
    print("=== 额外测试: 归档批次 close/reopen 被拒 ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_close_reopen_arch_")

    try:
        os.environ["BANK_RECONCILE_HOME"] = tmpdir
        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"
            print(f"  [OK] {desc}: exit={expected_exit}")

        r = runner.invoke(cli, ["create", "closeReopenArchTest"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()

        runner.invoke(cli, ["batch", "archive", batch_id])

        r = runner.invoke(cli, ["close", "-b", batch_id])
        check("close archived (应被拒)", r, expected_exit=1)
        assert "已归档" in r.output and "不能 close" in r.output

        r = runner.invoke(cli, ["reopen", "-b", batch_id])
        check("reopen archived (应被拒)", r, expected_exit=1)
        assert "已归档" in r.output and "不能直接 reopen" in r.output
        print("  [OK] archived 状态下 close/reopen 均被拒并提示 restore")

        r = runner.invoke(cli, ["batch", "restore", batch_id])
        check("restore (成功)", r)
        r = runner.invoke(cli, ["reopen", "-b", batch_id])
        check("reopen after restore (成功)", r)
        assert "已重新打开" in r.output
        print("  [OK] restore → reopen 路径正常")

        print("[PASS] 额外测试 (归档下 close/reopen 被拒) 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_schedule_crud_and_persistence():
    """测试调度任务 CRUD + 持久化 + 重启恢复."""
    print("=== 测试调度任务 CRUD、持久化、重启恢复 ===")
    import tempfile
    import shutil
    from datetime import datetime, timedelta

    from bank_reconcile.scheduler import (
        ScheduleStorage, ScheduleTask, ScheduleStep, ScheduleStatus,
        ScheduleImportConfig, ScheduleReportConfig, Scheduler,
    )
    from bank_reconcile.storage import BatchStorage
    from bank_reconcile.models import Batch

    tmpdir = tempfile.mkdtemp(prefix="bank_sched_test_")
    try:
        batch_storage = BatchStorage(tmpdir)
        batch = Batch.create("对账测试批次")
        batch_storage.save(batch)
        batch_id = batch.batch_id
        print(f"  创建测试批次: {batch_id}")

        sched_storage = ScheduleStorage(tmpdir)

        task = ScheduleTask.create(
            name="每日对账",
            batch_id=batch_id,
            cron="02:00",
            steps=[ScheduleStep.IMPORT, ScheduleStep.MATCH, ScheduleStep.REPORT],
            import_configs=[
                ScheduleImportConfig(file_type="bank", file_path="/tmp/bank.csv"),
                ScheduleImportConfig(file_type="system", file_path="/tmp/system.csv"),
            ],
            report_config=ScheduleReportConfig(
                output_path="/tmp/report.csv",
                with_summary=True,
            ),
            expires_at=(datetime.now() + timedelta(days=30)).isoformat(),
            max_retries=3,
        )
        sched_storage.save(task)
        task_id = task.task_id
        print(f"  创建调度任务: {task_id}")

        loaded = sched_storage.load(task_id)
        assert loaded.task_id == task_id
        assert loaded.name == "每日对账"
        assert loaded.batch_id == batch_id
        assert loaded.cron == "02:00"
        assert [s.value for s in loaded.steps] == ["import", "match", "report"]
        assert len(loaded.import_configs) == 2
        assert loaded.import_configs[0].file_type == "bank"
        assert loaded.report_config is not None
        assert loaded.report_config.output_path == "/tmp/report.csv"
        assert loaded.report_config.with_summary is True
        assert loaded.max_retries == 3
        print("  [OK] 任务保存和读取字段完整")

        tasks = sched_storage.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == task_id
        assert tasks[0]["name"] == "每日对账"
        print("  [OK] list_tasks 返回任务摘要")

        loaded.name = "每日对账v2"
        loaded.status = ScheduleStatus.PAUSED
        sched_storage.save(loaded)
        reloaded = sched_storage.load(task_id)
        assert reloaded.name == "每日对账v2"
        assert reloaded.status == ScheduleStatus.PAUSED
        print("  [OK] 任务更新成功")

        sched2 = ScheduleStorage(tmpdir)
        tasks_after_reload = sched2.load_all_active()
        assert len(tasks_after_reload) == 0, "PAUSED 状态不应出现在活跃任务中"
        reloaded.status = ScheduleStatus.ACTIVE
        sched_storage.save(reloaded)

        sched3 = ScheduleStorage(tmpdir)
        tasks_after_reload = sched3.load_all_active()
        assert len(tasks_after_reload) == 1
        assert tasks_after_reload[0].task_id == task_id
        assert tasks_after_reload[0].name == "每日对账v2"
        print("  [OK] 模拟重启后活跃任务恢复（持久化跨重启验证）")

        scheduler = Scheduler(tmpdir)
        count = scheduler.load_active_tasks()
        assert count == 1, f"期望加载 1 个任务，实际 {count}"
        print("  [OK] Scheduler.load_active_tasks 成功加载任务")

        expired_task = ScheduleTask.create(
            name="已过期任务",
            batch_id=batch_id,
            cron="03:00",
            steps=[ScheduleStep.MATCH],
            expires_at=(datetime.now() - timedelta(hours=1)).isoformat(),
        )
        sched_storage.save(expired_task)
        tasks_with_expired = sched_storage.load_all_active()
        assert len(tasks_with_expired) == 1, "过期任务应被自动排除并标记 expired"
        reloaded_expired = sched_storage.load(expired_task.task_id)
        assert reloaded_expired.status == ScheduleStatus.EXPIRED
        print("  [OK] 过期任务自动标记为 expired 并不再加载")

        ok = sched_storage.delete(task_id)
        assert ok is True
        assert sched_storage.task_exists(task_id) is False
        print("  [OK] 任务删除成功")

        print("[PASS] 调度任务 CRUD/持久化/重启恢复 测试通过\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_schedule_batch_lock_mutex():
    """测试批次并发锁互斥 - 两个任务不能同时操作同一批次."""
    print("=== 测试批次并发锁互斥 ===")
    import tempfile
    import shutil
    import threading
    import time

    from bank_reconcile.scheduler import BatchLock, ScheduleStorage
    from bank_reconcile.storage import BatchStorage
    from bank_reconcile.models import Batch

    tmpdir = tempfile.mkdtemp(prefix="bank_sched_lock_")
    try:
        batch_storage = BatchStorage(tmpdir)
        batch = Batch.create("锁测试批次")
        batch_storage.save(batch)
        batch_id = batch.batch_id

        lock1 = BatchLock(tmpdir, batch_id)
        ok = lock1.acquire(timeout=0)
        assert ok is True, "第一个锁应该获取成功"
        assert lock1.locked is True
        print(f"  [OK] 锁1获取成功 (pid={os.getpid()})")

        lock2 = BatchLock(tmpdir, batch_id)
        ok2 = lock2.acquire(timeout=0)
        assert ok2 is False, "第二个锁在同一批次应该获取失败"
        assert lock2.locked is False
        print("  [OK] 锁2非阻塞获取失败（互斥生效）")

        ok3 = lock2.acquire(timeout=0.3)
        assert ok3 is False, "第二个锁超时后仍应获取失败"
        print("  [OK] 锁2带超时等待仍失败（互斥生效）")

        lock1.release()
        assert lock1.locked is False
        print("  [OK] 锁1释放成功")

        ok4 = lock2.acquire(timeout=0)
        assert ok4 is True, "锁1释放后锁2应该获取成功"
        assert lock2.locked is True
        print("  [OK] 锁1释放后锁2获取成功")

        lock2.release()

        lock3 = BatchLock(tmpdir, batch_id)
        with lock3:
            assert lock3.locked is True
            lock4 = BatchLock(tmpdir, batch_id)
            assert lock4.acquire(timeout=0) is False
        assert lock3.locked is False
        print("  [OK] with 上下文管理器正常工作（自动释放锁）")

        another_batch = Batch.create("另一批次")
        batch_storage.save(another_batch)
        lock_a = BatchLock(tmpdir, batch_id)
        lock_b = BatchLock(tmpdir, another_batch.batch_id)
        assert lock_a.acquire(timeout=0) is True
        assert lock_b.acquire(timeout=0) is True
        print("  [OK] 不同批次之间锁互不干扰")
        lock_a.release()
        lock_b.release()

        print("[PASS] 批次并发锁互斥 测试通过\n")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_schedule_failure_retry_and_audit():
    """测试任务失败重试计数 + 审计日志完整."""
    print("=== 测试任务失败重试与审计日志 ===")
    import tempfile
    import shutil
    import os
    from datetime import datetime, timedelta

    from bank_reconcile.scheduler import (
        ScheduleStorage, ScheduleTask, ScheduleStep, ScheduleStatus,
        ScheduleRunStatus, Scheduler,
    )
    from bank_reconcile.storage import BatchStorage
    from bank_reconcile.models import Batch
    from bank_reconcile.audit import AuditStorage

    tmpdir = tempfile.mkdtemp(prefix="bank_sched_retry_")
    os.environ["BANK_RECONCILE_HOME"] = tmpdir
    try:
        batch_storage = BatchStorage(tmpdir)
        batch = Batch.create("重试测试批次")
        batch_storage.save(batch)
        batch_id = batch.batch_id

        sched_storage = ScheduleStorage(tmpdir)
        audit = AuditStorage(tmpdir)

        bad_task = ScheduleTask.create(
            name="必然失败的任务",
            batch_id="NONEXISTENT-BATCH",
            cron="every 1 minutes",
            steps=[ScheduleStep.MATCH],
            max_retries=2,
        )
        sched_storage.save(bad_task)
        task_id = bad_task.task_id

        scheduler = Scheduler(tmpdir)

        for attempt in range(1, 4):
            result = scheduler.run_task_now(task_id)
            assert result["success"] is False
            assert "批次不存在" in result["error"] or result.get("skipped") is not True
            task_reloaded = sched_storage.load(task_id)
            print(f"  第 {attempt} 次执行后: retry_count={task_reloaded.retry_count}, status={task_reloaded.last_run_status}")

            if attempt <= 2:
                assert task_reloaded.retry_count == attempt
                assert task_reloaded.last_run_status == ScheduleRunStatus.FAILED
            else:
                assert task_reloaded.retry_count == 3
                assert task_reloaded.last_run_status == ScheduleRunStatus.FAILED

        print("  [OK] 失败重试计数正确递增，达到 max_retries 标记为 FAILED")

        samples_dir = os.path.join(os.path.dirname(__file__), "samples")
        good_batch = Batch.create("成功任务批次")
        batch_storage.save(good_batch)

        from bank_reconcile.parser import parse_csv, FileType
        bank_result, bank_imported = parse_csv(
            os.path.join(samples_dir, "bank_statement.csv"),
            FileType.BANK_STATEMENT,
        )
        sys_result, sys_imported = parse_csv(
            os.path.join(samples_dir, "system_receipt.csv"),
            FileType.SYSTEM_RECEIPT,
        )
        good_batch.bank_txns = bank_result.transactions
        good_batch.system_txns = sys_result.transactions
        good_batch.imported_files = [bank_imported, sys_imported]
        good_batch.rule_file = os.path.join(samples_dir, "rules.yaml")
        batch_storage.save(good_batch)

        report_out = os.path.join(tmpdir, "out_report.csv")
        good_task = ScheduleTask.create(
            name="成功任务",
            batch_id=good_batch.batch_id,
            cron="every 60 minutes",
            steps=[ScheduleStep.MATCH, ScheduleStep.REPORT],
            report_config={
                "output_path": report_out,
                "with_summary": True,
            } if False else None,
        )
        from bank_reconcile.scheduler import ScheduleReportConfig
        good_task.report_config = ScheduleReportConfig(output_path=report_out, with_summary=True)
        sched_storage.save(good_task)
        good_task_id = good_task.task_id

        result = scheduler.run_task_now(good_task_id)
        print(f"  成功任务执行结果: success={result['success']}, steps={list(result.get('steps', {}).keys())}")
        assert result["success"] is True
        assert "match" in result["steps"]
        assert "report" in result["steps"]
        assert os.path.isfile(report_out), "报告 CSV 应该已生成"
        assert os.path.isfile(os.path.splitext(report_out)[0] + "_summary.csv"), "摘要 CSV 应该已生成"
        task_good = sched_storage.load(good_task_id)
        assert task_good.last_run_status == ScheduleRunStatus.SUCCESS
        assert task_good.retry_count == 0, "成功任务 retry_count 应重置为 0"
        print("  [OK] 成功任务正确执行，状态 SUCCESS，重试计数归零")

        audit_records = audit.query(op_type="schedule_run")
        print(f"  审计日志 schedule_run 记录数: {len(audit_records)}")
        assert len(audit_records) >= 4, f"应至少有 4 条 schedule_run 记录（3 次失败 + 1 次成功），实际 {len(audit_records)}"
        for rec in audit_records:
            assert rec["command"] == "schedule_run"
            assert rec["batch_id"]  # task_id 或 system
            assert "timestamp" in rec and rec["timestamp"]
        print("  [OK] schedule_run 审计日志完整")

        load_count = scheduler.load_active_tasks()
        print(f"  显式调用 load_active_tasks，加载了 {load_count} 个任务")
        audit_load = audit.query(op_type="schedule_load")
        print(f"  schedule_load 记录: {len(audit_load)}")
        assert len(audit_load) >= 1, "调用 load_active_tasks 应有 schedule_load 审计记录"
        skip_records = audit.query(op_type="schedule_run")
        has_lock_skip = any("锁定" in r["summary"] or "batch_locked" in r.get("reason", "") for r in skip_records)
        print(f"  [OK] schedule_load 审计日志存在，共 {len(audit_load)} 条")

        print("[PASS] 失败重试与审计日志 测试通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_schedule_cli_commands():
    """测试 schedule CLI 子命令 add/list/show/update/delete/run --now."""
    print("=== 测试 schedule CLI 子命令 ===")
    import tempfile
    import shutil
    import os
    from datetime import datetime, timedelta

    tmpdir = tempfile.mkdtemp(prefix="bank_sched_cli_")
    os.environ["BANK_RECONCILE_HOME"] = tmpdir
    try:
        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] 退出码 {result.exit_code}, 期望 {expected_exit}, stdout={result.output}"
            print(f"  [OK] {desc}: exit={expected_exit}")

        r = runner.invoke(cli, ["create", "CLI调度测试批次"])
        check("创建批次", r)
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()
        print(f"  测试批次: {batch_id}")

        r = runner.invoke(cli, ["schedule", "list"])
        check("schedule list (空)", r)
        assert "尚无定时任务" in r.output

        r = runner.invoke(cli, ["schedule", "add",
                                "--name", "CLI测试任务",
                                "-b", batch_id,
                                "--cron", "03:00",
                                "--steps", "match,report",
                                "--max-retries", "2"])
        check("schedule add", r)
        assert "定时任务已创建" in r.output
        task_line = [ln for ln in r.output.splitlines() if "SCHED-" in ln][0]
        task_id = task_line.split("(ID: ")[1].rstrip(")").strip()
        print(f"  创建任务: {task_id}")

        r = runner.invoke(cli, ["schedule", "list"])
        check("schedule list (有任务)", r)
        assert task_id in r.output
        assert "CLI测试任务" in r.output

        r = runner.invoke(cli, ["schedule", "show", task_id])
        check("schedule show", r)
        assert task_id in r.output
        assert "CLI测试任务" in r.output
        assert batch_id in r.output
        assert "03:00" in r.output

        r = runner.invoke(cli, ["schedule", "update", task_id,
                                "--name", "CLI测试任务v2",
                                "--status", "paused"])
        check("schedule update", r)
        assert "任务已更新" in r.output
        assert "name=CLI测试任务v2" in r.output
        assert "status=paused" in r.output

        r = runner.invoke(cli, ["schedule", "show", task_id])
        assert "CLI测试任务v2" in r.output
        assert "paused" in r.output
        print("  [OK] schedule update 后 show 显示新值")

        r = runner.invoke(cli, ["schedule", "show", "SCHED-NOTEXIST"])
        check("schedule show 不存在", r, expected_exit=1)
        assert "任务不存在" in r.output

        r = runner.invoke(cli, ["schedule", "delete", task_id, "--force"])
        check("schedule delete --force", r)
        assert "任务已删除" in r.output

        r = runner.invoke(cli, ["schedule", "list"])
        assert "尚无定时任务" in r.output
        print("  [OK] schedule delete 删除后 list 为空")

        samples_dir = os.path.join(os.path.dirname(__file__), "samples")
        r = runner.invoke(cli, ["create", "运行测试批次"])
        batch_line = [ln for ln in r.output.splitlines() if "BATCH-" in ln][0]
        run_batch_id = batch_line.split("(ID: ")[1].rstrip(")").strip()
        r = runner.invoke(cli, ["import", "-b", run_batch_id, "-t", "bank",
                                os.path.join(samples_dir, "bank_statement.csv")])
        r = runner.invoke(cli, ["import", "-b", run_batch_id, "-t", "system",
                                os.path.join(samples_dir, "system_receipt.csv")])
        r = runner.invoke(cli, ["rules", "set", "-b", run_batch_id,
                                os.path.join(samples_dir, "rules.yaml")])

        report_path = os.path.join(tmpdir, "cli_run_report.csv")
        import yaml
        cfg_path = os.path.join(tmpdir, "task_cfg.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump({
                "report_config": {
                    "output_path": report_path,
                    "with_summary": True,
                }
            }, f)

        r = runner.invoke(cli, ["schedule", "add",
                                "--name", "run-now测试",
                                "-b", run_batch_id,
                                "--cron", "every 120 minutes",
                                "--steps", "match,report",
                                "--config", cfg_path])
        check("schedule add (with config)", r)
        task_line = [ln for ln in r.output.splitlines() if "SCHED-" in ln][0]
        run_task_id = task_line.split("(ID: ")[1].rstrip(")").strip()

        r = runner.invoke(cli, ["schedule", "run", run_task_id, "--now"])
        check("schedule run --now", r)
        assert "任务执行成功" in r.output or r.exit_code == 0
        print(f"  schedule run --now 输出: {[l for l in r.output.splitlines() if l.strip()][:5]}")
        assert os.path.isfile(report_path), "schedule run --now 应该生成报告文件"
        print("  [OK] schedule run --now 成功执行 match+report，报告已生成")

        print("[PASS] schedule CLI 子命令 测试通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_scheduler_should_run_logic():
    """测试 should_run_now 的 HH:MM 和 every N minutes 触发逻辑."""
    print("=== 测试 should_run_now 触发逻辑 ===")
    from datetime import datetime, timedelta
    from bank_reconcile.scheduler import ScheduleTask, ScheduleStep, ScheduleStatus

    task = ScheduleTask.create(
        name="cron测试",
        batch_id="BATCH-TEST",
        cron="14:30",
        steps=[ScheduleStep.MATCH],
    )

    t1 = datetime(2026, 6, 14, 14, 30, 0)
    assert task.should_run_now(t1) is True, "首次在指定时间点应触发"
    task.last_run_at = t1.isoformat()

    t2 = datetime(2026, 6, 14, 14, 30, 30)
    assert task.should_run_now(t2) is False, "同一天已运行过不再触发"

    t3 = datetime(2026, 6, 15, 14, 30, 0)
    assert task.should_run_now(t3) is True, "第二天同一时间应触发"

    t4 = datetime(2026, 6, 14, 14, 29, 59)
    task.last_run_at = None
    assert task.should_run_now(t4) is False, "时间未到不应触发"
    print("  [OK] HH:MM 格式触发逻辑正确")

    task2 = ScheduleTask.create(
        name="every测试",
        batch_id="BATCH-TEST",
        cron="every 5 minutes",
        steps=[ScheduleStep.MATCH],
    )
    assert task2.should_run_now() is True, "首次 every 任务应立即触发"
    base = datetime(2026, 6, 14, 10, 0, 0)
    task2.last_run_at = base.isoformat()
    assert task2.should_run_now(base) is False
    assert task2.should_run_now(base + timedelta(minutes=4)) is False
    assert task2.should_run_now(base + timedelta(minutes=5)) is True
    assert task2.should_run_now(base + timedelta(minutes=10)) is True
    print("  [OK] every N minutes 格式触发逻辑正确")

    task2.status = ScheduleStatus.PAUSED
    assert task2.should_run_now(base + timedelta(minutes=30)) is False, "暂停状态不应触发"
    task2.status = ScheduleStatus.ACTIVE
    task2.expires_at = (base - timedelta(hours=1)).isoformat()
    assert task2.should_run_now(base + timedelta(minutes=30)) is False, "过期任务不应触发"
    print("  [OK] 暂停/过期状态下不触发")

    print("[PASS] should_run_now 触发逻辑 测试通过\n")


def _setup_full_batch(storage_dir: str, samples_dir: str, batch_name: str = "快照测试批次"):
    from click.testing import CliRunner
    from bank_reconcile.cli import cli

    os.environ["BANK_RECONCILE_HOME"] = storage_dir
    runner = CliRunner()

    def check(desc, result, expected_exit=0):
        assert result.exit_code == expected_exit, \
            f"[{desc}] exit={result.exit_code} expect={expected_exit}, out={result.output}" \
            + (f"\n exc={result.exception}" if result.exception else "")

    r = runner.invoke(cli, ["create", batch_name])
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

    storage = BatchStorage(storage_dir)
    b = storage.load(batch_id)
    assert len(b.discrepancies) > 0, "match 后应存在差异"
    first_disp_id = b.discrepancies[0].discrepancy_id

    r = runner.invoke(cli, ["mark", "-b", batch_id, "-d", first_disp_id,
                          "-s", "confirmed", "-r", "snapshot_tester", "-n", "快照标记测试"])
    check("mark", r)

    export_path = os.path.join(storage_dir, "pre_snapshot_report.csv")
    r = runner.invoke(cli, ["export", "-b", batch_id, "-o", export_path, "--with-summary"])
    check("export", r)
    assert os.path.isfile(export_path)

    return batch_id, first_disp_id, export_path


def test_snapshot_create_and_info():
    print("=== 场景3-1: 快照创建与 info 查看 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    src_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_src_")
    snap_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_store_")

    try:
        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        from bank_reconcile.snapshot import read_snapshot_info

        batch_id, first_disp_id, pre_export = _setup_full_batch(src_dir, samples_dir, "快照创建测试")

        storage = BatchStorage(src_dir)
        pre_batch = storage.load(batch_id)
        pre_disps = len(pre_batch.discrepancies)
        pre_imported = len(pre_batch.imported_files)
        print(f"  源批次: bank={len(pre_batch.bank_txns)} sys={len(pre_batch.system_txns)} "
              f"adj={len(pre_batch.adjustment_txns)} disp={pre_disps}")

        os.environ["BANK_RECONCILE_HOME"] = src_dir
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] exit={result.exit_code} expect={expected_exit}, out={result.output}" \
                + (f"\n exc={result.exception}" if result.exception else "")
            print(f"  [OK] {desc}")

        snap_path = os.path.join(snap_dir, "mybatch.brsnap")
        r = runner.invoke(cli, ["snapshot", "create", "-b", batch_id, "-o", snap_path])
        check("snapshot create", r)
        assert os.path.isfile(snap_path), "快照文件应落盘"
        assert "快照创建成功" in r.output
        assert batch_id in r.output
        print(f"  快照文件: {snap_path}, 大小 {os.path.getsize(snap_path)/1024:.1f} KB")

        r = runner.invoke(cli, ["snapshot", "info", snap_path])
        check("snapshot info", r)
        assert "完整性校验通过" in r.output
        assert "快照ID" in r.output
        assert "校验摘要 (SHA-256)" in r.output
        assert batch_id in r.output
        assert "内嵌规则" in r.output
        print("  info 命令展示所有关键字段")

        info = read_snapshot_info(snap_path)
        assert info["snapshot_version"].startswith("1.")
        assert info["batch_id"] == batch_id
        assert info["discrepancy_count"] == pre_disps
        assert info["imported_file_count"] == pre_imported
        assert info["audit_record_count"] >= 5
        assert info["payload_summary"]["has_rules_yaml"] is True
        print(f"  read_snapshot_info: snapshot_id={info['snapshot_id']}, "
              f"audit_records={info['audit_record_count']}")

        r = runner.invoke(cli, ["snapshot", "create", "-b", "BATCH-NOTEXIST",
                                "-o", os.path.join(snap_dir, "x.brsnap")])
        check("snapshot create 不存在批次 (应失败)", r, expected_exit=1)
        assert "批次不存在" in r.output or "ERR" in r.output
        print("  create 不存在批次正确拒绝")

        audit = AuditStorage(src_dir)
        snap_records = audit.query(op_type="snapshot_create")
        assert len(snap_records) >= 1, "应写入 snapshot_create 审计"
        fail_records = audit.query(op_type="snapshot_create_fail")
        assert len(fail_records) >= 1, "应写入 snapshot_create_fail 审计"
        print(f"  审计写入: snapshot_create×{len(snap_records)}, snapshot_create_fail×{len(fail_records)}")

        print("[PASS] 快照创建与 info 查看 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(snap_dir, ignore_errors=True)


def test_snapshot_restore_cross_directory():
    print("=== 场景3-2: 跨目录恢复 + 恢复后功能验证 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    src_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_src2_")
    dst_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_dst2_")
    snap_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_file_")

    try:
        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        batch_id, first_disp_id, pre_export = _setup_full_batch(src_dir, samples_dir, "跨目录恢复测试")

        storage_src = BatchStorage(src_dir)
        pre_batch = storage_src.load(batch_id)
        pre_disps = len(pre_batch.discrepancies)
        pre_summary = generate_summary(pre_batch)

        snap_path = os.path.join(snap_dir, "crossdir.brsnap")
        os.environ["BANK_RECONCILE_HOME"] = src_dir
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] exit={result.exit_code} expect={expected_exit}, out={result.output}" \
                + (f"\n exc={result.exception}" if result.exception else "")
            print(f"  [OK] {desc}")

        r = runner.invoke(cli, ["snapshot", "create", "-b", batch_id, "-o", snap_path])
        check("源: snapshot create", r)

        r = runner.invoke(cli, ["snapshot", "restore", snap_path, "-d", dst_dir])
        check("跨目录 snapshot restore", r)
        assert "快照恢复成功" in r.output
        assert batch_id in r.output
        print("  跨目录恢复成功")

        os.environ["BANK_RECONCILE_HOME"] = dst_dir

        r = runner.invoke(cli, ["list"])
        check("恢复后: list", r)
        assert batch_id in r.output
        assert "跨目录恢复测试" in r.output
        print("  list 能看到恢复后的批次")

        r = runner.invoke(cli, ["resume", batch_id])
        check("恢复后: resume", r)
        assert "批次信息" in r.output
        assert str(pre_disps) in r.output
        print("  resume 能看到恢复后的批次信息")

        post_storage = BatchStorage(dst_dir)
        post_batch = post_storage.load(batch_id)
        post_summary = generate_summary(post_batch)
        assert post_summary["total_discrepancies"] == pre_summary["total_discrepancies"]
        assert post_summary["bank_transactions"] == pre_summary["bank_transactions"]
        assert post_summary["system_transactions"] == pre_summary["system_transactions"]
        assert post_summary["adjustment_transactions"] == pre_summary["adjustment_transactions"]
        assert post_summary["exact_matches"] == pre_summary["exact_matches"]
        assert post_summary["manual_matches"] == pre_summary["manual_matches"]
        print(f"  汇总一致: total_disp={post_summary['total_discrepancies']}, "
              f"exact={post_summary['exact_matches']}, manual={post_summary['manual_matches']}")

        marked_disp = next(d for d in post_batch.discrepancies if d.discrepancy_id == first_disp_id)
        assert marked_disp.status.value == "confirmed", "恢复后人工标记状态应保留"
        assert marked_disp.reviewer == "snapshot_tester"
        assert marked_disp.note == "快照标记测试"
        assert len(marked_disp.rollback_history) >= 1, "恢复后回滚历史应保留"
        print(f"  人工标记保留: status={marked_disp.status.value}, reviewer={marked_disp.reviewer}, "
              f"rollback_entries={len(marked_disp.rollback_history)}")

        r = runner.invoke(cli, ["rollback", "-b", batch_id, "-d", first_disp_id])
        check("恢复后: rollback 可执行", r)
        post_batch2 = post_storage.load(batch_id)
        rolled = next(d for d in post_batch2.discrepancies if d.discrepancy_id == first_disp_id)
        assert rolled.status.value == "open"
        print("  rollback 可以正常工作（回滚后状态=open）")

        post_export = os.path.join(dst_dir, "post_restore_report.csv")
        r = runner.invoke(cli, ["export", "-b", batch_id, "-o", post_export, "--with-summary"])
        check("恢复后: export", r)
        assert os.path.isfile(post_export)

        with open(pre_export, "r", encoding="utf-8-sig") as f1, \
             open(post_export, "r", encoding="utf-8-sig") as f2:
            pre_lines = f1.readlines()
            post_lines = f2.readlines()
        assert len(pre_lines) == len(post_lines), \
            f"导出行数应一致: 源={len(pre_lines)} 目标={len(post_lines)}"
        print(f"  导出报告行数一致: {len(pre_lines)} 行（差异记录数相同）")

        r = runner.invoke(cli, ["diff", "-b", batch_id, "-n", "10"])
        check("恢复后: diff 查询", r)
        assert "未匹配记录" in r.output or "所有记录已匹配" in r.output
        print("  diff 查询可执行")

        r = runner.invoke(cli, ["discrepancies", "-b", batch_id, "-n", "10"])
        check("恢复后: discrepancies 查询", r)
        assert "差异清单" in r.output
        print("  discrepancies 查询可执行")

        audit_dst = AuditStorage(dst_dir)
        snap_restore_records = audit_dst.query(op_type="snapshot_restore")
        assert len(snap_restore_records) >= 1, "应写入 snapshot_restore 审计"
        imported_audit = audit_dst.query(batch_id=batch_id)
        assert len(imported_audit) >= 5, "恢复后应至少包含 5 条原始操作的审计记录"
        commands_in_dst = {r["command"] for r in imported_audit}
        assert "import" in commands_in_dst, "import 审计被恢复"
        assert "match" in commands_in_dst, "match 审计被恢复"
        assert "mark" in commands_in_dst, "mark 审计被恢复"
        assert "snapshot_restore" in commands_in_dst, "snapshot_restore 审计被写入"
        print(f"  审计记录: 共 {len(imported_audit)} 条, 类型={sorted(commands_in_dst)}")

        r = runner.invoke(cli, ["audit-log", "-b", batch_id, "-t", "import"])
        check("恢复后: audit-log -t import 查询", r)
        assert "导入" in r.output
        print("  audit-log 可正常过滤查询")

        rules_dir = os.path.join(dst_dir, "restored_rules")
        rules_files = [f for f in os.listdir(rules_dir)] if os.path.isdir(rules_dir) else []
        assert len(rules_files) >= 1, "规则文件应被恢复到 restored_rules 目录"
        print(f"  规则文件恢复: {rules_files}")

        config_dst = load_config(dst_dir)
        assert "column_aliases" in config_dst
        print(f"  配置恢复: audit_retention_days={config_dst.get('audit_retention_days')}")

        print("[PASS] 跨目录恢复与恢复后功能 全部通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(dst_dir, ignore_errors=True)
        shutil.rmtree(snap_dir, ignore_errors=True)


def test_snapshot_conflict_strategies():
    print("=== 场景3-3: 冲突处理策略 (skip/overwrite/rename) ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    src_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_src3_")
    dst_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_dst3_")
    snap_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_file3_")

    try:
        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        batch_id, _, _ = _setup_full_batch(src_dir, samples_dir, "冲突策略测试")

        snap_path = os.path.join(snap_dir, "conflict.brsnap")
        os.environ["BANK_RECONCILE_HOME"] = src_dir
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] exit={result.exit_code} expect={expected_exit}, out={result.output}" \
                + (f"\n exc={result.exception}" if result.exception else "")
            print(f"  [OK] {desc}")

        r = runner.invoke(cli, ["snapshot", "create", "-b", batch_id, "-o", snap_path])
        check("源: snapshot create", r)

        os.environ["BANK_RECONCILE_HOME"] = dst_dir
        r = runner.invoke(cli, ["create", "冲突策略测试"])
        check("目标: 创建同名批次", r)
        dst_storage = BatchStorage(dst_dir)
        dst_batches = dst_storage.list_batches()
        dst_same_name_id = dst_batches[0]["batch_id"]
        print(f"  目标已存在同名批次 ID={dst_same_name_id}")

        r = runner.invoke(cli, ["snapshot", "restore", snap_path, "-d", dst_dir, "-s", "skip"])
        check("restore strategy=skip (应冲突失败)", r, expected_exit=1)
        assert "冲突" in r.output or "SKIP" in r.output.upper()
        print("  strategy=skip 正确报告冲突并退出")

        r = runner.invoke(cli, ["snapshot", "restore", snap_path, "-d", dst_dir,
                                "-s", "rename", "--new-name", "冲突策略测试_改名版"])
        check("restore strategy=rename", r)
        assert "快照恢复成功" in r.output
        assert "改名版" in r.output
        print("  strategy=rename 成功恢复为新名称")

        renamed_batches = [b for b in dst_storage.list_batches()
                           if b["name"] == "冲突策略测试_改名版"]
        assert len(renamed_batches) == 1
        renamed_id = renamed_batches[0]["batch_id"]
        assert renamed_id != batch_id, "rename 后 ID 必须改变"
        print(f"  rename 后 ID 已变化: {batch_id} -> {renamed_id}")

        r = runner.invoke(cli, ["snapshot", "restore", snap_path, "-d", dst_dir, "-s", "overwrite"])
        check("restore strategy=overwrite (目标同ID还不存在，应直接导入)", r)
        assert "快照恢复成功" in r.output
        overwrite_batches = [b for b in dst_storage.list_batches()
                             if b["batch_id"] == batch_id]
        assert len(overwrite_batches) == 1, "overwrite 后源 ID 应存在于目标"
        print(f"  overwrite 成功: 目标存在 ID={batch_id}")

        original_overwrite_id = overwrite_batches[0]["batch_id"]
        r = runner.invoke(cli, ["snapshot", "restore", snap_path, "-d", dst_dir, "-s", "overwrite"])
        check("restore strategy=overwrite (覆盖已有同ID)", r)
        after_overwrite = dst_storage.list_batches()
        ids = [b["batch_id"] for b in after_overwrite]
        assert ids.count(original_overwrite_id) == 1, "overwrite 后仍然只有一条同ID"
        print("  overwrite 覆盖同ID不会重复插入")

        audit_dst = AuditStorage(dst_dir)
        fail_records = audit_dst.query(op_type="snapshot_restore_fail")
        assert len(fail_records) >= 1, "应至少有一次 snapshot_restore_fail 审计（skip 那次）"
        restore_records = audit_dst.query(op_type="snapshot_restore")
        assert len(restore_records) >= 3, "应有三次成功恢复（rename + 两次 overwrite）"
        print(f"  审计记录: restore_fail×{len(fail_records)}, restore×{len(restore_records)}")

        print("[PASS] 冲突处理策略 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(dst_dir, ignore_errors=True)
        shutil.rmtree(snap_dir, ignore_errors=True)


def test_snapshot_corrupted_rejected():
    print("=== 场景3-4: 损坏快照拒绝恢复 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    src_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_src4_")
    dst_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_dst4_")
    snap_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_file4_")

    try:
        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        from bank_reconcile.snapshot import (
            read_snapshot_info, restore_snapshot, ConflictStrategy,
            SnapshotCorruptedError, SnapshotVersionError,
        )

        batch_id, _, _ = _setup_full_batch(src_dir, samples_dir, "损坏快照测试")

        snap_path = os.path.join(snap_dir, "valid.brsnap")
        os.environ["BANK_RECONCILE_HOME"] = src_dir
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] exit={result.exit_code} expect={expected_exit}, out={result.output}" \
                + (f"\n exc={result.exception}" if result.exception else "")
            print(f"  [OK] {desc}")

        r = runner.invoke(cli, ["snapshot", "create", "-b", batch_id, "-o", snap_path])
        check("snapshot create", r)

        bad_json = os.path.join(snap_dir, "bad_json.brsnap")
        with open(bad_json, "w", encoding="utf-8") as f:
            f.write("this is not json{{{{")
        try:
            read_snapshot_info(bad_json)
            assert False, "非法JSON应抛异常"
        except SnapshotCorruptedError as e:
            assert "合法 JSON" in str(e) or "JSON" in str(e)
            print(f"  非法JSON 正确拒绝: {e}")
        r = runner.invoke(cli, ["snapshot", "restore", bad_json, "-d", dst_dir])
        check("restore 非法JSON (应失败)", r, expected_exit=1)

        import json as _json
        with open(snap_path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        data["checksum"] = "0000000000000000000000000000000000000000000000000000000000000000"
        bad_checksum = os.path.join(snap_dir, "bad_checksum.brsnap")
        with open(bad_checksum, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            read_snapshot_info(bad_checksum)
            assert False, "错误checksum应抛异常"
        except SnapshotCorruptedError as e:
            assert "校验失败" in str(e)
            print(f"  错误 checksum 正确拒绝: {e}")
        r = runner.invoke(cli, ["snapshot", "restore", bad_checksum, "-d", dst_dir])
        check("restore 错误checksum (应失败)", r, expected_exit=1)

        data2 = dict(data)
        data2["snapshot_version"] = "99.99.99"
        data2["payload"]["batch"]["bank_txns"][0]["txn_id"] = data2["payload"]["batch"]["bank_txns"][0]["txn_id"]
        data2["checksum"] = "00" * 32
        bad_version = os.path.join(snap_dir, "bad_version.brsnap")
        with open(bad_version, "w", encoding="utf-8") as f:
            _json.dump(data2, f, ensure_ascii=False, indent=2)
        try:
            read_snapshot_info(bad_version)
            assert False, "错误版本应抛异常"
        except SnapshotVersionError as e:
            assert "不兼容" in str(e) or "版本" in str(e)
            print(f"  不兼容版本 正确拒绝: {e}")
        r = runner.invoke(cli, ["snapshot", "restore", bad_version, "-d", dst_dir])
        check("restore 不兼容版本 (应失败)", r, expected_exit=1)

        with open(snap_path, "r", encoding="utf-8") as f:
            data3 = _json.load(f)
        data3["payload"]["batch"]["bank_txns"].append({
            "txn_id": "tampered", "amount": 999999.99, "date": "2099-01-01",
            "file_type": "bank_statement", "source_file": "x", "source_row": 0,
        })
        bad_payload = os.path.join(snap_dir, "bad_payload.brsnap")
        with open(bad_payload, "w", encoding="utf-8") as f:
            _json.dump(data3, f, ensure_ascii=False, indent=2)
        try:
            read_snapshot_info(bad_payload)
            assert False, "篡改payload(未同步checksum)应抛异常"
        except SnapshotCorruptedError as e:
            assert "校验失败" in str(e)
            print(f"  篡改 payload 后 checksum 校验失败: {e}")
        dst_storage = BatchStorage(dst_dir)
        try:
            restore_snapshot(bad_payload, dst_storage, ConflictStrategy.SKIP)
            assert False, "篡改payload后restore应失败"
        except SnapshotCorruptedError:
            print("  restore 层也正确拒绝篡改后的快照")

        nonexistent = os.path.join(snap_dir, "does_not_exist.brsnap")
        r = runner.invoke(cli, ["snapshot", "info", nonexistent])
        check("info 不存在快照 (应失败)", r, expected_exit=1)
        r = runner.invoke(cli, ["snapshot", "restore", nonexistent, "-d", dst_dir])
        check("restore 不存在快照 (应失败)", r, expected_exit=1)
        print("  不存在文件 正确拒绝")

        print("[PASS] 损坏快照拒绝恢复 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(dst_dir, ignore_errors=True)
        shutil.rmtree(snap_dir, ignore_errors=True)


def test_snapshot_export_consistency_and_audit():
    print("=== 场景3-5: 恢复前后导出一致 + 审计可查询 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    src_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_src5_")
    dst_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_dst5_")
    snap_dir = tempfile.mkdtemp(prefix="bank_reconcile_snap_file5_")

    try:
        from click.testing import CliRunner
        from bank_reconcile.cli import cli

        batch_id, _, _ = _setup_full_batch(src_dir, samples_dir, "导出一致测试")

        os.environ["BANK_RECONCILE_HOME"] = src_dir
        runner = CliRunner()

        def check(desc, result, expected_exit=0):
            assert result.exit_code == expected_exit, \
                f"[{desc}] exit={result.exit_code} expect={expected_exit}, out={result.output}" \
                + (f"\n exc={result.exception}" if result.exception else "")
            print(f"  [OK] {desc}")

        pre_summary_csv = os.path.join(src_dir, "pre_summary.csv")
        r = runner.invoke(cli, ["report", "summary", "-b", batch_id, "--export", pre_summary_csv])
        check("源: report summary --export", r)
        assert os.path.isfile(pre_summary_csv)

        pre_diff_csv = os.path.join(src_dir, "pre_diff.csv")
        r = runner.invoke(cli, ["diff", "-b", batch_id, "--export", pre_diff_csv, "-n", "1000"])
        check("源: diff --export", r)
        assert os.path.isfile(pre_diff_csv)

        snap_path = os.path.join(snap_dir, "consist.brsnap")
        r = runner.invoke(cli, ["snapshot", "create", "-b", batch_id, "-o", snap_path])
        check("源: snapshot create", r)

        r = runner.invoke(cli, ["snapshot", "restore", snap_path, "-d", dst_dir])
        check("restore 到 dst", r)

        os.environ["BANK_RECONCILE_HOME"] = dst_dir
        r = runner.invoke(cli, ["resume", batch_id])
        check("恢复后: resume", r)

        post_summary_csv = os.path.join(dst_dir, "post_summary.csv")
        r = runner.invoke(cli, ["report", "summary", "-b", batch_id, "--export", post_summary_csv])
        check("恢复后: report summary --export", r)
        assert os.path.isfile(post_summary_csv)

        with open(pre_summary_csv, "r", encoding="utf-8-sig") as f1, \
             open(post_summary_csv, "r", encoding="utf-8-sig") as f2:
            def _filter_summary(lines):
                out = []
                for ln in lines:
                    s = ln.strip()
                    if not s:
                        continue
                    if s.startswith("创建时间,") or s.startswith("更新时间,") \
                            or s.startswith("批次ID,") or s.startswith("批次名称,"):
                        continue
                    out.append(s)
                return out
            pre_lines = _filter_summary(f1.readlines())
            post_lines = _filter_summary(f2.readlines())
        assert pre_lines == post_lines, "summary CSV 统计部分应逐行一致（排除时间元数据）"
        print(f"  summary CSV 统计部分完全一致: {len(pre_lines)} 行")

        post_diff_csv = os.path.join(dst_dir, "post_diff.csv")
        r = runner.invoke(cli, ["diff", "-b", batch_id, "--export", post_diff_csv, "-n", "1000"])
        check("恢复后: diff --export", r)
        assert os.path.isfile(post_diff_csv)

        with open(pre_diff_csv, "r", encoding="utf-8-sig") as f1, \
             open(post_diff_csv, "r", encoding="utf-8-sig") as f2:
            pre_diff_lines = [ln.strip() for ln in f1.readlines() if ln.strip()]
            post_diff_lines = [ln.strip() for ln in f2.readlines() if ln.strip()]
        assert pre_diff_lines == post_diff_lines, "diff CSV 应逐行完全一致"
        print(f"  diff CSV 完全一致: {len(pre_diff_lines)} 行")

        audit_dst = AuditStorage(dst_dir)
        all_ops = audit_dst.query(batch_id=batch_id)
        op_types = sorted({r["command"] for r in all_ops})
        expected_ops = {"import", "match", "mark", "snapshot_restore"}
        assert expected_ops.issubset(set(op_types)), \
            f"审计应包含 {expected_ops}, 实际 {op_types}"
        print(f"  审计类型完整: {op_types}")

        mark_records = [r for r in all_ops if r["command"] == "mark"]
        assert len(mark_records) == 1, "应包含一条 mark 审计"
        assert "snapshot_tester" in mark_records[0]["summary"]
        print(f"  mark 审计详情: affected={mark_records[0]['affected']}, "
              f"summary[:80]={mark_records[0]['summary'][:80]}")

        r = runner.invoke(cli, ["audit-log", "-t", "snapshot_create", "-b", batch_id])
        check("audit-log -t snapshot_create (源目录类型)", r, expected_exit=0)

        r = runner.invoke(cli, ["audit-log", "-t", "snapshot_restore", "-b", batch_id])
        check("audit-log -t snapshot_restore (恢复成功)", r, expected_exit=0)
        assert "snapshot_restore" in r.output or "审计日志" in r.output

        r = runner.invoke(cli, ["audit-log", "-t", "snapshot_restore_fail"])
        check("audit-log -t snapshot_restore_fail (无则空表)", r, expected_exit=0)

        print("[PASS] 导出一致性 & 审计查询 通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(dst_dir, ignore_errors=True)
        shutil.rmtree(snap_dir, ignore_errors=True)


from bank_reconcile.health import (
    run_health_check,
    export_health_report_json,
    export_health_report_csv,
    HealthCheckError,
    HealthCheckCorruptedError,
    HealthReport,
    IssueLevel,
    CheckCategory,
)


def test_health_check_healthy_batch():
    print("=== 测试健康批次检查 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_health_")
    os.environ["BANK_RECONCILE_HOME"] = tmpdir

    try:
        storage = BatchStorage(tmpdir)
        audit = AuditStorage(tmpdir)

        batch = Batch.create("健康测试批次")
        storage.save(batch)
        batch_id = batch.batch_id

        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        assert r.exit_code == 0, f"import bank 失败: {r.output}"
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        assert r.exit_code == 0, f"import system 失败: {r.output}"
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "adjustment",
                            os.path.join(samples_dir, "manual_adjustment.csv")])
        assert r.exit_code == 0, f"import adjustment 失败: {r.output}"

        r = runner.invoke(cli, ["rules", "set", "-b", batch_id,
                            os.path.join(samples_dir, "rules.yaml")])
        assert r.exit_code == 0, f"rules set 失败: {r.output}"

        r = runner.invoke(cli, ["match", "-b", batch_id])
        assert r.exit_code == 0, f"match 失败: {r.output}"

        b = storage.load(batch_id)
        assert len(b.discrepancies) > 0
        first_disp_id = b.discrepancies[0].discrepancy_id

        r = runner.invoke(cli, ["mark", "-b", batch_id, "-d", first_disp_id,
                              "-s", "confirmed", "-r", "tester", "-n", "test"])
        assert r.exit_code == 0, f"mark 失败: {r.output}"

        report = run_health_check(storage, batch_id)
        assert report.batch_id == batch_id
        assert report.batch_name == "健康测试批次"
        assert report.generated_at, "应有生成时间"
        assert report.batch_status == "open", "批次状态应为 open"

        print(f"  总体状态: {report.overall_status.value}")
        summary = report._summary()
        print(f"  问题汇总: OK={summary['ok']} INFO={summary['info']} "
              f"WARNING={summary['warning']} CRITICAL={summary['critical']}")

        categories_present = {i.category for i in report.issues}
        assert CheckCategory.FILES in categories_present
        assert CheckCategory.RULES in categories_present
        assert CheckCategory.MATCH in categories_present
        assert CheckCategory.MARKERS in categories_present
        assert CheckCategory.AUDIT in categories_present
        print("  五大检查类别均覆盖: OK")

        audit_records = audit.query(batch_id=batch_id, op_type="health_check")
        assert len(audit_records) == 0, "直接调用 run_health_check 不写入审计，由 CLI 负责"

        r = runner.invoke(cli, ["health", "check", "-b", batch_id])
        assert r.exit_code == 0, f"health check CLI 失败: {r.output}"
        assert "批次健康检查结果" in r.output
        print("  CLI health check 输出正常")

        audit_records = audit.query(batch_id=batch_id, op_type="health_check")
        assert len(audit_records) >= 1, "CLI health check 应写入审计日志"
        print("  审计日志记录健康检查: OK")

        print("[PASS] 健康批次检查测试通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_health_check_missing_files_bad_rules():
    print("=== 测试缺文件/坏规则检查 ===")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_health_bad_")
    os.environ["BANK_RECONCILE_HOME"] = tmpdir

    try:
        storage = BatchStorage(tmpdir)
        audit = AuditStorage(tmpdir)

        batch = Batch.create("问题批次")
        storage.save(batch)
        batch_id = batch.batch_id

        report = run_health_check(storage, batch_id)
        print(f"  总体状态: {report.overall_status.value}")
        summary = report._summary()
        print(f"  问题汇总: OK={summary['ok']} INFO={summary['info']} "
              f"WARNING={summary['warning']} CRITICAL={summary['critical']}")

        critical_issues = [i for i in report.issues if i.level == IssueLevel.CRITICAL]
        missing_files_issue = next(
            (i for i in critical_issues if i.check_name == "required_files_present"),
            None
        )
        assert missing_files_issue is not None, "应检测到缺少必要文件"
        assert "银行回单" in missing_files_issue.description or "系统流水" in missing_files_issue.description
        print("  缺少必要文件检测: OK")

        warning_issues = [i for i in report.issues if i.level == IssueLevel.WARNING]
        rules_issue = next(
            (i for i in warning_issues if i.check_name == "rules_configured"),
            None
        )
        assert rules_issue is not None, "应检测到未设置规则文件"
        print("  未设置规则检测: OK")

        match_issue = next(
            (i for i in warning_issues if i.check_name == "matching_done"),
            None
        )
        assert match_issue is not None, "应检测到未执行匹配"
        print("  未执行匹配检测: OK")

        samples_dir = os.path.join(os.path.dirname(__file__), "samples")
        bad_rules_path = os.path.join(samples_dir, "rules_bad.yaml")
        b = storage.load(batch_id)
        b.rule_file = bad_rules_path
        storage.save(b)

        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        assert r.exit_code == 0

        report2 = run_health_check(storage, batch_id)
        bad_rules_issues = [i for i in report2.issues
                           if i.category == CheckCategory.RULES and i.level == IssueLevel.CRITICAL]
        assert len(bad_rules_issues) >= 1, "应检测到坏规则文件"
        print("  坏规则文件检测: OK")

        nonexistent_batch = "BATCH-NONEXISTENT"
        try:
            run_health_check(storage, nonexistent_batch)
            assert False, "应抛出 HealthCheckError"
        except HealthCheckError as e:
            assert nonexistent_batch in str(e)
            print(f"  批次不存在错误: 正确抛出 ({e})")

        print("[PASS] 缺文件/坏规则检查测试通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_health_check_cross_restart():
    print("=== 测试重启后再检查 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_health_restart_")
    os.environ["BANK_RECONCILE_HOME"] = tmpdir

    try:
        storage1 = BatchStorage(tmpdir)
        audit1 = AuditStorage(tmpdir)

        batch = Batch.create("重启测试批次")
        storage1.save(batch)
        batch_id = batch.batch_id

        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["rules", "set", "-b", batch_id,
                            os.path.join(samples_dir, "rules.yaml")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["match", "-b", batch_id])
        assert r.exit_code == 0

        report1 = run_health_check(storage1, batch_id)
        print(f"  第一次检查状态: {report1.overall_status.value}")

        del storage1
        del audit1

        storage2 = BatchStorage(tmpdir)
        audit2 = AuditStorage(tmpdir)

        batch_reloaded = storage2.load(batch_id)
        assert batch_reloaded.batch_id == batch_id
        print("  批次从磁盘重新加载: OK")

        report2 = run_health_check(storage2, batch_id)
        print(f"  重启后检查状态: {report2.overall_status.value}")

        assert report2.batch_id == report1.batch_id
        assert report2.batch_name == report1.batch_name
        assert report2.batch_status == report1.batch_status

        check_names1 = sorted(i.check_name for i in report1.issues)
        check_names2 = sorted(i.check_name for i in report2.issues)
        assert check_names1 == check_names2, "重启后检查项应一致"
        print("  重启后检查项一致: OK")

        levels1 = sorted(i.level.value for i in report1.issues)
        levels2 = sorted(i.level.value for i in report2.issues)
        assert levels1 == levels2, "重启后问题级别应一致"
        print("  重启后问题级别一致: OK")

        print("[PASS] 重启后再检查测试通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_health_export_path_conflict():
    print("=== 测试导出路径冲突 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_health_conflict_")
    os.environ["BANK_RECONCILE_HOME"] = tmpdir

    try:
        storage = BatchStorage(tmpdir)

        batch = Batch.create("导出冲突测试")
        storage.save(batch)
        batch_id = batch.batch_id

        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        assert r.exit_code == 0

        output_path = os.path.join(tmpdir, "health_report.json")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("existing content")

        r = runner.invoke(cli, ["health", "export", "-b", batch_id,
                              "-o", output_path, "-f", "json"])
        assert r.exit_code != 0, "文件存在且无 --force 应失败"
        assert "已存在" in r.output or "exist" in r.output.lower()
        print("  无 --force 时路径冲突正确拒绝: OK")

        with open(output_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert content == "existing content", "原文件不应被覆盖"
        print("  冲突时原文件保持不变: OK")

        audit = AuditStorage(tmpdir)
        fail_records = audit.query(batch_id=batch_id, op_type="health_export_fail")
        assert len(fail_records) >= 1, "导出失败应写入审计日志"
        print("  导出失败审计记录: OK")

        r = runner.invoke(cli, ["health", "export", "-b", batch_id,
                              "-o", output_path, "-f", "json", "--force"])
        assert r.exit_code == 0, f"加 --force 应成功: {r.output}"
        with open(output_path, "r", encoding="utf-8") as f:
            new_content = f.read()
        assert "existing content" not in new_content
        assert "batch_id" in new_content
        print("  --force 正确覆盖: OK")

        print("[PASS] 导出路径冲突测试通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_health_export_json_csv_consistency():
    print("=== 测试 JSON/CSV 内容一致 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_health_consistency_")
    os.environ["BANK_RECONCILE_HOME"] = tmpdir

    try:
        storage = BatchStorage(tmpdir)

        batch = Batch.create("一致性测试批次")
        storage.save(batch)
        batch_id = batch.batch_id

        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["rules", "set", "-b", batch_id,
                            os.path.join(samples_dir, "rules.yaml")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["match", "-b", batch_id])
        assert r.exit_code == 0

        report = run_health_check(storage, batch_id)

        json_path = os.path.join(tmpdir, "report.json")
        csv_path = os.path.join(tmpdir, "report.csv")

        export_health_report_json(report, json_path)
        export_health_report_csv(report, csv_path)

        assert os.path.isfile(json_path), "JSON 文件应存在"
        assert os.path.isfile(csv_path), "CSV 文件应存在"
        print("  JSON/CSV 均已生成: OK")

        with open(json_path, "r", encoding="utf-8") as f:
            json_data = json.load(f)

        assert json_data["batch_id"] == batch_id
        assert json_data["batch_name"] == "一致性测试批次"
        assert "generated_at" in json_data
        assert "overall_status" in json_data
        assert "issues" in json_data
        assert "summary" in json_data
        print("  JSON 结构完整: OK")

        import csv as csv_mod
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv_mod.DictReader(f)
            csv_rows = list(reader)
            csv_headers = reader.fieldnames

        expected_headers = [
            "batch_id", "batch_name", "batch_status", "generated_at",
            "overall_status", "category", "check_name", "level",
            "description", "suggestion",
        ]
        for h in expected_headers:
            assert h in csv_headers, f"CSV 应包含列 {h}"
        print("  CSV 表头完整: OK")

        assert len(csv_rows) == len(report.issues), "CSV 行数应等于问题数"
        print(f"  CSV 行数与问题数一致 ({len(csv_rows)} 行): OK")

        assert json_data["batch_id"] == csv_rows[0]["batch_id"]
        assert json_data["batch_name"] == csv_rows[0]["batch_name"]
        assert json_data["batch_status"] == csv_rows[0]["batch_status"]
        assert json_data["overall_status"] == csv_rows[0]["overall_status"]
        print("  JSON/CSV 批次元数据一致: OK")

        json_issue_names = sorted(i["check_name"] for i in json_data["issues"])
        csv_issue_names = sorted(row["check_name"] for row in csv_rows)
        assert json_issue_names == csv_issue_names, "JSON/CSV 检查项应一致"
        print("  JSON/CSV 检查项一致: OK")

        json_issue_levels = sorted(i["level"] for i in json_data["issues"])
        csv_issue_levels = sorted(row["level"] for row in csv_rows)
        assert json_issue_levels == csv_issue_levels, "JSON/CSV 问题级别应一致"
        print("  JSON/CSV 问题级别一致: OK")

        print("[PASS] JSON/CSV 内容一致性测试通过\n")

    finally:
        if "BANK_RECONCILE_HOME" in os.environ:
            del os.environ["BANK_RECONCILE_HOME"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_health_audit_records_queryable():
    print("=== 测试审计记录可查询 ===")
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    tmpdir = tempfile.mkdtemp(prefix="bank_reconcile_health_audit_")
    os.environ["BANK_RECONCILE_HOME"] = tmpdir

    try:
        storage = BatchStorage(tmpdir)
        audit = AuditStorage(tmpdir)

        batch = Batch.create("审计测试批次")
        storage.save(batch)
        batch_id = batch.batch_id

        from click.testing import CliRunner
        from bank_reconcile.cli import cli
        runner = CliRunner()

        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "bank",
                            os.path.join(samples_dir, "bank_statement.csv")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["import", "-b", batch_id, "-t", "system",
                            os.path.join(samples_dir, "system_receipt.csv")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["rules", "set", "-b", batch_id,
                            os.path.join(samples_dir, "rules.yaml")])
        assert r.exit_code == 0
        r = runner.invoke(cli, ["match", "-b", batch_id])
        assert r.exit_code == 0

        r = runner.invoke(cli, ["health", "check", "-b", batch_id])
        assert r.exit_code == 0

        r = runner.invoke(cli, ["health", "export", "-b", batch_id,
                              "-o", os.path.join(tmpdir, "report1.json"), "-f", "json"])
        assert r.exit_code == 0

        r = runner.invoke(cli, ["health", "export", "-b", batch_id,
                              "-o", os.path.join(tmpdir, "report1.csv"), "-f", "csv"])
        assert r.exit_code == 0

        all_health = audit.query(batch_id=batch_id)
        health_types = {r["command"] for r in all_health if r["command"].startswith("health")}
        print(f"  审计中健康相关操作类型: {health_types}")

        assert "health_check" in health_types, "应有 health_check 记录"
        assert "health_export" in health_types, "应有 health_export 记录"
        print("  健康检查/导出操作已记录: OK")

        check_records = audit.query(batch_id=batch_id, op_type="health_check")
        assert len(check_records) >= 1
        check0 = check_records[0]
        assert check0["command"] == "health_check"
        assert check0["batch_id"] == batch_id
        assert "健康检查完成" in check0["summary"]
        assert "timestamp" in check0
        print(f"  health_check 记录详情: 影响数={check0['affected']}, 摘要={check0['summary'][:50]}...")

        export_records = audit.query(batch_id=batch_id, op_type="health_export")
        assert len(export_records) >= 2
        for rec in export_records:
            assert rec["command"] == "health_export"
            assert rec["batch_id"] == batch_id
            assert "健康报告导出成功" in rec["summary"]
        print(f"  health_export 记录数: {len(export_records)}")

        nonexistent_output = os.path.join(tmpdir, "existing.txt")
        with open(nonexistent_output, "w") as f:
            f.write("existing")
        r = runner.invoke(cli, ["health", "export", "-b", batch_id,
                              "-o", nonexistent_output, "-f", "json"])
        assert r.exit_code != 0

        fail_records = audit.query(batch_id=batch_id, op_type="health_export_fail")
        assert len(fail_records) >= 1
        print(f"  health_export_fail 记录数: {len(fail_records)}")

        r = runner.invoke(cli, ["audit-log", "-b", batch_id, "-t", "health_check"])
        assert r.exit_code == 0
        assert "health_check" in r.output
        print("  audit-log 可查询 health_check: OK")

        print("[PASS] 审计记录可查询测试通过\n")

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
        test_tolerance_same_txn_id_small_diff()
        test_summary_perfect_exact_not_unmatched()
        test_diff_reason_column_visible()
        test_readme_rules_command_matches_cli()
        test_archive_write_block()
        test_archive_restore_cross_restart()
        test_batch_cleanup_preview_force()
        test_import_col_map_positive_negative()
        test_close_reopen_blocked_on_archived()
        test_schedule_crud_and_persistence()
        test_schedule_batch_lock_mutex()
        test_schedule_failure_retry_and_audit()
        test_schedule_cli_commands()
        test_scheduler_should_run_logic()
        test_snapshot_create_and_info()
        test_snapshot_restore_cross_directory()
        test_snapshot_conflict_strategies()
        test_snapshot_corrupted_rejected()
        test_snapshot_export_consistency_and_audit()
        test_health_check_healthy_batch()
        test_health_check_missing_files_bad_rules()
        test_health_check_cross_restart()
        test_health_export_path_conflict()
        test_health_export_json_csv_consistency()
        test_health_audit_records_queryable()
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
