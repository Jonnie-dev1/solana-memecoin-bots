#!/usr/bin/env python3
"""
TRENCH BOT PRO  -  self-learning Pump.fun launch scanner
========================================================

What it does
------------
* Streams every new Pump.fun launch + every trade on it (PumpPortal free feed).
* Watches each coin for its first ~6 minutes and scores it 0-100 on:
    - unique buyers and buyer VELOCITY (is buying speeding up?)
    - buy vs sell pressure and net SOL flowing in
    - market-cap growth and bonding-curve progress
    - dev behaviour (size of dev buy, did the dev sell?)
    - sniper share, whale share, BUNDLES (same-second identical buys)
    - fake volume (floods of tiny bot buys)
    - socials on the token metadata (X / Telegram / website)
    - SMART WALLETS it has learned (wallets that keep buying early into runners)
    - DEV BLACKLIST it has learned (devs who dumped previous launches)
* Before alerting, pulls a RugCheck report (top holders, insiders, danger flags).
* Alerts on Telegram with a tier (A+ / A), reasons and one-tap links.
* Tracks every alerted coin for 60 minutes (peak and final multiple), reports
  back at 15 and 60 minutes, and logs it all to trench_pro_log.csv.
* Learns over time: the longer it runs, the better its wallet and dev memory.

    python trench_bot_pro.py --test     # Telegram test
    python trench_bot_pro.py            # run
    python trench_bot_pro.py --stats    # how each score tier has performed

Setup once:  python -m pip install websockets
This bot never trades and never needs a private key or seed phrase.
Every alert is a high-risk lottery ticket. Size tiny. Most new coins go to zero.
"""

import asyncio
import csv
import html
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

try:
    import websockets
except ImportError:
    if "--stats" not in sys.argv:
        print("Missing package. In cmd run:  python -m pip install websockets")
        sys.exit(1)

# ==========================================================================
# SETTINGS
# ==========================================================================
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")

ALERT_SCORE = 55            # minimum to alert
A_PLUS_SCORE = 75           # A+ tier
CHECK_AT = [60, 120, 210, 330]   # seconds after launch to score
WATCH_UNALERTED = 360       # stop watching coins that never alerted
TRACK_ALERTED_MIN = 60      # keep tracking alerted coins this long
REPORT_AT_MIN = [15, 60]    # Telegram follow-ups
MAX_WATCHING = 500

MIN_BUYERS = 15             # never score coins with fewer unique buyers
SNIPE_SEC = 3               # buys this soon after launch = snipers
TINY_BUY_SOL = 0.01         # buys smaller than this look like volume bots
HOT_WORDS = ["cat", "dog", "elon", "trump", "ai", "pepe", "frog", "gta", "claude",
             "meme", "moon", "doge", "chad", "wif"]
BAD_WORDS = ["test", "rug", "scam", "fund", "usd", "reserve", "official", "airdrop"]

FEED = "wss://pumpportal.fun/api/data"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN_FILE = os.path.join(HERE, "trench_pro_brain.json")
LOG_FILE = os.path.join(HERE, "trench_pro_log.csv")
UA = {"User-Agent": "trench-bot-pro/1.0", "Accept": "application/json"}
SUPPLY = 1_000_000_000


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_json(url, timeout=8):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None


