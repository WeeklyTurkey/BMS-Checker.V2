from playwright.sync_api import sync_playwright

url = "https://in.bookmyshow.com/movies/hyderabad/the-odyssey/buytickets/ET00452034/20260829"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900}
    )
    
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        
        # 1. Check Date Tabs
        dates = page.evaluate('''() => {
            return Array.from(document.querySelectorAll('a.date-href')).map(a => {
                let isActive = a.className.includes('active') || a.parentElement.className.includes('active');
                if (!isActive) {
                    let style = window.getComputedStyle(a);
                    if (style.backgroundColor !== 'rgba(0, 0, 0, 0)' && style.backgroundColor !== 'transparent' && style.backgroundColor !== 'rgb(255, 255, 255)') {
                        isActive = true;
                    }
                }
                return {
                    href: a.getAttribute('href'),
                    dateText: a.innerText.replace(/\\n/g, ' '),
                    isActive: isActive,
                    className: a.className,
                    bg: window.getComputedStyle(a).backgroundColor
                };
            });
        }''')
        print("Dates found:", len(dates))
        for d in dates:
            print(" ", d)

        # 2. Check Showtimes
        theatres = page.locator("a.__venue-name").all()
        for t in theatres[:3]:
            print("Venue:", t.inner_text())
            container = t.evaluate_handle('el => el.closest(".listing-info")')
            if container:
                times = container.evaluate('''el => {
                    return Array.from(el.querySelectorAll('.showtime-pill-wrapper, a')).map(a => {
                        let text = a.innerText.trim();
                        if (text.includes('PM') || text.includes('AM')) {
                            let style = window.getComputedStyle(a);
                            return {
                                text: text,
                                className: a.className,
                                color: style.color,
                                data_avail: a.getAttribute('data-availability')
                            };
                        }
                        return null;
                    }).filter(Boolean);
                }''')
                for st in times:
                    print("  ", st)
                
    except Exception as e:
        print(f"Error: {e}")
        
    browser.close()
