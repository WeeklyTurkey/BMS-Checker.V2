from playwright.sync_api import sync_playwright
import sys
import os

url = "https://in.bookmyshow.com/movies/hyderabad/the-odyssey/buytickets/ET00452034/20260829"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    
    print(f"Navigating to {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        
        print("URL after navigation:", page.url)
        
        dates = page.locator("a.date-href").all()
        print(f"Found {len(dates)} date tabs:")
        for d in dates:
            href = d.get_attribute("href")
            classes = d.get_attribute("class")
            print(f"  Date tab href: {href}, classes: {classes}")
            
        theatres = page.locator("a.__venue-name").all()
        print(f"Found {len(theatres)} theatres")
        if theatres:
            t = theatres[0]
            print("First theatre:", t.inner_text())
            container = t.evaluate_handle('el => el.closest("li.list")')
            if container:
                showtimes = container.evaluate('''(el) => {
                    const times = el.querySelectorAll('a.showtime-pill');
                    return Array.from(times).map(t => ({
                        time: t.innerText,
                        className: t.className,
                        attribute: t.getAttribute('data-availability')
                    }));
                }''')
                print("Showtimes for first theatre:")
                for st in showtimes:
                    print(" ", st)
    except Exception as e:
        print(f"Error: {e}")
        
    browser.close()
