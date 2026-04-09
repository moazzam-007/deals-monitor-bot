import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os
import asyncio
import logging
import threading
from typing import Optional

import aiohttp
from flask import Flask, jsonify
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.enums import MessageEntityType

from config import Config
from url_resolver import URLResolver

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)
url_resolver = URLResolver()

# Per-channel "starting point" — only messages AFTER this ID are processed
last_message_ids = {}  # {channel_id: last_processed_msg_id}

# Temp directory for downloaded media
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Flask health endpoints (keeps Render alive when pinged)
# ---------------------------------------------------------------------------
@flask_app.route("/")
def home():
    return jsonify({
        "service": "deals-monitor-bot",
        "status": "running",
        "channels_monitored": len(Config.CHANNELS),
    })


@flask_app.route("/health")
def health():
    if "_bot_thread" in globals() and _bot_thread.is_alive():
        return jsonify({"status": "healthy"})
    return jsonify({"status": "unhealthy", "error": "Bot thread is dead"}), 500


# ---------------------------------------------------------------------------
# n8n webhook communication
# ---------------------------------------------------------------------------
async def call_n8n_webhook(payload: dict) -> Optional[dict]:
    """
    POST message data to the n8n webhook.
    n8n responds synchronously with:
      { "action": "post" | "skip", "affiliate_links": { original_url: aff_url } }
    """
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(Config.N8N_WEBHOOK_URL, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"n8n response: action={data.get('action')}")
                    return data
                else:
                    body = await resp.text()
                    logger.error(f"n8n error {resp.status}: {body[:200]}")
                    return None
    except Exception as e:
        logger.error(f"n8n webhook call failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Post to output channel
# ---------------------------------------------------------------------------
async def post_to_channel(client: Client, message, affiliate_links: dict):
    """
    Download the original media, replace links in caption,
    and re-post to the output channel.
    """
    caption = message.caption or message.text or ""

    # Replace each original URL with its affiliate counterpart
    for original_url, aff_url in affiliate_links.items():
        if aff_url and aff_url != original_url:
            caption = caption.replace(original_url, aff_url)

    try:
        if message.photo:
            path = await client.download_media(
                message,
                file_name=os.path.join(DOWNLOAD_DIR, f"photo_{message.id}.jpg"),
            )
            try:
                if path:
                    await client.send_photo(
                        chat_id=Config.OUTPUT_CHANNEL_ID,
                        photo=path,
                        caption=caption,
                    )
                else:
                    await client.send_message(Config.OUTPUT_CHANNEL_ID, text=caption)
            finally:
                if path:
                    _safe_remove(path)

        elif message.video:
            path = await client.download_media(
                message,
                file_name=os.path.join(DOWNLOAD_DIR, f"video_{message.id}.mp4"),
            )
            try:
                if path:
                    await client.send_video(
                        chat_id=Config.OUTPUT_CHANNEL_ID,
                        video=path,
                        caption=caption,
                    )
                else:
                    await client.send_message(Config.OUTPUT_CHANNEL_ID, text=caption)
            finally:
                if path:
                    _safe_remove(path)

        elif message.animation:
            path = await client.download_media(
                message,
                file_name=os.path.join(DOWNLOAD_DIR, f"gif_{message.id}.mp4"),
            )
            try:
                if path:
                    await client.send_animation(
                        chat_id=Config.OUTPUT_CHANNEL_ID,
                        animation=path,
                        caption=caption,
                    )
                else:
                    await client.send_message(Config.OUTPUT_CHANNEL_ID, text=caption)
            finally:
                if path:
                    _safe_remove(path)

        else:
            # Text-only message
            await client.send_message(Config.OUTPUT_CHANNEL_ID, text=caption)

        logger.info(f"✅ Posted message {message.id} to output channel")

    except FloodWait as e:
        logger.warning(f"FloodWait: sleeping {e.value}s")
        await asyncio.sleep(e.value)
    except Exception as e:
        logger.error(f"Failed to post message {message.id}: {e}", exc_info=True)


def _safe_remove(path: str):
    """Delete a temp file, ignoring errors."""
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Core message processing
# ---------------------------------------------------------------------------
async def process_message(client: Client, message):
    """
    1. Extract URLs from the message
    2. Resolve shortened URLs and extract product IDs
    3. Send payload to n8n webhook
    4. If n8n says "post" → re-post with affiliate links
    """
    try:
        channel_id = message.chat.id

        # Skip already-processed messages
        if message.id <= last_message_ids.get(channel_id, 0):
            return

        # Get text content
        text = message.caption or message.text or ""
        if not text:
            last_message_ids[channel_id] = message.id
            return

        # Extract URLs from text AND entities (handles hidden hyperlinks)
        all_urls = url_resolver.extract_urls(text)
        entities = message.caption_entities or message.entities or []
        for entity in entities:
            if entity.type == MessageEntityType.TEXT_LINK and entity.url:
                if entity.url not in all_urls:
                    all_urls.append(entity.url)
            elif entity.type == MessageEntityType.URL:
                url_text = text[entity.offset:entity.offset + entity.length]
                if url_text not in all_urls:
                    all_urls.append(url_text)

        if not all_urls:
            last_message_ids[channel_id] = message.id
            return

        # Filter to product URLs only
        product_urls = [u for u in all_urls if url_resolver.is_product_url(u)]
        if not product_urls:
            last_message_ids[channel_id] = message.id
            return

        # Resolve and extract product IDs (blocking I/O → run in executor)
        loop = asyncio.get_running_loop()
        processed_links = []
        for url in product_urls:
            result = await loop.run_in_executor(None, url_resolver.process_url, url)
            processed_links.append(result)

        if not processed_links:
            last_message_ids[channel_id] = message.id
            return

        # Build payload for n8n
        payload = {
            "links": processed_links,
            "caption": text,
            "has_photo": bool(message.photo),
            "has_video": bool(message.video),
            "source_channel": getattr(message.chat, "title", "Unknown"),
            "source_channel_id": channel_id,
            "message_id": message.id,
        }

        source = payload["source_channel"]
        logger.info(
            f"📤 [{source}] Sending {len(processed_links)} link(s) to n8n"
        )

        # Call n8n
        response = await call_n8n_webhook(payload)
        if not response:
            return

        action = response.get("action", "skip")
        if action == "skip":
            logger.info(f"⏭️  Skipped (duplicate): msg {message.id}")
            last_message_ids[channel_id] = message.id
            return

        affiliate_links = response.get("affiliate_links", {})
        if not affiliate_links:
            logger.warning(f"No affiliate links returned for msg {message.id}")
            last_message_ids[channel_id] = message.id
            return

        # Post to output channel
        await post_to_channel(client, message, affiliate_links)

        # Rate-limit delay
        await asyncio.sleep(Config.POST_DELAY)

        # Mark successfully processed!
        last_message_ids[channel_id] = message.id

    except Exception as e:
        logger.error(f"Error processing msg {message.id}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Pyrogram bot runner
# ---------------------------------------------------------------------------
async def run_bot():
    """Start the Pyrogram userbot, register handlers, run polling backup."""

    client = Client(
        name=":memory:",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        session_string=Config.STRING_SESSION,
        workers=10,
        in_memory=True,
    )

    # ---- Real-time message handler ----
    channel_filter = filters.chat(Config.CHANNELS)

    @client.on_message(channel_filter)
    async def on_channel_message(_client, message):
        await process_message(_client, message)

    # ---- Start client ----
    await client.start()
    logger.info(f"✅ Pyrogram started — monitoring {len(Config.CHANNELS)} channels")

    # ---- Set starting points (skip old messages) ----
    for ch_id in Config.CHANNELS:
        try:
            async for msg in client.get_chat_history(ch_id, limit=1):
                last_message_ids[ch_id] = msg.id
                logger.info(f"   Channel {ch_id}: starting from msg {msg.id}")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"   Channel {ch_id}: could not set start point — {e}")

    logger.info("✅ Starting points set. Real-time monitoring active.")

    # ---- Polling backup loop ----
    while True:
        try:
            await asyncio.sleep(Config.POLLING_INTERVAL)
            logger.info("🔄 Polling backup cycle started")

            for ch_id in Config.CHANNELS:
                try:
                    msgs = []
                    async for msg in client.get_chat_history(
                        ch_id, limit=Config.POLLING_LIMIT
                    ):
                        msgs.append(msg)
                    
                    # Reverse so oldest in the polled batch gets processed first
                    msgs.reverse()
                    for msg in msgs:
                        await process_message(client, msg)
                except FloodWait as e:
                    logger.warning(f"FloodWait on poll for {ch_id}: {e.value}s")
                    await asyncio.sleep(e.value)
                except Exception as e:
                    logger.warning(f"Poll error for {ch_id}: {e}")
                await asyncio.sleep(1)

            logger.info("🔄 Polling cycle done")

        except Exception as e:
            logger.error(f"Polling loop error: {e}", exc_info=True)
            await asyncio.sleep(60)


def _run_bot_thread():
    """Run the async bot inside a dedicated thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot())


# ---------------------------------------------------------------------------
# Start the bot thread on module import — required for Gunicorn
# (Gunicorn imports bot:flask_app, so __main__ block never runs)
# Using BOT_STARTED guard to prevent multiple threads if Gunicorn tries multiple times
# ---------------------------------------------------------------------------
if os.environ.get("BOT_STARTED") != "1":
    os.environ["BOT_STARTED"] = "1"
    logger.info("🚀 Starting bot thread...")
    _bot_thread = threading.Thread(target=_run_bot_thread, daemon=True)
    _bot_thread.start()
    logger.info("✅ Bot thread launched")


# ---------------------------------------------------------------------------
# Entrypoint (local development only)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=Config.PORT)
