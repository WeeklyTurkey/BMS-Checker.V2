from playwright.sync_api import sync_playwright

url = "https://in.bookmyshow.com/movies/hyderabad/the-odyssey/buytickets/ET00452034/20260829"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        
        # 1. Date tabs
        # In BMS, date tabs are usually wrapped in a list or div. Let's find elements with "JUL"
        # The screenshot shows a red box with "SAT 18 JUL".
        # Let's run JS to find the active date tab.
        active_date_info = page.evaluate('''() => {
            // Find the element with red background or specific active class
            // Usually there's an 'active' class or something similar.
            // Let's just find all date wrappers.
            let active = null;
            let all = [];
            document.querySelectorAll('a').forEach(a => {
                if (a.innerText.includes('JUL') || a.innerText.includes('AUG')) {
                    if (a.innerText.length < 20) {
                        let style = window.getComputedStyle(a);
                        let bg = style.backgroundColor;
                        let color = style.color;
                        all.push({text: a.innerText.replace(/\\n/g, ' '), href: a.href, bg: bg, color: color, className: a.className});
                        if (bg === 'rgb(248, 68, 100)' || a.className.includes('active') || a.parentElement.className.includes('active')) {
                            active = {text: a.innerText.replace(/\\n/g, ' '), href: a.href, className: a.className};
                        }
                    }
                }
            });
            return {active: active, all: all.slice(0,3)};
        }''')
        print("Active Date Info:", active_date_info)
        
        # 2. Showtimes availability
        # We need to know how to distinguish green/yellow/grey.
        showtimes_info = page.evaluate('''() => {
            let info = [];
            document.querySelectorAll('a.__venue-name').forEach(v => {
                let li = v.closest('li');
                if (li) {
                    let times = li.querySelectorAll('a'); // showtime links
                    let valid_times = [];
                    times.forEach(t => {
                        let text = t.innerText.trim();
                        if (text.includes('AM') || text.includes('PM')) {
                            let style = window.getComputedStyle(t);
                            let color = style.color; // text color is usually green/yellow
                            valid_times.push({text: text.replace(/\\n/g, ' '), className: t.className, color: color});
                        }
                    });
                    if (valid_times.length > 0) {
                        info.push({venue: v.innerText.trim(), times: valid_times});
                    }
                }
            });
            return info.slice(0, 3);
        }''')
        print("Showtimes Info:")
        for t in showtimes_info:
            print(t['venue'])
            for st in t['times']:
                print("  ", st)
                
    except Exception as e:
        print(f"Error: {e}")
        
    browser.close()
