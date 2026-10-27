# MetaTrader 5 (Forex / CFD) live trading

QuantDinger can trade Forex/CFD symbols (for example XAUUSD on HFM) through a
running MetaTrader 5 terminal. The terminal runs next to QuantDinger (Windows,
or Linux via Wine, for example the `gmag11/metatrader5_vnc` image) and exposes
the official `MetaTrader5` Python package over an `rpyc` classic server.

```
strategy (market=Forex, execution=live)
  -> pending order worker (exchange_id=mt5)
     -> rpyc (private Docker network) -> MetaTrader5 package -> MT5 terminal -> broker
```

## Setup

1. Run the MT5 terminal container on the same Docker network as the backend
   workers and log in to your broker account inside the terminal (noVNC).
   The broker password is never stored in QuantDinger.
2. The rpyc version next to the terminal must equal the backend's (`rpyc==6.0.2`).
3. Backend environment:
   - `MT5_ALLOWED_HOSTS=mt5` (comma list of terminal hosts the backend may reach)
   - `MT5_ALLOW_REAL_ACCOUNTS=false` (set `true` only to permit real accounts)
4. Broker Accounts -> Add account -> MetaTrader 5. Set host/port, symbol
   suffix (HFM: Premium none, Zero `b`, Pro `.v`), lot and risk limits.
5. Create a Forex strategy, choose **Live trading**, and select the MT5 account.

## Safety

- Real accounts are refused unless the server sets `MT5_ALLOW_REAL_ACCOUNTS=true`
  and the credential has `mt5_allow_real=true`.
- Entry size comes from the credential (fixed lot by default, or the strategy
  quantity capped by `mt5_max_lot`).
- Every entry has a stop loss (strategy protection, or the credential default
  in points). Wrong-side or too-close stops are rejected.
- `mt5_max_positions`, the broker-day realized loss cap (`mt5_max_daily_loss`),
  and the allowed-symbols list are checked before every entry.
- Only positions with the credential's magic number are read or closed.
