# Beancount Multi-Platform Bill Deduplicator

> A Beancount-based double-entry accounting tool for deduplicating and converting bills from Alipay, WeChat Pay, and bank cards.

[![CI](https://img.shields.io/github/actions/workflow/status/CacinieP/FinancialBeancount/ci.yml?branch=main&style=flat-square)](https://github.com/CacinieP/FinancialBeancount/actions)
[![License](https://img.shields.io/github/license/CacinieP/FinancialBeancount?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square)](requirements.txt)

**[Beancount](https://github.com/beancount/beancount)** is an excellent plain-text double-entry accounting system. This project is an independent third-party tool for converting bills from China's major payment platforms (Alipay, WeChat Pay) and banks into Beancount format, with smart deduplication.

---

## Who This Is For

- People already using Beancount or migrating to plain-text double-entry accounting
- People with bills from Alipay, WeChat Pay, and bank cards who worry about duplicate imports
- People who want to clean bills locally without uploading financial data to third-party services
- People who need auditable intermediate results instead of a black-box export file

## Features

- **Three-level fingerprint deduplication**: L1 (exact) / L2 (loose) / L3 (fuzzy) matching strategies
- **Multi-format support**: CSV, XLSX, PDF auto-conversion
- **Smart account classification**: Auto-classification based on Beancount best practices
- **Platform priority**: Alipay > WeChat > Bank (by information completeness)
- **Special-case detection**: Internal transfers, cross-midnight early-morning transactions, consecutive same-amount transaction protection

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/CacinieP/FinancialBeancount.git
cd FinancialBeancount

# 2. Install dependencies (optional, for XLSX/PDF support)
pip install -r requirements.txt

# 3. Place your statement files in input/ folder
cp /path/to/your/statements/* input/

# 4. Run the pipeline
python example_usage.py

# 5. Check the output
# output/output.beancount        - Deduplicated transactions
# output/duplicate_report.beancount - Duplicate transaction report
```

## Project Structure

```
FinancialBeancount/
├── beancount_dedup/       # Main package
│   ├── models.py          # Data models
│   ├── fingerprinter.py   # Three-level fingerprint hashing
│   ├── deduplicator.py    # Deduplication engine
│   ├── account_classifier.py  # Intelligent account classification
│   ├── converters/        # Format converters (PDF/XLSX→CSV)
│   ├── parsers/           # Platform-specific parsers
│   └── exporters/         # Beancount format exporter
├── test_data/             # Sample anonymized data
├── input/                 # User statements (not in git)
├── output/                # Generated output (not in git)
└── example_usage.py       # Usage example
```

## Usage

### Python API

```python
from beancount_dedup import DeduplicationEngine
from beancount_dedup.parsers.alipay_parser import AlipayParser
from beancount_dedup.exporters.beancount import BeancountExporter

# Parse statements
alipay_txs = AlipayParser().parse("alipay_202401.csv").transactions

# Deduplicate
engine = DeduplicationEngine()
engine.add_transactions(alipay_txs)
unique_txs = engine.get_unique_transactions()

# Export to Beancount format
exporter = BeancountExporter()
exporter.export(unique_txs, output_path="output.beancount")
```

### Command Line

```bash
# Process all files in input/ folder
python example_usage.py
```

## Account Classification

This project uses Beancount best practices for account hierarchy:

```
Assets:Current:Digital:Alipay    # Alipay wallet
Assets:Current:Digital:WeChat    # WeChat wallet
Assets:Current:Bank:CMB          # China Merchants Bank

Expenses:Food:Restaurant         # Restaurants
Expenses:Food:Delivery           # Food delivery
Expenses:Transport:Private       # Taxi/Didi
Expenses:Shopping:Online         # Online shopping (JD/Taobao)
Expenses:Entertainment:Subscription  # Subscriptions (Netflix, etc.)
```

## Supported Platforms

| Platform | Format | Status |
|----------|--------|--------|
| Alipay | CSV | ✅ Fully supported |
| WeChat Pay | CSV | ✅ Fully supported |
| Bank Cards | CSV | ✅ Generic format |
| Excel (XLSX/XLS) | → CSV | ✅ Auto-conversion |
| PDF | → CSV | ⚠️ Requires `pdfplumber` |

## Testing

```bash
# Unit tests
python test_dedup.py

# E2E tests
python test_e2e_pipeline.py

# Test with sample data
cp test_data/*.csv input/
python example_usage.py
```

## Deduplication Strategy

| Level | Time Window | Description |
|-------|-------------|-------------|
| L1 | 2 minutes | Exact match: date + amount + normalized counterparty |
| L2 | 2 minutes | Loose match: date + amount (ignore name variations) |
| L3 | 1 day | Fuzzy match: cross-day / fee scenarios (marked for review) |

## Privacy & Security

- **All processing is local** — No data is sent to external servers
- **input/ and output/ folders are excluded from git** — Your financial data never leaves your machine
- **Sample test data is anonymized** — No real personal information in the repository
- **Review-before-import workflow** — Fuzzy matches are reported for manual review instead of being silently discarded

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## Acknowledgments

- [Beancount](https://github.com/beancount/beancount) by Martin Blais — The excellent plain text double-entry accounting system that inspired this project
- [Beancount Documentation](https://beancount.github.io/docs/) — Comprehensive documentation and best practices
- All contributors to the Beancount ecosystem

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

## Disclaimer

This is an independent third-party tool and is NOT officially affiliated with, endorsed by, or connected to:
- Alipay or Ant Group
- WeChat Pay or Tencent
- Any financial institutions mentioned

This tool is provided for educational and personal finance management purposes. The authors are not responsible for any financial decisions made based on the output of this software.

## Links

- [Beancount Official Repository](https://github.com/beancount/beancount)
- [Beancount Documentation](https://beancount.github.io/docs/)
- [External Contributions & Tools](https://beancount.github.io/docs/external_contributions.html)
- [Plain Text Accounting](https://plaintextaccounting.org/)

---

# Beancount 多平台账单去重工具

> 基于 Beancount 复式记账规范的多平台账单去重工具，支持支付宝、微信、银行卡账单的去重与格式转换。

[![CI](https://img.shields.io/github/actions/workflow/status/CacinieP/FinancialBeancount/ci.yml?branch=main&style=flat-square)](https://github.com/CacinieP/FinancialBeancount/actions)
[![License](https://img.shields.io/github/license/CacinieP/FinancialBeancount?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square)](requirements.txt)

**[Beancount](https://github.com/beancount/beancount)** 是一个优秀的纯文本复式记账系统。本项目是一个独立的第三方工具，用于将中国主流支付平台（支付宝、微信支付）和银行账单转换为 Beancount 格式，并智能去重。

---

## 适合谁

- 已经在用 Beancount 或准备迁移到纯文本复式记账的人
- 同时有支付宝、微信支付、银行卡账单，担心重复导入的人
- 想在本地完成账单清洗，不希望把财务数据上传到第三方服务的人
- 需要保留可审计中间结果，而不是只拿到一个黑箱导出文件的人

## 功能特性

- **三级哈希指纹去重**：L1(精确) / L2(宽松) / L3(模糊) 匹配策略
- **多格式支持**：CSV、XLSX、PDF 自动转换
- **智能账户分类**：基于 Beancount 最佳实践的自动分类
- **平台优先级**：支付宝 > 微信 > 银行（信息完整度优先）
- **特殊场景识别**：内部转账、跨天凌晨交易、连续相同金额交易保护

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/CacinieP/FinancialBeancount.git
cd FinancialBeancount

# 2. 安装依赖（可选，XLSX/PDF 支持需要）
pip install -r requirements.txt

# 3. 将账单文件放入 input/ 目录
cp /path/to/your/statements/* input/

# 4. 运行处理流程
python example_usage.py

# 5. 查看输出
# output/output.beancount        - 去重后的交易
# output/duplicate_report.beancount - 重复交易报告
```

## 项目结构

```
FinancialBeancount/
├── beancount_dedup/       # 主包
│   ├── models.py          # 数据模型
│   ├── fingerprinter.py   # 三级指纹哈希
│   ├── deduplicator.py    # 去重引擎
│   ├── account_classifier.py  # 智能账户分类
│   ├── converters/        # 格式转换器 (PDF/XLSX→CSV)
│   ├── parsers/           # 平台专用解析器
│   └── exporters/         # Beancount 格式导出器
├── test_data/             # 匿名化样本数据
├── input/                 # 用户账单（不在 git 中）
├── output/                # 生成输出（不在 git 中）
└── example_usage.py       # 使用示例
```

## 使用示例

### Python API

```python
from beancount_dedup import DeduplicationEngine
from beancount_dedup.parsers.alipay_parser import AlipayParser
from beancount_dedup.exporters.beancount import BeancountExporter

# 解析账单
alipay_txs = AlipayParser().parse("alipay_202401.csv").transactions

# 去重
engine = DeduplicationEngine()
engine.add_transactions(alipay_txs)
unique_txs = engine.get_unique_transactions()

# 导出为 Beancount 格式
exporter = BeancountExporter()
exporter.export(unique_txs, output_path="output.beancount")
```

### 命令行

```bash
# 处理 input/ 目录下的所有文件
python example_usage.py
```

## 账户分类

本项目使用 Beancount 最佳实践的账户层级：

```
Assets:Current:Digital:Alipay    # 支付宝钱包
Assets:Current:Digital:WeChat    # 微信钱包
Assets:Current:Bank:CMB          # 招商银行

Expenses:Food:Restaurant         # 餐厅
Expenses:Food:Delivery           # 外卖
Expenses:Transport:Private       # 出租车/滴滴
Expenses:Shopping:Online         # 网购（京东/淘宝）
Expenses:Entertainment:Subscription  # 订阅（Netflix 等）
```

## 支持平台

| 平台 | 格式 | 状态 |
|------|------|------|
| 支付宝 | CSV | ✅ 完全支持 |
| 微信支付 | CSV | ✅ 完全支持 |
| 银行卡 | CSV | ✅ 通用格式 |
| Excel (XLSX/XLS) | → CSV | ✅ 自动转换 |
| PDF | → CSV | ⚠️ 需安装 `pdfplumber` |

## 测试

```bash
# 单元测试
python test_dedup.py

# 端到端测试
python test_e2e_pipeline.py

# 使用样本数据测试
cp test_data/*.csv input/
python example_usage.py
```

## 去重策略

| 级别 | 时间窗口 | 说明 |
|------|----------|------|
| L1 | 2 分钟 | 精确匹配：日期 + 金额 + 归一化交易对方 |
| L2 | 2 分钟 | 宽松匹配：日期 + 金额（忽略名称变体） |
| L3 | 1 天 | 模糊匹配：跨天/手续费场景（标记待人工审核） |

## 隐私与安全

- **所有处理均在本地** — 不向外部服务器发送任何数据
- **input/ 和 output/ 目录已排除在 git 之外** — 财务数据不会离开你的电脑
- **样本测试数据已匿名化** — 仓库中不包含真实个人信息
- **导入前审核工作流** — 模糊匹配会报告待人工审核，而非静默丢弃

## 贡献指南

欢迎贡献！请随时提交 Pull Request。

1. Fork 本仓库
2. 创建功能分支（`git checkout -b feature/AmazingFeature`）
3. 提交变更（`git commit -m 'Add some AmazingFeature'`）
4. 推送分支（`git push origin feature/AmazingFeature`）
5. 发起 Pull Request

## 致谢

- [Beancount](https://github.com/beancount/beancount) by Martin Blais — 启发本项目的优秀纯文本复式记账系统
- [Beancount 文档](https://beancount.github.io/docs/) — 全面的文档与最佳实践
- Beancount 生态系统的所有贡献者

## 许可证

本项目采用 MIT 许可证 — 详见 [LICENSE](LICENSE) 文件。

## 免责声明

这是一个独立的第三方工具，与以下机构无官方关联、背书或联系：
- 支付宝或蚂蚁集团
- 微信支付或腾讯
- 本工具中提及的任何金融机构

本工具仅供教育和个人财务管理目的。作者不对基于本软件输出做出的任何财务决策负责。

## 相关链接

- [Beancount 官方仓库](https://github.com/beancount/beancount)
- [Beancount 文档](https://beancount.github.io/docs/)
- [外部贡献与工具](https://beancount.github.io/docs/external_contributions.html)
- [纯文本记账](https://plaintextaccounting.org/)
