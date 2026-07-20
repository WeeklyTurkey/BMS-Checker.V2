from playwright.sync_api import sync_playwright

url = "https://in.bookmyshow.com/movies/hyderabad/the-odyssey/buytickets/ET00452034/20260829"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        
        theatres = page.locator("a.__venue-name").all()
        for t in theatres[:3]:
            venue = t.inner_text()
            print("Venue:", venue)
            container = t.evaluate_handle('el => el.closest(".listing-info")')
            if container:
                # Find the showtime links
                times = container.evaluate('''el => {
                    const links = el.querySelectorAll('a.showtime-pill');
                    if (links.length === 0) {
                        // try another selector
                        return Array.from(el.querySelectorAll('div, a')).map(el => {
                           let text = el.innerText || '';
                           if(text.includes('PM') || text.includes('AM')) {
                               return {html: el.outerHTML, text: text.trim()};
                           }
                           return null;
                        }).filter(Boolean);
                    }
                    return Array.from(links).map(a => ({html: a.outerHTML}));
                }''')
                for st in times:
                    print(f"  -> {st['html'][:300]}")
            print("-" * 20)
                
    except Exception as e:
        print(f"Error: {e}")
        
    browser.close()
