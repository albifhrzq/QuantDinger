"""
MetaTrader 5 API routes (Forex / CFD brokers such as HFM).

The broker login (account + password) lives inside the MetaTrader 5 terminal;
QuantDinger stores only the terminal address and per-account risk limits in
``qd_exchange_credentials`` (``exchange_id = 'mt5'``). One credential per user
is the expected setup; ``credential_id`` selects among several.
"""

from __future__ import annotations

import json

from flask import g, jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.mt5_trading.credentials import mt5_config_from_request, probe_mt5
from app.utils.auth import login_required
from app.utils.credential_crypto import decrypt_credential_blob, encrypt_credential_blob
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

mt5_blp = Blueprint("mt5", __name__)


def _rows(user_id: int) -> list:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id, name, api_key_hint, encrypted_config
            FROM qd_exchange_credentials
            WHERE user_id = %s AND exchange_id = 'mt5'
            ORDER BY updated_at DESC NULLS LAST, id DESC
            """,
            (int(user_id),),
        )
        rows = cur.fetchall() or []
        cur.close()
    return rows


def _requested_credential_id():
    body = request.get_json(silent=True) or {}
    value = request.args.get("credential_id", body.get("credential_id"))
    try:
        cid = int(value) if value not in (None, "") else 0
    except (TypeError, ValueError):
        cid = 0
    return cid or None


def _load(user_id: int) -> dict:
    rows = _rows(user_id)
    cid = _requested_credential_id()
    if cid:
        rows = [r for r in rows if int(r["id"]) == cid]
    if not rows:
        return {}
    row = rows[0]
    cfg = json.loads(decrypt_credential_blob(row.get("encrypted_config")) or "{}")
    cfg["credential_id"] = int(row["id"])
    cfg["name"] = row.get("name") or ""
    return cfg


def _client(cfg: dict):
    from app.services.live_trading.factory import create_mt5_client

    client = create_mt5_client(cfg)
    if not client.connect():
        return None
    return client


def _with_client(fn):
    try:
        cfg = _load(int(g.user_id))
        if not cfg:
            return jsonify({"success": False, "error": "Not connected to MT5"}), 400
        client = _client(cfg)
        if client is None:
            return jsonify({"success": False, "error": "MT5 terminal unreachable or not logged in"}), 502
        try:
            return fn(client, cfg)
        finally:
            client.disconnect()
    except Exception as exc:
        logger.error("MT5 route failed: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500


@mt5_blp.route("/accounts", methods=["GET"])
@login_required
def accounts():
    rows = _rows(int(g.user_id))
    return jsonify({"success": True, "data": [
        {"id": int(r["id"]), "name": r.get("name") or "", "api_key_hint": r.get("api_key_hint") or ""} for r in rows
    ]})


@mt5_blp.route("/status", methods=["GET"])
@login_required
def status():
    try:
        cfg = _load(int(g.user_id))
    except Exception as exc:
        return jsonify({"success": True, "data": {"connected": False, "error": str(exc)}})
    if not cfg:
        return jsonify({"success": True, "data": {"connected": False}})
    client = _client(cfg)
    if client is None:
        return jsonify({"success": True, "data": {
            "connected": False, "credential_id": cfg["credential_id"],
            "host": cfg.get("mt5_host"), "port": cfg.get("mt5_port"),
            "error": "MT5 terminal unreachable or not logged in",
        }})
    try:
        data = client.get_connection_status()
        data["credential_id"] = cfg["credential_id"]
        data["name"] = cfg.get("name") or ""
        data["limits"] = {k: cfg.get(k) for k in (
            "mt5_symbol_suffix", "mt5_symbol_map", "mt5_allowed_symbols", "mt5_lot_mode", "mt5_fixed_lot",
            "mt5_max_lot", "mt5_max_positions", "mt5_max_daily_loss", "mt5_require_sl",
            "mt5_default_sl_points", "mt5_default_tp_points", "mt5_allow_real")}
        return jsonify({"success": True, "data": data})
    finally:
        client.disconnect()


@mt5_blp.route("/connect", methods=["POST"])
@login_required
def connect():
    """Validate the terminal connection and save (or update) the MT5 credential."""
    data = request.get_json(silent=True) or {}
    try:
        cfg = mt5_config_from_request(data)
        info = probe_mt5(cfg)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    vault = cfg.to_vault_dict()
    hint = f"{info.get('login')}@{info.get('server')} ({info.get('trade_mode')})"
    name = str(data.get("name") or "").strip() or f"MT5 {info.get('login')}"
    blob = encrypt_credential_blob(json.dumps(vault, ensure_ascii=False))
    user_id = int(g.user_id)
    cid = _requested_credential_id()
    with get_db_connection() as db:
        cur = db.cursor()
        if not cid:
            for row in _rows(user_id):
                try:
                    prev = json.loads(decrypt_credential_blob(row.get("encrypted_config")) or "{}")
                except Exception:
                    prev = {}
                if prev.get("mt5_host") == vault["mt5_host"] and int(prev.get("mt5_port") or 0) == vault["mt5_port"] \
                        and int(prev.get("mt5_magic") or 0) == vault["mt5_magic"]:
                    cid = int(row["id"])
                    break
        if cid:
            cur.execute(
                """
                UPDATE qd_exchange_credentials
                SET name = %s, api_key_hint = %s, encrypted_config = %s, updated_at = NOW()
                WHERE id = %s AND user_id = %s AND exchange_id = 'mt5'
                """,
                (name, hint, blob, cid, user_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO qd_exchange_credentials (user_id, name, exchange_id, api_key_hint, encrypted_config, created_at, updated_at)
                VALUES (%s, %s, 'mt5', %s, %s, NOW(), NOW())
                RETURNING id
                """,
                (user_id, name, hint, blob),
            )
            cid = int((cur.fetchone() or {}).get("id") or 0)
        db.commit()
        cur.close()
    return jsonify({"success": True, "message": "Connected successfully",
                    "data": {"connected": True, "credential_id": cid, **info}})


@mt5_blp.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    # MT5 sessions are opened per request; nothing persistent to tear down.
    return jsonify({"success": True})


@mt5_blp.route("/account", methods=["GET"])
@login_required
def account():
    def run(client, _cfg):
        summary = client.get_account_summary()
        if summary.get("success") is False:
            return jsonify({"success": False, "error": "brokerAccounts.accountLoadFailed"}), 502
        return jsonify({"success": True, "data": summary})
    return _with_client(run)


@mt5_blp.route("/positions", methods=["GET"])
@login_required
def positions():
    return _with_client(lambda c, _cfg: jsonify({"success": True, "data": c.get_positions(raise_on_error=True)}))


@mt5_blp.route("/orders", methods=["GET"])
@login_required
def orders():
    limit = int(request.args.get("limit") or 100)
    return _with_client(lambda c, _cfg: jsonify({"success": True, "data": c.get_orders(limit=limit, raise_on_error=True)}))


@mt5_blp.route("/quote", methods=["GET"])
@login_required
def quote():
    symbol = str(request.args.get("symbol") or "XAUUSD")
    return _with_client(lambda c, _cfg: jsonify({"success": True, "data": c.get_quote(symbol)}))
