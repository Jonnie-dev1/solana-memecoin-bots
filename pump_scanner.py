#!/usr/bin/env python3
"""
Pump Scanner (experimental) - scores Solana coins 0-100 on "pre-pump" signals
=============================================================================

Just for messing about / learning. It does NOT predict the future. It scores
signals that often show up before a move, alerts you on Telegram when a coin
scores high, then checks back 15 and 60 minutes later and tells you what
actually happened - so you can see if the score is any good.

Run (from the folder this file is in):
    python pump_scanner.py --test     # Telegram test message
    python pump_scanner.py --once     # one scan, prints every coin's score
    python pump_scanner.py            # run all day

Results are saved to pump_scores_log.csv (score + how it did after 15/60 min).
Never put a seed phrase or private key in this file - it never trades.
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
from datetime import datetime

# --------------------------------------------------------------------------
# SETTINGS
# --------------------------------------------------------------------------
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")

POLL_SECONDS = 10
ALERT_SCORE = 50            # alert when score >= this (raise to get fewer alerts)
REALERT_HOURS = 3
FOLLOW_UP_MINUTES = [15, 60]

# Hard safety filters - coins failing these are never scored
MIN_LIQ = 10_000
MIN_MCAP, MAX_MCAP = 30_000, 3_000_000
MIN_AGE_MIN, MAX_AGE_H = 10, 48
MAX_AVG_TRADE_USD = 700     # bigger average trades = likely wash bots

BAD_NAME_WORDS = ["fund", "reserve", "trust", "dividend", "oil", "treasury",
                  "etf", "xstock", "exchange", "bank", "usd", "stable"]
HOT_WORDS = ["cat", "dog", "elon", "trump", "ai", "pepe", "frog", "gta", "dacity"]
SEARCH_TERMS = ["cat", "dog", "trump", "elon", "ai", "pepe", "frog", "meme", "gta"]

# --------------------------------------------------------------------------
DEX = "https://api.dexscreener.com"
FEEDS = [f"{DEX}/token-profiles/latest/v1", f"{DEX}/token-boosts/latest/v1",
         f"{DEX}/token-boosts/top/v1"]
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "pump_state.json")
LOG_FILE = os.path.join(HERE, "pump_scores_log.csv")
UA = {"User-Agent": "pump-scanner/1.0", "Accept": "application/json"}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
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
    return f"${x/1e6:.2f}M" if x >= 1e6 else f"${x/1e3:.0f}K" if x >= 1e3 else f"${x:.0f}"


def num(d, *keys):
    for k in keys:
        d = (d or {}).get(k)
    return float(d or 0)


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------
def collect_pairs():
    mints = []
    for url in FEEDS:
        try:
            data = get_json(url) or []
            for item in (data if isinstance(data, list) else [data]):
                if item.get("chainId") == "solana" and item.get("tokenAddress"):
                    mints.append(item["tokenAddress"])
        except Exception as e:
            log(f"feed error: {e}")
    pairs = pairs_for(list(dict.fromkeys(mints)))
    for term in SEARCH_TERMS:
        try:
            data = get_json(f"{DEX}/latest/dex/search?q={urllib.parse.quote(term)}") or {}
            pairs += [p for p in data.get("pairs") or [] if p.get("chainId") == "solana"]
        except Exception as e:
            log(f"search error '{term}': {e}")
    best = {}
    for p in pairs:
        if (p.get("quoteToken") or {}).get("symbol", "").upper() not in ("SOL", "WSOL"):
            continue
        mint = (p.get("baseToken") or {}).get("address")
        if mint and num(p, "liquidity", "usd") > num(best.get(mint), "liquidity", "usd"):
            best[mint] = p
    return list(best.values())


def pairs_for(mints):
    out = []
    for i in range(0, len(mints), 30):
        try:
            data = get_json(f"{DEX}/tokens/v1/solana/{','.join(mints[i:i+30])}") or []
            out += data if isinstance(data, list) else []
        except Exception as e:
            log(f"pair lookup error: {e}")
    return out


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def hard_filter(p, now_ms):
    base = p.get("baseToken") or {}
    name = f"{base.get('name','')} {base.get('symbol','')}".lower()
    if any(w in name for w in BAD_NAME_WORDS):
        return "fake-fund style name"
    liq, mcap = num(p, "liquidity", "usd"), float(p.get("marketCap") or p.get("fdv") or 0)
    age_min = (now_ms - (p.get("pairCreatedAt") or now_ms)) / 60000
    tx1h = num(p, "txns", "h1", "buys") + num(p, "txns", "h1", "sells")
    if liq < MIN_LIQ:
        return "liquidity too low"
    if not MIN_MCAP <= mcap <= MAX_MCAP:
        return "mcap out of range"
    if not MIN_AGE_MIN <= age_min <= MAX_AGE_H * 60:
        return "age out of range"
    if tx1h < 30:
        return "barely trading"
    if mcap and liq / mcap > 1.2:
        return "weird liquidity data"
    if num(p, "volume", "h1") / max(tx1h, 1) > MAX_AVG_TRADE_USD:
        return "looks like wash trading"
    return None


def score_pair(p, now_ms):
    """Return (score 0-100, list of reasons)."""
    s, why = 0.0, []
    vol5, vol1h = num(p, "volume", "m5"), num(p, "volume", "h1")
    b5, s5 = num(p, "txns", "m5", "buys"), num(p, "txns", "m5", "sells")
    ch5, ch1, ch6 = num(p, "priceChange", "m5"), num(p, "priceChange", "h1"), num(p, "priceChange", "h6")
    liq = num(p, "liquidity", "usd")
    mcap = float(p.get("marketCap") or p.get("fdv") or 0)
    age_min = (now_ms - (p.get("pairCreatedAt") or now_ms)) / 60000
    name = f"{(p.get('baseToken') or {}).get('name','')} {(p.get('baseToken') or {}).get('symbol','')}".lower()

    # 1. Volume spike: last 5m vs average 5m of the last hour (max 25)
    avg5 = vol1h / 12 if vol1h else 0
    if avg5 > 0 and vol5 > avg5:
        ratio = vol5 / avg5
        pts = min(25, (ratio - 1) * 10)
        s += pts
        if pts >= 5:
            why.append(f"volume spike x{ratio:.1f}")

    # 2. Buy pressure in last 5m (max 20)
    if b5 + s5 >= 15:
        share = b5 / (b5 + s5)
        if share > 0.5:
            pts = min(20, (share - 0.5) * 80)
            s += pts
            if pts >= 5:
                why.append(f"buyers {int(b5)} vs sellers {int(s5)} (5m)")

    # 3. Fresh 5m momentum - rising, but not a vertical candle (max 15)
    if 2 <= ch5 <= 25:
        s += min(15, ch5 * 1.2)
        why.append(f"+{ch5:.0f}% in 5m")
    elif ch5 > 40:
        s -= 10
        why.append(f"already spiking +{ch5:.0f}% in 5m (chasing risk)")

    # 4. 1h trend positive (max 10)
    if 5 <= ch1 <= 150:
        s += 10
        why.append(f"+{ch1:.0f}% over 1h")
    elif ch1 < -20:
        s -= 5

    # 5. Healthy liquidity for its size (max 10)
    ratio = liq / mcap if mcap else 0
    if 0.15 <= ratio <= 0.6:
        s += 10
        why.append(f"liquidity {ratio:.0%} of mcap")
    elif ratio < 0.08:
        s -= 5

    # 6. Room to run - hasn't already gone parabolic (max 10)
    if ch6 < 300:
        s += 10
    elif ch6 > 1000:
        s -= 10
        why.append(f"already +{ch6:.0f}% in 6h")

    # 7. Sweet-spot age (max 5) and narrative (max 5)
    if 20 <= age_min <= 360:
        s += 5
    if any(w in name for w in HOT_WORDS):
        s += 5
        why.append("matches a hot narrative")

    return max(0, min(100, round(s))), why


def rugcheck(mint):
    try:
        data = get_json(RUGCHECK.format(mint=mint))
    except Exception:
        data = None
    if not data:
        return 0, "RugCheck unavailable - check manually", True
    risks = data.get("risks") or []
    danger = [r.get("name", "?") for r in risks if (r.get("level") or "").lower() == "danger"]
    warn = [r.get("name", "?") for r in risks if (r.get("level") or "").lower() == "warn"]
    if danger:
        return 0, "DANGER: " + ", ".join(danger), False
    penalty = min(15, 5 * len(warn))
    txt = "no flags" if not warn else "warnings: " + ", ".join(warn[:3])
    return penalty, txt, True


# --------------------------------------------------------------------------
# Telegram + logging
# --------------------------------------------------------------------------
def tg(text):
    body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                     data=body, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        log(f"Telegram error: {e}")
        print(text)
        return False


def bar(score):
    filled = score // 10
    return "🟩" * filled + "⬜" * (10 - filled)


def alert_text(p, score, why, rug_txt):
    b = p.get("baseToken") or {}
    mint = b.get("address", "")
    mcap = float(p.get("marketCap") or p.get("fdv") or 0)
    age = int((time.time() * 1000 - (p.get("pairCreatedAt") or 0)) / 60000)
    lines = [
        f"🔮 <b>PUMP SCORE {score}/100</b>  {bar(score)}",
        f"<b>{html.escape(b.get('name','?'))} (${html.escape(b.get('symbol','?'))})</b>",
        f"MC {money(mcap)} | Liq {money(num(p,'liquidity','usd'))} | Age {age}m",
        "",
        "<b>Why:</b>",
    ] + [f"• {html.escape(w)}" for w in why[:6]] + [
        f"• RugCheck: {html.escape(rug_txt)}",
        "",
        f"Plan: target {money(mcap*1.3)}-{money(mcap*1.5)} | stop {money(mcap*0.8)} | 15 min",
        f"<a href=\"{p.get('url','')}\">DexScreener</a> | "
        f"<a href=\"https://gmgn.ai/sol/token/{mint}\">GMGN</a> | "
        f"<a href=\"https://rugcheck.xyz/tokens/{mint}\">RugCheck</a>",
        f"<code>{mint}</code>",
        "Experimental score, not a prediction. I'll report back in 15 and 60 min.",
    ]
    return "\n".join(lines)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"alerted": {}, "pending": []}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def write_log(row):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["alert_time", "name", "symbol", "mint", "score", "mcap_at_alert",
                        "minutes_after", "mcap_after", "change_pct"])
        w.writerow(row)


def follow_ups(st):
    """Check coins we alerted earlier and report how they did."""
    now = time.time()
    due = [x for x in st["pending"] if now >= x["due"]]
    if not due:
        return
    pairs = {(p.get("baseToken") or {}).get("address"): p
             for p in pairs_for(list({x["mint"] for x in due}))}
    for x in due:
        st["pending"].remove(x)
        p = pairs.get(x["mint"])
        mcap_now = float((p or {}).get("marketCap") or (p or {}).get("fdv") or 0)
        change = (mcap_now / x["mcap"] - 1) * 100 if x["mcap"] and mcap_now else -100
        icon = "🚀" if change >= 30 else "✅" if change > 0 else "🔻"
        tg(f"{icon} <b>{html.escape(x['symbol'])}</b> {x['minutes']} min after alert "
           f"(score {x['score']}): {money(x['mcap'])} → {money(mcap_now)} "
           f"(<b>{change:+.0f}%</b>)")
        write_log([x["time"], x["name"], x["symbol"], x["mint"], x["score"], x["mcap"],
                   x["minutes"], mcap_now, f"{change:.1f}"])


def scan(st, verbose=False):
    now_ms = time.time() * 1000
    pairs = collect_pairs()
    scored = []
    for p in pairs:
        reason = hard_filter(p, now_ms)
        sym = (p.get("baseToken") or {}).get("symbol", "?")
        if reason:
            if verbose:
                log(f"{sym:>10}  --  {reason}")
            continue
        score, why = score_pair(p, now_ms)
        scored.append((score, p, why))
        if verbose:
            log(f"{sym:>10}  {score:>3}  {', '.join(why[:3])}")

    alerts = 0
    for score, p, why in sorted(scored, key=lambda t: -t[0]):
        if score < ALERT_SCORE:
            break
        b = p.get("baseToken") or {}
        mint = b.get("address")
        sym = b.get("symbol", "?")
        if time.time() - st["alerted"].get(mint, 0) < REALERT_HOURS * 3600:
            log(f"{sym} score {score} - already alerted recently, skipping")
            continue
        penalty, rug_txt, safe = rugcheck(mint)
        if not safe:
            log(f"{sym} score {score} - blocked by RugCheck ({rug_txt})")
            continue
        final = score - penalty
        if final < ALERT_SCORE:
            log(f"{sym} score {score} -> {final} after RugCheck warnings ({rug_txt})")
            continue
        if not tg(alert_text(p, final, why, rug_txt)):
            log(f"{sym} score {final} - Telegram send FAILED")
        st["alerted"][mint] = time.time()
        mcap = float(p.get("marketCap") or p.get("fdv") or 0)
        for m in FOLLOW_UP_MINUTES:
            st["pending"].append({"mint": mint, "symbol": b.get("symbol", "?"),
                                  "name": b.get("name", "?"), "score": final, "mcap": mcap,
                                  "minutes": m, "due": time.time() + m * 60,
                                  "time": datetime.now().isoformat(timespec="seconds")})
        alerts += 1
        log(f"ALERT {b.get('symbol')} score {final}")
    top = max((s for s, _, _ in scored), default=0)
    log(f"Scored {len(scored)} of {len(pairs)} coins | top score {top} | {alerts} alert(s)")


def main():
    if "--test" in sys.argv:
        print("Sent!" if tg("🔮 Pump scanner connected.") else "Failed - check token/chat id")
        return
    st = load_state()
    if "--once" in sys.argv:
        scan(st, verbose=True)
        save_state(st)
        return
    log("Pump scanner running. Ctrl+C to stop.")
    while True:
        try:
            follow_ups(st)
            scan(st)
            save_state(st)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"error: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
