# main.py -- Gate.io용, 4분할(dca: 16.7/16.7/33.3/33.3), 레버리지 3, TP 3.1%
import os
import json
import math
import threading
import requests
import ccxt
from fastapi import FastAPI, Request

app = FastAPI()

# ===== 환경변수 / 기본값 =====
# webhook passphrase (트레이딩뷰와 동일하게)
PASS = os.getenv("WEBHOOK_PASSPHRASE", os.getenv("WEBHOOK_PASS", "CHANGE_ME"))

# 투자 기본 단위 (USDT)
CAPITAL = float(os.getenv("CAPITAL_USDT", "100"))

# 레버리지 (정수)
LEVERAGE = int(os.getenv("LEVERAGE", "3"))

# TP(익절) -- 사용자가 '3.1' 처럼 퍼센트로 넣을 것 -> 내부적으로 0.031로 변환
_raw_tp = float(os.getenv("TP_PCT", "3.1"))
TP_PCT = (_raw_tp / 100.0) if _raw_tp > 1.0 else _raw_tp

# 거래소 및 마켓 타입
EXCHANGE_ID = os.getenv("EXCHANGE", "gateio").lower()   # 'gateio'
MARKET_TYPE = os.getenv("MARKET_TYPE", "future")        # 'future' or 'swap' or 'spot'
SANDBOX = os.getenv("SANDBOX", "true").lower() == "true"

# 텔레그램(선택)
TELE_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELE_CHAT = os.getenv("TELEGRAM_CHAT_ID")

# API 키 (절대 코드에 하드코딩하지 마세요!)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# 상태 저장 파일 (간단 파일; 필요 시 DB로 교체 권장)
STATE_FILE = "state.json"
lock = threading.Lock()

# dca 비율 (요청하신 값)
DCA = {1: 0.167, 2: 0.167, 3: 0.333, 4: 0.333}

# ===== 거래소 초기화 (ccxt) =====
def make_exchange():
    ex_class = getattr(ccxt, EXCHANGE_ID)
    conf = {
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": MARKET_TYPE}
    }
    ex = ex_class(conf)
    # 테스트넷(샌드박스) 모드가 있는 경우 시도
    if SANDBOX and hasattr(ex, "set_sandbox_mode"):
        try:
            ex.set_sandbox_mode(True)
        except Exception:
            pass
    # load markets for normalization & precision info
    ex.load_markets()
    return ex

exchange = make_exchange()

# ===== 헬퍼 함수들 =====
def notify(text: str):
    # 콘솔에도 찍힘
    print("[NOTIFY]", text)
    # 텔레그램이 설정되어 있으면 메시지 전송
    if TELE_TOKEN and TELE_CHAT:
        try:
            requests.get(
                f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage",
                params={"chat_id": TELE_CHAT, "text": text[:3900]}, timeout=5
            )
        except Exception as e:
            print("[NOTIFY ERR]", e)