# ==========================================================================
# BRAIN: learned smart wallets + dev reputation (saved between runs)
# ==========================================================================
class Brain:
    def __init__(self):
        self.wallets = {}   # wallet -> [appearances, hits]
        self.devs = {}      # dev -> [launches, dumps, runners]
        try:
            with open(BRAIN_FILE) as f:
                d = json.load(f)
                self.wallets, self.devs = d.get("wallets", {}), d.get("devs", {})
        except Exception:
            pass

    def save(self):
        if len(self.wallets) > 60000:   # prune weak wallets
            self.wallets = {w: v for w, v in self.wallets.items() if v[1] > 0 or v[0] < 5}
        tmp = BRAIN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"wallets": self.wallets, "devs": self.devs}, f)
        os.replace(tmp, BRAIN_FILE)

    def is_smart(self, w):
        a, h = self.wallets.get(w, (0, 0))
        return h >= 2 and h / max(a, 1) >= 0.25

    def learn(self, coin):
        """Called when we stop watching a coin."""
        runner = coin.peak_x >= 4
        for w in coin.early_buyers:
            v = self.wallets.setdefault(w, [0, 0])
            v[0] += 1
            if runner:
                v[1] += 1
        if coin.dev:
            d = self.devs.setdefault(coin.dev, [0, 0, 0])
            d[0] += 1
            if coin.dev_sold and coin.peak_x < 2:
                d[1] += 1
            if runner:
                d[2] += 1

    def dev_record(self, dev):
        return self.devs.get(dev, [0, 0, 0])

    def stats(self):
        smart = sum(1 for w in self.wallets if self.is_smart(w))
        bad = sum(1 for d in self.devs.values() if d[1] >= 1)
        return smart, bad


