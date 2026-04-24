"""
Aviation News Discord Bot
=========================
Fetches aviation-related news from RSS feeds and NewsAPI,
then posts unique articles as embeds (with images) to a Discord channel.

Setup:
  1. pip install discord.py aiohttp feedparser python-dotenv beautifulsoup4
  2. Create a .env file with:
       DISCORD_TOKEN=your_bot_token
       TARGET_CHANNEL_ID=your_channel_id
       NEWSAPI_KEY=your_newsapi_key   # optional
  3. python bot1.0.py
"""

# ============================================================
# IMPORTS
# ============================================================
import asyncio
import json
import logging
import os
import re
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Deque, List, Optional, Set
from urllib.parse import urlparse, urlunparse

import aiohttp
import discord
import feedparser
from bs4 import BeautifulSoup
from discord.ext import commands, tasks
from dotenv import load_dotenv


# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================
load_dotenv()

TOKEN: Optional[str]           = os.getenv("DISCORD_TOKEN")
NEWSAPI_KEY: Optional[str]     = os.getenv("NEWSAPI_KEY")
_raw_channel_id: Optional[str] = os.getenv("TARGET_CHANNEL_ID")

CHANNEL_ID: Optional[int] = None
if _raw_channel_id is not None and _raw_channel_id.strip().isdigit():
    CHANNEL_ID = int(_raw_channel_id.strip())
elif _raw_channel_id is not None:
    log.error("❌  TARGET_CHANNEL_ID is not a valid integer in .env")


# ============================================================
# DUPLICATE-POST HISTORY
# ============================================================
HISTORY_LIMIT: int = 500          # increased from 200
POSTED_FILE: str   = "posted.json"

# ── TWO data structures working together ──────────────────────────────────
# deque  → maintains order, auto-drops oldest when full, saved to disk
# set    → O(1) instant lookup (deque `in` is slow O(n) linear search)
# Both are always kept in sync.
_posted_deque: Deque[str] = deque(maxlen=HISTORY_LIMIT)
_posted_set:   Set[str]   = set()


def load_posted_links() -> None:
    """Load previously posted URLs from disk into both deque and set."""
    global _posted_deque, _posted_set
    if not os.path.exists(POSTED_FILE):
        log.info("📂  No posted.json found — starting fresh.")
        return
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, list):
                _posted_deque = deque(data, maxlen=HISTORY_LIMIT)
                _posted_set   = set(data)   # ← fast lookup set
                log.info("📂  Loaded %d previously posted URLs.", len(_posted_set))
                return
            log.warning("⚠️  posted.json had unexpected structure — resetting.")
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("⚠️  Could not read posted.json (%s) — starting fresh.", exc)


def save_posted_links() -> None:
    """Persist deque to disk."""
    try:
        with open(POSTED_FILE, "w", encoding="utf-8") as fh:
            json.dump(list(_posted_deque), fh, indent=2)
    except OSError as exc:
        log.error("❌  Failed to save posted.json: %s", exc)


def is_posted(norm_url: str) -> bool:
    """O(1) instant duplicate check using the set."""
    return norm_url in _posted_set


def mark_posted(norm_url: str) -> None:
    """Add URL to both deque and set, keeping them in sync."""
    # If deque is full it will drop the oldest item — remove from set too
    if len(_posted_deque) == HISTORY_LIMIT:
        oldest = _posted_deque[0]
        _posted_set.discard(oldest)

    _posted_deque.append(norm_url)
    _posted_set.add(norm_url)
    save_posted_links()


def normalise_url(url: str) -> str:
    """
    Strip query params, fragments AND trailing slashes for reliable dedup.
    e.g. https://site.com/article?utm_source=twitter → https://site.com/article
    """
    parsed = urlparse(url.strip())
    clean  = parsed._replace(query="", fragment="")
    result = urlunparse(clean).rstrip("/").lower()   # lowercase + no trailing slash
    return result


# Load history on startup
load_posted_links()


# ============================================================
# RSS FEED SOURCES
# ============================================================
RSS_FEEDS: List[str] = [
    "https://simpleflying.com/feed",
    "https://www.flightglobal.com/rss",
    "https://aviationweek.com/rss.xml",
    "https://theaircurrent.com/feed",
    "https://aeroworldindia.com/feed",
    "https://www.aviationpros.com/rss",
    "https://www.ch-aviation.com/portal/news/rss",
    "https://centreforaviation.com/rss",
]

ARTICLES_PER_FEED: int = 5


# ============================================================
# BOT SETUP
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ============================================================
# TEXT CLEANING
# ============================================================

def strip_html(raw: str) -> str:
    """Remove all HTML tags, emails, schema noise and collapse whitespace."""
    if not raw:
        return ""
    try:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "time", "meta"]):
            tag.decompose()
        text = soup.get_text(separator=" ")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)

    text = re.sub(r"\S+@\S+", "", text)
    text = re.sub(r'\b(typeof|property|datatype|content|about|lang)\s*=\s*"[^"]*"', "", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#?\w+;",     " ", text)
    text = re.sub(r"\s+",         " ", text).strip()
    return text


# ============================================================
# IMAGE EXTRACTION
# ============================================================

def _looks_like_image(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))


