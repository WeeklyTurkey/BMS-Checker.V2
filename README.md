# 🎟️ BookMyShow Ticket Availability Checker

A Python script that monitors a BookMyShow movie page and sends you a **Telegram alert** the moment booking opens for your target theatre and date.

> **⚠️ Personal use only.** This script is designed to be run hourly via cron. Please respect BookMyShow's servers and Terms of Service — don't increase the polling frequency beyond once per hour.

---

## How It Works

```
┌─────────────────────────────────────────────────┐
│  cron (every hour)                              │
│  └─► python bms_checker.py                      │
│       ├─► Launch headless Chromium (Playwright)  │
│       ├─► Navigate to BMS movie page + date      │
│       ├─► Check: Is target date tab active?      │
│       │   └─ NO  → "Not open yet" → exit         │
│       │   └─ YES → Check: Is theatre listed?     │
│       │       └─ NO  → exit                       │
│       │       └─ YES → 🎉 Send Telegram alert!   │
│       └─► Save state (avoid duplicate alerts)    │
└─────────────────────────────────────────────────┘
```

**Key detection method:** When you navigate to a BMS showtimes URL for a date that hasn't opened for booking yet, BMS **redirects you back** to the nearest available date. The script detects this redirect as the primary signal. It also cross-checks the date tab styling in the DOM.

---

## Prerequisites

- **Python 3.9+**
- **pip** (Python package manager)
- A **Telegram Bot** (free, takes 2 minutes to set up)

---

## Step 1: Install Dependencies

```bash
# Clone or download this project, then:
cd bms-checker

# Install Python packages
pip install -r requirements.txt

# Install Playwright's Chromium browser (required, ~150MB download)
python -m playwright install chromium

# On Linux, you may also need system dependencies:
python -m playwright install-deps chromium
```

---

## Step 2: Set Up Telegram Bot

You need a Telegram Bot to receive alerts. Here's how to create one:

### 2a. Create a Bot via @BotFather

