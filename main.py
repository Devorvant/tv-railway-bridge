import os
import time
import math
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, HTTPException

# Bybit client (pip install pybit)
from pybit.unified_trading import HTTP

app = FastAPI()

# ==== ENV ====
TV_TOKEN = os.getenv("TV_TOKEN", "")
BYBIT_KEY = os.getenv("BYBIT_KEY", "")
BYBIT_SECRET = os.getenv("BYBIT_SECRET", "")

# Testnet by default (set BYBIT_TESTNET=0 to use mainnet)
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "1") != "0"

# Trading params
LEVERAGE = int(os.getenv("LEVERAGE", "3"))               # e.g. 3
RISK_FRACTION = float(os.getenv("RISK_FRACTION", "0.5")) # e.g. 0.5 = 50% equity
CATEGORY = os.getenv("BYBIT_CATEGORY", "linear")         # linear for USDT Perp

print("TV_TOKEN set?", bool(TV_TOKEN), "len=", len(TV_TOKEN))
print("BYBIT_TESTNET:", BYBIT_TESTNET)
print("BYBIT_KEY set?", bool(BYBIT_KEY), "BYBIT_SECRET set?", bool(BYBIT_SECRET))

last_payload: Optional[Dict[str, Any]] = None
last_result: Optional[Dict[str, Any]] = None

# Create Bybit session if keys exist
bybit: Optional[HTTP] = None
if BYBIT_KEY and BYBIT_SECRET:
    bybit = HTTP(testnet=BYBIT_TESTNET, api_key=BYBIT_KEY, api_secret=BYBIT_SECRET)

# ==== Routes ====
@app.get("/")
def root():
    return {"ok": True, "service": "tv-webhook-receiver"}

@app.get("/last")
def last():
    return {"ok": True, "last": last_payload, "last_result": last_result}

# ==== Helpers ====
def mask_token(data: Dict[str, Any]) -> Dict[str, Any]:
    safe = dict(data)
    if "token" in safe:
        safe["token"] = "***"
    return safe

def require_token(data: Dict[str, Any]):
    token = str(data.get("token", ""))
    if TV_TOKEN and token != TV_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

def ensure_bybit_ready():
    if bybit is None:
        raise HTTPException(
            status_code=500,
            detail="Bybit API not configured (set BYBIT_KEY and BYBIT_SECRET in Railway Variables)",
        )

def clean_symbol(sym: str) -> str:
    # TradingView иногда шлёт APTUSDT.P / APTUSDT.PERP и т.п.
    s = (sym or "").strip().upper()
    for suf in [".P", ".PERP", "PERP"]:
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s

def bybit_set_isolated_margin_mode():
    """
    Unified account setting. Safe to call repeatedly.
    """
    ensure_bybit_ready()
    try:
        bybit.set_margin_mode(setMarginMode="ISOLATED_MARGIN")
    except Exception as e:
        print("WARN: set_margin_mode:", repr(e))

def bybit_set_leverage(symbol: str, lev: int):
    ensure_bybit_ready()
    try:
        bybit.set_leverage(
            category=CATEGORY,
            symbol=symbol,
            buyLeverage=str(lev),
            sellLeverage=str(lev),
        )
    except Exception as e:
        print("WARN: set_leverage:", repr(e))

def bybit_get_equity_usdt() -> float:
    """
    Returns equity in USDT for Unified account.
    """
    ensure_bybit_ready()
    resp = bybit.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    lst = resp.get("result", {}).get("list", [])
    if not lst:
        return 0.0
    coins = lst[0].get("coin", [])
    for c in coins:
        if c.get("coin") == "USDT":
            return float(c.get("equity", 0) or 0)
    return 0.0

def bybit_get_last_price(symbol: str) -> float:
    ensure_bybit_ready()
    resp = bybit.get_tickers(category=CATEGORY, symbol=symbol)
    lst = resp.get("result", {}).get("list", [])
    if not lst:
        return 0.0
    return float(lst[0].get("lastPrice", 0) or 0)

def bybit_get_position_size(symbol: str) -> float:
    """
    Returns current position size (positive long, negative short, 0 flat).
    """
    ensure_bybit_ready()
    resp = bybit.get_positions(category=CATEGORY, symbol=symbol)
    pos_list = resp.get("result", {}).get("list", []) or []
    if not pos_list:
        return 0.0

    p = pos_list[0]
    size = float(p.get("size", 0) or 0)
    side = (p.get("side", "") or "").lower()  # "Buy"/"Sell"
    if side == "sell":
        return -size
    return size