def read_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def write_state(s: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def normalize_symbol(sym: str) -> str:
    # 트레이딩뷰에서 들어오는 심볼 예: BTCUSDT / BTC/USDT
    if not sym:
        return sym
    s = sym.replace("/", "").replace("-", "").upper()
    # 찾아서 가장 알맞은 마켓키 반환
    for k in exchange.markets.keys():
        k2 = k.replace("/", "").replace(":", "").replace("-", "").upper()
        if k2 == s:
            return k
    # fallback: try with USDT suffix
    if not s.endswith("USDT"):
        s2 = s + "USDT"
        for k in exchange.markets.keys():
            if k.replace("/", "").replace(":", "").replace("-", "").upper() == s2:
                return k
    return sym  # 마지막 수단

def qty_to_precision(symbol: str, qty: float) -> float:
    try:
        q_str = exchange.amount_to_precision(symbol, qty)
        return float(q_str)
    except Exception:
        # fallback: round to 6 decimals
        return float(round(qty, 6))

def try_set_leverage(symbol: str, lev: int):
    try:
        # ccxt common call: set_leverage(leverage, symbol)
        if hasattr(exchange, "set_leverage"):
            exchange.set_leverage(int(lev), symbol)
    except Exception as e:
        notify(f"[LEVERR] {symbol} {e}")

def cancel_order_if_exists(order_id, symbol):
    if not order_id:
        return
    try:
        exchange.cancel_order(order_id, symbol)
    except Exception as e:
        # 취소 실패해도 계속 진행
        notify(f"[CANCEL ERR] {order_id} {symbol} {e}")

def place_tp_limit(symbol: str, side: str, qty: float, price: float):
    params = {}
    # reduceOnly 키는 거래소마다 다름; 여러 키 시도
    for key in ("reduceOnly", "reduce_only", "reduce_only_order"):
        try:
            r = exchange.create_limit_sell_order(symbol, qty, price, {key: True}) if side == "long" else exchange.create_limit_buy_order(symbol, qty, price, {key: True})
            return r
        except Exception:
            pass
    # 마지막으로 기본으로 시도
    if side == "long":
        return exchange.create_limit_sell_order(symbol, qty, price)
    else:
        return exchange.create_limit_buy_order(symbol, qty, price)

# ===== FastAPI 라우트 =====
@app.get("/")
def root():
    return {"ok": True, "msg": "bot running"}

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    # 보안 패스프레이즈 체크
    if data.get("passphrase") != PASS:
        return {"ok": False, "error": "bad passphrase"}
    # 입력 정보
    symbol_raw = data.get("symbol", "BTCUSDT")
    side = data.get("side", "long").lower()
    action = data.get("action", "entry").lower()
    step = int(data.get("step", 1))

    # normalize market symbol
    symbol = normalize_symbol(symbol_raw)

    try:
        ticker = exchange.fetch_ticker(symbol)
        last = float(ticker.get("last") or ticker.get("close") or 0)
    except Exception as e:
        notify(f"[TICKERR] {symbol_raw} -> {symbol} {e}")
        return {"ok": False, "error": f"ticker error {e}"}

    with lock:
        state = read_state()
        info = state.get(symbol, {"side": None, "qty": 0.0, "avg": None, "tp_id": None})

        # EXIT 처리
        if action in ("exit", "close", "tp"):
            qty = float(info.get("qty") or 0)
            if qty > 0:
                try:
                    if info.get("side") == "long":
                        exchange.create_market_sell_order(symbol, qty, {"reduceOnly": True})
                    else:
                        exchange.create_market_buy_order(symbol, qty, {"reduceOnly": True})
                except Exception as e:
                    notify(f"[EXIT ERR] {symbol} {e}")
            state[symbol] = {"side": None, "qty": 0.0, "avg": None, "tp_id": None}
            write_state(state)
            notify(f"[{symbol}] EXIT ALL")
            return {"ok": True, "result": "closed"}

        # ENTRY / ADD
        try_set_leverage(symbol, LEVERAGE)
        ratio = DCA.get(step, 0.0)
        if ratio <= 0:
            return {"ok": False, "error": "bad step"}

        intended = "long" if side in ("long", "buy") else "short"
        if info.get("side") and info.get("side") != intended and (info.get("qty") or 0) > 0:
            return {"ok": False, "error": "existing opposite position"}

        # 계산: notional = (CAPITAL_USDT * ratio) * LEVERAGE
        notional = CAPITAL * ratio * LEVERAGE
        raw_qty = notional / last
        qty = qty_to_precision(symbol, raw_qty)

        # 시장가 진입
        try:
            if intended == "long":
                order = exchange.create_market_buy_order(symbol, qty)
            else:
                order = exchange.create_market_sell_order(symbol, qty)
        except Exception as e:
            notify(f"[ENTRY ERR] {symbol} {e}")
            return {"ok": False, "error": f"entry failed {e}"}

        # 평균단가/수량 갱신 (간단화: last 기준)
        prev_qty = float(info.get("qty") or 0)
        prev_avg = float(info.get("avg") or 0) if info.get("avg") else None
        new_qty = prev_qty + qty
        if prev_avg:
            new_avg = (prev_avg * prev_qty + last * qty) / new_qty
        else:
            new_avg = last

        # 기존 TP 있으면 취소
        old_tp_id = info.get("tp_id")
        if old_tp_id:
            try:
                cancel_order_if_exists(old_tp_id, symbol)
            except Exception:
                pass

        # TP 가격 산출 및 등록
        tp_price = new_avg * (1 + TP_PCT) if intended == "long" else new_avg * (1 - TP_PCT)
        tp_price = round(tp_price, 2)  # 심볼별 틱사이즈에 맞게 조정 필요 시 수정

        tp_order = None
        try:
            tp_order = place_tp_limit(symbol, intended, new_qty, tp_price)
        except Exception as e:
            notify(f"[TP CREATE ERR] {symbol} {e}")

        tp_id = None
        if tp_order:
            tp_id = tp_order.get("id") or tp_order.get("info", {}).get("id")

        # 상태 저장
        state[symbol] = {"side": intended, "qty": new_qty, "avg": new_avg, "tp_id": tp_id}
        write_state(state)

        notify(f"[{symbol}] {intended.upper()} STEP {step} qty~{qty} @ {last:.2f} | TP {tp_price:.2f}")
        return {"ok": True, "qty": new_qty, "avg": new_avg, "tp": tp_price}
