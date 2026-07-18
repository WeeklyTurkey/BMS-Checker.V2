#!/usr/bin/env python3
"""
BookMyShow Ticket Availability Checker
=======================================
Monitors a specific BookMyShow movie page and alerts via Telegram
when booking opens for a target theatre and date.

IMPORTANT: This script is for personal use only.
Keep polling at hourly frequency (via cron) to stay respectful
of BookMyShow's servers and Terms of Service.

Usage:
    python bms_checker.py --movie-url <URL> --theatre <NAME> --date <YYYY-MM-DD>
    
    Or set defaults in the CONFIG section below and run:
    python bms_checker.py
"""

import argparse
import html
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import re
import requests
from urllib.parse import urlsplit, urlunsplit

# ============================================================================
# CONFIG — Set your defaults here (CLI args override these)
# ============================================================================
DEFAULT_MOVIE_URL = ""       # e.g. "https://in.bookmyshow.com/buytickets/the-odyssey-chennai/movie-chen-ET00480917-MT/20260718"
DEFAULT_THEATRE_NAME = ""    # e.g. "PVR" or "INOX" (partial match, case-insensitive)
DEFAULT_TARGET_DATE = ""     # e.g. "2026-07-25" (YYYY-MM-DD format)

# State file — stores last-seen result to avoid duplicate alerts
STATE_FILE = Path(__file__).parent / "bms_state.json"

# Re-alert interval: if booking is open and you haven't marked it done,
# re-send a reminder after this many seconds (1 hour = 3600)
RE_ALERT_INTERVAL_SECONDS = 3600  # 1 hour

# IST timezone offset
IST = timezone(timedelta(hours=5, minutes=30))

# Realistic browser User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ============================================================================
# TELEGRAM
# ============================================================================

