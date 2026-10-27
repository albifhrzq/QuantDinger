"""Credential helpers for MetaTrader 5 accounts."""

from __future__ import annotations

from typing import Any, Dict

from app.services.mt5_trading.client import (
    MT5Config,
    TRADE_MODE_NAMES,
    real_accounts_allowed_by_server,
)


def mt5_config_from_request(data: Dict[str, Any]) -> MT5Config:
    cfg = MT5Config.from_dict(dict(data or {}))
    if cfg.max_lot <= 0 or cfg.fixed_lot <= 0:
        raise ValueError("MT5_INVALID_LOT")
    if cfg.fixed_lot > cfg.max_lot:
        raise ValueError("MT5_FIXED_LOT_ABOVE_MAX")
    if cfg.allow_real and not real_accounts_allowed_by_server():
        raise ValueError("MT5_REAL_ACCOUNTS_DISABLED_ON_SERVER")
    return cfg


def probe_mt5(cfg: MT5Config) -> Dict[str, Any]:
    """Connect to the terminal and return a non-secret account summary."""
    from app.services.live_trading.factory import create_mt5_client

    client = create_mt5_client(cfg.to_vault_dict())
    try:
        if not client.connect():
            raise ValueError("MT5_TERMINAL_UNREACHABLE")
        acct = client.account_info()
        mode = TRADE_MODE_NAMES.get(int(acct.get("trade_mode", -1)), "unknown")
        if mode == "real" and not (cfg.allow_real and real_accounts_allowed_by_server()):
            raise ValueError("MT5_REAL_ACCOUNT_BLOCKED")
        return {
            "login": acct.get("login"),
            "server": acct.get("server"),
            "currency": acct.get("currency"),
            "leverage": acct.get("leverage"),
            "balance": acct.get("balance"),
            "trade_mode": mode,
            "environment": "live" if mode == "real" else "demo",
        }
    finally:
        client.disconnect()
