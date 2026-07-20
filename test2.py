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
        print("Title:", page.title())
        print("Body preview:", page.inner_text("body")[:500].replace('\n', ' '))
    except Exception as e:
        print(f"Error: {e}")
        
    browser.close()
