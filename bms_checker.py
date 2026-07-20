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

import hashlib
import random
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

# If a target keeps failing with the SAME error, don't re-alert every single
# run — that just burns your Telegram/attention budget for a known, ongoing
# problem. Re-send the failure alert at most this often, unless the error
# message itself changes (a new/different error always alerts immediately).
ERROR_RE_ALERT_INTERVAL_SECONDS = 3600  # 1 hour

# How many times to retry a target after a transient failure (proxy hiccup,
# timeout, etc.) before giving up and sending a failure alert.
RETRY_ATTEMPTS = 2
RETRY_BASE_DELAY_SECONDS = 8  # exponential backoff: 8s, 16s, ...

# Block heavy, non-essential resource types (images, fonts, media) before
# they're even requested. BMS's showtimes page pulls several MB of poster
# art and font files per load; none of it is needed to read date tabs,
# theatre names, or showtimes out of the DOM. This is the single biggest
# lever for cutting proxy bandwidth (= cost) per check, since almost every
# pay-as-you-go proxy bills by the GB.
BLOCK_RESOURCE_TYPES = {"image", "media", "font"}

# Third-party domains that BookMyShow's page loads but that have nothing to
# do with the actual scraping target (ads, analytics, deep-link resolvers,
# reCAPTCHA, web fonts). Confirmed via ScraperAPI's per-domain analytics:
# on a single day, doubleclick.net + google.com + googletagmanager.com +
# branch.io + app.link accounted for close to half of all billed credits,
# while contributing zero useful data. google.com in particular showed a
# ~15-17% success rate, consistent with reCAPTCHA challenges retrying and
# burning credits with no payoff. Matched by hostname (exact or subdomain),
# never by substring, so e.g. "notgoogletagmanager.com" is never caught.
BLOCK_DOMAINS = {
    "doubleclick.net",
    "googletagmanager.com",
    "google-analytics.com",
    "googlesyndication.com",
    "google.com",  # BMS's reCAPTCHA/ads calls live here; low success rate anyway
    "branch.io",
    "app.link",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
}

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
        # ── API/proxy provider ──
        # PRIMARY: ScraperAPI, via its "proxy port" method. Username is
        # literally "scraperapi" with extra params appended after periods
        # (e.g. "scraperapi.country_code=in"); password is your API key.
        # Docs: https://docs.scraperapi.com/making-requests/proxy-port-method
        #
        # ⚠️ HEADS-UP ON COST: ScraperAPI's standard country_code geotargeting
        # is only available for US/EU on Hobby and Startup plans — India
        # geotargeting (country_code=in) requires a Business/Enterprise plan.
        # On a lower tier, requesting country_code=in can silently fall back
        # to Premium/residential proxy billing *per request*, which is almost
        # certainly why credits vanished so fast last time (per-domain
        # analytics showed bookmyshow.com averaging ~8 credits/request, not
        # the 1-credit base rate). Check your plan tier before relying on
        # country_code=in — if you're not on Business+, either upgrade or
        # drop the country_code param and accept non-Indian exit IPs (BMS
        # may serve different/less content, so test this first).
        #
        # BLOCK_DOMAINS below (ad/analytics/recaptcha/font domains) also
        # matters a lot here — those alone made up close to half of billed
        # credits in testing, regardless of provider.
        proxy_server = "http://proxy-server.scraperapi.com:8001"
        proxy_username = "scraperapi.country_code=in"
        proxy_password = os.environ.get("SCRAPER_API_KEY")

        if not proxy_password:
            # No ScraperAPI key — fall back to a generic BYO proxy provider.
            # Works with ANY provider that exposes a standard HTTP proxy
            # gateway (DataImpulse, IPRoyal, Webshare, Smartproxy, Bright
            # Data, etc). Point these three env vars at your provider's
            # dashboard values:
            #   PROXY_SERVER   e.g. "http://gw.dataimpulse.com:823"
            #   PROXY_USERNAME e.g. "user__cr.in" (India geo-target flag —
            #                                       syntax varies by provider)
            #   PROXY_PASSWORD e.g. "abc123"
            proxy_server = os.environ.get("PROXY_SERVER")
            proxy_username = os.environ.get("PROXY_USERNAME")
            proxy_password = os.environ.get("PROXY_PASSWORD")

        using_proxy = bool(proxy_server and proxy_username and proxy_password)
        launch_kwargs = {"headless": True}

        if using_proxy:
            print(f"🛡️  Routing request through proxy: {proxy_server}")
            launch_kwargs["proxy"] = {
                "server": proxy_server,
                "username": proxy_username,
                "password": proxy_password,
            }
        elif proxy_server or proxy_username or proxy_password:
            print("⚠️  Proxy env vars are partially set (need PROXY_SERVER, "
                  "PROXY_USERNAME, and PROXY_PASSWORD all three) — running without a proxy.")
        else:
            print("⚠️  No SCRAPER_API_KEY or PROXY_* env vars set — running without a proxy. "
                  "BMS will very likely block direct requests.")

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=using_proxy
        )

        # Cut bandwidth/credits by not downloading images/fonts/media, and by
        # not even connecting to ad/analytics/recaptcha/font domains that
        # have nothing to do with the DOM we actually read. Both are cheap
        # to keep even without a proxy.
        block_stats = {"blocked_resource_type": 0, "blocked_domain": 0, "allowed": 0}

        def _is_blocked_domain(hostname: str) -> bool:
            hostname = (hostname or "").lower()
            return any(hostname == d or hostname.endswith("." + d) for d in BLOCK_DOMAINS)

        def _block_heavy_resources(route):
            request = route.request
            if request.resource_type in BLOCK_RESOURCE_TYPES:
                block_stats["blocked_resource_type"] += 1
                route.abort()
                return
            hostname = urlsplit(request.url).hostname
            if _is_blocked_domain(hostname):
                block_stats["blocked_domain"] += 1
                route.abort()
                return
            block_stats["allowed"] += 1
            route.continue_()

        context.route("**/*", _block_heavy_resources)

        page = context.new_page()
        
        try:
            # Navigate to the target date URL (increase timeout if using proxy)
            goto_timeout = 90000 if using_proxy else 30000
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=goto_timeout)
            except Exception as target_navigation_error:
                # Some BMS responses reject a URL for a date that is not in
                # the date picker. Load the supplied movie page instead so we
                # can inspect its available date tabs and return a normal
                # NOT AVAILABLE result rather than failing the whole target.
                print(
                    f"⚠️  Target date URL could not be opened; "
                    f"checking the base movie page instead: {target_navigation_error}"
                )
                page.goto(movie_url, wait_until="domcontentloaded", timeout=goto_timeout)
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
            total_seen = sum(block_stats.values())
            if total_seen:
                print(
                    f"📊 Requests: {block_stats['allowed']} allowed, "
                    f"{block_stats['blocked_domain']} blocked (ad/analytics/tracker domains), "
                    f"{block_stats['blocked_resource_type']} blocked (image/font/media) "
                    f"— {total_seen - block_stats['allowed']}/{total_seen} "
                    f"({(total_seen - block_stats['allowed']) / total_seen:.0%}) never hit the proxy."
                )
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
                # BMS updates the theatre list asynchronously after the date
                # click; allow the response/render cycle to finish.
                page.wait_for_timeout(6000)
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
                page.wait_for_timeout(6000)
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

        # Current BMS markup labels venue names with this generated class.
        # Start here so partial names such as "ALLU" resolve to the actual
        # venue row before using the broader fallback selectors below.
        try:
            venue_names = page.locator("span[class*='eXSbEM']").all()
            for venue in venue_names:
                if theatre_name_lower in venue.inner_text().strip().lower():
                    parent = venue
                    for _ in range(10):
                        if parent.locator('div[role="button"]').count() > 0:
                            theatre_container = parent
                            break
                        parent = parent.locator("..")
                    if theatre_container:
                        break
        except Exception:
            pass
        
        for selector in theatre_selectors:
            if theatre_container:
                break
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
                        const currentBmsUnavailable =
                            className.includes('clnjka') ||
                            className.includes('sold') ||
                            className.includes('disabled') ||
                            className.includes('grey') ||
                            className.includes('unavailable');
                        const isVivid = (color) => {
                            const match = color.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                            if (!match) return false;
                            const channels = match.slice(1, 4).map(Number);
                            return Math.max(...channels) - Math.min(...channels) > 35;
                        };
                        // BMS uses yellow borders for FAST FILLING and green
                        // borders for AVAILABLE. A grey slot is unavailable;
                        // any non-grey, interactive slot is available.
                        const visuallyAvailable =
                            isVivid(style.borderLeftColor) ||
                            isVivid(style.borderColor) ||
                            isVivid(style.backgroundColor);
                        if (currentBmsUnavailable ||
                            (child.getAttribute('role') === 'button' &&
                             !visuallyAvailable)) {
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


def _check_bms_with_retries(movie_url: str, theatre_name: str, target_date: str) -> dict:
    """
    Wrap check_bms() with a small retry/backoff loop. Residential proxy IPs
    are shared and rotate, so an isolated timeout or connection drop is
    common and NOT a sign that BMS changed its page structure — retrying
    with a fresh proxy connection usually just works. Only give up (and let
    the caller send a failure alert) after RETRY_ATTEMPTS extra tries.
    """
    last_error = None
    for attempt in range(RETRY_ATTEMPTS + 1):
        try:
            return check_bms(movie_url, theatre_name, target_date)
        except Exception as e:
            last_error = e
            if attempt < RETRY_ATTEMPTS:
                delay = RETRY_BASE_DELAY_SECONDS * (2 ** attempt) + random.uniform(0, 3)
                print(f"   ⚠️  Attempt {attempt + 1} failed ({e}); retrying in {delay:.0f}s...")
                time.sleep(delay)
    raise last_error


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
        # If a theatre/date (optionally + movie-url) was given, only mark the
        # matching target(s) as done. This matters once you're tracking more
        # than one target — otherwise booking one movie would silently
        # silence alerts for all your other targets too.
        if args.theatre or args.date:
            prefix = f"{args.theatre.lower()}_{args.date}"
            if args.movie_url:
                prefix += f"_{hashlib.sha1(args.movie_url.encode('utf-8')).hexdigest()[:8]}"
            matched = [k for k in state if k.startswith(prefix)]
            if not matched:
                print(f"⚠️  No tracked target matched theatre='{args.theatre}' date='{args.date}'. "
                      f"Nothing was changed. Run without --theatre/--date to mark ALL targets done.")
                return
            for key in matched:
                state[key]["done"] = True
            save_state(state)
            print(f"✅ Marked {len(matched)} target(s) as done. No more re-alerts for them.")
        else:
            for key in state:
                state[key]["done"] = True
            save_state(state)
            print("✅ Marked ALL tracked targets as done. No more re-alerts will be sent.")
            print("   Tip: pass --theatre/--date to mark just one target done instead.")
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
        
        # Include a short hash of the movie URL so two different movies that
        # happen to share the same theatre + date don't overwrite each
        # other's state (this used to be a real bug with multi-target setups).
        url_fingerprint = hashlib.sha1(movie_url.encode("utf-8")).hexdigest()[:8]
        state_key = f"{theatre_name.lower()}_{target_date}_{url_fingerprint}"
        print(f"\n▶️  Target {i+1}/{len(targets)}: {theatre_name} on {target_date}")
        
        try:
            result = _check_bms_with_retries(movie_url, theatre_name, target_date)
        except Exception as e:
            # Page check failed — send error alert so user doesn't miss a silent failure
            error_text = str(e)[:200]
            previous = state.get(state_key, {})
            last_error_text = previous.get("last_error_text")
            last_error_ts = previous.get("last_error_timestamp", 0)
            elapsed = time.time() - last_error_ts
            # Only re-send the SAME error if enough time has passed. A
            # different error always alerts right away.
            should_error_alert = (
                error_text != last_error_text
                or elapsed >= ERROR_RE_ALERT_INTERVAL_SECONDS
            )

            if should_error_alert:
                error_msg = (
                    f"⚠️ <b>BMS Checker Script FAILED</b> ⚠️\n\n"
                    f"Error: <code>{html.escape(error_text)}</code>\n\n"
                    f"🎬 Movie URL: {html.escape(movie_url)}\n"
                    f"🏢 Theatre: {html.escape(theatre_name)}\n"
                    f"📅 Date: {html.escape(target_date)}\n\n"
                    f"⏰ Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}\n\n"
                    f"👉 <b>Check manually — the script might be broken or BMS changed their page structure.</b>"
                )
                send_telegram(tg_token, tg_chat_id, error_msg)
                print(f"❌ Script failed for this target. Error alert sent via Telegram.")
            else:
                print(
                    f"❌ Script failed for this target (same error as last alert, "
                    f"{elapsed/60:.0f} min ago). Suppressing duplicate alert."
                )

            state[state_key] = {
                **previous,
                "last_error_text": error_text,
                "last_error_timestamp": time.time() if should_error_alert else last_error_ts,
            }
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
                f"🎬 <b>{html.escape(result['movie_name'] or 'Movie')}</b>\n"
                f"🏢 <b>{html.escape(theatre_name)}</b>\n"
                f"📅 <b>{html.escape(target_date)}</b>\n"
                f"🕐 Showtimes: {html.escape(showtime_str)}\n\n"
                f"🔗 <a href=\"{html.escape(result['target_url'], quote=True)}\">BOOK NOW on BookMyShow</a>\n\n"
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
            # Date is open but there are no available showtimes. This includes
            # a theatre that is listed with only greyed-out/sold-out slots.
            print(f"ℹ️  No available showtimes for '{theatre_name}' on {target_date}.")
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
            
            # Update state — theatre not found
            state[state_key] = {
                "theatre_found": False,
                "date_available": True,
                "last_check_timestamp": time.time(),
                "last_check_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                "done": previous.get("done", False),
                "details": result["theatre_details"],
                "status_notified": previous.get("status_notified", False) or status_sent,
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