1. Open Telegram and search for **@BotFather** (or go to [t.me/BotFather](https://t.me/BotFather))
2. Send `/newbot`
3. Choose a **name** for your bot (e.g., "BMS Ticket Alert")
4. Choose a **username** (must end in `bot`, e.g., `bms_ticket_checker_bot`)
5. BotFather will give you a **token** like: `7123456789:AAH1234abcd5678efgh`
6. **Save this token** — this is your `TELEGRAM_BOT_TOKEN`

### 2b. Get Your Chat ID

1. Open Telegram and search for your newly created bot
2. Send it any message (e.g., "hello")
3. Open this URL in your browser (replace `YOUR_TOKEN` with your bot token):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
4. Look for `"chat":{"id":123456789}` in the response
5. **Save this number** — this is your `TELEGRAM_CHAT_ID`

### 2c. Set Environment Variables

```bash
# Add these to your shell profile (~/.zshrc, ~/.bashrc, etc.)
export TELEGRAM_BOT_TOKEN="7123456789:AAH1234abcd5678efgh"
export TELEGRAM_CHAT_ID="123456789"

# Then reload:
source ~/.zshrc   # or ~/.bashrc
```

---

## Step 3: Find Your BMS Movie URL

1. Go to [bookmyshow.com](https://in.bookmyshow.com)
2. Select your **city**
3. Find your **movie** and click on it
4. Click **"Book Tickets"**
5. If prompted, select the **format** (2D, IMAX, etc.)
6. You should now be on the **showtimes page** — copy this URL

The URL will look something like:
```
https://in.bookmyshow.com/buytickets/the-odyssey-chennai/movie-chen-ET00480917-MT/20260718
```

---

## Step 4: Run the Script

### Basic Usage

```bash
python bms_checker.py \
  --movie-url "https://in.bookmyshow.com/buytickets/the-odyssey-chennai/movie-chen-ET00480917-MT/20260718" \
  --theatre "PVR" \
  --date "2026-07-25"
```

### CLI Arguments

| Argument | Description | Example |
|----------|-------------|---------|
| `--movie-url` | BMS showtimes page URL | `"https://in.bookmyshow.com/buytickets/..."` |
| `--theatre` | Theatre name (partial match, case-insensitive) | `"PVR"`, `"INOX"`, `"Palazzo"` |
| `--date` | Target date (YYYY-MM-DD) | `"2026-07-25"` |
| `--mark-done` | Stop re-alerting (run after you've booked) | — |
| `--reset` | Reset state file for fresh checking | — |

### Set Defaults (Optional)

Instead of passing CLI args every time, edit the `CONFIG` section at the top of `bms_checker.py`:

```python
DEFAULT_MOVIE_URL = "https://in.bookmyshow.com/buytickets/..."
DEFAULT_THEATRE_NAME = "PVR"
DEFAULT_TARGET_DATE = "2026-07-25"
```

Then just run: `python bms_checker.py`

### Checking Multiple Movies
You can check multiple movies, theatres, or dates simultaneously by creating a `targets.json` file in the same folder:

```json
[
  {
    "url": "https://in.bookmyshow.com/buytickets/movie1...",
    "theatre": "PVR",
    "date": "2026-07-25"
  },
  {
    "url": "https://in.bookmyshow.com/buytickets/movie2...",
    "theatre": "INOX",
    "date": "2026-07-26"
  }
]
```
Then run the script pointing to this file:
```bash
python bms_checker.py --targets targets.json
```

---

## Step 5: Set Up Cron (Automated Hourly Checks)

### On macOS / Linux

```bash
# Open crontab editor
crontab -e

# Add this line (runs every hour at minute 0):
0 * * * * cd /path/to/bms-checker && /usr/bin/env TELEGRAM_BOT_TOKEN="your-token" TELEGRAM_CHAT_ID="your-chat-id" /path/to/python bms_checker.py --movie-url "YOUR_URL" --theatre "YOUR_THEATRE" --date "YOUR_DATE" >> /path/to/bms-checker/cron.log 2>&1
```

**Example with real paths:**

```bash
0 * * * * cd /Users/apple/.gemini/antigravity-ide/scratch/bms-checker && /usr/bin/env TELEGRAM_BOT_TOKEN="7123456789:AAH1234abcd" TELEGRAM_CHAT_ID="123456789" /usr/local/bin/python3 bms_checker.py --movie-url "https://in.bookmyshow.com/buytickets/the-odyssey-chennai/movie-chen-ET00480917-MT/20260718" --theatre "PVR" --date "2026-07-25" >> cron.log 2>&1
```

> **💡 Tip:** To find your Python path, run: `which python3`

### Verify Cron Is Working

```bash
# List your cron jobs
crontab -l

# Check the log after the next hour
tail -f /path/to/bms-checker/cron.log
```

---

## ☁️ Run on GitHub Actions (Recommended — No Laptop Needed)

The easiest way to run this 24/7 without your laptop is **GitHub Actions** — it's **completely free** and runs on GitHub's servers.

### 6a. Create a GitHub Repository

1. Go to [github.com/new](https://github.com/new) and create a **private** repository (e.g., `bms-checker`)
2. Push this project to the repo:

```bash
cd /Users/apple/.gemini/antigravity-ide/scratch/bms-checker
git init
git add .
git commit -m "Initial commit: BMS ticket checker"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/bms-checker.git
git push -u origin main
```

### 6b. Add Secrets (Telegram credentials)

1. Go to your repo on GitHub → **Settings** → **Secrets and variables** → **Actions**
2. Under **Secrets** tab, click **New repository secret** and add:
   - Name: `TELEGRAM_BOT_TOKEN` → Value: your bot token
   - Name: `TELEGRAM_CHAT_ID` → Value: your chat ID
   - Name: `SCRAPER_API_KEY` → Value: your ScraperAPI key

   **Primary provider: [ScraperAPI](https://scraperapi.com)**, via its proxy
   port method (`proxy-server.scraperapi.com:8001`, username `scraperapi.country_code=in`,
   password = your API key — this is all wired up automatically once
   `SCRAPER_API_KEY` is set).

   ⚠️ **Cost heads-up:** ScraperAPI's standard `country_code` geotargeting
   only covers US/EU on Hobby and Startup plans — India geotargeting
   (`country_code=in`) requires a **Business/Enterprise plan**. On a lower
   tier, requesting India IPs can silently fall back to Premium/residential
   billing *per request*, which is the most likely reason credits vanished
   in ~1.5 days on a 5,000-credit trial. Check your plan tier before relying
   on `country_code=in`.

   The script now also blocks (a) images/fonts/media before download, and
   (b) requests to known ad/analytics/tracker/recaptcha domains —
   `doubleclick.net`, `googletagmanager.com`, `google.com`, `branch.io`,
   `app.link`, and Google Fonts — via `BLOCK_RESOURCE_TYPES` and
   `BLOCK_DOMAINS` in `bms_checker.py`. Per-domain analytics showed those
   tracker domains alone accounting for close to half of billed credits on
   a real run, contributing zero useful data. Each check now prints a
   summary line (`📊 Requests: N allowed, N blocked...`) so you can see the
   savings directly in the logs.

   **Alternative / bring-your-own provider:** if you'd rather not deal with
   ScraperAPI's plan-tier geotargeting limits, leave `SCRAPER_API_KEY`
   unset and instead set `PROXY_SERVER` / `PROXY_USERNAME` / `PROXY_PASSWORD`
   to any provider with a plain HTTP proxy gateway — e.g.
   [DataImpulse](https://dataimpulse.com) (pay-as-you-go by the GB, balance
   never expires), IPRoyal, Webshare, Smartproxy, or Bright Data. You want
   *residential*, not datacenter, IPs with India geo-targeting, since BMS
   blocks most datacenter ranges. These three env vars are only used as a
   fallback when `SCRAPER_API_KEY` isn't set.

### 6c. Add Variables (Movie config)

1. Still in **Settings** → **Secrets and variables** → **Actions**
2. Switch to the **Variables** tab, click **New repository variable** and add:
   - Name: `BMS_MOVIE_URL` → Value: your BMS movie URL
   - Name: `BMS_THEATRE_NAME` → Value: your theatre name (e.g., `PVR`)
   - Name: `BMS_TARGET_DATE` → Value: your date (e.g., `2026-07-25`)

### 6d. Verify It Works

1. Go to your repo → **Actions** tab
2. Click on **"BMS Ticket Checker"** workflow on the left
3. Click **"Run workflow"** → **"Run workflow"** (manual trigger to test)
4. Watch the run — you should see logs and get a Telegram message if booking is open

The workflow will now automatically run **every hour** on GitHub's servers. ✅

> **💡 Checking Multiple Movies on GitHub**: 
> If you want to check multiple movies, create a `targets.json` file in your repository (see format above) and commit it. The GitHub Action will automatically detect it and check all movies in the file, ignoring the single-movie variables.

> **💡 To change the single movie/theatre/date later**, just update the repository variables in GitHub Settings.

> **💡 To stop the checker**, go to Actions → BMS Ticket Checker → click the `⋯` menu → Disable workflow.

---

## After You've Booked

Once you've successfully booked your tickets, you have two options:

**Option A — Disable the GitHub Actions workflow:**
Go to your repo → Actions → BMS Ticket Checker → `⋯` → **Disable workflow**

**Option B — Mark as done locally (if running via cron):**
```bash
python bms_checker.py --mark-done
```

To start checking again (e.g., for a different date):
```bash
python bms_checker.py --reset
```

---

## Alert Messages You'll Receive

### ✅ Booking Opened (Success)
```
🎟️🎟️🎟️ TICKETS ARE OPEN! 🎟️🎟️🎟️

🎬 The Odyssey
🏢 PVR
📅 2026-07-25
🕐 Showtimes: 10:00 AM, 04:00 PM, 07:30 PM

🔗 BOOK NOW on BookMyShow

⚡ GO GO GO — Book before it sells out!
```

### ⚠️ Script Error
```
⚠️ BMS Checker Script FAILED ⚠️

Error: TimeoutError: page.goto: Timeout 30000ms exceeded...

👉 Check manually — the script might be broken or BMS changed their page structure.
```

Before this alert fires, the script retries the same target up to
`RETRY_ATTEMPTS` times with backoff (residential proxy hiccups are common
and usually resolve on retry). If a target keeps failing with the exact
same error, you'll only get re-alerted once an hour for it, not every run,
so a known ongoing issue doesn't spam you — a *different* error still
alerts immediately.

---

## Re-Alert Behavior

The script re-alerts you **every 1 hour** if booking is still open and you haven't run `--mark-done`. This is intentional so you don't miss it even if you missed the first alert.

To change the re-alert interval, edit `RE_ALERT_INTERVAL_SECONDS` in the script:

```python
RE_ALERT_INTERVAL_SECONDS = 3600   # 1 hour (default)
RE_ALERT_INTERVAL_SECONDS = 7200   # 2 hours
RE_ALERT_INTERVAL_SECONDS = 10800  # 3 hours
```

---

## State File

The script creates a `bms_state.json` file in the same directory to track:
- Whether the theatre was previously found
- When the last alert was sent
- Whether you've marked it as done

You can inspect it anytime:

```bash
cat bms_state.json
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `playwright._impl._errors.Error: Executable doesn't exist` | Run `python -m playwright install chromium` |
| `ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set` | Set the env vars (see Step 2c) |
| Script runs but no Telegram message | Send a message to your bot first, then re-check your Chat ID |
| Cron job not running | Check `crontab -l`, ensure full paths are used, check `cron.log` |
| BMS page structure changed | The script sends a failure alert via Telegram. Open an issue or update selectors. |
| `Error: Browser closed` on Linux server | Run `python -m playwright install-deps chromium` for system libs |
| GitHub Actions not triggering | Check Actions tab for errors. Scheduled workflows may be delayed by a few minutes. |
| Proxy/API "out of credits" or 407/402 errors | Check your provider's dashboard balance. Pay-as-you-go providers like DataImpulse don't expire balances, so this usually means you're actually out of funds — top up, or switch providers by updating the three `PROXY_*` secrets. |

---

## Project Structure

```
bms-checker/
├── .github/
│   └── workflows/
│       └── check-tickets.yml   # GitHub Actions workflow (runs every hour)
├── .gitignore                  # Ignores state files, caches, debug dumps
├── bms_checker.py              # Main script
├── requirements.txt            # Python dependencies
├── README.md                   # This file
├── targets.json                # Optional: multi-target config
├── dev/                        # One-off scripts used while reverse-engineering
│   └── ...                     # BMS's DOM (not part of the running pipeline)
├── bms_state.json              # Auto-generated state file (after first run)
└── cron.log                    # Cron output log (if using local cron)
```

> The `dev/` scripts were used to inspect BMS's page structure while
> building the selectors in `bms_checker.py` (date tabs, theatre containers,
> showtime buttons). They hit BMS directly, bypass the proxy, and aren't
> needed to run the checker — kept only as a reference if BMS changes its
> markup again and you need to re-inspect it.

---

## License

This project is for personal, educational use only. Use responsibly and respect BookMyShow's Terms of Service.
