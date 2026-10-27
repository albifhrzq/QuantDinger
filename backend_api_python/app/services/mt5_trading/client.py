"""
Native MetaTrader 5 broker client (Forex / CFD, e.g. HFM).

QuantDinger talks to a MetaTrader 5 terminal through the ``MetaTrader5``
Python package running *inside* the terminal host (Windows or Wine), exposed
over an ``rpyc`` classic server.  All remote work goes through one
JSON-in / JSON-out helper executed in the terminal's Python, so no rpyc
proxies of dicts/namedtuples cross the wire and the remote surface stays
small and auditable.

Safety model (enforced here, before anything reaches the broker):

* real (non-demo) accounts are refused unless ``MT5_ALLOW_REAL_ACCOUNTS=true``
  is set on the server *and* the credential has ``allow_real=true``;
* every opening order is sized by the credential (fixed lot by default),
  capped by ``max_lot``, needs a stop loss, and respects ``max_positions``
  and a broker-day realized loss cap;
* only positions carrying this credential's magic number are ever touched.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.services.mt5_trading.symbols import SymbolMapper

logger = logging.getLogger(__name__)

TRADE_MODE_NAMES = {0: "demo", 1: "contest", 2: "real"}

# Executed once per connection inside the terminal's Python interpreter.
REMOTE_SRC = r'''
import json, time, MetaTrader5 as mt5

def _d(x):
    if x is None:
        return None
    return x._asdict() if hasattr(x, "_asdict") else x

def _fill(sym):
    info = mt5.symbol_info(sym)
    fm = info.filling_mode if info else 0
    if fm & 1: return mt5.ORDER_FILLING_FOK
    if fm & 2: return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN

def _server_now(sym):
    tk = mt5.symbol_info_tick(sym) if sym else None
    return int(tk.time) if tk and tk.time else int(time.time())

def qd_rpc(name, payload):
    a = json.loads(payload)
    if name == "ping":
        ok = mt5.initialize() if not mt5.terminal_info() else True
        t = mt5.terminal_info()
        return json.dumps({"ok": bool(ok), "connected": bool(t and t.connected),
                           "trade_allowed": bool(t and t.trade_allowed), "err": mt5.last_error()})
    if name == "account":
        return json.dumps(_d(mt5.account_info()), default=str)
    if name == "symbol":
        mt5.symbol_select(a["symbol"], True)
        return json.dumps(_d(mt5.symbol_info(a["symbol"])), default=str)
    if name == "tick":
        mt5.symbol_select(a["symbol"], True)
        t = mt5.symbol_info_tick(a["symbol"])
        for _ in range(10):
            if t is not None and t.bid > 0 and t.ask > 0:
                break
            time.sleep(0.3)
            t = mt5.symbol_info_tick(a["symbol"])
        return json.dumps(_d(t), default=str)
    if name == "positions":
        ps = mt5.positions_get(symbol=a["symbol"]) if a.get("symbol") else mt5.positions_get()
        magic = a.get("magic")
        return json.dumps([_d(p) for p in (ps or []) if magic is None or p.magic == magic], default=str)
    if name == "deals":
        now = _server_now(a.get("symbol"))
        frm = now - int(a.get("seconds", 7 * 86400))
        ds = mt5.history_deals_get(frm, now + 2 * 86400) or []
        magic = a.get("magic")
        out = [_d(d) for d in ds if (magic is None or d.magic == magic) and (not a.get("symbol") or d.symbol == a["symbol"])]
        return json.dumps(out[-int(a.get("limit", 200)):], default=str)
    if name == "day_pnl":
        now = _server_now(a.get("symbol"))
        day_start = now - (now % 86400)
        ds = mt5.history_deals_get(day_start - 2 * 86400, now + 2 * 86400) or []
        tot = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
                  for d in ds if d.magic == a["magic"] and day_start <= int(d.time) < day_start + 86400)
        return json.dumps({"realized": tot, "server_day_start": day_start})
    if name == "send":
        r = a["req"]
        req = {
            "action": r["action"], "symbol": r["symbol"], "volume": r.get("volume", 0.0),
            "type": r.get("type"), "price": r.get("price", 0.0), "sl": r.get("sl", 0.0), "tp": r.get("tp", 0.0),
            "deviation": r.get("deviation", 50), "magic": r["magic"], "comment": r.get("comment", "qd"),
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": _fill(r["symbol"]),
        }
        if r.get("position"): req["position"] = r["position"]
        req = {k: v for k, v in req.items() if v is not None}
        res = mt5.order_send(req)
        if res is None:
            return json.dumps({"retcode": None, "comment": "order_send returned None", "err": mt5.last_error()})
        return json.dumps({"retcode": res.retcode, "comment": res.comment, "order": res.order,
                           "deal": res.deal, "price": res.price, "volume": res.volume}, default=str)
    if name == "consts":
        return json.dumps({"BUY": mt5.ORDER_TYPE_BUY, "SELL": mt5.ORDER_TYPE_SELL,
                           "DEAL": mt5.TRADE_ACTION_DEAL, "SLTP": mt5.TRADE_ACTION_SLTP,
                           "DONE": mt5.TRADE_RETCODE_DONE, "PLACED": mt5.TRADE_RETCODE_PLACED,
                           "DONE_PARTIAL": mt5.TRADE_RETCODE_DONE_PARTIAL})
    raise ValueError("unknown rpc " + name)
'''


class MT5Error(Exception):
    """Transport / terminal failure (nothing was sent, safe to retry)."""


class MT5Rejected(Exception):
    """Pre-trade guard rejection (deterministic, do not retry)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"mt5_{code}: {detail}" if detail else f"mt5_{code}")
        self.code = code
        self.detail = detail


