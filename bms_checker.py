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
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

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


def send_telegram(token: str, chat_id: str, message: str):
    """Send a message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            print(f"✅ Telegram message sent successfully.")
        else:
            print(f"⚠️  Telegram API returned {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"❌ Failed to send Telegram message: {e}")


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
    url_parts = movie_url.rstrip("/").split("/")
    
    # Check if last segment looks like a date (8 digits)
    if url_parts[-1].isdigit() and len(url_parts[-1]) == 8:
        url_parts[-1] = date_str_for_url
    else:
        url_parts.append(date_str_for_url)
    
    target_url = "/".join(url_parts)
    
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
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()
        
        try:
            # Navigate to the target date URL
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            # Wait for dynamic content to render
            page.wait_for_timeout(5000)
            
            # ── Step 1: Handle any popups/modals ──
            _dismiss_popups(page)
            
            # ── Step 2: Extract movie name ──
            result["movie_name"] = _extract_movie_name(page)
            print(f"🎬 Movie: {result['movie_name']}")
            
            # ── Step 3: Check if the target date is available ──
            #
            # BMS behavior: If a date hasn't opened for booking yet,
            # navigating to its URL redirects back to the nearest available date.
            # We detect this by checking:
            #   a) The current URL's date segment
            #   b) Which date tab is currently selected/highlighted
            #   c) Whether the target date tab appears greyed out
            
            current_url = page.url
            print(f"📍 Landed on URL: {current_url}")
            
            # Check if we got redirected away from our target date
            if date_str_for_url not in current_url:
                print(f"⚠️  Redirected away from target date {target_date}!")
                print(f"   This means booking has NOT opened for this date yet.")
                result["date_available"] = False
                result["theatre_details"] = f"Date {target_date} not yet available (redirected to different date)"
                browser.close()
                return result
            
            # Also verify by checking the date tabs in the DOM
            date_available = _check_date_tab(page, target_date, target_dt)
            result["date_available"] = date_available
            
            if not date_available:
                result["theatre_details"] = f"Date {target_date} tab appears greyed out / not yet open"
                print(f"⚠️  Date tab for {target_date} appears greyed out.")
                browser.close()
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
    Check whether the target date tab is active (booking open) or greyed out.
    
    BMS date tabs structure:
    - Active/available dates are inside clickable <a> or interactive <div> elements
    - Greyed/unavailable dates have reduced opacity or different styling
    - The currently selected date has a highlight (usually red/pink background)
    
    If we successfully navigated to the target date URL without redirect,
    the date is very likely available. This function does additional DOM verification.
    """
    target_day = str(target_dt.day)
    
    try:
        # Look for date scroll container and individual date items
        # BMS uses a horizontal scrollable date picker
        date_items = page.query_selector_all('[class*="date-"] a, [class*="Date"] a, [class*="scroll"] a[href*="buytickets"]')
        
        if date_items:
            for item in date_items:
                text = item.inner_text().strip()
                href = item.get_attribute("href") or ""
                
                date_str = target_dt.strftime("%Y%m%d")
                if date_str in href:
                    # Found a link for our target date — it's clickable = available
                    print(f"   ✅ Found clickable date tab for {target_date}")
                    return True
        
        # Alternative approach: check all elements containing the day number
        # and see if any match our target date
        all_date_elements = page.query_selector_all('[class*="date"], [class*="Date"], [class*="day"], [class*="Day"]')
        
        for el in all_date_elements:
            text = el.inner_text().strip()
            if target_day in text:
                # Check if this element or its parent has a 'disabled' or 'grey' indicator
                classes = el.get_attribute("class") or ""
                opacity = el.evaluate("el => window.getComputedStyle(el).opacity")
                color = el.evaluate("el => window.getComputedStyle(el).color")
                pointer = el.evaluate("el => window.getComputedStyle(el).pointerEvents")
                
                print(f"   Date tab '{text}': opacity={opacity}, color={color}, pointer-events={pointer}")
                
                # Greyed out typically has lower opacity or grey color
                if opacity and float(opacity) < 0.5:
                    return False
                if pointer == "none":
                    return False
                if "disabled" in classes.lower() or "grey" in classes.lower():
                    return False
        
        # If we got here and the URL wasn't redirected, assume available
        # (the URL check in the main function is the most reliable indicator)
        print("   ℹ️  Could not definitively verify date tab state via DOM, but URL check passed.")
        return True
        
    except Exception as e:
        print(f"   ⚠️  Error checking date tabs: {e}")
        # If URL wasn't redirected, assume available
        return True


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
                            parent_el = parent.evaluate_handle("el => el.parentElement")
                            if not parent_el:
                                break
                            parent_text = parent_el.evaluate("el => el.innerText || ''")
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
            container_text = theatre_container.evaluate("el => el.innerText || ''")
            found = True
            
            # Extract showtime strings (patterns like "10:00 AM", "04:30 PM")
            import re
            time_pattern = re.compile(r'\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)', re.IGNORECASE)
            times_found = time_pattern.findall(container_text)
            showtimes = [t.strip() for t in times_found]
            
            details = f"Theatre '{theatre_name}' found with {len(showtimes)} showtime(s): {', '.join(showtimes) if showtimes else 'see page'}"
        else:
            # Fallback: theatre name exists on page but we couldn't isolate the container
            # This still means the theatre is listed
            found = True
            details = f"Theatre '{theatre_name}' found on page (could not isolate specific container)"
            
            # Try to extract times from nearby text
            import re
            # Find theatre name position and grab surrounding text
            lower_body = body_text.lower()
            idx = lower_body.find(theatre_name_lower)
            if idx >= 0:
                # Grab text around the theatre name (500 chars after)
                snippet = body_text[idx:idx+500]
                time_pattern = re.compile(r'\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)', re.IGNORECASE)
                times_found = time_pattern.findall(snippet)
                showtimes = [t.strip() for t in times_found]
                if showtimes:
                    details += f" — Showtimes: {', '.join(showtimes)}"
    
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
    movie_url = args.movie_url
    theatre_name = args.theatre
    target_date = args.date
    
    if not movie_url:
        print("ERROR: --movie-url is required (or set DEFAULT_MOVIE_URL in the script)")
        sys.exit(1)
    if not theatre_name:
        print("ERROR: --theatre is required (or set DEFAULT_THEATRE_NAME in the script)")
        sys.exit(1)
    if not target_date:
        print("ERROR: --date is required (or set DEFAULT_TARGET_DATE in the script)")
        sys.exit(1)
    
    # Validate date format
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: Invalid date format '{target_date}'. Use YYYY-MM-DD.")
        sys.exit(1)
    
    # Load Telegram config
    tg_token, tg_chat_id = get_telegram_config()
    
    # State key for this specific check
    state_key = f"{theatre_name.lower()}_{target_date}"
    
    print("=" * 60)
    print(f"🎬 BookMyShow Checker — {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 60)
    
    state = load_state()
    
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
        print(f"\n❌ Script failed. Error alert sent via Telegram.")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("📊 RESULTS")
    print("=" * 60)
    print(f"   Date available:  {result['date_available']}")
    print(f"   Theatre found:   {result['theatre_found']}")
    print(f"   Details:         {result['theatre_details']}")
    print(f"   Movie:           {result['movie_name']}")
    if result['showtimes']:
        print(f"   Showtimes:       {', '.join(result['showtimes'])}")
    print("=" * 60)
    
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
        save_state(state)
        print("\n🎉 ALERT SENT! Booking is open!")
        
    elif trigger:
        # Theatre found but we already alerted recently — just update state
        print("\n✅ Booking is still open (already alerted recently).")
        
    elif result["date_available"] and not result["theatre_found"]:
        # Date is open but theatre not listed
        print(f"\nℹ️  Date {target_date} is open, but theatre '{theatre_name}' is not listed.")
        print("   The theatre may not be showing this movie, or listings are still being added.")
        
        # Update state — theatre not found
        state[state_key] = {
            "theatre_found": False,
            "date_available": True,
            "last_check_timestamp": time.time(),
            "last_check_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            "done": state.get(state_key, {}).get("done", False),
            "details": result["theatre_details"],
        }
        save_state(state)
        
    else:
        # Date not available yet
        print(f"\n⏳ Booking has NOT opened yet for {target_date}. Will check again next run.")
        
        # Update state
        state[state_key] = {
            "theatre_found": False,
            "date_available": False,
            "last_check_timestamp": time.time(),
            "last_check_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            "done": state.get(state_key, {}).get("done", False),
            "details": result["theatre_details"],
        }
        save_state(state)
    
    print(f"\n🕐 Next check: whenever cron runs this script again.")


if __name__ == "__main__":
    main()
