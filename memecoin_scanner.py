#!/usr/bin/env python3
"""
Memecoin Scanner v1  -  DexScreener -> RugCheck -> Telegram alerts
===================================================================

What it does
------------
Every 60 seconds it pulls the latest Solana coins from DexScreener's free API,
throws out the junk with YOUR checklist filters, runs a RugCheck safety check,
and sends you a Telegram message with links when a coin passes.

An alert means "this coin passed your filters". It is NOT a guarantee it will
pump. You still check holders / dev / bundles and decide yourself.

Setup (one time)
----------------
1. Install Python 3 from python.org (tick "Add Python to PATH" on Windows).
   No extra packages are needed.
2. Create your Telegram bot:
   - In Telegram, message @BotFather -> /newbot -> pick a name.
   - Copy the bot TOKEN it gives you.
   - Open a chat with your new bot and send it any message (e.g. "hi").
   - In a browser open:  https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
     and find  "chat":{"id": 123456789  -> that number is your CHAT ID.
3. Paste the token and chat id into CONFIG below (or set environment
   variables TG_BOT_TOKEN and TG_CHAT_ID).
4. Test Telegram:     python memecoin_scanner.py --test
5. One dry scan:      python memecoin_scanner.py --once
6. Run all day:       python memecoin_scanner.py

Keep your bot token private. Never put a wallet seed phrase or private key in
this file - the scanner never needs one and never trades.
"""

import csv
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# CONFIG - tweak these to match your checklist
# --------------------------------------------------------------------------
CONFIG = {
    "telegram_bot_token": os.environ.get("TG_BOT_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TG_CHAT_ID", ""),

    "poll_seconds": 5,               # how often to scan

    # Size and age
    "min_liquidity_usd": 20_000,     # thin pools = expensive exits
    "min_mcap_usd": 50_000,
    "max_mcap_usd": 2_000_000,
    "min_age_minutes": 10,           # skip the first sniper minutes
    "max_age_hours": 12,

    # Activity and momentum
    "min_txns_1h": 150,              # people are actually trading it
    "min_change_1h": 5,              # moving up...
    "max_change_1h": 500,            # ...but not already vertical
    "max_change_6h": 1000,           # skip coins that already ran 10x+
    "require_more_buys_5m": True,    # buyers outnumber sellers right now

    # Red-flag checks
    "min_liq_to_mcap": 0.10,         # liquidity at least 10% of market cap
    "max_liq_to_mcap": 1.00,         # liquidity bigger than mcap = weird data
    "max_avg_trade_usd_1h": 600,     # huge average trades = likely wash bots
    "use_rugcheck": True,            # skip coins RugCheck marks as "danger"

    # Alerts
    "realert_hours": 6,              # don't alert the same coin again for 6h

    # Your trade plan (shown in each alert)
    "position_sol": 0.1,
    "take_profit_pct": 50,
    "stop_loss_pct": 20,
    "time_limit_min": 15,
}

# Names that are almost always fake "fund"/stock/stablecoin traps
BAD_NAME_WORDS = [
    "fund", "reserve", "trust", "dividend", "oil", "treasury", "etf",
    "xstock", "exchange", "bank", "usd", "stable",
]

# Optional: extra keyword searches each scan (e.g. today's viral narrative).
# Example: ["elon", "trump", "cat"]. Leave empty to only scan the latest feeds.
SEARCH_TERMS = ["cat", "dog", "trump", "elon", "ai", "pepe", "frog", "meme"]

# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------
DEX = "https://api.dexscreener.com"
FEEDS = [
    f"{DEX}/token-profiles/latest/v1",
    f"{DEX}/token-boosts/latest/v1",
    f"{DEX}/token-boosts/top/v1",
]
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "scanner_alerted.json")
LOG_FILE = os.path.join(HERE, "scanner_alerts_log.csv")
UA = {"User-Agent": "memecoin-scanner/1.0", "Accept": "application/json"}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited - back off
                time.sleep(5 * (attempt + 1))
                continue
            if e.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2)
    return None


def money(x):
    x = float(x or 0)
    if x >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x / 1_000:.0f}K"
    return f"${x:.0f}"


def pct(x):
    return f"{float(x or 0):+.0f}%"


# --------------------------------------------------------------------------
# Collect candidates
# --------------------------------------------------------------------------
def latest_solana_mints():
    mints = []
    for url in FEEDS:
        try:
            data = http_get_json(url) or []
        except Exception as e:
            log(f"Feed failed {url}: {e}")
            continue
        if isinstance(data, dict):
            data = [data]
        for item in data:
            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                mints.append(item["tokenAddress"])
    return list(dict.fromkeys(mints))  # unique, keep order


