# Solana Memecoin Alert Bots

Three Python bots that watch new Solana memecoins and send alerts to Telegram. I built them while learning to trade memecoins, so I could spot setups and rugs faster.

> Educational project. These bots **only send alerts**. They never trade, and they never touch a wallet or private key.

## The bots

| File | What it does |
|---|---|
| `memecoin_scanner.py` | Scans DexScreener for new Solana pairs that pass liquidity, age, activity and price-change filters. Checks each one on RugCheck and sends a Telegram alert with DexScreener, GMGN, RugCheck and Jupiter links. Logs every alert to CSV. |
| `pump_scanner.py` | Gives each coin a 0–100 "pump score" from volume spikes, buy pressure, momentum, liquidity ratio, age and trending words, with a RugCheck penalty. Sends follow-up reports 15 and 60 minutes after each alert to measure whether the score worked. |
| `trench_bot_pro.py` | Watches brand-new Pump.fun launches live over the PumpPortal websocket. Tracks buy velocity, bundles and bonding-curve progress, checks holders and insiders on RugCheck, and grades alerts A or A+. Learns smart wallets and dumping devs over time, and reports peak multiples. Run with `--stats` to see how its alerts performed. |

## Setup

1. Install Python 3.10 or newer.
2. Install the one extra package (only `trench_bot_pro.py` needs it):

   ```
   python -m pip install -r requirements.txt
   ```

3. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and get your chat ID.
4. Set your bot token and chat ID as environment variables. On Windows (cmd):

   ```
   set TG_BOT_TOKEN=your_token_here
   set TG_CHAT_ID=your_chat_id_here
   ```

   On macOS or Linux:

   ```
   export TG_BOT_TOKEN=your_token_here
   export TG_CHAT_ID=your_chat_id_here
   ```

5. Run a bot:

   ```
   python memecoin_scanner.py --test    # sends one test message
   python pump_scanner.py
   python trench_bot_pro.py
   python trench_bot_pro.py --stats     # how past alerts performed
   ```

## Data sources

- [DexScreener API](https://docs.dexscreener.com/) for pairs, prices and volume
- [RugCheck](https://rugcheck.xyz/) for token risk reports
- [PumpPortal](https://pumpportal.fun/) websocket for new Pump.fun launches
- Telegram Bot API for alerts

## What I learned

- Most alerts are noise. The first overnight test showed 1 in 4 alerts hit 2x, while 3 in 4 dropped more than 50%. Alerts work as a shortlist to check by hand, never as automatic buys.
- Coins with fake volume (thousands of $1 trades), clustered holders or high insider share almost always dump.
- Measuring results (the 15 and 60-minute follow-ups) matters more than the scoring formula itself.

## Roadmap

- Backtester to test scoring rules on historical data
- Dev wallet history and bundle detection with Helius
- Simple dashboard of alert results

## Disclaimer

Not financial advice. Memecoins are extremely risky and most go to zero. Never put a private key or seed phrase in code.
