

# Discord Aviation News Bot

A Python-based Discord bot that automatically fetches and posts aviation-related news into a Discord channel using RSS feeds and NewsAPI.

This bot is designed for aviation communities, airline discussion servers, airport operations groups, students, researchers, and anyone interested in real-time aviation updates.

---

## Features

### Automatic Aviation News Posting

The bot checks aviation news sources every few minutes and posts fresh articles automatically.

### RSS Feed Integration

Fetches aviation news from trusted sources like:

* Simple Flying
* FlightGlobal
* AeroWorld India

### NewsAPI Integration

Gets the latest aviation-related articles using NewsAPI.

### Duplicate Prevention

Stores previously posted links in `posted.json` to prevent reposting the same article.

### Discord Embed Messages

Posts clean and professional embed messages including:

* News title
* Short description
* Clickable article link

### Error Handling

Handles:

* Invalid API keys
* Missing environment variables
* Network failures
* Corrupted JSON files
* Discord permission issues

---

## Project Structure

```text
DISCORD_BOT_NEWS/
│
├── bot.py
├── requirements.txt
├── posted.json
├── .gitignore
└── .env
```

---

## File Explanation

### `bot.py`

Main Python file containing:

* Discord bot setup
* RSS fetching logic
* NewsAPI integration
* Duplicate detection
* Background scheduled tasks
* Error handling
* Bot startup logic

---

### `requirements.txt`

Contains required Python libraries.

```txt
discord.py
aiohttp
feedparser
python-dotenv
```

Install using:

```bash
pip install -r requirements.txt
```

---

### `posted.json`

Stores previously posted article links.

Purpose:
Prevent duplicate news posting.

Example:

```json
[
  "https://example.com/article1",
  "https://example.com/article2"
]
```

---

### `.env`

Stores secret environment variables.

```env
DISCORD_TOKEN=your_discord_token
TARGET_CHANNEL_ID=your_channel_id
NEWSAPI_KEY=your_newsapi_key
```

Never upload this file to GitHub.

---

### `.gitignore`

Prevents sensitive or unnecessary files from being uploaded.

Recommended:

```txt
.env
__pycache__/
*.pyc
```

---

## Required Environment Variables

### `DISCORD_TOKEN`

Your bot token from Discord Developer Portal.

### `TARGET_CHANNEL_ID`

The Discord channel ID where news will be posted.

Must be numeric only.

### `NEWSAPI_KEY`

Your API key from NewsAPI.

Used for fetching latest aviation news.

---

## How It Works

### Step 1: Bot Starts

The bot logs into Discord using your token.

### Step 2: Background Task Starts

A scheduled loop runs every few minutes:

```python
@tasks.loop(minutes=4)
```

### Step 3: RSS Feeds Are Fetched

The bot checks all configured RSS URLs.

### Step 4: NewsAPI Is Checked

Additional aviation news is fetched using API.

### Step 5: Duplicate Check

Each article link is normalized and checked against `posted.json`.

### Step 6: New Article Is Posted

If not already posted, the bot sends an embed message to Discord.

### Step 7: Link Is Saved

The article link is stored for future duplicate prevention.

---

## Hosting Options

### Render

Good for beginners.

Use **Background Worker**, not Web Service.

### Railway

Simple GitHub deployment.

### Oracle Cloud

Best for true 24/7 hosting using VPS + PM2.

Recommended for production use.

---

## Common Errors & Fixes

### Invalid NewsAPI Key

Error:

```text
apiKeyInvalid
```

Fix:
Use the correct NewsAPI key.

---

### Channel Not Found

Fix:
Check your `TARGET_CHANNEL_ID`.

---

### Bot Timeout on Render

Cause:
Using Web Service instead of Background Worker.

Fix:
Deploy as Background Worker.

---

### Invalid Discord Token

Fix:
Regenerate token from Discord Developer Portal.

---

## Future Improvements

### Multi-Server Support

Allow multiple servers to select their own target channels.

### Slash Commands

Examples:

* `/latest`
* `/help`
* `/aviation`

### AI News Summary

Automatically summarize aviation articles using AI.

### Database Upgrade

Replace `posted.json` with:

* MongoDB
* Redis
* PostgreSQL

### Top.gg Listing

Make the bot public and discoverable by all Discord users.

### Premium Features

Monetization options:

* Faster alerts
* AI summaries
* Custom airline tracking

---

## Research Paper Potential

This project can be converted into an IEEE research paper by focusing on:

* Automated information dissemination
* Event-driven system design
* Async architecture
* API integration
* Real-time alert systems
* AI-powered news summarization

### Possible Research Title

**Design and Implementation of an Automated Aviation News Aggregation Bot Using Discord API**

---

## Final Goal

Build a production-ready public aviation intelligence bot that can:

* Serve multiple Discord communities
* Provide real-time aviation alerts
* Use AI for smarter summaries
* Become monetizable as a SaaS product

---

## Author

**Shihan Ahmad**
B.Tech – Cyber Security
Focused on automation, cybersecurity, AI systems, and scalable Discord bot development.
