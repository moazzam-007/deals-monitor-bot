# 🛒 Deals Monitor Bot

A Pyrogram-based Telegram userbot that monitors multiple deal channels, extracts product links, and reposts them with affiliate links via a Wishlink integration powered by an n8n workflow.

## Architecture

```
┌───────────────────────────────────┐
│  Pyrogram Userbot (Render)        │
│                                   │
│  • Monitor 86+ deal channels      │
│  • Extract URLs from messages     │
│  • Resolve shortened URLs         │
│  • Extract product IDs            │
│  • POST to n8n webhook            │
│  • Receive affiliate links back   │
│  • Repost with photo + new links  │
└────────────┬──────────────────────┘
             │ HTTP POST (sync)
             ▼
┌───────────────────────────────────┐
│  n8n Workflow                     │
│                                   │
│  • Receive webhook                │
│  • Duplicate check (Google Sheet) │
│  • Wishlink API → affiliate link  │
│  • Log to Google Sheet            │
│  • Respond with affiliate links   │
└───────────────────────────────────┘
```

## Supported Platforms

| Platform  | Shortened Domain | Product ID Format     |
|-----------|------------------|-----------------------|
| Amazon    | amzn.to, a.co    | ASIN (10 chars)       |
| Flipkart  | fkrt.it, fkrt.cc | /p/ITEM_ID            |
| Myntra    | myntr.it         | Numeric ID from path  |
| AJIO      | —                | /p/PRODUCT_CODE       |
| Meesho    | —                | /product-name/p/ID    |
| Others    | bittli.in, etc.  | URL hash (fallback)   |

## Setup

### 1. Generate Session String

Run this **once** on your local machine:

```bash
pip install pyrogram tgcrypto
python generate_session.py
```

Enter your phone number and OTP when prompted. Copy the session string.

### 2. Get Channel IDs

To find a channel's ID, forward a message from that channel to [@userinfobot](https://t.me/userinfobot) on Telegram. The ID will be a negative number like `-1001234567890`.

### 3. Environment Variables

Set these on Render (or copy `.env.example` to `.env` for local testing):

| Variable            | Description                                      | Example                              |
|---------------------|--------------------------------------------------|--------------------------------------|
| `API_ID`            | Telegram API ID from my.telegram.org             | `12345678`                           |
| `API_HASH`          | Telegram API hash from my.telegram.org           | `abcdef1234567890abcdef`             |
| `STRING_SESSION`    | Pyrogram session string                          | `BQC7...`                            |
| `CHANNELS`          | Comma-separated channel IDs to monitor           | `-1001111,-1002222,-1003333`         |
| `OUTPUT_CHANNEL_ID` | Your channel ID where deals will be posted       | `-1002065122146`                     |
| `N8N_WEBHOOK_URL`   | n8n webhook URL for processing                   | `https://n8n.example.com/webhook/x`  |
| `POST_DELAY`        | Seconds between posts (rate limit)               | `3`                                  |
| `POLLING_INTERVAL`  | Seconds between polling backup cycles            | `600`                                |
| `POLLING_LIMIT`     | Messages to check per channel during polling     | `5`                                  |

### 4. Deploy to Render

1. Push this repo to GitHub
2. Go to [Render Dashboard](https://dashboard.render.com)
3. Click **New → Web Service**
4. Connect your GitHub repo
5. Render will auto-detect `render.yaml`
6. Add all environment variables
7. Deploy

### 5. Keep Alive (n8n Ping)

Create a simple n8n workflow to keep the Render service alive:

```
Schedule Trigger (every 10 min) → HTTP Request (GET https://your-bot.onrender.com/health)
```

## How It Works

1. **Real-time handler** catches new messages from monitored channels instantly
2. **Polling backup** runs every 10 minutes to catch any missed messages
3. For each message with product links:
   - URLs are extracted from text/caption
   - Shortened URLs (amzn.to, fkrt.it, etc.) are resolved to final URLs
   - Product IDs are extracted per platform (ASIN for Amazon, etc.)
   - Data is sent to the n8n webhook
4. n8n checks for duplicates in Google Sheets and calls the Wishlink API
5. If the product is new, n8n returns affiliate links
6. The bot downloads the original photo and reposts to your channel with affiliate links

## File Structure

```
deals-monitor-bot/
├── bot.py               # Main application (Pyrogram + Flask)
├── config.py            # Environment variable configuration
├── url_resolver.py      # URL resolution & product ID extraction
├── generate_session.py  # One-time session string generator
├── requirements.txt     # Python dependencies
├── Procfile             # Render/Gunicorn start command
├── render.yaml          # Render deployment blueprint
├── .env.example         # Example environment variables
├── .gitignore           # Git ignore rules
└── README.md            # This file
```

## n8n Webhook Request Format

The bot sends a POST request with this JSON payload:

```json
{
  "links": [
    {
      "original_url": "https://amzn.to/4t47e7A",
      "resolved_url": "https://www.amazon.in/dp/B08N5WRWNW",
      "product_id": "amz_B08N5WRWNW",
      "platform": "amazon"
    }
  ],
  "caption": "Pigeon Pedestal Fan @ 2249 https://amzn.to/4t47e7A",
  "has_photo": true,
  "has_video": false,
  "source_channel": "Deals Looters",
  "source_channel_id": -1001234567890,
  "message_id": 415
}
```

## n8n Expected Response Format

```json
{
  "action": "post",
  "affiliate_links": {
    "https://amzn.to/4t47e7A": "https://wishlink.com/budget.looks/s/abc123"
  }
}
```

Or for duplicates:

```json
{
  "action": "skip"
}
```
