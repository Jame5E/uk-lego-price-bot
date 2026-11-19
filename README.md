# UK LEGO Price Tracker Discord Bot (GBP)

A Discord bot that monitors your favourite LEGO sets/pieces in **GBP (£)** across UK retailers (Amazon UK, Argos, LEGO.com UK, Smyths Toys, John Lewis, BrickLink UK), factors in **UK shipping**, pings you in your chosen Discord channel when a deal hits your target, and gives a **Buy Now vs. Wait** forecast based on discount depth, price history, and retirement status.

## Features

- **Channel Ping Alerts:** Pings `<@YOUR_USER_ID>` in your chosen channel with a rich embed.
- **UK Shipping-Aware (£):** Estimates delivery fees per retailer (e.g., Argos £3.95 or free Click & Collect, Amazon UK free over £35 / Prime, LEGO.com UK free over £50, Smyths free over £20).
- **Drop More vs. Go Back Up Forecast:** Compares the total landed price against RRP, tracked historical low, 90-day average, and whether the set is retiring soon.
- **Slash Commands:**
  - `/track` — Add a LEGO set or piece to your wishlist
  - `/wishlist` — View all tracked items and latest prices
  - `/check` — Run an immediate price check
  - `/history` — View recorded price history in £
  - `/remove` — Remove an item from your wishlist
  - `/simulate_sale` — Send a test alert ping to preview the embed

## Setup

### 1. Create the Discord Bot
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Open the **Bot** tab → click **Reset Token** and copy your bot token.
3. Open **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `View Channel`, `Read Message History` (to edit its status message), and optionally `Manage Messages` (to pin it)
4. Open the generated URL to invite the bot to your Discord server.
5. In Discord, enable **Developer Mode** (User Settings → Advanced), then right-click your target channel → **Copy Channel ID**, and right-click your username → **Copy User ID**.

### 2. Configure `.env`
```bash
cp .env.example .env
```
Fill in `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, and `DISCORD_USER_ID`.

### 3. Install & Run (24/7 Bot Mode)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

## Free Hosting Options (24/7)
1. **Oracle Cloud Always Free Tier** (London `uk-london-1` region available): Gives you a free always-on Linux VM (requires card verification, £0/month).
2. **Google Cloud `e2-micro` Free Tier**: Free always-on small Linux VM (note: free tier applies to specific US regions).
3. **Old Laptop / Raspberry Pi at Home**: £0/month, runs `python run.py` in `tmux` or `systemd`.
4. **Koyeb (Free Nano Service)**: Good for lightweight bots if you want a managed PaaS without a VM.