def _first_img_src(html: str) -> Optional[str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        img  = soup.find("img")
        if img:
            raw_src = img.get("src", "")
            src = str(raw_src) if raw_src else ""
            if src.startswith("http"):
                return src
    except Exception:
        pass
    return None


def extract_image_from_entry(entry: object) -> Optional[str]:
    """Check 4 locations in order to find the article image."""
    # 1. media:content
    mc = getattr(entry, "media_content", None)
    if mc and isinstance(mc, list):
        for m in mc:
            u = m.get("url", "")
            if u and _looks_like_image(u):
                return u

    # 2. media:thumbnail
    mt = getattr(entry, "media_thumbnail", None)
    if mt and isinstance(mt, list):
        for m in mt:
            u = m.get("url", "")
            if u and _looks_like_image(u):
                return u

    # 3. enclosures
    enc = getattr(entry, "enclosures", None)
    if enc and isinstance(enc, list):
        for e in enc:
            u = e.get("url", "") or e.get("href", "")
            if u and _looks_like_image(u):
                return u

    # 4. <img> inside summary/content HTML
    for field in ("summary", "content"):
        val = getattr(entry, field, None)
        raw_html = ""
        if isinstance(val, list) and val:
            raw_html = val[0].get("value", "") if isinstance(val[0], dict) else str(val[0])
        elif isinstance(val, str):
            raw_html = val
        if raw_html:
            img = _first_img_src(raw_html)
            if img:
                return img

    return None


# ============================================================
# RSS FETCHER
# ============================================================

async def fetch_rss_entries(session: aiohttp.ClientSession, url: str) -> list:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning("❌  HTTP %s → %s", resp.status, url)
                return []
            text = await resp.text()
            loop = asyncio.get_running_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, text)
            return list(feed.entries)
    except asyncio.TimeoutError:
        log.warning("❌  Timeout → %s", url)
    except aiohttp.ClientError as exc:
        log.error("❌  Network error → %s: %s", url, exc)
    except Exception as exc:
        log.error("❌  Error → %s: %s", url, exc)
    return []


# ============================================================
# EMBED BUILDER
# ============================================================

def make_embed(
    title: str,
    url: str,
    description: str,
    image_url: Optional[str] = None,
    color: int = 0x1E90FF,
) -> discord.Embed:
    clean_desc = strip_html(description)
    if len(clean_desc) > 300:
        clean_desc = clean_desc[:297] + "…"

    embed = discord.Embed(
        title=title,
        url=url,
        description=clean_desc if clean_desc else "*No description available.*",
        color=color,
    )
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text="✈️ Aviation News Bot")
    return embed


# ============================================================
# POST ARTICLE
# ============================================================

async def post_article(
    channel: discord.TextChannel,
    title: str,
    url: str,
    desc: str,
    image_url: Optional[str] = None,
) -> bool:
    embed = make_embed(title, url, desc, image_url)
    try:
        await channel.send(embed=embed)
        return True
    except discord.Forbidden:
        log.error("❌  No permission in #%s (%s)", channel.name, channel.id)
        return False
    except discord.HTTPException as exc:
        log.error("❌  HTTP error posting '%s': %s", title, exc)
        return True


# ============================================================
# MAIN NEWS-FETCHING LOOP
# ============================================================