def get_telegram_config():
    """Load Telegram credentials from environment variables."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set as environment variables.")
        print("  export TELEGRAM_BOT_TOKEN='your-bot-token'")
        print("  export TELEGRAM_CHAT_ID='your-chat-id'")
        sys.exit(1)
    return token, chat_id


def send_telegram(token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram Bot API and return whether it succeeded."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        try:
            response_data = resp.json()
        except ValueError:
            response_data = {}

        if resp.status_code == 200 and response_data.get("ok") is True:
            print(f"✅ Telegram message sent successfully.")
            return True
        else:
            description = response_data.get("description") or resp.text
            print(f"⚠️  Telegram API failed ({resp.status_code}): {description}")
            return False
    except Exception as e:
        print(f"❌ Failed to send Telegram message: {e}")
        return False


# ============================================================================
# STATE MANAGEMENT
# ============================================================================

def load_state() -> dict:
    """Load persisted state from JSON file."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_state(state: dict):
    """Persist state to JSON file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def should_alert(state: dict, key: str) -> bool:
    """
    Determine if we should send an alert.
    
    Returns True if:
    - Theatre was not previously found (False → True transition)
    - Theatre was found before but the last alert was sent > RE_ALERT_INTERVAL_SECONDS ago
      AND the user hasn't manually marked it as done
    """
    entry = state.get(key, {})
    
    # If manually marked done, never re-alert
    if entry.get("done", False):
        print("ℹ️  Already marked as done. Skipping alert.")
        return False
    
    # If theatre wasn't found before, this is a new alert
    if not entry.get("theatre_found", False):
        return True
    
    # Theatre was already found — check re-alert interval
    last_alert_ts = entry.get("last_alert_timestamp", 0)
    now = time.time()
    elapsed = now - last_alert_ts
    
    if elapsed >= RE_ALERT_INTERVAL_SECONDS:
        print(f"ℹ️  Re-alerting (last alert was {elapsed/3600:.1f} hours ago).")
        return True
    else:
        remaining = (RE_ALERT_INTERVAL_SECONDS - elapsed) / 60
        print(f"ℹ️  Already alerted recently. Next re-alert in ~{remaining:.0f} minutes.")
        return False


# ============================================================================
# CORE: BookMyShow Page Checker
# ============================================================================

def check_bms(movie_url: str, theatre_name: str, target_date: str) -> dict:
    """
    Navigate to the BookMyShow movie page and check:
    1. Whether the target date has booking open (date tab is active/clickable vs greyed)
    2. If open, whether the target theatre appears in the listings
    
    Returns a dict with:
        - date_available: bool — whether the date tab is active (not greyed out)
        - theatre_found: bool — whether the target theatre is listed for that date
        - theatre_details: str — additional info about what was found
        - showtimes: list — showtime info if theatre found
        - movie_name: str — extracted movie name
    """
    from playwright.sync_api import sync_playwright
    
    # Parse the target date and construct the URL for that date
    # BMS URL pattern: .../buytickets/MOVIE_CODE/YYYYMMDD
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    date_str_for_url = target_dt.strftime("%Y%m%d")
    
    # Reconstruct the URL with the target date
    # BMS URLs look like: https://in.bookmyshow.com/buytickets/movie-name-city/movie-city-CODE-MT/YYYYMMDD
    # We replace the last segment (date) or append it
    # Replace only the path's final date segment, preserving query strings and
    # fragments. This also handles URLs copied from BMS with a trailing slash.
    parsed_url = urlsplit(movie_url)
    path = parsed_url.path.rstrip("/")
    path_parts = path.split("/")
    if path_parts and re.fullmatch(r"\d{8}", path_parts[-1] or ""):
        path_parts[-1] = date_str_for_url
    else:
        path_parts.append(date_str_for_url)
    target_url = urlunsplit((parsed_url.scheme, parsed_url.netloc,
                             "/".join(path_parts), parsed_url.query,
                             parsed_url.fragment))
    
    result = {
        "date_available": False,
        "theatre_found": False,
        "theatre_details": "",
        "showtimes": [],
        "movie_name": "",
        "target_url": target_url,
    }
    
    print(f"🔍 Checking: {target_url}")
    print(f"🎯 Looking for theatre: '{theatre_name}' on date: {target_date}")
    
    with sync_playwright() as p:
        # Check if ScraperAPI is configured for residential proxying
        scraper_api_key = os.environ.get("SCRAPER_API_KEY")
        launch_kwargs = {"headless": True}
        
        if scraper_api_key:
            print("🛡️  Routing request through ScraperAPI (India Proxy)...")
            launch_kwargs["proxy"] = {
                "server": "http://proxy-server.scraperapi.com:8001",
                "username": "scraperapi.country_code=in",
                "password": scraper_api_key
            }
            
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=bool(scraper_api_key)
        )
        page = context.new_page()
        
        try:
            # Navigate to the target date URL (increase timeout if using proxy)
            goto_timeout = 90000 if scraper_api_key else 30000
            page.goto(target_url, wait_until="domcontentloaded", timeout=goto_timeout)
            # Wait for dynamic content to render
            page.wait_for_timeout(5000)
            
            # ── Step 1: Handle any popups/modals ──
            _dismiss_popups(page)
            
            # ── Step 2: Extract movie name ──
            result["movie_name"] = _extract_movie_name(page)
            print(f"🎬 Movie: {result['movie_name']}")
            
            # ── Step 3: Check if the target date is available ──
            # BMS behavior: If a date hasn't opened for booking yet,
            # navigating to its URL does NOT redirect. It just silently loads the
            # nearest available date (usually today). We MUST verify the date tab exists.
            
            current_url = page.url
            print(f"📍 Landed on URL: {current_url}")
            
            # Verify by checking the date tabs in the DOM
            date_available = _check_date_tab(page, target_date, target_dt)
            result["date_available"] = date_available
            
            if not date_available:
                result["theatre_details"] = (
                    f"NOT AVAILABLE: date {target_date} is not present in the "
                    "BookMyShow date picker"
                )
                print(f"⚠️  NOT AVAILABLE: date {target_date} is not present in the date picker.")
                return result
            
            print(f"✅ Date {target_date} is available for booking!")
            
            # ── Step 4: Check if target theatre is listed ──
            theatre_found, details, showtimes = _find_theatre(page, theatre_name)
            result["theatre_found"] = theatre_found
            result["theatre_details"] = details
            result["showtimes"] = showtimes
            
            if theatre_found:
                print(f"🎉 Theatre '{theatre_name}' FOUND with {len(showtimes)} showtime(s)!")
            else:
                print(f"❌ Theatre '{theatre_name}' not found in listings for {target_date}.")
            
        except Exception as e:
            print(f"❌ Error during page check: {e}")
            traceback.print_exc()
            raise
        finally:
            browser.close()
    
    return result


def _dismiss_popups(page):
    """Dismiss any cookie consent banners, age confirmations, or modal overlays."""
    popup_selectors = [
        # Cookie consent
        'button:has-text("Accept")',
        'button:has-text("Got it")',
        # Age confirmation
        'button:has-text("Continue")',
        # Close buttons on modals
        '[class*="close-btn"]',
        '[class*="CloseBtn"]',
        'button[aria-label="Close"]',
    ]
    for selector in popup_selectors:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(500)
        except Exception:
            pass


def _extract_movie_name(page) -> str:
    """Try to extract the movie name from the page."""
    # BMS typically has the movie name in the page title or header
    selectors = [
        'h1',
        '[class*="MovieTitle"]',
        '[class*="movie-title"]',
        '[class*="showtime-header"] h1',
        'title',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if text and len(text) < 200:
                    return text
        except Exception:
            pass
    
    # Fallback: extract from page title
    title = page.title()
    if title:
        # Page titles are like "Movie Name Showtimes in City..."
        return title.split(" Movie")[0].split(" Showtimes")[0].strip()
    return "Unknown Movie"


def _check_date_tab(page, target_date: str, target_dt: datetime) -> bool:
    """
    Check whether the target date exists in the date picker.
    BMS does not redirect when an invalid/future date is requested; it just defaults
    to today's date in the UI. 
    If the requested date tab is not rendered in the DOM, booking is not open.
    """
    date_str_for_url = target_date.replace("-", "")
    try:
        # BMS currently renders date-picker entries as divs whose IDs are the
        # date code (for example id="20260720"), not as a.date-href anchors.
        # Check that exact ID first so cinema/showtime links cannot create a
        # false positive.
        date_elements = page.locator(f'[id="{date_str_for_url}"]')
        if date_elements.count() > 0:
            tab = date_elements.first
            try:
                tab.click()
                page.wait_for_timeout(2500)
                print(f"   ✅ Found and selected date {target_date} in the date picker")
            except Exception as e:
                print(f"   ⚠️  Date {target_date} exists but could not be selected: {e}")
                return False
            return True

        # Compatibility fallback for older BMS markup.
        tabs = page.locator("a.date-href")
        matching = []
        for tab in tabs.all():
            href = tab.get_attribute("href") or ""
            href_date = re.search(r"/(\d{8})(?:[/?#]|$)", href)
            text = re.sub(r"\s+", " ", tab.inner_text()).strip().upper()
            if (href_date and href_date.group(1) == date_str_for_url) or (
                str(target_dt.day) in text and target_dt.strftime("%b").upper() in text
            ):
                matching.append(tab)

        if not matching:
            print(f"   ❌ Date tab for {target_date} not found in the BMS date picker.")
            return False

        tab = matching[0]
        classes = (tab.get_attribute("class") or "").lower()
        parent_classes = (tab.locator("..").get_attribute("class") or "").lower()
        aria_current = (tab.get_attribute("aria-current") or "").lower()
        is_active = "active" in classes or "active" in parent_classes or aria_current in {"date", "true", "page"}
        if not is_active:
            try:
                tab.click()
                page.wait_for_timeout(2500)
                print(f"   ✅ Selected date tab for {target_date}")
            except Exception as e:
                print(f"   ⚠️  Date tab exists but could not be selected: {e}")
                return False
        else:
            print(f"   ✅ Date tab for {target_date} is active")
        return True
    except Exception as e:
        print(f"   ⚠️  Error checking date tabs: {e}")
        return False


def _find_theatre(page, theatre_name: str) -> tuple:
    """
    Search the showtimes listing for the target theatre.
    
    Returns: (found: bool, details: str, showtimes: list[str])
    """
    theatre_name_lower = theatre_name.lower()
    found = False
    details = ""
    showtimes = []
    
    try:
        # Get the full page text to do a broad search first
        body_text = page.inner_text("body")
        
        if theatre_name_lower not in body_text.lower():
            return False, f"Theatre '{theatre_name}' not mentioned anywhere on the page", []
        
        # Now find the specific theatre element
        # BMS theatre listings are typically in containers with the theatre name
        # Common patterns: <a> tags with theatre name, <div> with cinema info
        
        # Strategy: find all elements that contain the theatre name
        # and then look for nearby showtime elements
        
        # Try multiple selector strategies
        theatre_selectors = [
            f'a:has-text("{theatre_name}")',
            f'div:has-text("{theatre_name}")',
            f'span:has-text("{theatre_name}")',
        ]
        
        theatre_container = None
        
        for selector in theatre_selectors:
            try:
                elements = page.query_selector_all(selector)
                for el in elements:
                    text = el.inner_text().strip()
                    # Find the most specific element containing just the theatre name
                    if theatre_name_lower in text.lower() and len(text) < 500:
                        # Walk up to find the parent container that includes showtimes
                        parent = el
                        for _ in range(10):  # Walk up max 10 levels
                            try:
                                parent_el = parent.evaluate_handle("el => el.parentElement")
                                if not parent_el:
                                    break
                                parent_text = parent_el.evaluate("el => el.innerText || ''")
                            except Exception:
                                break
                            # Theatre containers typically have time patterns like "10:00" or "PM"
                            if ("AM" in parent_text or "PM" in parent_text) and len(parent_text) < 2000:
                                theatre_container = parent_el
                                break
                            parent = parent_el
                        
                        if theatre_container:
                            break
                if theatre_container:
                    break
            except Exception:
                continue
        
        if theatre_container:
            # Extract showtimes using the current BMS markup. BMS renders these
            # as div[role="button"] rather than links, and its generated class
            # names encode the state: eUDeRW = available, hlrCBW = fast
            # filling, clNJKa = unavailable/sold out in the current UI.
            times_found = theatre_container.evaluate('''el => {
                const times = [];
                const elements = el.querySelectorAll('a, div[role="button"]');
                for (const child of elements) {
                    let text = child.innerText || '';
                    if (text.match(/^\\s*\\d{1,2}:\\d{2}\\s*(?:AM|PM|am|pm)/i) && text.length < 120) {
                        let isAvailable = true;

                        const className = String(child.className || '').toLowerCase();
                        const ariaDisabled = child.getAttribute('aria-disabled') === 'true';
                        const nativeDisabled = child.hasAttribute('disabled');
                        const style = window.getComputedStyle(child);
                        if (ariaDisabled || nativeDisabled ||
                            style.pointerEvents === 'none' ||
                            style.display === 'none' ||
                            style.visibility === 'hidden') {
                            isAvailable = false;
                        }

                        // Current BMS React markup uses these generated classes
                        // for showtime states. Keep the positive allow-list so
                        // a grey/sold-out slot cannot trigger an alert.
                        const currentBmsAvailable =
                            className.includes('euderw') || className.includes('hlrcbw');
                        const currentBmsUnavailable =
                            className.includes('clnjka') ||
                            className.includes('sold') ||
                            className.includes('disabled') ||
                            className.includes('grey') ||
                            className.includes('unavailable');
                        if (currentBmsUnavailable ||
                            (child.getAttribute('role') === 'button' && !currentBmsAvailable)) {
                            isAvailable = false;
                        }
                        
                        if (isAvailable) {
                            times.push(text.trim());
                        }
                    }
                }
                // deduplicate
                return Array.from(new Set(times));
            }''')
            
            showtimes = [t.strip() for t in times_found]
            
            if showtimes:
                found = True
                details = f"Theatre '{theatre_name}' found with {len(showtimes)} AVAILABLE showtime(s): {', '.join(showtimes)}"
            else:
                found = False
                details = f"Theatre '{theatre_name}' found, but all showtimes appear SOLD OUT (greyed out)."
        else:
            # Fallback: theatre name exists on page but we couldn't isolate the container
            # This still means the theatre is listed, but we don't know if they are available.
            # To avoid false positives on sold-out days, we assume not found if we can't verify times.
            found = False
            details = f"Theatre '{theatre_name}' found on page but could not extract showtimes to verify availability."
    
    except Exception as e:
        print(f"   ⚠️  Error searching for theatre: {e}")
        traceback.print_exc()
        return False, f"Error searching for theatre: {e}", []
    
    return found, details, showtimes


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="BookMyShow Ticket Availability Checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bms_checker.py --movie-url "https://in.bookmyshow.com/buytickets/the-odyssey-chennai/movie-chen-ET00480917-MT/20260718" --theatre "PVR" --date "2026-07-25"
  python bms_checker.py --mark-done    # Stop re-alerting after you've booked
  python bms_checker.py --reset        # Reset state to start checking again
        """
    )
    parser.add_argument("--movie-url", default=DEFAULT_MOVIE_URL,
                       help="BookMyShow movie page URL (showtimes page)")
    parser.add_argument("--theatre", default=DEFAULT_THEATRE_NAME,
                       help="Theatre name to match (partial, case-insensitive)")
    parser.add_argument("--date", default=DEFAULT_TARGET_DATE,
                       help="Target date in YYYY-MM-DD format")
    parser.add_argument("--targets",
                       help="Path to a JSON file containing an array of targets to check (overrides individual flags)")
    parser.add_argument("--mark-done", action="store_true",
                       help="Mark current check as done (stop re-alerting)")
    parser.add_argument("--reset", action="store_true",
                       help="Reset state file to start fresh")
    
    args = parser.parse_args()
    
    # Handle --mark-done
    if args.mark_done:
        state = load_state()
        for key in state:
            state[key]["done"] = True
        save_state(state)
        print("✅ Marked as done. No more re-alerts will be sent.")
        print("   Run with --reset to start checking again.")
        return
    
    # Handle --reset
    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        print("✅ State reset. Will check fresh on next run.")
        return
    
    # Validate required config
    targets = []
    
    if args.targets:
        targets_file = Path(args.targets)
        if targets_file.exists():
            try:
                with open(targets_file, "r") as f:
                    targets = json.load(f)
            except Exception as e:
                print(f"ERROR: Failed to parse targets file {args.targets}: {e}")
                sys.exit(1)
        else:
            print(f"ERROR: Targets file {args.targets} does not exist.")
            sys.exit(1)
    else:
        movie_url = args.movie_url
        theatre_name = args.theatre
        target_date = args.date
        
        if not movie_url:
            print("ERROR: --movie-url is required (or set DEFAULT_MOVIE_URL) if --targets is not used")
            sys.exit(1)
        if not theatre_name:
            print("ERROR: --theatre is required (or set DEFAULT_THEATRE_NAME) if --targets is not used")
            sys.exit(1)
        if not target_date:
            print("ERROR: --date is required (or set DEFAULT_TARGET_DATE) if --targets is not used")
            sys.exit(1)
            
        targets = [{
            "movie_url": movie_url,
            "theatre": theatre_name,
            "date": target_date
        }]
    
    # Validate required keys and date format for all targets
    for i, t in enumerate(targets):
        for required_key in ["theatre", "date"]:
            if required_key not in t:
                print(f"ERROR: Target {i+1} is missing required key '{required_key}'")
                sys.exit(1)
        if not (t.get("movie_url") or t.get("url")):
            print(f"ERROR: Target {i+1} is missing 'movie_url' or 'url'")
            sys.exit(1)
        try:
            datetime.strptime(t["date"], "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: Invalid date format '{t['date']}' in target {i+1}. Use YYYY-MM-DD.")
            sys.exit(1)
    
    # Load Telegram config
    tg_token, tg_chat_id = get_telegram_config()
    
    print("=" * 60)
    print(f"🎬 BookMyShow Checker — {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"🎯 Checking {len(targets)} target(s)...")
    print("=" * 60)
    
    state = load_state()
    
    for i, target in enumerate(targets):
        movie_url = target.get("movie_url") or target.get("url")
        theatre_name = target["theatre"]
        target_date = target["date"]
        
        if not movie_url:
            print(f"⚠️  Skipping target {i+1}: missing 'movie_url' or 'url' value")
            continue
        
        state_key = f"{theatre_name.lower()}_{target_date}"
        print(f"\n▶️  Target {i+1}/{len(targets)}: {theatre_name} on {target_date}")
        
        try:
            result = check_bms(movie_url, theatre_name, target_date)
        except Exception as e:
            # Page check failed — send error alert so user doesn't miss a silent failure
            error_msg = (
                f"⚠️ <b>BMS Checker Script FAILED</b> ⚠️\n\n"
                f"Error: <code>{str(e)[:200]}</code>\n\n"
                f"🎬 Movie URL: {movie_url}\n"
                f"🏢 Theatre: {theatre_name}\n"
                f"📅 Date: {target_date}\n\n"
                f"⏰ Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}\n\n"
                f"👉 <b>Check manually — the script might be broken or BMS changed their page structure.</b>"
            )
            send_telegram(tg_token, tg_chat_id, error_msg)
            print(f"❌ Script failed for this target. Error alert sent via Telegram.")
            save_state(state)  # Save state so far to avoid losing progress
            continue
        
        print("-" * 40)
        print("📊 RESULTS")
        print(f"   Date available:  {result['date_available']}")
        print(f"   Theatre found:   {result['theatre_found']}")
        print(f"   Details:         {result['theatre_details']}")
        print(f"   Movie:           {result['movie_name']}")
        if result['showtimes']:
            print(f"   Showtimes:       {', '.join(result['showtimes'])}")
        
        # Determine if we should alert
        trigger = result["date_available"] and result["theatre_found"]
        
        if trigger and should_alert(state, state_key):
            # 🎉 BOOKING IS OPEN — Send alert!
            showtime_str = ", ".join(result["showtimes"]) if result["showtimes"] else "check page for times"
            
            message = (
                f"🎟️🎟️🎟️ <b>TICKETS ARE OPEN!</b> 🎟️🎟️🎟️\n\n"
                f"🎬 <b>{result['movie_name']}</b>\n"
                f"🏢 <b>{theatre_name}</b>\n"
                f"📅 <b>{target_date}</b>\n"
                f"🕐 Showtimes: {showtime_str}\n\n"
                f"🔗 <a href=\"{result['target_url']}\">BOOK NOW on BookMyShow</a>\n\n"
                f"⚡ <b>GO GO GO — Book before it sells out!</b>\n\n"
                f"<i>Run <code>python bms_checker.py --mark-done</code> after booking to stop reminders.</i>"
            )
            send_telegram(tg_token, tg_chat_id, message)
            
            # Update state
            state[state_key] = {
                "theatre_found": True,
                "date_available": True,
                "last_alert_timestamp": time.time(),
                "last_alert_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                "done": False,
                "details": result["theatre_details"],
            }
            print("🎉 ALERT SENT! Booking is open!")
            
        elif trigger:
            # Theatre found but we already alerted recently — just update state
            print("✅ Booking is still open (already alerted recently).")
            
        elif result["date_available"] and not result["theatre_found"]:
            # Date is open but theatre not listed
            print(f"ℹ️  Date {target_date} is open, but theatre '{theatre_name}' is not listed.")
            
            # Update state — theatre not found
            state[state_key] = {
                "theatre_found": False,
                "date_available": True,
                "last_check_timestamp": time.time(),
                "last_check_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                "done": state.get(state_key, {}).get("done", False),
                "details": result["theatre_details"],
            }
            
        else:
            # Date not available yet. Notify once so a successful workflow does
            # not look silent, then suppress repeats until the state changes.
            print(f"⏳ NOT AVAILABLE: booking has not opened for {target_date}.")
            previous = state.get(state_key, {})
            if not previous.get("status_notified", False):
                status_message = (
                    f"ℹ️ <b>BMS availability update</b>\n\n"
                    f"🎬 <b>{html.escape(result['movie_name'] or 'Movie')}</b>\n"
                    f"🏢 <b>{html.escape(theatre_name)}</b>\n"
                    f"📅 <b>{html.escape(target_date)}</b>\n\n"
                    f"❌ <b>NOT AVAILABLE</b>\n"
                    f"{html.escape(result['theatre_details'])}"
                )
                status_sent = send_telegram(tg_token, tg_chat_id, status_message)
            else:
                status_sent = True
            
            # Update state
            state[state_key] = {
                "theatre_found": False,
                "date_available": False,
                "last_check_timestamp": time.time(),
                "last_check_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                "done": state.get(state_key, {}).get("done", False),
                "details": result["theatre_details"],
                "status_notified": previous.get("status_notified", False) or status_sent,
            }
    
    # Save the accumulated state once after processing all targets
    save_state(state)
    
    print("\n" + "=" * 60)
    print(f"🕐 Next check: whenever cron runs this script again.")


if __name__ == "__main__":
    main()
