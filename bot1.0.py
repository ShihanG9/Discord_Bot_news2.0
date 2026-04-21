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
from typing import Deque, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import aiohttp
import discord
import feedparser
from bs4 import BeautifulSoup          # NEW: for stripping HTML tags
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

TOKEN: Optional[str]       = os.getenv("DISCORD_TOKEN")
NEWSAPI_KEY: Optional[str] = os.getenv("NEWSAPI_KEY")
_raw_channel_id: Optional[str] = os.getenv("TARGET_CHANNEL_ID")

CHANNEL_ID: Optional[int] = None
if _raw_channel_id is not None and _raw_channel_id.strip().isdigit():
    CHANNEL_ID = int(_raw_channel_id.strip())
else:
    if _raw_channel_id is not None:
        log.error("❌  TARGET_CHANNEL_ID is not a valid integer in .env")


# ============================================================
# DUPLICATE-POST HISTORY
# ============================================================
HISTORY_LIMIT: int = 200
POSTED_FILE: str   = "posted.json"


def load_posted_links() -> Deque[str]:
    if not os.path.exists(POSTED_FILE):
        return deque(maxlen=HISTORY_LIMIT)
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, list):
                return deque(data, maxlen=HISTORY_LIMIT)
            log.warning("⚠️  posted.json had unexpected structure — resetting.")
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("⚠️  Could not read posted.json (%s) — starting fresh.", exc)
    return deque(maxlen=HISTORY_LIMIT)


def save_posted_links(posted: Deque[str]) -> None:
    try:
        with open(POSTED_FILE, "w", encoding="utf-8") as fh:
            json.dump(list(posted), fh, indent=2)
    except OSError as exc:
        log.error("❌  Failed to save posted.json: %s", exc)


def normalise_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


posted_links: Deque[str] = load_posted_links()


# ============================================================
# RSS FEED SOURCES
# ============================================================
RSS_FEEDS: List[str] = [
    "https://simpleflying.com/feed",
    
    "https://www.flightglobal.com/rss",
    "https://aviationweek.com/rss.xml",
    
    "https://theaircurrent.com/feed",
    
    
]

ARTICLES_PER_FEED: int = 1


# ============================================================
# BOT SETUP
# ============================================================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ============================================================
# TEXT + IMAGE HELPERS  ← NEW
# ============================================================

def strip_html(raw: str) -> str:
    """
    Aggressively clean HTML: removes all tags, emails, schema metadata,
    and collapses whitespace. Handles <span>, <p>, <img>, <time> etc.
    """
    if not raw:
        return ""
    try:
        soup = BeautifulSoup(raw, "html.parser")
        # Remove non-content tags entirely
        for tag in soup(["script", "style", "time", "meta"]):
            tag.decompose()
        text = soup.get_text(separator=" ")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)

    # Remove email addresses
    text = re.sub(r"\S+@\S+", "", text)
    # Remove schema noise: typeof="...", property="...", content="..."
    text = re.sub(r'\b(typeof|property|datatype|content|about|lang)\s*=\s*"[^"]*"', "", text)
    # Remove HTML entities
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#?\w+;", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_image_from_entry(entry: object) -> Optional[str]:
    """
    Try every common location where RSS feeds hide article images.
    Returns the first valid image URL found, or None.
    """

    # 1. media:content (most common — used by SimplyFlying, FlightGlobal etc.)
    media_content = getattr(entry, "media_content", None)
    if media_content and isinstance(media_content, list):
        for media in media_content:
            url = media.get("url", "")
            if url and _looks_like_image(url):
                return url

    # 2. media:thumbnail
    media_thumbnail = getattr(entry, "media_thumbnail", None)
    if media_thumbnail and isinstance(media_thumbnail, list):
        for thumb in media_thumbnail:
            url = thumb.get("url", "")
            if url and _looks_like_image(url):
                return url

    # 3. enclosures (podcasts / images attached directly)
    enclosures = getattr(entry, "enclosures", None)
    if enclosures and isinstance(enclosures, list):
        for enc in enclosures:
            url = enc.get("url", "") or enc.get("href", "")
            if url and _looks_like_image(url):
                return url

    # 4. Parse the summary/content HTML for <img> tags
    for field in ("summary", "content"):
        raw_html = ""
        val = getattr(entry, field, None)
        if isinstance(val, list) and val:
            raw_html = val[0].get("value", "") if isinstance(val[0], dict) else str(val[0])
        elif isinstance(val, str):
            raw_html = val

        if raw_html:
            img_url = _first_img_src(raw_html)
            if img_url:
                return img_url

    return None


def _looks_like_image(url: str) -> bool:
    """Return True if the URL path ends with a common image extension."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))


def _first_img_src(html: str) -> Optional[str]:
    """Extract the src of the first <img> tag in an HTML string."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img")
        if img:
            raw_src = img.get("src", "")
            # .get() returns str | list[str] | None — cast to str safely
            src = str(raw_src) if raw_src else ""
            if src.startswith("http"):
                return src
    except Exception:
        pass
    return None


# ============================================================
# RSS FETCHER
# ============================================================

async def fetch_rss_entries(session: aiohttp.ClientSession, url: str) -> list:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning("❌  HTTP %s when fetching %s", resp.status, url)
                return []
            text = await resp.text()
            loop = asyncio.get_running_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, text)
            return list(feed.entries)
    except asyncio.TimeoutError:
        log.warning("❌  Timeout fetching %s", url)
    except aiohttp.ClientError as exc:
        log.error("❌  Network error fetching %s: %s", url, exc)
    except Exception as exc:
        log.error("❌  Unexpected error fetching %s: %s", url, exc)
    return []