def real_accounts_allowed_by_server() -> bool:
    return str(os.getenv("MT5_ALLOW_REAL_ACCOUNTS", "false")).strip().lower() in ("1", "true", "yes", "on")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


@dataclass
class MT5Config:
    host: str = "mt5"
    port: int = 8001
    magic: int = 26102026
    symbol_suffix: str = ""
    symbol_map: str = ""
    allowed_symbols: str = ""          # comma list of broker symbols; empty = any mapped symbol
    lot_mode: str = "fixed"           # fixed | proportional
    fixed_lot: float = 0.01
    max_lot: float = 0.05
    max_positions: int = 2
    max_daily_loss: float = 10.0       # account currency, 0 disables
    require_sl: bool = True
    default_sl_points: float = 1500.0
    default_tp_points: float = 3000.0
    max_slippage_points: int = 50
    allow_real: bool = False
    timeout: float = 60.0

    @classmethod
    def from_dict(cls, cfg: Dict[str, Any]) -> "MT5Config":
        cfg = cfg or {}

        def g(*keys, default=None):
            for k in keys:
                if k in cfg and cfg[k] not in (None, ""):
                    return cfg[k]
            return default

        def b(v, default=False):
            if v is None:
                return default
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("1", "true", "yes", "on")

        return cls(
            host=str(g("mt5_host", "host", default=os.getenv("MT5_DEFAULT_HOST", "mt5"))).strip(),
            port=int(g("mt5_port", "port", default=os.getenv("MT5_DEFAULT_PORT", 8001))),
            magic=int(g("mt5_magic", "magic", default=26102026)),
            symbol_suffix=str(g("mt5_symbol_suffix", "symbol_suffix", default="")).strip(),
            symbol_map=str(g("mt5_symbol_map", "symbol_map", default="")).strip(),
            allowed_symbols=str(g("mt5_allowed_symbols", "allowed_symbols", default="")).strip(),
            lot_mode=str(g("mt5_lot_mode", "lot_mode", default="fixed")).strip().lower(),
            fixed_lot=_f(g("mt5_fixed_lot", "fixed_lot", default=0.01), 0.01),
            max_lot=_f(g("mt5_max_lot", "max_lot", default=0.05), 0.05),
            max_positions=int(_f(g("mt5_max_positions", "max_positions", default=2), 2)),
            max_daily_loss=_f(g("mt5_max_daily_loss", "max_daily_loss", default=10), 10),
            require_sl=b(g("mt5_require_sl", "require_sl"), True),
            default_sl_points=_f(g("mt5_default_sl_points", "default_sl_points", default=1500), 1500),
            default_tp_points=_f(g("mt5_default_tp_points", "default_tp_points", default=3000), 3000),
            max_slippage_points=int(_f(g("mt5_max_slippage_points", "max_slippage_points", default=50), 50)),
            allow_real=b(g("mt5_allow_real", "allow_real"), False),
        )

    def to_vault_dict(self) -> Dict[str, Any]:
        return {
            "exchange_id": "mt5",
            "mt5_host": self.host,
            "mt5_port": self.port,
            "mt5_magic": self.magic,
            "mt5_symbol_suffix": self.symbol_suffix,
            "mt5_symbol_map": self.symbol_map,
            "mt5_allowed_symbols": self.allowed_symbols,
            "mt5_lot_mode": self.lot_mode,
            "mt5_fixed_lot": self.fixed_lot,
            "mt5_max_lot": self.max_lot,
            "mt5_max_positions": self.max_positions,
            "mt5_max_daily_loss": self.max_daily_loss,
            "mt5_require_sl": self.require_sl,
            "mt5_default_sl_points": self.default_sl_points,
            "mt5_default_tp_points": self.default_tp_points,
            "mt5_max_slippage_points": self.max_slippage_points,
            "mt5_allow_real": self.allow_real,
        }


