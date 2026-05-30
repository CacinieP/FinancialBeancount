# Beancount 多平台账单去重工具

> 基于 Beancount 复式记账规范的多平台账单去重工具，支持支付宝、微信、银行卡账单的去重与格式转换。

[![CI](https://img.shields.io/github/actions/workflow/status/CacinieP/FinancialBeancount/ci.yml?branch=main&style=flat-square)](https://github.com/CacinieP/FinancialBeancount/actions)
[![License](https://img.shields.io/github/license/CacinieP/FinancialBeancount?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square)](requirements.txt)

**[Beancount](https://github.com/beancount/beancount)** 是一个优秀的纯文本复式记账系统。本项目是一个独立的第三方工具，用于将中国主流支付平台（支付宝、微信支付）和银行账单转换为 Beancount 格式，并智能去重。

---

## Who This Is For / 适合谁

- 已经在用 Beancount 或准备迁移到纯文本复式记账的人
- 同时有支付宝、微信支付、银行卡账单，担心重复导入的人
- 想在本地完成账单清洗，不希望把财务数据上传到第三方服务的人
- 需要保留可审计中间结果，而不是只拿到一个黑箱导出文件的人

## Features / 功能特性

- **三级哈希指纹去重**：L1(精确) / L2(宽松) / L3(模糊) 匹配策略
- **多格式支持**：CSV、XLSX、PDF 自动转换
- **智能账户分类**：基于 Beancount 最佳实践的自动分类
- **平台优先级**：支付宝 > 微信 > 银行（信息完整度优先）
- **特殊场景识别**：内部转账、跨天凌晨交易、连续相同金额交易保护

## Quick Start / 快速开始

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

## Project Structure / 项目结构

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

## Usage / 使用示例

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

## Account Classification / 账户分类

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

## Supported Platforms / 支持平台

| Platform | Format | Status |
|----------|--------|--------|
| Alipay / 支付宝 | CSV | ✅ Fully supported |
| WeChat Pay / 微信支付 | CSV | ✅ Fully supported |
| Bank Cards / 银行卡 | CSV | ✅ Generic format |
| Excel (XLSX/XLS) | → CSV | ✅ Auto-conversion |
| PDF | → CSV | ⚠️ Requires `pdfplumber` |

## Testing / 测试

```bash
# Unit tests
python test_dedup.py

# E2E tests
python test_e2e_pipeline.py

# Test with sample data
cp test_data/*.csv input/
python example_usage.py
```

## Deduplication Strategy / 去重策略

| Level | Time Window | Description |
|-------|-------------|-------------|
| L1 | 2 minutes | Exact match: date + amount + normalized counterparty |
| L2 | 2 minutes | Loose match: date + amount (ignore name variations) |
| L3 | 1 day | Fuzzy match:跨天/手续费 scenarios (marked for review) |

## Privacy & Security / 隐私与安全

- **All processing is local** - No data is sent to external servers
- **input/ and output/ folders are excluded from git** - Your financial data never leaves your machine
- **Sample test data is anonymized** - No real personal information in the repository
- **Review-before-import workflow** - Fuzzy matches are reported for manual review instead of being silently discarded

## Contributing / 贡献指南

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## Acknowledgments / 致谢

- [Beancount](https://github.com/beancount/beancount) by Martin Blais - The excellent plain text double-entry accounting system that inspired this project
- [Beancount Documentation](https://beancount.github.io/docs/) - Comprehensive documentation and best practices
- All contributors to the Beancount ecosystem

## License / 许可证

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Disclaimer / 免责声明

This is an independent third-party tool and is NOT officially affiliated with, endorsed by, or connected to:
- Alipay (支付宝) or Ant Group (蚂蚁集团)
- WeChat Pay (微信支付) or Tencent (腾讯)
- Any financial institutions mentioned

This tool is provided for educational and personal finance management purposes. The authors are not responsible for any financial decisions made based on the output of this software.

## Links / 相关链接

- [Beancount Official Repository](https://github.com/beancount/beancount)
- [Beancount Documentation](https://beancount.github.io/docs/)
- [External Contributions & Tools](https://beancount.github.io/docs/external_contributions.html)
- [Plain Text Accounting](https://plaintextaccounting.org/)
