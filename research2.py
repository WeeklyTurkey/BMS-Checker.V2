from playwright.sync_api import sync_playwright
import re

url = "https://in.bookmyshow.com/movies/hyderabad/the-odyssey/buytickets/ET00452034/20260829"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        
        # Let's find the date tabs container
        # BMS usually has a UL with class containing "date-container" or similar, or A tags with date
        # Let's just find elements containing the text "JUL" or "AUG"
        elements = page.locator("a").all()
        for el in elements:
            try:
                text = el.inner_text().strip()
                href = el.get_attribute("href") or ""
                classes = el.get_attribute("class") or ""
                if "JUL" in text or "AUG" in text or "SEP" in text:
                    if len(text) < 15: # likely a date tab
                        print(f"Date Tab Text: '{text}', Href: '{href}', Classes: '{classes}'")
            except:
                pass
                
        # Now find theatres and their showtimes
        theatres = page.locator("a.__venue-name").all()
        for el in theatres[:1]:
            print("Theatre:", el.inner_text())
            container = el.evaluate_handle('el => el.closest("li")')
            showtimes_html = container.evaluate('el => el.innerHTML')
            print("Theatre HTML snippet:", showtimes_html[:1000])
            
    except Exception as e:
        print(f"Error: {e}")
        
    browser.close()
