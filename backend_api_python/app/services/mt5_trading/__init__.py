"""
MetaTrader 5 trading module (Forex / CFDs, e.g. HFM).

QuantDinger reaches a running MetaTrader 5 terminal through an rpyc classic
server that hosts the official ``MetaTrader5`` Python package next to the
terminal (Windows, or Linux via Wine). The user logs in to the broker inside
the terminal; QuantDinger never stores the broker password.
"""

from app.services.mt5_trading.client import (
    MT5Client,
    MT5Config,
    MT5Error,
    MT5Rejected,
    OrderResult,
    create_mt5_client,
    real_accounts_allowed_by_server,
)
from app.services.mt5_trading.symbols import SymbolMapper, normalize_pair

__all__ = [
    "MT5Client",
    "MT5Config",
    "MT5Error",
    "MT5Rejected",
    "OrderResult",
    "SymbolMapper",
    "create_mt5_client",
    "normalize_pair",
    "real_accounts_allowed_by_server",
]
