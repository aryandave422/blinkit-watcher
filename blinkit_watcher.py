"""
Blinkit PlayStation voucher watcher (product 698590 and its price options).
Sends a Telegram alert only when an option changes from out of stock to in stock.

Usage:
  python blinkit_watcher.py --dry-run   # checks and prints, sends nothing
  python blinkit_watcher.py             # runs forever, sends Telegram alerts
  python blinkit_watcher.py --once      # one check, then exit

Secrets come from environment variables (never write them in this file):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

import requests

PRID = "698590"
LAT, LON = "22.4707", "70.0577"          # Jamnagar (361001), approximate
URL = f"https://blinkit.com/v1/layout/product/{PRID}"
STATE_FILE = "state_blinkit.json"
POLL_SECONDS = 3                          # base wait between checks (seconds)
FAIL_ALERT_AFTER = 5                      # consecutive failures before warning you
IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
    "Accept": "application/json",
    "lat": LAT,
    "lon": LON,
    "app_client": "consumer_web",
}
COOKIES = {"gr_1_lat": LAT, "gr_1_lon": LON}


def now_ist():
    return datetime.now(IST).strftime("%I:%M:%S %p IST").lstrip("0")


# One reusable connection: skips the slow TLS handshake on every check (saves ~0.2-0.4 s)
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.cookies.update(COOKIES)


def fetch():
    r = SESSION.post(URL, json={}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} (possibly blocked)")
    return r.json()


def parse_variants(data):
    """Return {product_id: {price, inventory}} for every price option."""
    out = {}
    for sn in data["response"]["snippets"]:
        items = sn.get("data", {}).get("horizontal_item_list") or []
        for it in items:
            click = it.get("tracking", {}).get("click_map", {})
            if click.get("event_name") != "Product Variant Clicked":
                continue
            pid = str(click.get("product_id"))
            out[pid] = {
                "price": int(float(click.get("price") or 0)),
                "inventory": int(float(click.get("inventory") or 0)),
            }
    if not out:
        raise RuntimeError("no price options found (Blinkit may have changed its format)")
    return out


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def send_telegram(text, button_url=None, dry_run=False):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if dry_run:
        print("\n--- (dry run) would send ---\n" + text + "\n----------------------------")
        return True
    if not token or not chat:
        print("Telegram not set up: missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return False
    payload = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "🛒 Buy on Blinkit", "url": button_url}]]
        }
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=15
            )
            if r.ok:
                return True
            print("Telegram error:", r.status_code, r.text[:200])
        except Exception as e:
            print("Telegram send failed:", e)
        time.sleep(2)
    return False


def alert_text(restocked):
    lines = ["🚨 <b>PSN STOCK RESTOCK ALERT!</b>", "", "🏪 Store: <b>Blinkit</b>",
             f"🕒 Detected: {now_ist()}", ""]
    for pid, v in sorted(restocked.items(), key=lambda kv: kv[1]["price"]):
        link = f"https://blinkit.com/prn/x/prid/{pid}"
        lines.append(f"🟢 Sony PlayStation Voucher ₹{v['price']} (qty {v['inventory']})")
        lines.append(f'👉 <a href="{link}">Direct Purchase Link</a>')
        lines.append("")
    lines.append("⚡ Act fast! Stock sells out within minutes.")
    return "\n".join(lines)


def check_once(state, dry_run):
    variants = parse_variants(fetch())
    restocked = {}
    for pid, v in variants.items():
        in_stock = v["inventory"] > 0
        if in_stock and not state.get(pid, False):
            restocked[pid] = v
        state[pid] = in_stock
    summary = ", ".join(
        f"₹{v['price']}: {'IN STOCK' if v['inventory'] > 0 else 'out'}"
        for v in sorted(variants.values(), key=lambda x: x["price"])
    )
    print(f"[{now_ist()}] {summary}")
    if restocked:
        first_pid = sorted(restocked)[0]
        send_telegram(alert_text(restocked), f"https://blinkit.com/prn/x/prid/{first_pid}", dry_run)
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only, send nothing")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--interval", type=float, default=POLL_SECONDS,
                    help="seconds between checks (default 3; try 2 or 1 at your own risk)")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="stop cleanly after this many minutes (0 = run forever); used by the cloud job")
    args = ap.parse_args()

    state = load_state()
    end_time = time.time() + args.max_minutes * 60 if args.max_minutes > 0 else None
    fails = 0
    warned = False
    print("Blinkit watcher started. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                check_once(state, args.dry_run)
                if warned:
                    send_telegram("✅ Blinkit checks are working again.", dry_run=args.dry_run)
                fails, warned = 0, False
            except Exception as e:
                fails += 1
                print(f"[{now_ist()}] check failed ({fails}): {e}")
                if fails >= FAIL_ALERT_AFTER and not warned:
                    send_telegram(
                        f"⚠️ Blinkit checks failing {fails} times in a row: {e}",
                        dry_run=args.dry_run,
                    )
                    warned = True
            if args.once:
                break
            if end_time and time.time() >= end_time:
                print("Reached --max-minutes, stopping cleanly.")
                break
            # normal: your --interval (+ small random delay). After failures, back off (6, 12, 24... up to 60 s)
            wait = args.interval if fails == 0 else min(60, args.interval * (2 ** fails))
            time.sleep(wait + random.uniform(0, args.interval * 0.3))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
