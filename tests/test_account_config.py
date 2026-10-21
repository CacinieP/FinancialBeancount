"""
Tests for user account configuration (YAML ``accounts`` section) reaching the classifier.
"""

from datetime import datetime
from decimal import Decimal

from beancount_dedup.account_classifier import BeancountAccountClassifier
from beancount_dedup.cli import main
from beancount_dedup.config import AccountConfig, load_config
from beancount_dedup.exporters.beancount import BeancountExporter
from beancount_dedup.models import Platform, Transaction, TransactionType

CONFIG_YAML = """
accounts:
  platform_wallets:
    alipay: "Assets:Alipay"
  merchant_overrides:
    "星巴克": "Expenses:Food:Coffee"
    "ACME": "Income:Salary:ACME"
  bank_codes:
    "微众": "WEBANK"
"""


def _classify(classifier, counterparty, tx_type, platform="alipay"):
    return classifier.classify_transaction(counterparty, "", tx_type=tx_type, platform=platform)[
        "opposing_account"
    ]


class TestClassifierOverrides:
    def test_defaults_unchanged_without_config(self):
        classifier = BeancountAccountClassifier()
        assert classifier.determine_asset_account("alipay") == "Assets:Current:Digital:Alipay"
        assert _classify(classifier, "星巴克", "expense") == "Expenses:Food:Restaurant"

    def test_user_override_beats_builtin(self):
        accounts = AccountConfig(merchant_overrides={"星巴克": "Expenses:Food:Coffee"})
        classifier = BeancountAccountClassifier(accounts)
        assert _classify(classifier, "星巴克", "expense") == "Expenses:Food:Coffee"

    def test_override_is_case_insensitive(self):
        accounts = AccountConfig(merchant_overrides={"Costco": "Expenses:Shopping:Warehouse"})
        classifier = BeancountAccountClassifier(accounts)
        assert _classify(classifier, "COSTCO 开市客", "expense") == "Expenses:Shopping:Warehouse"

    def test_override_respects_direction(self):
        accounts = AccountConfig(merchant_overrides={"ACME": "Income:Salary:ACME"})
        classifier = BeancountAccountClassifier(accounts)
        assert _classify(classifier, "ACME 工资", "income") == "Income:Salary:ACME"
        assert _classify(classifier, "ACME 食堂", "expense") == "Expenses:Unknown"

    def test_wallet_and_bank_code_overrides(self):
        accounts = AccountConfig(
            platform_wallets={"alipay": "Assets:Alipay"}, bank_codes={"微众": "WEBANK"}
        )
        classifier = BeancountAccountClassifier(accounts)
        assert classifier.determine_asset_account("alipay") == "Assets:Alipay"
        assert classifier.determine_asset_account("unionpay") == "Assets:Current:Digital:UnionPay"
        assert (
            classifier.determine_asset_account("bank", "微众银行") == "Assets:Current:Bank:WEBANK"
        )

    def test_instances_do_not_share_state(self):
        BeancountAccountClassifier(AccountConfig(bank_codes={"微众": "WEBANK"}))
        fresh = BeancountAccountClassifier()
        assert "微众" not in fresh.bank_codes
        assert "微众" not in BeancountAccountClassifier.BANK_CODE_MAPPING


class TestLoadConfig:
    def test_accounts_section_loaded(self, tmp_path):
        path = tmp_path / "beancount-dedup.yaml"
        path.write_text(CONFIG_YAML, encoding="utf-8")
        accounts = load_config(str(path)).accounts
        assert accounts.platform_wallets["alipay"] == "Assets:Alipay"
        assert accounts.platform_wallets["wechat"] == "Assets:Current:Digital:WeChat"
        assert accounts.merchant_overrides["星巴克"] == "Expenses:Food:Coffee"
        assert accounts.bank_codes == {"微众": "WEBANK"}

    def test_commented_out_sections_are_empty(self, tmp_path):
        path = tmp_path / "beancount-dedup.yaml"
        path.write_text(
            'accounts:\n  merchant_overrides:\n    # "瑞幸": "Expenses:Food:Coffee"\n'
            "  bank_codes:\n",
            encoding="utf-8",
        )
        accounts = load_config(str(path)).accounts
        assert accounts.merchant_overrides == {}
        assert accounts.bank_codes == {}

    def test_example_config_loads(self):
        accounts = load_config("beancount-dedup.example.yaml").accounts
        assert accounts.merchant_overrides == {}
        assert accounts.platform_wallets["alipay"] == "Assets:Current:Digital:Alipay"


class TestExporterAndCli:
    def test_exporter_uses_config(self):
        accounts = AccountConfig(
            platform_wallets={"alipay": "Assets:Alipay"},
            merchant_overrides={"星巴克": "Expenses:Food:Coffee"},
        )
        tx = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0),
            amount=Decimal("-30.00"),
            tx_type=TransactionType.EXPENSE,
            counterparty="星巴克",
            description="拿铁",
        )
        output = BeancountExporter(accounts=accounts).export([tx])
        assert "Assets:Alipay" in output
        assert "Expenses:Food:Coffee" in output

    def test_cli_applies_config(self, tmp_path, monkeypatch):
        config = tmp_path / "custom.yaml"
        config.write_text(CONFIG_YAML, encoding="utf-8")
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "alipay.csv").write_text(
            "交易号,商家订单号,交易创建时间,付款时间,最近修改时间,交易来源地,类型,交易对方,商品名称,"
            "金额（元）,收/支,交易状态,服务费（元）,成功退款（元）,备注,资金状态,交易方式\n"
            "2024011522001234567890123456,,2024-01-15 14:00:00,2024-01-15 14:00:00,"
            "2024-01-15 14:00:00,APP,餐饮美食,星巴克,拿铁咖啡,30.00,支出,支付成功,0.00,,,余额,余额支付\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        main(["--config", str(config)])
        output = (tmp_path / "output" / "output.beancount").read_text(encoding="utf-8")
        assert "Assets:Alipay" in output
        assert "Expenses:Food:Coffee" in output