def bybit_get_qty_rules(symbol: str):
    """
    Берём правила инструмента: min/max qty и шаг qtyStep.
    """
    ensure_bybit_ready()
    resp = bybit.get_instruments_info(category=CATEGORY, symbol=symbol)
    lst = resp.get("result", {}).get("list", []) or []
    if not lst:
        raise HTTPException(status_code=400, detail=f"symbol not found in instruments_info: {symbol}")
    lot = lst[0].get("lotSizeFilter", {}) or {}
    min_qty = float(lot.get("minOrderQty", 0) or 0)
    max_qty = float(lot.get("maxOrderQty", 0) or 0)
    step = float(lot.get("qtyStep", 0) or 0)
    if step <= 0:
        step = 1.0
    return min_qty, max_qty, step

def round_down_to_step(qty: float, step: float) -> float:
    return math.floor(qty / step) * step

def fmt_number(x: float) -> str:
    # чтобы не отправлять "1e-07" и лишние нули
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return s if s else "0"

def calc_qty(symbol: str) -> float:
    equity = bybit_get_equity_usdt()
    price = bybit_get_last_price(symbol)
    if equity <= 0 or price <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"cannot fetch equity/price (equity={equity}, price={price})"
        )

    notional = equity * RISK_FRACTION * LEVERAGE
    raw_qty = notional / price

    min_qty, max_qty, step = bybit_get_qty_rules(symbol)
    qty = round_down_to_step(raw_qty, step)

    if max_qty > 0:
        qty = min(qty, max_qty)

    if qty < min_qty:
        raise HTTPException(
            status_code=400,
            detail=f"qty too small after rounding: raw={raw_qty}, step={step}, rounded={qty}, minQty={min_qty}"
        )

    return qty

def place_market(symbol: str, side: str, qty: float, reduce_only: bool):
    ensure_bybit_ready()
    return bybit.place_order(
        category=CATEGORY,
        symbol=symbol,
        side=side,              # "Buy" or "Sell"
        orderType="Market",
        qty=fmt_number(qty),
        reduceOnly=reduce_only,
        timeInForce="GTC",
    )

def close_full_position(symbol: str):
    """
    Closes the entire position with reduceOnly market order using current size.
    Much safer than closing by "calculated qty".
    """
    pos = bybit_get_position_size(symbol)
    if pos == 0:
        return {"ok": True, "note": "no position to close"}

    qty = abs(pos)
    if pos > 0:
        return place_market(symbol, "Sell", qty, reduce_only=True)
    else:
        return place_market(symbol, "Buy", qty, reduce_only=True)

# ==== Webhook Handler ====
async def handle_webhook(req: Request):
    global last_payload, last_result

    data = await req.json()
    require_token(data)

    print("WEBHOOK:", mask_token(data))
    last_payload = data

    if bybit is None:
        last_result = {"ok": True, "note": "bybit not configured - receiver only"}
        return {"ok": True, "mode": "receiver_only"}

    symbol = clean_symbol(str(data.get("symbol", "APTUSDT")))
    action = str(data.get("action", "")).upper()

    # Ensure isolated + leverage each time (safe)
    bybit_set_isolated_margin_mode()
    bybit_set_leverage(symbol, LEVERAGE)

    try:
        if action == "LONG":
            pos = bybit_get_position_size(symbol)
            if pos < 0:
                close_full_position(symbol)
                time.sleep(0.2)

            qty = calc_qty(symbol)
            result = place_market(symbol, "Buy", qty, reduce_only=False)
            last_result = {"action": "LONG", "symbol": symbol, "qty": qty, "bybit": result}
            return {"ok": True, "bybit": result}

        if action == "SHORT":
            pos = bybit_get_position_size(symbol)
            if pos > 0:
                close_full_position(symbol)
                time.sleep(0.2)

            qty = calc_qty(symbol)
            result = place_market(symbol, "Sell", qty, reduce_only=False)
            last_result = {"action": "SHORT", "symbol": symbol, "qty": qty, "bybit": result}
            return {"ok": True, "bybit": result}

        if action in ("CLOSE", "CLOSE_LONG", "STOP_LONG", "STOP_SHORT", "CLOSE_SHORT"):
            result = close_full_position(symbol)
            last_result = {"action": action, "symbol": symbol, "bybit": result}
            return {"ok": True, "bybit": result}

        last_result = {"ok": True, "ignored": True, "reason": f"unknown action: {action}"}
        return {"ok": True, "ignored": True}

    except HTTPException:
        raise
    except Exception as e:
        print("ERROR:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook")
async def webhook(req: Request):
    return await handle_webhook(req)

@app.post("/")
async def webhook_root(req: Request):
    return await handle_webhook(req)
