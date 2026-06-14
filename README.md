# 银行回单对账 CLI (bank-reconcile)

一个用于银行回单与系统收款流水对账的命令行工具。支持导入三类文件（银行回单、系统流水、手工调整），按配置规则自动识别差异，支持人工复核标记，并可导出可追溯来源的差异报告。

## 功能特性

- 三类文件导入：银行回单、系统收款流水、手工调整表
- 四类差异识别：缺失（银行/系统）、金额不符、重复流水、待人工确认
- 规则驱动：金额容差、日期窗口、人工复核关键词等可配置
- 状态持久化：所有批次数据存本地 JSON，重启可 resume
- 完整审计：差异编号、复核人、备注、回滚记录、导出历史全程保留
- 操作审计日志：import/match/mark/rollback 自动记录到 SQLite，可查询导出
- 报告可追溯：每条差异关联到源文件、行号和原始数据

## 项目结构

```
bank_reconcile/
├── bank_reconcile/
│   ├── __init__.py
│   ├── cli.py         # CLI 入口与子命令
│   ├── models.py      # 数据模型
│   ├── parser.py      # 文件解析模块
│   ├── rules.py       # 规则引擎
│   ├── matcher.py     # 匹配与差异识别
│   ├── storage.py     # 批次状态持久化
│   ├── report.py      # 报告导出
│   ├── audit.py       # 操作审计（SQLite）
│   └── config.py      # 全局配置加载
├── samples/           # 样例数据
│   ├── bank_statement.csv
│   ├── system_receipt.csv
│   ├── manual_adjustment.csv
│   ├── rules.yaml
│   └── rules_bad.yaml
├── pyproject.toml
├── requirements.txt
└── README.md
```

## 安装

```bash
pip install -r requirements.txt
# 或开发者模式
pip install -e .
```

## 快速开始

### 1. 创建批次

```bash
bank-reconcile create "2024年1月对账"
```

### 2. 查看批次列表

```bash
bank-reconcile list
```

### 3. 导入三类文件

```bash
# 导入银行回单
bank-reconcile import --batch-id BATCH-XXXXXXXX --type bank samples/bank_statement.csv

# 导入系统流水
bank-reconcile import --batch-id BATCH-XXXXXXXX --type system samples/system_receipt.csv

# 导入手工调整
bank-reconcile import --batch-id BATCH-XXXXXXXX --type adjustment samples/manual_adjustment.csv
```

### 4. 设置对账规则

```bash
bank-reconcile rules set --batch-id BATCH-XXXXXXXX samples/rules.yaml
```
也可以先校验规则文件再设置：

```bash
bank-reconcile rules validate samples/rules.yaml
```


### 5. 执行匹配生成差异清单

```bash
bank-reconcile match --batch-id BATCH-XXXXXXXX
```

### 6. 查看差异

```bash
bank-reconcile discrepancies --batch-id BATCH-XXXXXXXX
# 按状态过滤
bank-reconcile discrepancies --batch-id BATCH-XXXXXXXX --status open
# 按类型过滤
bank-reconcile discrepancies --batch-id BATCH-XXXXXXXX --type amount_mismatch
```

### 7. 标记差异（确认/忽略）

```bash
# 确认差异
bank-reconcile mark --batch-id BATCH-XXXXXXXX -d DISP-XXXXXXXXXX -s confirmed -r zhangsan -n "已核实"

# 忽略差异
bank-reconcile mark --batch-id BATCH-XXXXXXXX -d DISP-XXXXXXXXXX -s ignored -r zhangsan -n "无需处理"
```

### 8. 回滚标记

```bash
bank-reconcile rollback --batch-id BATCH-XXXXXXXX -d DISP-XXXXXXXXXX
```

### 9. 恢复批次（resume）

```bash
bank-reconcile resume BATCH-XXXXXXXX
```

### 10. 导出报告

```bash
# 导出全部差异
bank-reconcile export --batch-id BATCH-XXXXXXXX -o report/discrepancies.csv

# 导出指定状态
bank-reconcile export --batch-id BATCH-XXXXXXXX -o report/open_items.csv -s open

# 同时导出摘要
bank-reconcile export --batch-id BATCH-XXXXXXXX -o report/full.csv --with-summary
```

### 11. 查看操作审计日志

```bash
# 查看全部审计记录（终端表格）
bank-reconcile audit-log

# 按操作类型过滤
bank-reconcile audit-log --type import

# 按批次过滤
bank-reconcile audit-log --batch BATCH-XXXXXXXX

# 按日期范围过滤
bank-reconcile audit-log --from 2024-01-01 --to 2024-01-31

# 导出为 CSV
bank-reconcile audit-log -o report/audit_log.csv -f csv

# 导出为 JSON
bank-reconcile audit-log -o report/audit_log.json -f json
```

## 差异类型说明

| 类型 | 说明 |
|------|------|
| `missing_in_bank` | 系统有记录但银行无此流水 |
| `missing_in_system` | 银行有记录但系统无此流水 |
| `amount_mismatch` | 同一笔流水两边金额不符 |
| `duplicate` | 同一来源内存在重复流水号 |
| `needs_manual_review` | 摘要包含人工复核关键词，需人工确认 |

## 失败路径覆盖

- **非法金额**：解析时跳过并记录错误，不影响其他数据
- **缺少交易号**：解析时标记为错误行
- **重复流水号**：解析时检测重复，同时匹配阶段也会识别
- **错误规则文件**：加载时校验，提示具体字段错误，不影响旧批次
- **旧批次保留**：所有批次独立存储，可随时 resume 查看

## 配置说明

### 全局配置 (config.yaml)

存储目录下的 `config.yaml` 文件，用于全局配置。当前支持：

```yaml
audit_retention_days: 90         # 审计日志保留天数，默认 90 天，超期自动清理
```

### 规则文件 (YAML)

```yaml
amount_tolerance: 0.01           # 金额容差（元）
date_window_days: 3              # 日期窗口（天）
require_exact_txn_id: true       # 是否要求精确交易号匹配
case_sensitive_txn_id: false     # 交易号是否区分大小写
manual_review_keywords:          # 人工复核关键词
  - 调账
  - 手续费
  - 利息
  - 退款
  - 红冲
  - 待确认
ignore_duplicate_if_amount_differs: false  # 金额不同时是否忽略重复
consider_adjustments: true       # 是否考虑手工调整
```

### CSV 列名自动识别

支持的列名（中英文均可）：

- 交易号：交易流水号 / 流水号 / 订单号 / 收款流水号 / transaction_id
- 金额：金额 / 交易金额 / 收款金额 / 调整金额 / amount
- 日期：交易日期 / 收款日期 / 调整日期 / date
- 对方：对方户名 / 付款方 / 客户 / counterparty
- 摘要：摘要 / 备注 / 用途 / 调整原因 / description

### 存储位置

默认存储目录为当前工作目录下的 `.bank_reconcile/batches/`，每个批次一个 JSON 文件。
可通过环境变量 `BANK_RECONCILE_HOME` 指定自定义目录。

## License

MIT