@tasks.loop(minutes=4)
async def fetch_and_post_news() -> None:
    if CHANNEL_ID is None:
        log.error("❌  TARGET_CHANNEL_ID not set — skipping.")
        return

    channel_obj = bot.get_channel(CHANNEL_ID)
    if channel_obj is None:
        try:
            channel_obj = await bot.fetch_channel(CHANNEL_ID)
        except discord.NotFound:
            log.error("❌  Channel %s not found.", CHANNEL_ID)
            return
        except discord.Forbidden:
            log.error("❌  No access to channel %s.", CHANNEL_ID)
            return

    if not isinstance(channel_obj, discord.TextChannel):
        log.error("❌  Channel %s is not a text channel.", CHANNEL_ID)
        return

    channel: discord.TextChannel = channel_obj

    # ── Per-cycle seen set ────────────────────────────────────────────────
    # This catches duplicates WITHIN the same fetch cycle
    # (e.g. same article in two different RSS feeds at the same time)
    cycle_seen: Set[str] = set()

    async with aiohttp.ClientSession() as session:

        # ── RSS feeds ─────────────────────────────────────────────────────
        for feed_url in RSS_FEEDS:
            entries = await fetch_rss_entries(session, feed_url)
            for entry in entries[:ARTICLES_PER_FEED]:
                raw_link: str = str(getattr(entry, "link",    "") or "")
                title: str    = str(getattr(entry, "title",   "Untitled") or "Untitled")
                desc: str     = str(getattr(entry, "summary", "") or "")

                if not raw_link:
                    continue

                norm = normalise_url(raw_link)

                # CHECK 1: already posted in a previous cycle?
                if is_posted(norm):
                    log.info("⏭️  [history] Duplicate: %s", title)
                    continue

                # CHECK 2: already seen in THIS cycle (same article, 2 feeds)?
                if norm in cycle_seen:
                    log.info("⏭️  [cycle]   Duplicate: %s", title)
                    continue

                image_url = extract_image_from_entry(entry)
                log.info("✅  Posting: %s", title)

                ok = await post_article(channel, title, raw_link, desc, image_url)
                if not ok:
                    return

                # Mark in BOTH guards immediately after posting
                mark_posted(norm)
                cycle_seen.add(norm)
                await asyncio.sleep(1.5)   # slightly longer delay = safer rate limit

        # ── NewsAPI ───────────────────────────────────────────────────────
        if not NEWSAPI_KEY:
            log.info("⚠️  NEWSAPI_KEY not set — skipping.")
            return

        newsapi_url = (
            "https://newsapi.org/v2/everything"
            "?q=aviation+airline+airport+flight"
            "&language=en"
            "&sortBy=publishedAt"
            f"&apiKey={NEWSAPI_KEY}"
        )

        try:
            async with session.get(newsapi_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("❌  NewsAPI HTTP %s: %s", resp.status, body[:200])
                    return
                data: dict = await resp.json()
        except asyncio.TimeoutError:
            log.warning("❌  Timeout fetching NewsAPI.")
            return
        except aiohttp.ClientError as exc:
            log.error("❌  Network error fetching NewsAPI: %s", exc)
            return

        articles = data.get("articles", [])
        if not isinstance(articles, list):
            return

        for article in articles[:3]:
            if not isinstance(article, dict):
                continue

            raw_link  = str(article.get("url",         "") or "")
            title     = str(article.get("title",       "") or "Untitled")
            desc      = str(article.get("description", "") or "")
            image_url = article.get("urlToImage") or None

            if not raw_link or title in ("[Removed]", ""):
                continue

            norm = normalise_url(raw_link)

            if is_posted(norm):
                log.info("⏭️  [history] Duplicate (NewsAPI): %s", title)
                continue

            if norm in cycle_seen:
                log.info("⏭️  [cycle]   Duplicate (NewsAPI): %s", title)
                continue

            log.info("✅  Posting (NewsAPI): %s", title)
            ok = await post_article(channel, title, raw_link, desc, image_url)
            if not ok:
                return

            mark_posted(norm)
            cycle_seen.add(norm)
            await asyncio.sleep(1.5)


@fetch_and_post_news.before_loop
async def before_fetch() -> None:
    await bot.wait_until_ready()


# ============================================================
# BOT EVENTS
# ============================================================

@bot.event
async def on_ready() -> None:
    user = bot.user
    log.info("✅  Logged in as %s  (ID: %s)", user, user.id if user else "Unknown")
    if not fetch_and_post_news.is_running():
        fetch_and_post_news.start()


# ============================================================
# BOT COMMANDS
# ============================================================

@bot.command(name="ping")
async def ping(ctx: commands.Context) -> None:
    await ctx.send(f"🏓 Pong! Latency: {round(bot.latency * 1000)} ms")


@bot.command(name="fetchnow")
@commands.has_permissions(manage_messages=True)
async def fetch_now(ctx: commands.Context) -> None:
    await ctx.send("🔄 Triggering manual news fetch…")
    await fetch_and_post_news()
    await ctx.send("✅ Done!")


@fetch_now.error
async def fetch_now_error(ctx: commands.Context, error: Exception) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need **Manage Messages** permission to use this command.")
    else:
        log.error("❌  Unexpected error in !fetchnow: %s", error)
        await ctx.send("❌ An unexpected error occurred.")


@bot.command(name="stats")
async def stats(ctx: commands.Context) -> None:
    """Show how many articles have been posted so far."""
    await ctx.send(f"📊 Total unique articles posted: **{len(_posted_set)}**")


# ============================================================
# KEEP-ALIVE SERVER (stops Render free tier from sleeping)
# ============================================================

class _KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress HTTP logs


def _run_keep_alive() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _KeepAliveHandler)
    log.info("🌐  Keep-alive server on port %s", port)
    server.serve_forever()


# ============================================================
# ENTRY POINT
# ============================================================

def main() -> None:
    if not TOKEN:
        log.error("❌  DISCORD_TOKEN not set — cannot start.")
        return
    if CHANNEL_ID is None:
        log.error("❌  TARGET_CHANNEL_ID not set or invalid — cannot start.")
        return

    Thread(target=_run_keep_alive, daemon=True).start()

    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("❌  Invalid DISCORD_TOKEN.")
    except discord.PrivilegedIntentsRequired:
        log.error(
            "❌  Message Content Intent not enabled.\n"
            "    discord.com/developers → Your App → Bot → Privileged Gateway Intents"
        )
    except KeyboardInterrupt:
        log.info("🛑  Bot stopped.")


if __name__ == "__main__":
    main()