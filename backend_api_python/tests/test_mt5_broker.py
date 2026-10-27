"""MetaTrader 5 native broker: client guards, symbol mapping, policy, worker routing."""
from __future__ import annotations

import json

import pytest

from app.services.broker_market_policy import (
    LIVE_MARKET_CATEGORIES,
    allowed_market_types,
    is_long_only_broker,
    validate_strategy_config,
)
from app.services.mt5_trading import MT5Client, MT5Config, SymbolMapper
from app.services.mt5_trading.client import REMOTE_SRC  # noqa: F401  (import check)

BUY, SELL, DEAL = 0, 1, 1


class FakeTerminal:
    """In-memory stand-in for the rpyc namespace of an MT5 terminal."""

    def __init__(self, trade_mode=0, bid=4000.0, ask=4000.3):
        self.trade_mode = trade_mode
        self.bid, self.ask = bid, ask
        self.positions = []
        self.sent = []
        self.day_pnl = 0.0
        self.next_ticket = 1000
        self.namespace = {"qd_rpc": self.rpc}

    def close(self):
        pass

    def rpc(self, name, payload):
        a = json.loads(payload)
        if name == "ping":
            return json.dumps({"ok": True, "connected": True})
        if name == "account":
            return json.dumps({"login": 123, "server": "HFMarketsGlobal-Demo4", "trade_mode": self.trade_mode,
                               "balance": 1000.0, "equity": 1000.0, "currency": "USD", "trade_allowed": True,
                               "leverage": 500})
        if name == "symbol":
            return json.dumps({"name": a["symbol"], "point": 0.01, "digits": 2, "volume_min": 0.01,
                               "volume_step": 0.01, "volume_max": 50.0, "trade_contract_size": 100.0,
                               "trade_stops_level": 0})
        if name == "tick":
            return json.dumps({"bid": self.bid, "ask": self.ask, "time": 1_800_000_000})
        if name == "positions":
            return json.dumps([p for p in self.positions if not a.get("symbol") or p["symbol"] == a["symbol"]])
        if name == "day_pnl":
            return json.dumps({"realized": self.day_pnl})
        if name == "deals":
            return json.dumps([])
        if name == "consts":
            return json.dumps({"BUY": BUY, "SELL": SELL, "DEAL": DEAL, "SLTP": 6, "DONE": 10009,
                               "PLACED": 10008, "DONE_PARTIAL": 10010})
        if name == "send":
            req = a["req"]
            self.sent.append(req)
            if req.get("position"):
                for p in self.positions:
                    if p["ticket"] == req["position"]:
                        p["volume"] = round(p["volume"] - req["volume"], 8)
                self.positions = [p for p in self.positions if p["volume"] > 1e-9]
            else:
                self.next_ticket += 1
                self.positions.append({"ticket": self.next_ticket, "symbol": req["symbol"], "type": req["type"],
                                       "volume": req["volume"], "price_open": req["price"], "magic": req["magic"],
                                       "sl": req["sl"], "tp": req["tp"], "time": self.next_ticket, "profit": 0.0})
            return json.dumps({"retcode": 10009, "comment": "Request executed", "order": self.next_ticket,
                               "deal": self.next_ticket, "price": req["price"], "volume": req["volume"]})
        raise AssertionError(name)


def make(cfg=None, **term):
    t = FakeTerminal(**term)
    c = MT5Client(MT5Config.from_dict(cfg or {}), connection_factory=lambda: t)
    assert c.connect()
    return c, t


def test_symbol_mapper_suffix_and_map():
    m = SymbolMapper("GOLD=XAUUSD", ".v")
    assert m.to_broker("Forex:XAUUSD") == "XAUUSD.v"
    assert m.to_broker("EUR/USD") == "EURUSD.v"
    assert m.to_broker("gold") == "XAUUSD"
    assert m.to_strategy("XAUUSD.v") == "XAUUSD"


def test_policy_allows_mt5_forex_both_directions():
    assert "Forex" in LIVE_MARKET_CATEGORIES
    assert allowed_market_types("mt5", "Forex") == {"spot", "swap"}
    assert not is_long_only_broker("mt5")
    validate_strategy_config(exchange_id="mt5", market_category="Forex", market_type="spot", trade_direction="short")
    with pytest.raises(ValueError):
        validate_strategy_config(exchange_id="mt5", market_category="Crypto", market_type="swap")
    with pytest.raises(ValueError):
        validate_strategy_config(exchange_id="binance", market_category="Forex", market_type="spot")