def search_pairs(term):
    data = http_get_json(f"{DEX}/latest/dex/search?q={urllib.parse.quote(term)}") or {}
    return [p for p in data.get("pairs") or [] if p.get("chainId") == "solana"]


def pairs_for_mints(mints):
    pairs = []
    for i in range(0, len(mints), 30):  # API takes up to 30 tokens per call
        chunk = ",".join(mints[i:i + 30])
        try:
            data = http_get_json(f"{DEX}/tokens/v1/solana/{chunk}") or []
            pairs.extend(data if isinstance(data, list) else [])
        except Exception as e:
            log(f"Pair lookup failed: {e}")
    return pairs


def best_pair_per_token(pairs):
    """Keep the deepest SOL pair for each token."""
    best = {}
    for p in pairs:
        if (p.get("quoteToken") or {}).get("symbol", "").upper() not in ("SOL", "WSOL"):
            continue
        mint = (p.get("baseToken") or {}).get("address")
        if not mint:
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if mint not in best or liq > ((best[mint].get("liquidity") or {}).get("usd") or 0):
            best[mint] = p
    return list(best.values())


# --------------------------------------------------------------------------
# Filters (your checklist)
# --------------------------------------------------------------------------
def check_pair(p, cfg=CONFIG, now_ms=None):
    """Return (passed: bool, reason: str)."""
    now_ms = now_ms or time.time() * 1000
    base = p.get("baseToken") or {}
    name = f"{base.get('name', '')} {base.get('symbol', '')}".lower()
    if any(w in name for w in BAD_NAME_WORDS):
        return False, "fake-fund/stable style name"

    liq = (p.get("liquidity") or {}).get("usd") or 0
    mcap = p.get("marketCap") or p.get("fdv") or 0
    created = p.get("pairCreatedAt") or 0
    age_min = (now_ms - created) / 60000 if created else 0
    tx = p.get("txns") or {}
    tx1h = (tx.get("h1") or {})
    tx5m = (tx.get("m5") or {})
    txns_1h = (tx1h.get("buys") or 0) + (tx1h.get("sells") or 0)
    vol1h = (p.get("volume") or {}).get("h1") or 0
    ch = p.get("priceChange") or {}
    ch1h, ch6h = ch.get("h1") or 0, ch.get("h6") or 0

    if liq < cfg["min_liquidity_usd"]:
        return False, f"liquidity {money(liq)} too low"
    if not (cfg["min_mcap_usd"] <= mcap <= cfg["max_mcap_usd"]):
        return False, f"mcap {money(mcap)} out of range"
    if not created or age_min < cfg["min_age_minutes"]:
        return False, "too new"
    if age_min > cfg["max_age_hours"] * 60:
        return False, "too old"
    if txns_1h < cfg["min_txns_1h"]:
        return False, f"only {txns_1h} txns in 1h"
    if not (cfg["min_change_1h"] <= ch1h <= cfg["max_change_1h"]):
        return False, f"1h change {pct(ch1h)} outside range"
    if ch6h > cfg["max_change_6h"]:
        return False, f"already ran {pct(ch6h)} in 6h"
    if cfg["require_more_buys_5m"] and (tx5m.get("buys") or 0) <= (tx5m.get("sells") or 0):
        return False, "sellers winning last 5m"
    ratio = liq / mcap if mcap else 0
    if not (cfg["min_liq_to_mcap"] <= ratio <= cfg["max_liq_to_mcap"]):
        return False, f"liquidity/mcap ratio {ratio:.2f} looks off"
    if txns_1h and vol1h / txns_1h > cfg["max_avg_trade_usd_1h"]:
        return False, "average trade size suggests wash trading"
    return True, "passed"


