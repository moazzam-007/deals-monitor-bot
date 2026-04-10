import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())  # Must be first — fixes Gunicorn/Python3.10+ startup

import os
import logging
import threading
from collections import deque

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
# BoundedSet — O(1) lookup + automatic eviction of oldest entries
# Replaces plain deque (which has O(n) 'in' lookup)
# ---------------------------------------------------------------------------
class BoundedSet:
    """
    Thread-safe set with a max size.
    Evicts the oldest entry when full (like deque(maxlen=N)).
    O(1) lookup, O(1) add.
    """
    def __init__(self, maxlen: int):
        self.maxlen = maxlen
        self._set: set = set()
        self._deque: deque = deque()

    def __contains__(self, item) -> bool:
        return item in self._set

    def add(self, item):
        if item in self._set:
            return
        if len(self._deque) >= self.maxlen:
            oldest = self._deque.popleft()
            self._set.discard(oldest)
        self._deque.append(item)
        self._set.add(item)

    def __len__(self):
        return len(self._set)


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)
url_resolver = URLResolver()

# asyncio Queue — ensures messages processed one at a time
# Prevents concurrent n8n / Wishlink API calls
_message_queue: asyncio.Queue = None
_worker_task: asyncio.Task = None  # stored reference — detects silent crashes

# Layer 1 in-memory duplicate detection
# O(1) lookup + auto-eviction at 2000 entries
seen_product_ids = BoundedSet(maxlen=2000)


# ---------------------------------------------------------------------------
# Flask health endpoints (pinged by n8n to keep Render alive)
# ---------------------------------------------------------------------------
@flask_app.route("/")
def home():
    return jsonify({
        "service": "deals-monitor-bot",
        "status": "running",
        "channels_monitored": len(Config.CHANNELS),
        "queue_size": _message_queue.qsize() if _message_queue else 0,
        "seen_count": len(seen_product_ids),
    })


@flask_app.route("/health")
def health():
    thread_alive = "_bot_thread" in globals() and _bot_thread.is_alive()
    if thread_alive:
        return jsonify({"status": "healthy"})
    return jsonify({"status": "unhealthy", "error": "Bot thread is dead"}), 500


