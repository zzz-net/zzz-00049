"""端到端测试脚本 - 验证所有核心功能."""
import os
import sys
import shutil
import tempfile
import io
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank_reconcile.models import Batch, FileType, DiscrepancyStatus, DiscrepancyType
from bank_reconcile.parser import parse_csv
from bank_reconcile.rules import load_rules, RuleValidationError, MatchRules
from bank_reconcile.matcher import run_matching
from bank_reconcile.storage import BatchStorage
from bank_reconcile.report import export_discrepancies_csv, generate_summary


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
    print(f"正常规则加载: 容差={rules.amount_tolerance}, 关键词={len(rules.manual_review_keywords)}个")

    try:
        load_rules(os.path.join(samples_dir, "rules_bad.yaml"))
        assert False, "应该抛出异常"
    except RuleValidationError as e:
        print(f"错误规则正确拒绝: {e}")

    default = MatchRules.default()
    assert default.amount_tolerance == 0.01

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

        r = runner.invoke(cli, ["rules", "-b", batch_id,
                            os.path.join(samples_dir, "rules.yaml")])
        check("rules", r)

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


def main():
    try:
        test_parser()
        test_rules()
        test_matching()
        test_storage_and_lifecycle()
        test_source_traceability()
        test_error_paths()
        test_cli_rich_output()
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