def rugcheck(mint):
    """Return (ok: bool, summary: str)."""
    try:
        data = http_get_json(RUGCHECK.format(mint=mint))
    except Exception:
        data = None
    if not data:
        return True, "RugCheck unavailable - check manually"
    risks = data.get("risks") or []
    danger = [r.get("name", "?") for r in risks if (r.get("level") or "").lower() == "danger"]
    warn = [r.get("name", "?") for r in risks if (r.get("level") or "").lower() == "warn"]
    if danger:
        return False, "DANGER: " + ", ".join(danger)
    summary = f"score {data.get('score_normalised', data.get('score', '?'))}"
    if warn:
        summary += " | warnings: " + ", ".join(warn[:3])
    return True, summary


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
def build_message(p, rug_summary, copycats, cfg=CONFIG):
    base = p.get("baseToken") or {}
    mint = base.get("address", "")
    name = html.escape(base.get("name", "?"))
    sym = html.escape(base.get("symbol", "?"))
    mcap = p.get("marketCap") or p.get("fdv") or 0
    liq = (p.get("liquidity") or {}).get("usd") or 0
    age_min = int((time.time() * 1000 - (p.get("pairCreatedAt") or 0)) / 60000)
    ch = p.get("priceChange") or {}
    t1 = (p.get("txns") or {}).get("h1") or {}
    target = mcap * (1 + cfg["take_profit_pct"] / 100)
    stop = mcap * (1 - cfg["stop_loss_pct"] / 100)

    lines = [
        f"🚨 <b>{name} (${sym})</b> passed your filters",
        "",
        f"MC <b>{money(mcap)}</b> | Liq {money(liq)} | Age {age_min}m",
        f"5m {pct(ch.get('m5'))} | 1h {pct(ch.get('h1'))} | 6h {pct(ch.get('h6'))}",
        f"1h txns: {t1.get('buys', 0)} buys / {t1.get('sells', 0)} sells",
        f"RugCheck: {html.escape(rug_summary)}",
    ]
    if copycats:
        lines.append(f"⚠️ {copycats} other coins share this ticker - make sure you buy THIS contract")
    lines += [
        "",
        f"<b>Plan ({cfg['position_sol']} SOL):</b> target MC {money(target)} | "
        f"stop MC {money(stop)} | out in {cfg['time_limit_min']} min",
        "",
        f"<a href=\"{p.get('url', '')}\">DexScreener</a> | "
        f"<a href=\"https://gmgn.ai/sol/token/{mint}\">GMGN (holders/dev)</a> | "
        f"<a href=\"https://rugcheck.xyz/tokens/{mint}\">RugCheck</a> | "
        f"<a href=\"https://jup.ag/swap/SOL-{mint}\">Buy on Jupiter</a>",
        "",
        f"<code>{mint}</code>",
        "Not a guarantee. Check top holders, dev wallet and bundles before buying.",
    ]
    return "\n".join(lines)


def send_telegram(text, cfg=CONFIG):
    token, chat = cfg["telegram_bot_token"], cfg["telegram_chat_id"]
    if "PASTE" in token or "PASTE" in str(chat):
        print("\n--- (Telegram not set up - printing instead) ---\n" + text + "\n")
        return False
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        log(f"Telegram send failed: {e}")
        return False


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def log_alert(p):
    base = p.get("baseToken") or {}
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "name", "symbol", "mint", "mcap_at_alert",
                        "liquidity", "url", "result_after_15min (fill in)"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    base.get("name"), base.get("symbol"), base.get("address"),
                    p.get("marketCap") or p.get("fdv"),
                    (p.get("liquidity") or {}).get("usd"), p.get("url"), ""])


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def scan_once(state, verbose=False):
    mints = latest_solana_mints()
    pairs = pairs_for_mints(mints)
    for term in SEARCH_TERMS:
        try:
            pairs.extend(search_pairs(term))
        except Exception as e:
            log(f"Search '{term}' failed: {e}")
    pairs = best_pair_per_token(pairs)

    # count tickers to spot copycats
    tickers = {}
    for p in pairs:
        s = ((p.get("baseToken") or {}).get("symbol") or "").upper()
        tickers[s] = tickers.get(s, 0) + 1

    now = time.time()
    cooldown = CONFIG["realert_hours"] * 3600
    passed = 0
    for p in pairs:
        base = p.get("baseToken") or {}
        mint = base.get("address")
        if now - state.get(mint, 0) < cooldown:
            continue
        ok, reason = check_pair(p)
        if verbose:
            log(f"{base.get('symbol', '?'):>10}  {'PASS' if ok else 'skip'}  {reason}")
        if not ok:
            continue
        if CONFIG["use_rugcheck"]:
            safe, rug_summary = rugcheck(mint)
            if not safe:
                if verbose:
                    log(f"{base.get('symbol', '?'):>10}  skip  {rug_summary}")
                continue
        else:
            rug_summary = "not checked"
        copies = tickers.get((base.get("symbol") or "").upper(), 1) - 1
        send_telegram(build_message(p, rug_summary, copies))
        log_alert(p)
        state[mint] = now
        passed += 1
        log(f"ALERT sent: {base.get('name')} ({base.get('symbol')})")
    save_state(state)
    log(f"Scanned {len(pairs)} coins, {passed} alert(s).")


def main():
    args = sys.argv[1:]
    if "--test" in args:
        ok = send_telegram("✅ Memecoin scanner connected. You'll get alerts here.")
        print("Test message sent!" if ok else "Test failed - check token/chat id.")
        return
    state = load_state()
    if "--once" in args:
        scan_once(state, verbose=True)
        return
    log("Scanner running. Press Ctrl+C to stop.")
    while True:
        try:
            scan_once(state)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"Scan error: {e}")
        time.sleep(CONFIG["poll_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