# ============================================================
# EMBED BUILDER  ← UPDATED with image support
# ============================================================

def make_embed(
    title: str,
    url: str,
    description: str,
    image_url: Optional[str] = None,   # NEW parameter
    color: int = 0x1E90FF,
) -> discord.Embed:
    """Return a rich Discord embed with optional article image."""

    # Clean HTML tags from description
    clean_desc = strip_html(description)
    if len(clean_desc) > 300:
        clean_desc = clean_desc[:297] + "…"

    embed = discord.Embed(
        title=title,
        url=url,
        description=clean_desc if clean_desc else "*No description available.*",
        color=color,
    )

    # Set article image if we found one  ← NEW
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
    image_url: Optional[str] = None,   # NEW
) -> bool:
    embed = make_embed(title, url, desc, image_url)
    try:
        await channel.send(embed=embed)
        return True
    except discord.Forbidden:
        log.error("❌  Missing Send/Embed permission in #%s (%s)", channel.name, channel.id)
        return False
    except discord.HTTPException as exc:
        log.error("❌  Discord HTTP error posting '%s': %s", title, exc)
        return True


# ============================================================
# MAIN NEWS-FETCHING LOOP
# ============================================================

@tasks.loop(minutes=4)
async def fetch_and_post_news() -> None:

    if CHANNEL_ID is None:
        log.error("❌  Skipping — TARGET_CHANNEL_ID is not configured.")
        return

    channel_obj = bot.get_channel(CHANNEL_ID)
    if channel_obj is None:
        try:
            channel_obj = await bot.fetch_channel(CHANNEL_ID)
        except discord.NotFound:
            log.error("❌  Channel %s not found. Check TARGET_CHANNEL_ID.", CHANNEL_ID)
            return
        except discord.Forbidden:
            log.error("❌  Bot lacks access to channel %s.", CHANNEL_ID)
            return

    if not isinstance(channel_obj, discord.TextChannel):
        log.error("❌  Channel %s is not a text channel.", CHANNEL_ID)
        return

    channel: discord.TextChannel = channel_obj

    async with aiohttp.ClientSession() as session:

        # ── RSS feeds ────────────────────────────────────────────────────
        for feed_url in RSS_FEEDS:
            entries = await fetch_rss_entries(session, feed_url)
            for entry in entries[:ARTICLES_PER_FEED]:
                raw_link: str = str(getattr(entry, "link",    "") or "")
                title: str    = str(getattr(entry, "title",   "Untitled") or "Untitled")
                desc: str     = str(getattr(entry, "summary", "") or "")

                if not raw_link:
                    continue

                norm = normalise_url(raw_link)
                if norm in posted_links:
                    log.info("⏭️  Duplicate skipped: %s", title)
                    continue

                # Extract image from RSS entry  ← NEW
                image_url = extract_image_from_entry(entry)
                log.info("✅  Posting: %s | image: %s", title, image_url or "none")

                ok = await post_article(channel, title, raw_link, desc, image_url)
                if not ok:
                    return
                posted_links.append(norm)
                save_posted_links(posted_links)
                await asyncio.sleep(1)

        # ── NewsAPI ──────────────────────────────────────────────────────
        if not NEWSAPI_KEY:
            log.info("⚠️  NEWSAPI_KEY not set — skipping NewsAPI.")
            return

        newsapi_url = (
            "https://newsapi.org/v2/everything"
            "?q=aviation+airline+airport+flight"
            "&language=en"
            "&sortBy=publishedAt"
            f"&apiKey={NEWSAPI_KEY}"
        )

        try:
            async with session.get(
                newsapi_url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
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

            raw_link = str(article.get("url",         "") or "")
            title    = str(article.get("title",       "") or "Untitled")
            desc     = str(article.get("description", "") or "")
            # NewsAPI provides urlToImage directly  ← NEW
            image_url = article.get("urlToImage") or None

            if not raw_link or title in ("[Removed]", ""):
                continue

            norm = normalise_url(raw_link)
            if norm in posted_links:
                log.info("⏭️  Duplicate skipped (NewsAPI): %s", title)
                continue

            log.info("✅  Posting (NewsAPI): %s | image: %s", title, image_url or "none")
            ok = await post_article(channel, title, raw_link, desc, image_url)
            if not ok:
                return
            posted_links.append(norm)
            save_posted_links(posted_links)
            await asyncio.sleep(1)


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
    await ctx.send(f"🏓 Pong!  Latency: {round(bot.latency * 1000)} ms")


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


# ============================================================
# ENTRY POINT
# ============================================================

def main() -> None:
    if not TOKEN:
        log.error("❌  DISCORD_TOKEN is not set in .env — cannot start.")
        return
    if CHANNEL_ID is None:
        log.error("❌  TARGET_CHANNEL_ID is not set or invalid in .env — cannot start.")
        return

    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("❌  Invalid DISCORD_TOKEN — please check your .env file.")
    except discord.PrivilegedIntentsRequired:
        log.error(
            "❌  Message Content Intent is not enabled.\n"
            "    Go to: discord.com/developers → Your App → Bot → Privileged Gateway Intents"
        )
    except KeyboardInterrupt:
        log.info("🛑  Bot stopped by user.")


if __name__ == "__main__":
    main()
