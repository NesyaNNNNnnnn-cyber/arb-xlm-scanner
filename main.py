import os
import threading
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("arb-xlm-scanner")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR_MSK", "21"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE_MSK", "0"))
BYBIT_BASE = "https://api.bybit.com"
TELEGRAM_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

app = Flask(__name__)
state = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_scan": None,
    "last_market_data": {},
    "last_error": None,
    "mode": "market-data-only",
    "full_route_enabled": False,
}

session = requests.Session()
session.headers.update({"User-Agent": "ARB-XLM-Scanner/0.1"})


def bybit_orderbook(symbol, limit=50):
    response = session.get(
        f"{BYBIT_BASE}/v5/market/orderbook",
        params={"category": "spot", "symbol": symbol, "limit": limit},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error for {symbol}: {payload.get('retMsg')}")
    result = payload["result"]
    bids = [(float(p), float(q)) for p, q in result.get("b", [])]
    asks = [(float(p), float(q)) for p, q in result.get("a", [])]
    if not bids or not asks:
        raise RuntimeError(f"Empty order book for {symbol}")
    return {
        "symbol": symbol,
        "best_bid": bids[0][0],
        "best_ask": asks[0][0],
        "bids": bids,
        "asks": asks,
        "timestamp_ms": int(result.get("ts", 0)),
    }


def market_snapshot():
    xrp = bybit_orderbook("XRPUSDT")
    xlm = bybit_orderbook("XLMUSDT")
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "XRPUSDT": {
            "best_bid": xrp["best_bid"],
            "best_ask": xrp["best_ask"],
            "spread_pct": (xrp["best_ask"] / xrp["best_bid"] - 1) * 100,
        },
        "XLMUSDT": {
            "best_bid": xlm["best_bid"],
            "best_ask": xlm["best_ask"],
            "spread_pct": (xlm["best_ask"] / xlm["best_bid"] - 1) * 100,
        },
        "note": "Spot order books only. No XRP-to-XLM provider quote is integrated.",
    }


def tg_call(method, data=None):
    if not BOT_TOKEN:
        return None
    response = session.post(f"{TELEGRAM_BASE}/{method}", json=data or {}, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload.get("result")


def send_message(chat_id, text):
    if not BOT_TOKEN:
        log.info("Telegram not configured; message would be: %s", text)
        return
    tg_call("sendMessage", {"chat_id": chat_id, "text": text})


def scan_once():
    snapshot = market_snapshot()
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    state["last_market_data"] = snapshot
    state["last_error"] = None
    return snapshot


def format_snapshot(snapshot):
    xrp = snapshot["XRPUSDT"]
    xlm = snapshot["XLMUSDT"]
    return (
        "ARB XLM Scanner — рыночный мониторинг\n\n"
        f"XRP/USDT: bid {xrp['best_bid']:.6f} | ask {xrp['best_ask']:.6f}\n"
        f"XLM/USDT: bid {xlm['best_bid']:.6f} | ask {xlm['best_ask']:.6f}\n\n"
        "Полный арбитражный расчёт пока отключён: не подключена реальная котировка "
        "обмена XRP→XLM из провайдера Trust Wallet. Это не сигнал на сделку."
    )


def handle_command(chat_id, text):
    command = text.strip().split()[0].lower()
    if command in ("/start", "/help"):
        send_message(
            chat_id,
            "Привет! Это ARB XLM Scanner.\n"
            "Доступные команды:\n"
            "/status — состояние сканера\n"
            "/scan — получить текущие котировки Bybit\n"
            "/help — помощь\n\n"
            "Сейчас работает только мониторинг публичных стаканов XRPUSDT и XLMUSDT. "
            "Сигналы о прибыли не отправляются, пока не подключён провайдер обмена XRP→XLM."
        )
    elif command == "/status":
        send_message(
            chat_id,
            "Статус ARB XLM Scanner\n"
            f"Последняя проверка: {state['last_scan'] or 'ещё не было'}\n"
            f"Режим: {state['mode']}\n"
            f"Полная связка: {'включена' if state['full_route_enabled'] else 'не подключена'}\n"
            f"Ошибка: {state['last_error'] or 'нет'}"
        )
    elif command == "/scan":
        try:
            snapshot = scan_once()
            send_message(chat_id, format_snapshot(snapshot))
        except Exception as exc:
            state["last_error"] = str(exc)
            send_message(chat_id, f"Не удалось получить котировки Bybit: {exc}")
    else:
        send_message(chat_id, "Неизвестная команда. Используй /help.")


def telegram_polling():
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN is empty. Telegram polling is disabled.")
        return
    offset = None
    log.info("Telegram polling started")
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            response = session.get(f"{TELEGRAM_BASE}/getUpdates", params=params, timeout=35)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram getUpdates error: {payload}")
            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message or not message.get("text"):
                    continue
                chat_id = str(message["chat"]["id"])
                if CHAT_ID and chat_id != CHAT_ID:
                    continue
                handle_command(chat_id, message["text"])
        except Exception as exc:
            log.exception("Telegram polling failed: %s", exc)
            time.sleep(5)


def background_scanner():
    while True:
        try:
            scan_once()
        except Exception as exc:
            state["last_error"] = str(exc)
            log.warning("Market scan failed: %s", exc)
        time.sleep(max(10, SCAN_INTERVAL))


def daily_reporter():
    last_sent_date = None
    msk = ZoneInfo("Europe/Moscow")
    while True:
        now = datetime.now(msk)
        if now.hour == REPORT_HOUR and now.minute == REPORT_MINUTE and last_sent_date != now.date():
            if CHAT_ID and BOT_TOKEN:
                snap = state["last_market_data"]
                msg = (
                    "ARB XLM Scanner — ежедневный отчёт\n"
                    f"Дата: {now:%d.%m.%Y}\n"
                    f"Последняя проверка: {state['last_scan'] or 'нет данных'}\n"
                    "Режим: только рыночный мониторинг Bybit.\n"
                    "Подтверждённых арбитражных возможностей: нет данных — провайдер XRP→XLM не подключён.\n"
                    f"Ошибка: {state['last_error'] or 'нет'}"
                )
                try:
                    send_message(CHAT_ID, msg)
                except Exception:
                    log.exception("Daily report send failed")
            last_sent_date = now.date()
        time.sleep(20)


@app.get("/")
def index():
    return jsonify({"service": "ARB XLM Scanner", "status": "running", "mode": state["mode"]})


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "last_scan": state["last_scan"],
        "mode": state["mode"],
        "full_route_enabled": state["full_route_enabled"],
        "last_error": state["last_error"],
    })


def start_background_threads():
    threading.Thread(target=telegram_polling, daemon=True).start()
    threading.Thread(target=background_scanner, daemon=True).start()
    threading.Thread(target=daily_reporter, daemon=True).start()


if __name__ == "__main__":
    start_background_threads()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
