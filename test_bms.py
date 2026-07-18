from playwright.sync_api import sync_playwright

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(user_agent=USER_AGENT)
    page = context.new_page()
    page.goto("https://in.bookmyshow.com/movies/hyderabad/the-odyssey/buytickets/ET00452034/20260829", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    print("Title:", page.title())
    print("Body contains blocked?", "blocked" in page.inner_text("body").lower())
    browser.close()