def test_open_uses_fixed_lot_and_default_sl_tp():
    c, t = make()
    r = c.place_market_order("XAUUSD", "buy", 37.5, client_order_id="qd_2_9")
    assert r.success, r.message
    req = t.sent[-1]
    assert req["volume"] == 0.01 and req["type"] == BUY and req["magic"] == 26102026
    assert req["sl"] == pytest.approx(4000.3 - 15.0) and req["tp"] == pytest.approx(4000.3 + 30.0)
    assert r.filled == pytest.approx(1.0)       # 0.01 lot * 100 oz
    assert r.raw["lots"] == 0.01


def test_proportional_lot_is_capped_by_max_lot():
    c, t = make({"mt5_lot_mode": "proportional", "mt5_max_lot": 0.05})
    assert c.place_market_order("XAUUSD", "sell", 3.0).success          # 0.03 lot
    assert t.sent[-1]["volume"] == pytest.approx(0.03) and t.sent[-1]["type"] == SELL
    r = c.place_market_order("XAUUSD", "sell", 100.0)                   # 1.0 lot > max
    assert not r.success and "volume_out_of_range" in r.message


def test_real_account_is_blocked_by_default(monkeypatch):
    c, t = make(trade_mode=2)
    r = c.place_market_order("XAUUSD", "buy", 1)
    assert not r.success and "real_account_blocked" in r.message and not t.sent
    monkeypatch.setenv("MT5_ALLOW_REAL_ACCOUNTS", "true")
    c2, t2 = make({"mt5_allow_real": True}, trade_mode=2)
    assert c2.place_market_order("XAUUSD", "buy", 1).success


def test_guards_max_positions_daily_loss_and_allowed_symbols():
    c, t = make({"mt5_max_positions": 1, "mt5_allowed_symbols": "XAUUSD"})
    assert c.place_market_order("XAUUSD", "buy", 1).success
    assert "max_positions_reached" in c.place_market_order("XAUUSD", "buy", 1).message
    assert "symbol_not_allowed" in c.place_market_order("EURUSD", "buy", 1).message
    c2, t2 = make({"mt5_max_daily_loss": 10})
    t2.day_pnl = -12.0
    assert "daily_loss_limit_hit" in c2.place_market_order("XAUUSD", "buy", 1).message


def test_sl_on_wrong_side_is_rejected_and_zero_quote_fails_closed():
    c, _ = make()
    assert "sl_wrong_side" in c.place_market_order("XAUUSD", "buy", 1, stop_loss_price=4100).message
    c2, t2 = make(bid=0.0, ask=0.0)
    assert "no_live_quote" in c2.place_market_order("XAUUSD", "buy", 1).message


def test_close_positions_full_and_partial():
    c, t = make({"mt5_fixed_lot": 0.02})
    c.place_market_order("XAUUSD", "buy", 1)
    pos = c.get_positions()
    assert pos[0]["side"] == "long" and pos[0]["size"] == pytest.approx(2.0)
    r = c.close_positions("XAUUSD", "long", quantity=1.0)               # 0.01 of 0.02
    assert r.success and t.sent[-1]["volume"] == pytest.approx(0.01) and t.sent[-1]["type"] == SELL
    r = c.close_positions("XAUUSD", "long")
    assert r.success
    assert "no_matching_position" in c.close_positions("XAUUSD", "short").message


def test_factory_routes_mt5_and_enforces_host_allowlist(monkeypatch):
    from app.services.live_trading.base import LiveTradingError
    from app.services.live_trading.factory import create_client

    monkeypatch.setenv("MT5_ALLOWED_HOSTS", "mt5")
    client = create_client({"exchange_id": "mt5", "mt5_host": "mt5"}, market_type="spot")
    assert isinstance(client, MT5Client)
    with pytest.raises(LiveTradingError):
        create_client({"exchange_id": "mt5", "mt5_host": "evil.example"}, market_type="spot")


def test_live_order_request_allows_forex_short_on_spot():
    from app.services.strategy_v2.live_execution import LiveOrderRequest, StrategyV2OrderGateway

    base = dict(strategy_id=1, strategy_run_id=1, user_id=1, symbol="XAUUSD", action="open_short",
                quantity=1.0, reference_price=4000.0, signal_timestamp=1, market_type="spot",
                execution_mode="live")
    assert StrategyV2OrderGateway._validate(LiveOrderRequest(**base, market_category="Forex"))
    with pytest.raises(ValueError):
        StrategyV2OrderGateway._validate(LiveOrderRequest(**base, market_category="Crypto"))