# ==========================================================================
# Per-coin tracking
# ==========================================================================
class Coin:
    def __init__(self, ev):
        self.mint = ev.get("mint")
        self.name = str(ev.get("name", "?"))[:40]
        self.symbol = str(ev.get("symbol", "?"))[:15]
        self.dev = ev.get("traderPublicKey")
        self.uri = ev.get("uri")
        self.curve = ev.get("bondingCurveKey")
        self.created = time.time()
        self.start_mc = float(ev.get("marketCapSol") or 0) or 1.0
        self.mc = self.start_mc
        self.peak_mc = self.start_mc
        self.vsol = float(ev.get("vSolInBondingCurve") or 30)
        self.dev_pct = float(ev.get("initialBuy") or 0) / SUPPLY * 100
        self.buyers, self.sellers = set(), set()
        self.early_buyers = set()
        self.snipers = set()
        self.buy_times = []           # (t, wallet)
        self.buys = self.sells = 0
        self.buy_sol = self.sell_sol = 0.0
        self.tiny_buys = 0
        self.sol_by_wallet = defaultdict(float)
        self.same_second = defaultdict(list)   # int second -> [sol amounts]
        self.smart_buyers = set()
        self.dev_sold = False
        self.graduated = False
        self.socials = None
        self.checks = set()
        self.alerted = False
        self.alert = {}
        self.reported = set()
        self.trade_count = 0
        self.ds = None

    @property
    def age(self):
        return time.time() - self.created

    @property
    def peak_x(self):
        return self.peak_mc / self.start_mc

    def on_trade(self, ev, brain):
        kind = ev.get("txType")
        w = ev.get("traderPublicKey")
        sol = float(ev.get("solAmount") or 0)
        mc = float(ev.get("marketCapSol") or 0)
        if mc:
            self.mc = mc
            self.peak_mc = max(self.peak_mc, mc)
        if ev.get("vSolInBondingCurve"):
            self.vsol = float(ev["vSolInBondingCurve"])
        now = time.time()
        self.trade_count += 1
        if kind == "buy":
            self.buys += 1
            self.buy_sol += sol
            if sol < TINY_BUY_SOL:
                self.tiny_buys += 1
            if w and w != self.dev:
                self.buyers.add(w)
                self.sol_by_wallet[w] += sol
                self.buy_times.append((now, w))
                if now - self.created <= 60:
                    self.early_buyers.add(w)
                if now - self.created <= SNIPE_SEC:
                    self.snipers.add(w)
                if now - self.created <= 15:
                    self.same_second[int(now)].append(sol)
                if brain.is_smart(w):
                    self.smart_buyers.add(w)
        elif kind == "sell":
            self.sells += 1
            self.sell_sol += sol
            if w:
                self.sellers.add(w)
            if w and w == self.dev:
                self.dev_sold = True
        elif kind in ("migrate", "migration"):
            self.graduated = True

    def velocity(self):
        now = time.time()
        last = sum(1 for t, _ in self.buy_times if now - t <= 60)
        prev = sum(1 for t, _ in self.buy_times if 60 < now - t <= 120)
        return last, prev

    def bundle_wallets(self):
        n = 0
        for amounts in self.same_second.values():
            if len(amounts) >= 4:
                amounts = sorted(amounts)
                med = amounts[len(amounts) // 2] or 1e-9
                n += sum(1 for a in amounts if abs(a - med) / med <= 0.05)
        return n

    def curve_progress(self):
        # approx: virtual SOL starts ~30 and the coin graduates near ~115
        return max(0.0, min(1.0, (self.vsol - 30) / 85))


# ==========================================================================
# Scoring
# ==========================================================================
def score(c, brain):
    name = f"{c.name} {c.symbol}".lower()
    if any(w in name for w in BAD_WORDS):
        return None, ["bad name"]
    if c.dev_sold:
        return None, ["dev sold"]
    launches, dumps, runners = brain.dev_record(c.dev)
    if dumps >= 2 or (dumps >= 1 and runners == 0 and launches >= 3):
        return None, [f"blacklisted dev ({dumps} dumps)"]
    nb, ns = len(c.buyers), len(c.sellers)
    if nb < MIN_BUYERS:
        return None, [f"{nb} buyers"]

    s, why = 0.0, []

    # Buyers (max 15) + velocity (max 15)
    s += min(15, 5 + (nb - MIN_BUYERS) * 0.25)
    last, prev = c.velocity()
    if last >= 8 and last > prev * 1.2:
        s += min(15, (last / max(prev, 1)) * 5)
        why.append(f"buying accelerating ({prev}→{last} buyers/min)")
    elif last < prev * 0.5:
        s -= 8
        why.append("buying slowing down")
    why.insert(0, f"{nb} unique buyers")

    # Pressure (max 15)
    ratio = nb / max(ns, 1)
    if ratio >= 1.5:
        s += min(15, (ratio - 1) * 8)
        why.append(f"buyers:sellers {nb}:{ns}")
    elif ratio < 1:
        s -= 10
        why.append("sellers outnumber buyers")
    net = c.buy_sol - c.sell_sol
    if net > 0:
        s += min(8, net)

    # Growth + curve (max 12)
    g = c.mc / c.start_mc
    if g > 1.3:
        s += min(8, (g - 1) * 3)
        why.append(f"mcap x{g:.1f} since launch")
    prog = c.curve_progress()
    if prog >= 0.4:
        s += 4
        why.append(f"bonding curve ~{prog:.0%}")
    if c.mc < c.peak_mc * 0.6:
        s -= 10
        why.append(f"down {1 - c.mc / c.peak_mc:.0%} from peak")

    # Dev (max 6)
    if c.dev_pct <= 5:
        s += 6
    elif c.dev_pct > 15:
        s -= 15
        why.append(f"dev bought {c.dev_pct:.0f}%")
    if runners >= 1:
        s += 6
        why.append(f"dev had {runners} runner(s) before")

    # Distribution (max 10, penalties)
    top = max(c.sol_by_wallet.values(), default=0)
    whale = top / c.buy_sol if c.buy_sol else 0
    snipe = len(c.snipers) / max(nb, 1)
    bundles = c.bundle_wallets()
    if whale < 0.12 and snipe < 0.2 and bundles < 4:
        s += 10
        why.append("clean distribution")
    if whale > 0.3:
        s -= 15
        why.append(f"one wallet = {whale:.0%} of buys")
    if snipe > 0.35:
        s -= 12
        why.append(f"{len(c.snipers)} snipers")
    if bundles >= 6:
        s -= 15
        why.append(f"~{bundles} bundled buys")

    # Fake volume
    if c.buys >= 30 and c.tiny_buys / c.buys > 0.6:
        s -= 15
        why.append("flood of tiny buys (volume bot)")

    # Smart wallets (max 15)
    if c.smart_buyers:
        s += min(15, 7 * len(c.smart_buyers))
        why.append(f"🧠 {len(c.smart_buyers)} smart wallet(s) bought")

    # Socials + narrative (max 8)
    if c.socials:
        s += min(5, 2 * len(c.socials))
        why.append("has " + "/".join(sorted(c.socials)))
    if any(w in name for w in HOT_WORDS):
        s += 3
        why.append("hot narrative")

    return max(0, min(100, round(s))), why


def score_ds(c, brain):
    """Backup scoring from DexScreener stats when the live trade stream is quiet."""
    p = c.ds or {}
    name = f"{c.name} {c.symbol}".lower()
    if any(w in name for w in BAD_WORDS):
        return None, ["bad name"]
    launches, dumps, runners = brain.dev_record(c.dev)
    if dumps >= 2 or (dumps >= 1 and runners == 0 and launches >= 3):
        return None, [f"blacklisted dev ({dumps} dumps)"]
    tx = p.get("txns") or {}
    b1, s1 = (tx.get("h1") or {}).get("buys", 0) or 0, (tx.get("h1") or {}).get("sells", 0) or 0
    b5, s5 = (tx.get("m5") or {}).get("buys", 0) or 0, (tx.get("m5") or {}).get("sells", 0) or 0
    if b1 < 25:
        return None, [f"{b1} buys"]
    vol1 = float((p.get("volume") or {}).get("h1") or 0)
    ch5 = float((p.get("priceChange") or {}).get("m5") or 0)
    mc_usd = float(p.get("marketCap") or p.get("fdv") or 0)
    s, why = 0.0, [f"{b1} buys / {s1} sells (DexScreener data)"]
    s += min(20, 5 + (b1 - 25) * 0.2)
    ratio = b1 / max(s1, 1)
    if ratio >= 1.4:
        s += min(15, (ratio - 1) * 10)
    elif ratio < 1:
        s -= 10
        why.append("more sells than buys")
    if b5 >= 8 and b5 > s5 * 1.3:
        s += min(15, b5 * 0.6)
        why.append(f"last 5m: {b5} buys vs {s5} sells")
    elif b5 < s5:
        s -= 8
        why.append("selling in last 5m")
    if 3 <= ch5 <= 80:
        s += min(12, ch5 * 0.4 + 4)
        why.append(f"+{ch5:.0f}% in 5m")
    elif ch5 > 150:
        s -= 10
        why.append(f"already spiked +{ch5:.0f}% in 5m")
    elif ch5 < -25:
        s -= 10
        why.append(f"{ch5:.0f}% in 5m")
    price = sol_price()
    start_usd = c.start_mc * price if price else 0
    if start_usd and mc_usd > start_usd * 1.5:
        s += min(10, (mc_usd / start_usd - 1) * 3)
        why.append(f"mcap x{mc_usd / start_usd:.1f} since launch")
    avg = vol1 / max(b1 + s1, 1)
    if avg < 3 and b1 > 40:
        s -= 15
        why.append("tiny average trade (volume bot?)")
    elif avg > 600:
        s -= 10
        why.append("huge average trade (wash/whale?)")
    else:
        s += 8
    if c.dev_pct <= 5:
        s += 6
    elif c.dev_pct > 15:
        s -= 15
        why.append(f"dev bought {c.dev_pct:.0f}%")
    if c.socials:
        s += min(5, 2 * len(c.socials))
        why.append("has " + "/".join(sorted(c.socials)))
    if any(w in name for w in HOT_WORDS):
        s += 3
        why.append("hot narrative")
    return max(0, min(100, round(s))), why


def fetch_socials(uri):
    if not uri:
        return set()
    if uri.startswith("ipfs://"):
        uri = "https://ipfs.io/ipfs/" + uri[7:]
    meta = get_json(uri, timeout=5) or {}
    found = set()
    for k, label in (("twitter", "X"), ("telegram", "TG"), ("website", "web")):
        v = meta.get(k) or (meta.get("extensions") or {}).get(k)
        if v and isinstance(v, str) and len(v) > 5:
            found.add(label)
    return found


def rugcheck(c):
    """Return (ok, penalty, text)."""
    d = get_json(RUGCHECK.format(mint=c.mint), timeout=8)
    if not d:
        return True, 0, "RugCheck: not indexed yet - check GMGN"
    danger = [r.get("name", "?") for r in d.get("risks") or [] if (r.get("level") or "").lower() == "danger"]
    if danger:
        return False, 0, "DANGER: " + ", ".join(danger[:3])
    holders = [h for h in d.get("topHolders") or [] if h.get("address") not in (c.curve, None)
               and h.get("owner") != c.curve]
    top10 = sum(float(h.get("pct") or 0) for h in holders[:10])
    insiders = sum(1 for h in holders if h.get("insider"))
    pen, bits = 0, []
    if top10:
        bits.append(f"top10 {top10:.0f}%")
        if top10 > 35:
            pen += 15
    if insiders:
        bits.append(f"{insiders} insider wallets")
        pen += 5 * min(insiders, 3)
    gi = d.get("graphInsidersDetected")
    if gi:
        bits.append(f"{gi} linked insiders")
        pen += 10
    return True, pen, "RugCheck: " + (", ".join(bits) if bits else "no flags")


# ==========================================================================
# Telegram + logging
# ==========================================================================
SOL_USD = {"p": 0.0, "t": 0.0}


def sol_price():
    if SOL_USD["p"] and time.time() - SOL_USD["t"] < 600:
        return SOL_USD["p"]
    pairs = get_json("https://api.dexscreener.com/tokens/v1/solana/So11111111111111111111111111111111111111112") or []
    ps = sorted(float(p["priceUsd"]) for p in pairs if isinstance(p, dict) and p.get("priceUsd")
                and (p.get("baseToken") or {}).get("symbol", "").upper() in ("SOL", "WSOL"))
    if ps:
        SOL_USD.update(p=ps[len(ps) // 2], t=time.time())
    return SOL_USD["p"]


def usd(mc_sol):
    p = sol_price()
    if not p:
        return f"{mc_sol:.0f} SOL"
    v = mc_sol * p
    return f"${v/1e3:.1f}K" if v < 1e6 else f"${v/1e6:.2f}M"


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


def links(mint):
    return (f"👉 <a href=\"https://dexscreener.com/solana/{mint}\"><b>OPEN ON DEXSCREENER</b></a>\n"
            f"<a href=\"https://gmgn.ai/sol/token/{mint}\">GMGN</a> | "
            f"<a href=\"https://rugcheck.xyz/tokens/{mint}\">RugCheck</a> | "
            f"<a href=\"https://solscan.io/token/{mint}\">Solscan</a>")


def alert_msg(c, sc, why, rug_txt):
    tier = "🏆 A+" if sc >= A_PLUS_SCORE else "⭐ A"
    a = int(c.age)
    return "\n".join([
        f"⛏️ <b>TRENCH {tier} | {sc}/100</b>",
        f"<b>{html.escape(c.name)} (${html.escape(c.symbol)})</b> | {a // 60}m{a % 60:02d}s old",
        f"MC <b>{usd(c.mc)}</b> | buys {c.buys} / sells {c.sells} | curve ~{c.curve_progress():.0%}",
        "",
        *[f"• {html.escape(w)}" for w in why[:8]],
        f"• {html.escape(rug_txt)}",
        "",
        f"Plan: 0.02-0.03 SOL | sell half at {usd(c.mc * 2)} (2x) | out below {usd(c.mc * 0.7)}",
        links(c.mint),
        f"<code>{c.mint}</code>",
    ])


def write_log(c):
    new = not os.path.exists(LOG_FILE)
    a = c.alert
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "symbol", "mint", "score", "tier", "mc_alert_sol",
                        "peak_x_after_alert", "final_x_after_alert", "dev_sold", "smart_wallets"])
        w.writerow([a["time"], c.symbol, c.mint, a["score"], "A+" if a["score"] >= A_PLUS_SCORE else "A",
                    round(a["mc"], 2), round(a["peak"] / a["mc"], 2), round(c.mc / a["mc"], 2),
                    c.dev_sold, len(c.smart_buyers)])


def show_stats():
    if not os.path.exists(LOG_FILE):
        print("No results yet - let the bot run for a while first.")
        return
    rows = list(csv.DictReader(open(LOG_FILE)))
    print(f"\n{len(rows)} alerts tracked for 60 minutes\n")
    for tier in ("A+", "A"):
        r = [x for x in rows if x["tier"] == tier]
        if not r:
            continue
        peaks = [float(x["peak_x_after_alert"]) for x in r]
        finals = [float(x["final_x_after_alert"]) for x in r]
        print(f"{tier:>3}: {len(r)} alerts | hit 2x: {sum(p >= 2 for p in peaks) / len(r):.0%} | "
              f"hit 1.5x: {sum(p >= 1.5 for p in peaks) / len(r):.0%} | "
              f"median peak x{statistics.median(peaks):.2f} | median after 60m x{statistics.median(finals):.2f} | "
              f"died (<0.5x): {sum(f < 0.5 for f in finals) / len(r):.0%}")
    b = Brain()
    print(f"\nBrain: {b.stats()[0]} smart wallets learned, {b.stats()[1]} dumping devs remembered\n")


# ==========================================================================
# Engine
# ==========================================================================
class Engine:
    def __init__(self):
        self.brain = Brain()
        self.coins = {}
        self.ws = None
        self.trades = 0
        self.best = (0, "-")
        self.last_save = time.time()
        self.server_msgs = set()
        self.ds_hits = 0

    async def send(self, msg):
        try:
            await self.ws.send(json.dumps(msg))
        except Exception:
            pass

    async def on_event(self, ev):
        if "txType" not in ev and not ev.get("mint"):
            txt = str(ev)[:200]
            if txt not in self.server_msgs and len(self.server_msgs) < 10:
                self.server_msgs.add(txt)
                log(f"FEED SAYS: {txt}")
            return
        mint = ev.get("mint")
        if ev.get("txType") == "create" and mint:
            if len(self.coins) < MAX_WATCHING:
                self.coins[mint] = Coin(ev)
                await self.send({"method": "subscribeTokenTrade", "keys": [mint]})
        elif mint in self.coins:
            self.trades += 1
            self.coins[mint].on_trade(ev, self.brain)

    async def evaluate(self, c):
        use_ds = len(c.buyers) < MIN_BUYERS and c.ds is not None
        if c.socials is None and (len(c.buyers) >= MIN_BUYERS or use_ds):
            c.socials = await asyncio.to_thread(fetch_socials, c.uri)
        sc, why = score_ds(c, self.brain) if use_ds else score(c, self.brain)
        if sc is None:
            return
        if sc > self.best[0]:
            self.best = (sc, c.symbol)
        if sc < ALERT_SCORE:
            return
        ok, pen, rug_txt = await asyncio.to_thread(rugcheck, c)
        if not ok:
            log(f"{c.symbol} score {sc} blocked - {rug_txt}")
            return
        final = sc - pen
        if final < ALERT_SCORE:
            log(f"{c.symbol} score {sc} -> {final} after RugCheck ({rug_txt})")
            return
        c.alerted = True
        c.alert = {"score": final, "mc": c.mc, "peak": c.mc,
                   "time": datetime.now().isoformat(timespec="seconds"), "at": time.time()}
        await asyncio.to_thread(tg, alert_msg(c, final, why, rug_txt))
        log(f"ALERT {c.symbol} {final}/100")

    async def ds_poll(self):
        while True:
            await asyncio.sleep(15)
            want = [c for c in self.coins.values() if c.trade_count < 5 and
                    (c.alerted or 40 <= c.age <= WATCH_UNALERTED)]
            for i in range(0, len(want), 30):
                batch = want[i:i + 30]
                url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(c.mint for c in batch)
                pairs = await asyncio.to_thread(get_json, url, 10) or []
                by_mint = {}
                for p in pairs if isinstance(pairs, list) else []:
                    m = (p.get("baseToken") or {}).get("address")
                    if m and (m not in by_mint or float((p.get("liquidity") or {}).get("usd") or 0) >
                              float((by_mint[m].get("liquidity") or {}).get("usd") or 0)):
                        by_mint[m] = p
                price = await asyncio.to_thread(sol_price)
                for c in batch:
                    p = by_mint.get(c.mint)
                    if not p:
                        continue
                    c.ds = p
                    self.ds_hits += 1
                    mc_usd = float(p.get("marketCap") or p.get("fdv") or 0)
                    if mc_usd and price:
                        c.mc = mc_usd / price
                        c.peak_mc = max(c.peak_mc, c.mc)

    async def tick(self):
        while True:
            await asyncio.sleep(3)
            drop = []
            for mint, c in list(self.coins.items()):
                if not c.alerted:
                    for t in CHECK_AT:
                        if c.age >= t and t not in c.checks:
                            c.checks.add(t)
                            await self.evaluate(c)
                            break
                    if not c.alerted and c.age >= WATCH_UNALERTED:
                        drop.append(mint)
                else:
                    c.alert["peak"] = max(c.alert["peak"], c.mc)
                    mins = (time.time() - c.alert["at"]) / 60
                    for m in REPORT_AT_MIN:
                        if mins >= m and m not in c.reported:
                            c.reported.add(m)
                            x = c.mc / c.alert["mc"]
                            px = c.alert["peak"] / c.alert["mc"]
                            icon = "🚀" if px >= 2 else "✅" if x > 1 else "🔻"
                            await asyncio.to_thread(tg,
                                f"{icon} <b>{html.escape(c.symbol)}</b> {m}m after alert "
                                f"({c.alert['score']}/100): now x{x:.2f}, peak x{px:.2f}"
                                + (" | dev sold" if c.dev_sold else "")
                                + (" | 🎓 graduated" if c.graduated else "")
                                + f"\n<a href=\"https://dexscreener.com/solana/{mint}\">DexScreener</a>")
                    if mins >= TRACK_ALERTED_MIN:
                        write_log(c)
                        drop.append(mint)
            for m in drop:
                c = self.coins.pop(m, None)
                if c:
                    self.brain.learn(c)
            if drop:
                await self.send({"method": "unsubscribeTokenTrade", "keys": drop})
            if time.time() - self.last_save > 120:
                await asyncio.to_thread(self.brain.save)
                self.last_save = time.time()
            smart, bad = self.brain.stats()
            log(f"watching {len(self.coins)} | trades {self.trades} | dexscreener {self.ds_hits} | best {self.best[0]} ({self.best[1]}) "
                f"| brain: {smart} smart wallets, {bad} bad devs")

    async def run(self):
        while True:
            try:
                async with websockets.connect(FEED, ping_interval=20, max_size=2**22) as ws:
                    self.ws = ws
                    await self.send({"method": "subscribeNewToken"})
                    await self.send({"method": "subscribeMigration"})
                    if self.coins:
                        await self.send({"method": "subscribeTokenTrade", "keys": list(self.coins)})
                    log("Connected to live Pump.fun feed. Ctrl+C to stop.")
                    ticker = asyncio.create_task(self.tick())
                    poller = asyncio.create_task(self.ds_poll())
                    try:
                        async for raw in ws:
                            try:
                                await self.on_event(json.loads(raw))
                            except Exception:
                                continue
                    finally:
                        ticker.cancel()
                        poller.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"Connection lost ({e}). Reconnecting in 5s...")
                await asyncio.sleep(5)


if __name__ == "__main__":
    if "--stats" in sys.argv:
        show_stats()
    elif "--test" in sys.argv:
        print("Sent!" if tg("⛏️ Trench Bot PRO connected.") else "Failed - check token/chat id")
    else:
        eng = Engine()
        try:
            asyncio.run(eng.run())
        except KeyboardInterrupt:
            eng.brain.save()
            print("\nStopped. Brain saved.")
