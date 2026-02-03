#!/usr/bin/env python3
"""
Beancount 多平台账单去重工具 - 使用示例

默认从 input/ 文件夹读取账单文件，输出到 output/ 文件夹
"""

import sys
import os
from pathlib import Path

# 添加模块路径
sys.path.insert(0, str(Path(__file__).parent))

from beancount_dedup import DeduplicationEngine, TransactionFingerprinter, AutoConverter
from beancount_dedup.models import Transaction, Platform
from beancount_dedup.parsers.alipay_parser import AlipayParser, AlipayParserV2
from beancount_dedup.parsers.wechat_parser import WechatParser
from beancount_dedup.parsers.bank_parser import BankParser, CMBParser, ICBCParser
from beancount_dedup.parsers.base import AutoParser
from beancount_dedup.exporters.beancount import BeancountExporter
from datetime import datetime, timedelta
from decimal import Decimal


# 默认文件夹配置
INPUT_DIR = Path(__file__).parent / "input"
OUTPUT_DIR = Path(__file__).parent / "output"


def ensure_output_dir():
    """确保输出目录存在"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def process_input_folder():
    """
    自动处理 input/ 文件夹中的所有账单文件

    支持的文件格式：CSV, XLSX, XLS, PDF
    支持的平台：支付宝, 微信支付, 银行卡

    处理流程：
    1. 扫描 input/ 文件夹
    2. 对非 CSV 文件进行格式转换
    3. 解析所有 CSV 文件
    4. 去重并导出结果
    """
    ensure_output_dir()

    # 创建转换后 CSV 的存储目录
    converted_dir = OUTPUT_DIR / "converted"
    converted_dir.mkdir(parents=True, exist_ok=True)

    if not INPUT_DIR.exists():
        print(f"错误: input 文件夹不存在")
        print(f"请创建 {INPUT_DIR} 文件夹并放入账单文件")
        return

    # 查找所有支持的文件
    supported_extensions = ['.csv', '.xlsx', '.xls', '.pdf']
    input_files = []

    for ext in supported_extensions:
        input_files.extend(INPUT_DIR.glob(f"*{ext}"))

    if not input_files:
        print(f"错误: input 文件夹中没有找到账单文件")
        print(f"支持的文件格式: {', '.join(supported_extensions)}")
        print(f"请将账单文件放入 {INPUT_DIR} 文件夹")
        return

    # 分类文件
    csv_files = [f for f in input_files if f.suffix.lower() == '.csv']
    convert_files = [f for f in input_files if f.suffix.lower() != '.csv']

    print("=" * 60)
    print(f"文件扫描结果")
    print("=" * 60)
    print(f"  CSV 文件: {len(csv_files)} 个")
    print(f"  需要转换: {len(convert_files)} 个")
    if convert_files:
        print(f"    - XLSX/XLS: {len([f for f in convert_files if f.suffix.lower() in ['.xlsx', '.xls']])} 个")
        print(f"    - PDF: {len([f for f in convert_files if f.suffix.lower() == '.pdf'])} 个")
    print()

    # 步骤1: 转换非 CSV 文件
    conversion_results = []  # Initialize before the if block
    if convert_files:
        print("=" * 60)
        print("步骤 1/3: 格式转换")
        print("=" * 60)

        converter = AutoConverter()
        conversion_results = []

        for filepath in convert_files:
            ext = filepath.suffix.lower()
            print(f"\n[{ext.upper()}] {filepath.name}")

            # 指定输出路径到 output/converted/
            output_csv = str(converted_dir / f"{filepath.stem}.csv")

            conv_result = converter.convert(str(filepath), output_path=output_csv)
            conversion_results.append((filepath, conv_result))

            if conv_result.success:
                print(f"  [OK] 转换成功: {conv_result.rows_converted} 行 -> {Path(output_csv).name}")
                if conv_result.metadata.get('method'):
                    print(f"    方法: {conv_result.metadata['method']}")
                if conv_result.warnings:
                    for warn in conv_result.warnings:
                        print(f"    警告: {warn}")
            else:
                print(f"  [FAIL] 转换失败")
                for err in conv_result.errors:
                    print(f"    - {err}")

        # 统计转换结果
        success_count = sum(1 for _, r in conversion_results if r.success)
        print(f"\n转换完成: {success_count}/{len(convert_files)} 成功")

    # 步骤2: 解析所有 CSV 文件
    print("\n" + "=" * 60)
    print("步骤 2/3: 解析账单")
    print("=" * 60)

    auto_parser = AutoParser()
    auto_parser.register(AlipayParser())
    auto_parser.register(AlipayParserV2())
    auto_parser.register(WechatParser())
    auto_parser.register(CMBParser())
    auto_parser.register(ICBCParser())
    auto_parser.register(BankParser())

    all_transactions = []
    parse_results = []

    # 原始 CSV 文件
    for filepath in csv_files:
        print(f"\n[CSV] {filepath.name}")
        result = auto_parser.parse(str(filepath))
        parse_results.append((filepath.name, result))
        print(f"  {result}")

        all_transactions.extend(result.transactions)

    # 转换后的 CSV 文件
    if convert_files:
        for filepath, conv_result in conversion_results:
            if conv_result.success:
                csv_path = conv_result.output_path
                csv_name = Path(csv_path).name
                print(f"\n[CSV] {csv_name} (来自 {filepath.name})")

                result = auto_parser.parse(csv_path)
                parse_results.append((csv_name, result))
                print(f"  {result}")

                all_transactions.extend(result.transactions)

    if not all_transactions:
        print("\n没有成功解析任何交易")
        return

    # 步骤3: 去重并导出
    print("\n" + "=" * 60)
    print("步骤 3/3: 去重处理")
    print("=" * 60)
    print(f"\n共 {len(all_transactions)} 笔交易待处理...")

    engine = DeduplicationEngine()
    engine.add_transactions(all_transactions)

    # 生成报告
    report = engine.generate_report()
    print(f"\n{report}")

    # 导出结果
    unique_txs = engine.get_unique_transactions()
    exporter = BeancountExporter()

    output_file = str(OUTPUT_DIR / "output.beancount")
    exporter.export(unique_txs, output_path=output_file)
    print(f"\n去重后的交易已导出到: {output_file}")

    # 导出重复报告
    report_file = str(OUTPUT_DIR / "duplicate_report.beancount")
    exporter.export_duplicate_report(engine.processed, output_path=report_file)
    print(f"重复交易报告已导出到: {report_file}")

    # 导出转换后的 CSV 文件列表
    if conversion_results:
        print(f"\n转换后的 CSV 文件保存在: {converted_dir}")


def demo_basic_usage():
    """基础使用示例：手动创建交易并去重"""
    print("=" * 60)
    print("示例 1: 基础去重演示")
    print("=" * 60)

    # 创建去重引擎
    engine = DeduplicationEngine()

    # 模拟同一笔交易的两个来源
    # 场景：2024-01-15 在星巴克消费100元，使用支付宝绑定的建行卡支付

    # 支付宝记录
    tx_alipay = Transaction(
        platform=Platform.ALIPAY,
        datetime=datetime(2024, 1, 15, 14, 0, 0),
        amount=Decimal("-100.00"),
        counterparty="星巴克",
        description="拿铁咖啡",
        payment_method="建设银行(尾号8888)",
    )

    # 银行卡记录（延迟2分30秒记账）
    tx_bank = Transaction(
        platform=Platform.BANK,
        datetime=datetime(2024, 1, 15, 14, 2, 30),
        amount=Decimal("-100.00"),
        counterparty="支付宝-星巴克",
        description="快捷支付",
        payment_method="储蓄卡(8888)",
    )

    # 另一笔独立交易（微信消费）
    tx_wechat = Transaction(
        platform=Platform.WECHAT,
        datetime=datetime(2024, 1, 15, 18, 30, 0),
        amount=Decimal("-50.00"),
        counterparty="麦当劳",
        description="午餐",
    )

    # 添加交易到引擎
    result1 = engine.add_transaction(tx_alipay)
    result2 = engine.add_transaction(tx_bank)
    result3 = engine.add_transaction(tx_wechat)

    # 打印结果
    print(f"\n交易 1 (支付宝):")
    print(f"  状态: {result1.status.value}")
    print(f"  L1指纹: {result1.fingerprints['L1'][:16]}...")

    print(f"\n交易 2 (银行):")
    print(f"  状态: {result2.status.value}")
    print(f"  匹配级别: {result2.match_level}")
    print(f"  是否保留: {result2.kept}")
    if result2.duplicate_of:
        print(f"  重复于: {result2.duplicate_of.id} (支付宝)")

    print(f"\n交易 3 (微信):")
    print(f"  状态: {result3.status.value}")

    # 生成报告
    report = engine.generate_report()
    print(f"\n{report}")

    return engine


def demo_l3_fuzzy_match():
    """演示 L3 模糊匹配（跨天/手续费场景）"""
    print("\n" + "=" * 60)
    print("示例 2: L3 模糊匹配（跨天场景）")
    print("=" * 60)

    engine = DeduplicationEngine()

    # 场景：凌晨交易，支付宝和银行卡跨天记账
    tx_alipay = Transaction(
        platform=Platform.ALIPAY,
        datetime=datetime(2024, 1, 15, 23, 58, 0),  # 1月15日 23:58
        amount=Decimal("-200.00"),
        counterparty="京东",
        description="购物",
    )

    tx_bank = Transaction(
        platform=Platform.BANK,
        datetime=datetime(2024, 1, 16, 0, 1, 0),  # 1月16日 00:01（跨天）
        amount=Decimal("-200.00"),
        counterparty="支付宝-京东",
        description="快捷支付",
    )

    result1 = engine.add_transaction(tx_alipay)
    result2 = engine.add_transaction(tx_bank)

    print(f"\n支付宝: 2024-01-15 23:58:00")
    print(f"银行:   2024-01-16 00:01:00 (跨天)")
    print(f"\n支付宝交易状态: {result1.status.value}")
    print(f"银行交易状态:   {result2.status.value}")
    if result2.status.value == "review":
        print(f"复核原因: {result2.review_reason}")

    return engine


def demo_continuous_same_amount():
    """演示连续相同金额交易保护"""
    print("\n" + "=" * 60)
    print("示例 3: 连续相同金额交易（地铁刷卡）")
    print("=" * 60)

    engine = DeduplicationEngine()

    # 场景：10分钟内两次地铁乘车，每次3元
    base_time = datetime(2024, 1, 15, 8, 0, 0)

    transactions = [
        # 第一笔：支付宝+银行（正常重复对）
        Transaction(
            platform=Platform.ALIPAY,
            datetime=base_time,
            amount=Decimal("-3.00"),
            counterparty="地铁乘车码",
            description="地铁消费",
        ),
        Transaction(
            platform=Platform.BANK,
            datetime=base_time + timedelta(seconds=30),
            amount=Decimal("-3.00"),
            counterparty="财付通-地铁",
            description="快捷支付",
        ),
        # 第二笔（独立交易，8:05再次乘车）
        Transaction(
            platform=Platform.ALIPAY,
            datetime=base_time + timedelta(minutes=5),
            amount=Decimal("-3.00"),
            counterparty="地铁乘车码",
            description="地铁消费",
        ),
        Transaction(
            platform=Platform.BANK,
            datetime=base_time + timedelta(minutes=5, seconds=30),
            amount=Decimal("-3.00"),
            counterparty="财付通-地铁",
            description="快捷支付",
        ),
    ]

    results = engine.add_transactions(transactions)

    print("\n共4笔交易（2笔支付宝 + 2笔银行，对应2次独立乘车）")
    for i, (tx, result) in enumerate(zip(transactions, results), 1):
        print(f"\n交易 {i}: {tx.platform.value} {tx.datetime.strftime('%H:%M:%S')}")
        print(f"  状态: {result.status.value}")
        print(f"  匹配级别: {result.match_level or 'N/A'}")
        if result.duplicate_of:
            print(f"  重复于交易: {result.duplicate_of.id}")

    report = engine.generate_report()
    print(f"\n{report}")

    return engine


def demo_internal_transfer():
    """演示内部转账识别"""
    print("\n" + "=" * 60)
    print("示例 4: 内部转账识别")
    print("=" * 60)

    engine = DeduplicationEngine()

    # 场景：银行卡充值到支付宝余额
    tx_bank = Transaction(
        platform=Platform.BANK,
        datetime=datetime(2024, 1, 15, 10, 0, 0),
        amount=Decimal("-1000.00"),
        counterparty="支付宝充值",
        description="充值",
    )

    tx_alipay = Transaction(
        platform=Platform.ALIPAY,
        datetime=datetime(2024, 1, 15, 10, 0, 30),
        amount=Decimal("1000.00"),
        counterparty="建设银行(1234)",
        description="充值",
    )

    result1 = engine.add_transaction(tx_bank)
    result2 = engine.add_transaction(tx_alipay)

    print("\n银行卡: -1000 支付宝充值")
    print("支付宝: +1000 建设银行转入")
    print(f"\n银行交易状态: {result1.status.value}")
    print(f"支付宝交易状态: {result2.status.value}")
    if result1.status.value == "internal_transfer":
        print("识别为内部转账，非重复消费")

    return engine


def demo_export_beancount():
    """演示导出 Beancount 格式"""
    print("\n" + "=" * 60)
    print("示例 5: 导出 Beancount 格式")
    print("=" * 60)

    # 使用示例1的数据
    engine = demo_basic_usage()

    # 获取唯一交易
    unique_txs = engine.get_unique_transactions()

    # 创建导出器
    exporter = BeancountExporter()

    # 导出为 Beancount 格式
    output = exporter.export(
        unique_txs,
        include_meta=True,
        option_entries=[
            'option "title" "我的账本"',
            'option "operating_currency" "CNY"',
        ]
    )

    print("\n生成的 Beancount 内容：")
    print("-" * 60)
    print(output)
    print("-" * 60)

    # 同时导出重复报告
    all_txs = engine.processed
    dup_report = exporter.export_duplicate_report(all_txs)
    print("\n重复交易报告（注释格式）：")
    print(dup_report)

    return output


def parse_real_files(alipay_file=None, wechat_file=None, bank_file=None, output_file=None):
    """解析真实账单文件"""
    ensure_output_dir()

    print("=" * 60)
    print("真实账单文件解析")
    print("=" * 60)

    # 创建自动解析器
    auto_parser = AutoParser()
    auto_parser.register(AlipayParser())
    auto_parser.register(AlipayParserV2())
    auto_parser.register(WechatParser())
    auto_parser.register(CMBParser())
    auto_parser.register(ICBCParser())
    auto_parser.register(BankParser())

    # 创建转换器
    converter = AutoConverter()

    all_transactions = []

    # 解析各平台文件
    files = [
        (alipay_file, "支付宝"),
        (wechat_file, "微信"),
        (bank_file, "银行卡"),
    ]

    for filepath, name in files:
        if not filepath:
            continue

        print(f"\n解析 {name} 账单: {filepath}")

        # 如果不是 CSV 文件，先转换
        csv_path = filepath
        if not filepath.endswith('.csv'):
            conv_result = converter.convert(filepath)
            if conv_result.success:
                print(f"  格式转换成功: {conv_result.rows_converted} 行")
                csv_path = conv_result.output_path
            else:
                print(f"  格式转换失败: {conv_result.errors}")
                continue

        result = auto_parser.parse(csv_path)

        print(f"  总行数: {result.total_rows}")
        print(f"  成功解析: {result.parsed_rows}")
        if result.errors:
            print(f"  错误数: {len(result.errors)}")
            for err in result.errors[:3]:  # 只显示前3个错误
                print(f"    - {err}")

        all_transactions.extend(result.transactions)

    if not all_transactions:
        print("\n没有成功解析任何交易")
        return

    # 去重
    print(f"\n开始去重处理（共 {len(all_transactions)} 笔交易）...")
    engine = DeduplicationEngine()
    engine.add_transactions(all_transactions)

    # 生成报告
    report = engine.generate_report()
    print(f"\n{report}")

    # 导出结果
    unique_txs = engine.get_unique_transactions()
    exporter = BeancountExporter()

    if output_file is None:
        output_file = str(OUTPUT_DIR / "output.beancount")
    elif not os.path.isabs(output_file):
        output_file = str(OUTPUT_DIR / output_file)

    exporter.export(unique_txs, output_path=output_file)
    print(f"\n结果已导出到: {output_file}")

    # 导出重复报告
    report_file = output_file.replace(".beancount", "_report.beancount")
    exporter.export_duplicate_report(engine.processed, output_path=report_file)
    print(f"重复报告已导出到: {report_file}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Beancount 账单去重工具")
    parser.add_argument("--alipay", help="支付宝账单文件路径")
    parser.add_argument("--wechat", help="微信账单文件路径")
    parser.add_argument("--bank", help="银行卡账单文件路径")
    parser.add_argument("-o", "--output", help="输出文件路径")
    parser.add_argument("--demo", action="store_true", help="运行演示示例")

    args = parser.parse_args()

    if args.alipay or args.wechat or args.bank:
        # 解析指定文件
        parse_real_files(args.alipay, args.wechat, args.bank, args.output)
    elif args.demo:
        # 运行演示
        print("运行演示示例（使用模拟数据）...")
        print("使用 --help 查看命令行参数\n")

        demo_basic_usage()
        demo_l3_fuzzy_match()
        demo_continuous_same_amount()
        demo_internal_transfer()
        demo_export_beancount()
    else:
        # 默认处理 input 文件夹
        print("自动处理 input/ 文件夹中的账单文件")
        print("使用 --help 查看更多选项\n")

        process_input_folder()
