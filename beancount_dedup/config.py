"""
Configuration system for Beancount Dedup Tool.

Loads settings from a YAML config file, falling back to sensible defaults.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default config file locations (searched in order)
_DEFAULT_CONFIG_PATHS = [
    Path("beancount-dedup.yaml"),
    Path("beancount-dedup.yml"),
    Path(".beancount-dedup.yaml"),
    Path.home() / ".beancount-dedup" / "config.yaml",
]


@dataclass
class DedupConfig:
    """Deduplication thresholds and parameters."""

    l1_time_window_seconds: int = 120
    l2_time_window_seconds: int = 120
    l3_time_window_seconds: int = 86400  # 1 day
    l3_amount_tolerance_cents: int = 100  # 1 yuan


@dataclass
class AccountConfig:
    """Account classification overrides."""

    # Platform wallet accounts
    platform_wallets: dict[str, str] = field(
        default_factory=lambda: {
            "alipay": "Assets:Current:Digital:Alipay",
            "wechat": "Assets:Current:Digital:WeChat",
            "bank": "Assets:Current:Bank:Checking",
        }
    )

    # User-defined merchant → account overrides (added on top of built-in ones)
    merchant_overrides: dict[str, str] = field(default_factory=dict)

    # Bank name → code mapping
    bank_codes: dict[str, str] = field(default_factory=dict)


@dataclass
class PlatformConfig:
    """Platform-specific settings."""

    priority: list[str] = field(default_factory=lambda: ["alipay", "wechat", "bank"])

    # Transfer keywords per platform (extend built-in lists)
    transfer_keywords: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class AppConfig:
    """Root configuration object."""

    dedup: DedupConfig = field(default_factory=DedupConfig)
    accounts: AccountConfig = field(default_factory=AccountConfig)
    platforms: PlatformConfig = field(default_factory=PlatformConfig)

    # Paths
    input_dir: str = "input"
    output_dir: str = "output"

    # Export settings
    include_metadata: bool = True
    operating_currency: str = "CNY"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning an empty dict on failure."""
    try:
        import yaml
    except ImportError:
        logger.warning(
            "PyYAML not installed — install it with `pip install pyyaml` "
            "to enable config file support. Using defaults."
        )
        return {}

    if not path.exists():
        logger.debug("Config file not found: %s", path)
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read config file %s: %s", path, exc)
        return {}


def _dict_to_dedup(d: dict[str, Any]) -> DedupConfig:
    return DedupConfig(
        l1_time_window_seconds=d.get("l1_time_window_seconds", 120),
        l2_time_window_seconds=d.get("l2_time_window_seconds", 120),
        l3_time_window_seconds=d.get("l3_time_window_seconds", 86400),
        l3_amount_tolerance_cents=d.get("l3_amount_tolerance_cents", 100),
    )


def _dict_to_accounts(d: dict[str, Any]) -> AccountConfig:
    return AccountConfig(
        platform_wallets=d.get(
            "platform_wallets",
            {
                "alipay": "Assets:Current:Digital:Alipay",
                "wechat": "Assets:Current:Digital:WeChat",
                "bank": "Assets:Current:Bank:Checking",
            },
        ),
        merchant_overrides=d.get("merchant_overrides", {}),
        bank_codes=d.get("bank_codes", {}),
    )


def _dict_to_platforms(d: dict[str, Any]) -> PlatformConfig:
    return PlatformConfig(
        priority=d.get("priority", ["alipay", "wechat", "bank"]),
        transfer_keywords=d.get("transfer_keywords", {}),
    )


def load_config(config_path: str | None = None) -> AppConfig:
    """
    Load configuration from a YAML file.

    If *config_path* is ``None``, searches standard locations in order.
    Falls back to built-in defaults when no config file is found.
    """
    paths = [Path(config_path)] if config_path else _DEFAULT_CONFIG_PATHS

    raw: dict[str, Any] = {}
    for p in paths:
        raw = load_yaml(p)
        if raw:
            logger.info("Loaded config from %s", p)
            break

    if not raw:
        logger.debug("No config file found; using defaults")
        return AppConfig()

    return AppConfig(
        dedup=_dict_to_dedup(raw.get("dedup", {})),
        accounts=_dict_to_accounts(raw.get("accounts", {})),
        platforms=_dict_to_platforms(raw.get("platforms", {})),
        input_dir=raw.get("input_dir", "input"),
        output_dir=raw.get("output_dir", "output"),
        include_metadata=raw.get("include_metadata", True),
        operating_currency=raw.get("operating_currency", "CNY"),
    )