@dataclass
class OrderResult:
    """Order result, same surface as the IBKR/Alpaca OrderResult."""
    success: bool
    order_id: str = ""
    filled: float = 0.0          # QuantDinger units (lots * contract_size)
    avg_price: float = 0.0
    status: str = ""
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def _round_volume(vol: float, info: Dict[str, Any], *, down: bool = False) -> float:
    step = _f(info.get("volume_step"), 0.01) or 0.01
    vmin = _f(info.get("volume_min"), step) or step
    vmax = _f(info.get("volume_max"), vol) or vol
    steps = vol / step
    steps = math.floor(steps + 1e-9) if down else round(steps)
    v = round(steps * step, 8)
    return max(vmin, min(v, vmax))


class MT5Client:
    """MetaTrader 5 client with the IBKR/Alpaca-compatible broker surface."""

    exchange_id = "mt5"

    def __init__(self, config: Optional[MT5Config] = None, *, connection_factory=None):
        self.config = config or MT5Config()
        self.symbols = SymbolMapper(self.config.symbol_map, self.config.symbol_suffix)
        self._conn = None
        self._lock = threading.RLock()
        self._consts: Optional[Dict[str, int]] = None
        self._account: Optional[Dict[str, Any]] = None
        self._connection_factory = connection_factory

    # ------------------------------------------------------------ transport
    def _connect_transport(self):
        if self._connection_factory is not None:
            conn = self._connection_factory()
        else:
            import rpyc  # lazy: only needed when MT5 is used

            conn = rpyc.classic.connect(self.config.host, int(self.config.port))
            conn._config["sync_request_timeout"] = self.config.timeout
            conn.execute(REMOTE_SRC)
        self._conn = conn
        self._consts = None

    def _call(self, name: str, **payload):
        with self._lock:
            last = None
            for attempt in range(3):
                try:
                    if self._conn is None:
                        self._connect_transport()
                    raw = self._conn.namespace["qd_rpc"](name, json.dumps(payload))
                    return json.loads(raw)
                except (EOFError, OSError, ConnectionError) as exc:
                    last = exc
                    self._conn = None
                    time.sleep(1.0 * (attempt + 1))
                except Exception as exc:  # remote error: do not blindly retry
                    raise MT5Error(f"mt5 {name} failed: {exc}") from exc
            raise MT5Error(f"mt5 {name}: terminal unreachable at {self.config.host}:{self.config.port}: {last}")

    # ----------------------------------------------------------- lifecycle
    @property
    def connected(self) -> bool:
        return self._conn is not None and self._account is not None

    def connect(self) -> bool:
        try:
            st = self._call("ping")
            if not st.get("connected"):
                logger.error("MT5 terminal is running but not connected to a trade server: %s", st.get("err"))
                return False
            self._account = self._call("account") or None
            return self._account is not None
        except Exception as exc:
            logger.error("MT5 connect failed: %s", exc)
            self._conn = None
            self._account = None
            return False

    def disconnect(self):
        with self._lock:
            try:
                if self._conn is not None and hasattr(self._conn, "close"):
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._account = None

    def _ensure_connected(self):
        if not self.connected and not self.connect():
            raise MT5Error("Not connected to MT5 terminal (is it running and logged in?)")

    def consts(self) -> Dict[str, int]:
        if self._consts is None:
            self._consts = self._call("consts")
        return self._consts

    # ------------------------------------------------------------- account
    def account_info(self) -> Dict[str, Any]:
        acct = self._call("account")
        if not acct:
            raise MT5Error("MT5 returned no account (terminal not logged in?)")
        self._account = acct
        return acct

    def account_mode(self) -> str:
        return TRADE_MODE_NAMES.get(int(self.account_info().get("trade_mode", -1)), "unknown")

    def get_account_summary(self) -> Dict[str, Any]:
        try:
            self._ensure_connected()
            a = self.account_info()
            mode = TRADE_MODE_NAMES.get(int(a.get("trade_mode", -1)), "unknown")
            ccy = a.get("currency") or "USD"
            positions = self.get_positions()
            return {
                "success": True,
                "account": str(a.get("login") or ""),
                "account_id": str(a.get("login") or ""),
                "login": a.get("login"),
                "server": a.get("server"),
                "name": a.get("name"),
                "company": a.get("company"),
                "currency": ccy,
                "balance": _f(a.get("balance")),
                "equity": _f(a.get("equity")),
                "margin": _f(a.get("margin")),
                "margin_free": _f(a.get("margin_free")),
                "margin_level": _f(a.get("margin_level")),
                "profit": _f(a.get("profit")),
                "leverage": a.get("leverage"),
                "trade_allowed": bool(a.get("trade_allowed", True)),
                "trade_mode": mode,
                "paper": mode != "real",
                "position_count": len(positions),
                "real_allowed": bool(self.config.allow_real and real_accounts_allowed_by_server()),
            }
        except Exception as exc:
            logger.error("MT5 get_account_summary failed: %s", exc)
            return {"success": False, "error": str(exc)}

    def get_connection_status(self) -> Dict[str, Any]:
        acct = self._account or {}
        return {
            "connected": self.connected,
            "host": self.config.host,
            "port": self.config.port,
            "login": acct.get("login"),
            "account": str(acct.get("login") or ""),
            "server": acct.get("server") or "",
            "paper": TRADE_MODE_NAMES.get(int(acct.get("trade_mode", -1) or -1), "unknown") != "real" if acct else True,
            "trade_mode": TRADE_MODE_NAMES.get(int(acct.get("trade_mode", -1)), "unknown") if acct else "unknown",
            "magic": self.config.magic,
        }

    # ------------------------------------------------------------- symbols
    def symbol_info(self, broker_symbol: str) -> Dict[str, Any]:
        info = self._call("symbol", symbol=broker_symbol)
        if not info:
            raise MT5Rejected("symbol_not_found", f"{broker_symbol} not available on this MT5 server")
        return info

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        broker_symbol = self.symbols.to_broker(symbol)
        t = self._call("tick", symbol=broker_symbol) or {}
        bid, ask = _f(t.get("bid")), _f(t.get("ask"))
        last = (bid + ask) / 2 if bid > 0 and ask > 0 else _f(t.get("last"))
        return {"symbol": self.symbols.to_strategy(broker_symbol), "broker_symbol": broker_symbol, "bid": bid, "ask": ask,
                "last": last, "price": last, "time": t.get("time")}

    def get_quote(self, symbol: str, market_type: str = "Forex") -> Dict[str, Any]:
        try:
            return {"success": True, **self.get_ticker(symbol)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def _contract_size(self, info: Dict[str, Any]) -> float:
        return _f(info.get("trade_contract_size"), 1.0) or 1.0

    # ----------------------------------------------------------- positions
    def _raw_positions(self, broker_symbol: str = "") -> List[Dict[str, Any]]:
        return self._call("positions", symbol=broker_symbol or "", magic=self.config.magic) or []

    def get_positions(self, *, raise_on_error: bool = False) -> List[Dict[str, Any]]:
        """Bridge-owned (magic) positions in QuantDinger units (lots * contract size)."""
        try:
            c = self.consts()
            out: List[Dict[str, Any]] = []
            info_cache: Dict[str, Dict[str, Any]] = {}
            for p in self._raw_positions():
                bsym = str(p.get("symbol") or "")
                if bsym not in info_cache:
                    info_cache[bsym] = self.symbol_info(bsym)
                cs = self._contract_size(info_cache[bsym])
                lots = _f(p.get("volume"))
                side = "long" if int(p.get("type", 0)) == int(c["BUY"]) else "short"
                qty = lots * cs
                out.append({
                    "symbol": self.symbols.to_strategy(bsym),
                    "broker_symbol": bsym,
                    "ticket": p.get("ticket"),
                    "side": side,
                    "lots": lots,
                    "quantity": qty if side == "long" else -qty,
                    "qty": qty,
                    "size": qty,
                    "avgCost": _f(p.get("price_open")),
                    "avg_entry_price": _f(p.get("price_open")),
                    "current_price": _f(p.get("price_current")),
                    "currentPrice": _f(p.get("price_current")),
                    "market_value": qty * _f(p.get("price_current")),
                    "unrealized_pnl": _f(p.get("profit")) + _f(p.get("swap")),
                    "sl": _f(p.get("sl")),
                    "tp": _f(p.get("tp")),
                    "opened_at": p.get("time"),
                    "comment": p.get("comment"),
                })
            return out
        except Exception as exc:
            logger.error("MT5 get_positions failed: %s", exc)
            if raise_on_error:
                raise
            return []

    def get_orders(self, status: str = "all", limit: int = 100, *, raise_on_error: bool = False) -> List[Dict[str, Any]]:
        """Recent bridge-owned deals, newest first (MT5 market orders fill immediately)."""
        try:
            c = self.consts()
            deals = self._call("deals", magic=self.config.magic, limit=int(limit), seconds=30 * 86400) or []
            out = []
            for d in reversed(deals):
                if int(d.get("type", -1)) not in (int(c["BUY"]), int(c["SELL"])):
                    continue
                out.append({
                    "id": str(d.get("order") or d.get("ticket")),
                    "deal": d.get("ticket"),
                    "position_id": d.get("position_id"),
                    "symbol": self.symbols.to_strategy(str(d.get("symbol") or "")),
                    "side": "buy" if int(d.get("type")) == int(c["BUY"]) else "sell",
                    "entry": {0: "in", 1: "out", 2: "inout", 3: "out_by"}.get(int(d.get("entry", 0)), ""),
                    "quantity": _f(d.get("volume")),
                    "filled_qty": _f(d.get("volume")),
                    "filled_avg_price": _f(d.get("price")),
                    "profit": _f(d.get("profit")),
                    "commission": _f(d.get("commission")),
                    "swap": _f(d.get("swap")),
                    "status": "filled",
                    "filled_at": d.get("time"),
                    "submitted_at": d.get("time"),
                    "comment": d.get("comment"),
                })
            return out
        except Exception as exc:
            logger.error("MT5 get_orders failed: %s", exc)
            if raise_on_error:
                raise
            return []

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return []

    def day_realized(self, broker_symbol: str) -> float:
        return _f((self._call("day_pnl", symbol=broker_symbol, magic=self.config.magic) or {}).get("realized"))

    # -------------------------------------------------------------- guards
    def _check_account_allowed(self, account: Dict[str, Any]) -> None:
        mode = TRADE_MODE_NAMES.get(int(account.get("trade_mode", -1)), "unknown")
        if mode == "unknown":
            raise MT5Rejected("unknown_account_mode", str(account.get("trade_mode")))
        if mode == "real" and not (self.config.allow_real and real_accounts_allowed_by_server()):
            raise MT5Rejected(
                "real_account_blocked",
                "real MT5 accounts need MT5_ALLOW_REAL_ACCOUNTS=true on the server and allow_real on the credential",
            )
        if not account.get("trade_allowed", True):
            raise MT5Rejected("trading_disabled_on_account")

    def _entry_volume(self, quantity: float, info: Dict[str, Any]) -> float:
        if self.config.lot_mode == "proportional" and quantity > 0:
            lots = _round_volume(quantity / self._contract_size(info), info, down=True)
        else:
            lots = _round_volume(self.config.fixed_lot, info)
        if lots <= 0 or lots > self.config.max_lot + 1e-9:
            raise MT5Rejected("volume_out_of_range", f"{lots} lots (max {self.config.max_lot})")
        return lots

    def _check_allowed_symbol(self, broker_symbol: str) -> None:
        allowed = {s.strip().upper() for s in self.config.allowed_symbols.split(",") if s.strip()}
        if allowed and broker_symbol.upper() not in allowed:
            raise MT5Rejected("symbol_not_allowed", broker_symbol)

    # -------------------------------------------------------------- orders
    def _ok(self, res: Dict[str, Any]) -> bool:
        c = self.consts()
        return res.get("retcode") in (c["DONE"], c["PLACED"], c.get("DONE_PARTIAL"))

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        market_type: str = "Forex",
        *,
        client_order_id: str = "",
        stop_loss_price: float = 0.0,
        take_profit_price: float = 0.0,
        **_ignored,
    ) -> OrderResult:
        """Open a new position (buy = long entry, sell = short entry)."""
        try:
            self._ensure_connected()
            broker_symbol = self.symbols.to_broker(symbol)
            self._check_allowed_symbol(broker_symbol)
            account = self.account_info()
            self._check_account_allowed(account)
            info = self.symbol_info(broker_symbol)
            lots = self._entry_volume(float(quantity or 0.0), info)
            open_positions = self._raw_positions(broker_symbol)
            if self.config.max_positions > 0 and len(open_positions) >= self.config.max_positions:
                raise MT5Rejected("max_positions_reached", f"{len(open_positions)}/{self.config.max_positions}")
            if self.config.max_daily_loss > 0:
                day = self.day_realized(broker_symbol)
                if day <= -abs(self.config.max_daily_loss):
                    raise MT5Rejected("daily_loss_limit_hit", f"{day:.2f} <= -{abs(self.config.max_daily_loss):.2f}")

            tick = self._call("tick", symbol=broker_symbol) or {}
            bid, ask = _f(tick.get("bid")), _f(tick.get("ask"))
            if bid <= 0 or ask <= 0:
                raise MT5Rejected("no_live_quote", f"bid={bid} ask={ask}")
            buy = str(side).lower() == "buy"
            price = ask if buy else bid
            point = _f(info.get("point"), 0.01) or 0.01
            digits = int(info.get("digits", 2) or 2)

            sl = _f(stop_loss_price)
            tp = _f(take_profit_price)
            if sl <= 0 and self.config.default_sl_points > 0:
                sl = price - self.config.default_sl_points * point if buy else price + self.config.default_sl_points * point
            if tp <= 0 and self.config.default_tp_points > 0:
                tp = price + self.config.default_tp_points * point if buy else price - self.config.default_tp_points * point
            if sl and ((buy and sl >= price) or (not buy and sl <= price)):
                raise MT5Rejected("sl_wrong_side", f"sl={sl} price={price}")
            if tp and ((buy and tp <= price) or (not buy and tp >= price)):
                raise MT5Rejected("tp_wrong_side", f"tp={tp} price={price}")
            min_dist = _f(info.get("trade_stops_level")) * point
            if sl and abs(price - sl) < min_dist:
                raise MT5Rejected("sl_too_close", f"min distance {min_dist}")
            if self.config.require_sl and not sl:
                raise MT5Rejected("stop_loss_required")

            c = self.consts()
            req = {
                "action": c["DEAL"], "symbol": broker_symbol, "volume": lots,
                "type": c["BUY"] if buy else c["SELL"], "price": price,
                "sl": round(sl, digits) if sl else 0.0, "tp": round(tp, digits) if tp else 0.0,
                "deviation": self.config.max_slippage_points, "magic": self.config.magic,
                "comment": (client_order_id or "qd")[:31],
            }
            res = self._call("send", req=req) or {}
            ok = self._ok(res)
            filled_lots = _f(res.get("volume")) if ok else 0.0
            cs = self._contract_size(info)
            return OrderResult(
                success=ok,
                order_id=str(res.get("order") or ""),
                filled=filled_lots * cs,
                avg_price=_f(res.get("price")) or price,
                status="filled" if ok else "rejected",
                message="" if ok else f"retcode={res.get('retcode')} {res.get('comment')}",
                raw={"request": req, "result": res, "lots": filled_lots, "contract_size": cs,
                     "client_order_id": client_order_id, "broker_symbol": broker_symbol},
            )
        except MT5Rejected as exc:
            return OrderResult(success=False, status="rejected", message=str(exc), raw={"rejected": exc.code})
        except MT5Error as exc:
            return OrderResult(success=False, status="error", message=str(exc), raw={"transport_error": True})

    def place_limit_order(self, *args, **kwargs) -> OrderResult:
        # Strategy limit orders are executed as protected market orders on MT5.
        kwargs.pop("price", None)
        return self.place_market_order(*args, **kwargs)

    def close_positions(
        self,
        symbol: str,
        side: str,
        *,
        quantity: float = 0.0,
        client_order_id: str = "",
    ) -> OrderResult:
        """Close (quantity<=0 -> all) owned positions on one side; quantity is in QuantDinger units."""
        try:
            self._ensure_connected()
            broker_symbol = self.symbols.to_broker(symbol)
            info = self.symbol_info(broker_symbol)
            cs = self._contract_size(info)
            c = self.consts()
            want_buy = str(side).lower() == "long"
            remaining = float(quantity or 0.0) / cs if quantity and quantity > 0 else None
            vmin = _f(info.get("volume_min"), 0.01)
            closed_lots, notional, results = 0.0, 0.0, []
            for p in sorted(self._raw_positions(broker_symbol), key=lambda x: int(x.get("time") or 0)):
                is_buy = int(p.get("type", 0)) == int(c["BUY"])
                if is_buy != want_buy:
                    continue
                total = _f(p.get("volume"))
                vol = total
                if remaining is not None:
                    if remaining <= 1e-9:
                        break
                    vol = min(total, _round_volume(remaining, info, down=True))
                    if total - vol < vmin - 1e-9:
                        vol = total
                tick = self._call("tick", symbol=broker_symbol) or {}
                price = _f(tick.get("bid") if is_buy else tick.get("ask"))
                req = {
                    "action": c["DEAL"], "symbol": broker_symbol, "volume": vol,
                    "type": c["SELL"] if is_buy else c["BUY"], "position": p.get("ticket"),
                    "price": price, "deviation": self.config.max_slippage_points,
                    "magic": self.config.magic, "comment": ("close-" + (client_order_id or "qd"))[:31],
                }
                res = self._call("send", req=req) or {}
                ok = self._ok(res)
                results.append({"ticket": p.get("ticket"), "ok": ok, "request": req, "result": res})
                if ok:
                    done = _f(res.get("volume")) or vol
                    closed_lots += done
                    notional += done * (_f(res.get("price")) or price)
                    if remaining is not None:
                        remaining -= done
            if not results:
                return OrderResult(success=False, status="rejected", message="mt5_no_matching_position",
                                   raw={"rejected": "no_matching_position"})
            ok_all = all(r["ok"] for r in results)
            avg = notional / closed_lots if closed_lots > 0 else 0.0
            return OrderResult(
                success=closed_lots > 0,
                order_id=str(next((r["result"].get("order") for r in results if r["ok"]), "") or ""),
                filled=closed_lots * cs,
                avg_price=avg,
                status="filled" if ok_all else ("partially_filled" if closed_lots > 0 else "rejected"),
                message="" if ok_all else "; ".join(
                    f"#{r['ticket']} retcode={r['result'].get('retcode')} {r['result'].get('comment')}"
                    for r in results if not r["ok"]),
                raw={"closes": results, "lots": closed_lots, "contract_size": cs, "broker_symbol": broker_symbol},
            )
        except MT5Rejected as exc:
            return OrderResult(success=False, status="rejected", message=str(exc), raw={"rejected": exc.code})
        except MT5Error as exc:
            return OrderResult(success=False, status="error", message=str(exc), raw={"transport_error": True})

    def cancel_order(self, order_id: str) -> bool:
        return False


def create_mt5_client(exchange_config: Dict[str, Any], *, connection_factory=None) -> MT5Client:
    return MT5Client(MT5Config.from_dict(exchange_config or {}), connection_factory=connection_factory)
