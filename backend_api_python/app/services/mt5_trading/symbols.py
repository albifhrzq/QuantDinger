"""Strategy <-> MetaTrader 5 symbol mapping.

QuantDinger Forex strategies use plain pairs (``XAUUSD``, ``EUR/USD``,
``Forex:XAUUSD``). MT5 brokers often add an account-type suffix
(HFM: ``XAUUSD`` Premium, ``XAUUSDb`` Zero, ``XAUUSD.v`` Premium Pro).

Resolution order for a strategy symbol:
1. explicit map entry (``"XAUUSD=XAUUSD.v,GOLD=XAUUSD"``)
2. normalized pair + configured suffix
"""

from __future__ import annotations

from typing import Dict


def normalize_pair(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    if "@" in s:
        s = s.split("@", 1)[0]
    return s.replace("/", "").replace("-", "").replace("_", "").replace(" ", "")


def parse_symbol_map(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = normalize_pair(k), v.strip()
        if k and v:
            out[k] = v
    return out


class SymbolMapper:
    def __init__(self, symbol_map: str = "", suffix: str = ""):
        self.map = parse_symbol_map(symbol_map)
        self.suffix = str(suffix or "").strip()
        self.reverse = {v.upper(): k for k, v in self.map.items()}

    def to_broker(self, symbol: str) -> str:
        key = normalize_pair(symbol)
        if not key:
            raise ValueError("mt5_symbol_required")
        if key in self.map:
            return self.map[key]
        return key + self.suffix

    def to_strategy(self, broker_symbol: str) -> str:
        s = str(broker_symbol or "").strip()
        if s.upper() in self.reverse:
            return self.reverse[s.upper()]
        if self.suffix and s.endswith(self.suffix):
            s = s[: -len(self.suffix)]
        return normalize_pair(s)