# ---------------------------------------------------------------------------
# n8n webhook — fire & forget
# n8n handles: Layer 2 dedup (Sheets), Wishlink API, copyMessage, log
# ---------------------------------------------------------------------------
async def fire_n8n_webhook(payload: dict) -> bool:
    """POST payload to n8n webhook. Returns True on HTTP 200."""
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(Config.N8N_WEBHOOK_URL, json=payload) as resp:
                if resp.status == 200:
                    logger.info(
                        f"✅ n8n webhook fired: {payload.get('product_id')} | "
                        f"msg {payload.get('message_id')}"
                    )
                    return True
                else:
                    body = await resp.text()
                    logger.error(f"n8n error {resp.status}: {body[:200]}")
                    return False
    except Exception as e:
        logger.error(f"n8n webhook call failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Core message processing
# ---------------------------------------------------------------------------
async def process_message(client: Client, message):
    """
    Pipeline:
    1. Extract URLs from text + Telegram entities (hidden hyperlinks)
    2. Filter to known product/shortener domains
    3. Resolve first URL → canonical product URL + product_id
    4. Layer 1 check: BoundedSet (O(1), resets on restart)
    5. Fire n8n webhook {product_id, urls, caption, channel, message_id}
       → n8n: Layer 2 (Sheets) → Wishlink API → copyMessage → log
    """
    try:
        channel_id = message.chat.id
        source_channel = getattr(message.chat, "title", "Unknown")

        # --- Text content ---
        text = message.caption or message.text or ""
        if not text:
            return

        # --- Extract URLs from text ---
        all_urls = url_resolver.extract_urls(text)

        # --- Also extract from entities (handles invisible hyperlinks) ---
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
            return

        # --- Filter to known product/shortener domains ---
        product_urls = [u for u in all_urls if url_resolver.is_product_url(u)]
        if not product_urls:
            return

        # --- Process FIRST product URL only ---
        # Design decision: 1 Wishlink call per deal → avoids rate limits
        loop = asyncio.get_running_loop()
        first_result = await loop.run_in_executor(
            None, url_resolver.process_url, product_urls[0]
        )

        product_id = first_result.get("product_id", "")
        if not product_id:
            return

        # --- Layer 1: In-memory O(1) duplicate check ---
        if product_id in seen_product_ids:
            logger.info(f"⏭️  Layer 1 skip (in-memory): {product_id}")
            return

        # Mark as seen before webhook
        # Tradeoff: if webhook fails, deal is missed until restart (deque cleared)
        # Layer 2 (Sheets) handles cross-restart dedup
        seen_product_ids.add(product_id)

        # --- Build n8n payload ---
        payload = {
            "product_id":        product_id,
            "original_url":      first_result.get("original_url", ""),
            "resolved_url":      first_result.get("resolved_url", ""),
            "platform":          first_result.get("platform", "unknown"),
            "caption":           text,
            "source_channel":    source_channel,
            "source_channel_id": channel_id,
            "message_id":        message.id,
        }

        logger.info(
            f"📤 [{source_channel}] Firing n8n: {product_id} | "
            f"platform={first_result.get('platform')} | msg {message.id}"
        )

        await fire_n8n_webhook(payload)

    except Exception as e:
        logger.error(f"Error processing msg {message.id}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Queue worker — ONE message at a time
# Natural gap = n8n processing time (~3-5s) + POST_DELAY
# Wishlink rate limit: 5 calls/batch, 120s cooldown
# Our rate: ~1 call per (3+3)=6s → well within limits
# ---------------------------------------------------------------------------
async def queue_worker(client: Client):
    """Consume _message_queue sequentially. Never processes 2 messages at once."""
    logger.info("✅ Queue worker started")
    while True:
        message = await _message_queue.get()
        try:
            await process_message(client, message)
            await asyncio.sleep(Config.POST_DELAY)
        except Exception as e:
            logger.error(f"Queue worker error on msg {message.id}: {e}", exc_info=True)
        finally:
            # Always mark task done — even on exception
            _message_queue.task_done()


def _on_worker_done(task: asyncio.Task):
    """Callback: log if worker task dies unexpectedly."""
    if not task.cancelled():
        exc = task.exception()
        if exc:
            logger.critical(
                f"❌ Queue worker task died unexpectedly: {exc}",
                exc_info=exc
            )


# ---------------------------------------------------------------------------
# Pyrogram bot runner
# ---------------------------------------------------------------------------
async def run_bot():
    """Start Pyrogram userbot, register handlers, launch queue worker and polling."""
    global _message_queue, _worker_task
    _message_queue = asyncio.Queue()

    client = Client(
        name=":memory:",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        session_string=Config.STRING_SESSION,
        workers=10,
        in_memory=True,
    )

    # ---- Real-time handler: ONLY adds to queue (instant, non-blocking) ----
    # Using filters.channel + manual ID check instead of filters.chat(ids)
    # reason: filters.chat() with in-memory session can silently miss messages
    # when peers aren't cached yet at startup
    channels_set = set(Config.CHANNELS)

    @client.on_message(filters.channel)
    async def on_channel_message(_client, message):
        if message.chat and message.chat.id in channels_set:
            await _message_queue.put(message)
            logger.info(
                f"📥 Queued msg {message.id} from "
                f"'{getattr(message.chat, 'title', 'Unknown')}' "
                f"(queue depth: {_message_queue.qsize()})"
            )

    # ---- Start client ----
    await client.start()
    logger.info(f"✅ Pyrogram started — monitoring {len(Config.CHANNELS)} channels")

    # ---- Launch queue worker — store reference to detect crashes ----
    _worker_task = asyncio.create_task(queue_worker(client), name="queue-worker")
    _worker_task.add_done_callback(_on_worker_done)
    logger.info("✅ Queue worker task launched")

    # ---- Polling backup loop (catches messages missed during real-time gaps) ----
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

                    # Oldest first → natural order
                    msgs.reverse()
                    for msg in msgs:
                        text = msg.caption or msg.text or ""
                        if text:
                            # Queue it — deque check inside process_message
                            # will skip if already processed in real-time
                            await _message_queue.put(msg)

                except FloodWait as e:
                    # Cap wait and skip this channel this cycle
                    # (don't block all other channels)
                    wait_time = min(e.value, 30)
                    logger.warning(
                        f"FloodWait on poll for {ch_id}: sleeping {wait_time}s "
                        f"(full wait was {e.value}s) — skipping channel this cycle"
                    )
                    await asyncio.sleep(wait_time)
                    continue

                except Exception as e:
                    logger.warning(f"Poll error for {ch_id}: {e}")

                await asyncio.sleep(1)

            logger.info("🔄 Polling cycle done")

        except Exception as e:
            logger.error(f"Polling loop error: {e}", exc_info=True)
            await asyncio.sleep(60)


def _run_bot_thread():
    """Run async bot in a dedicated thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot())


# ---------------------------------------------------------------------------
# Auto-start on module import — required for Gunicorn
# BOT_STARTED guard prevents duplicate threads across Gunicorn workers
# ---------------------------------------------------------------------------
if os.environ.get("BOT_STARTED") != "1":
    os.environ["BOT_STARTED"] = "1"
    logger.info("🚀 Starting bot thread...")
    _bot_thread = threading.Thread(target=_run_bot_thread, daemon=True)
    _bot_thread.start()
    logger.info("✅ Bot thread launched")


# ---------------------------------------------------------------------------
# Local development entrypoint only
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=Config.PORT)
