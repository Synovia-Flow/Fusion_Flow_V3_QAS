"""Take full-page and per-section screenshots of Admin Settings."""
import os, sys, subprocess

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "-q"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"])
    from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000"
OUT  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "sop", "screenshots", "settings")
os.makedirs(OUT, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()

    # Login
    page.goto(f"{BASE}/auth/login")
    page.fill("input[name='username']", "birkdale")
    page.fill("input[name='password']", "admin")
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")

    # Settings page
    page.goto(f"{BASE}/admin/settings", wait_until="networkidle")

    # Full page
    page.screenshot(path=os.path.join(OUT, "settings_full.png"), full_page=True)
    print("settings_full.png saved")

    # Expand all sections first (click any collapsed ones)
    try:
        for btn in page.locator("button.collapsed, .accordion-button.collapsed").all():
            btn.click()
            page.wait_for_timeout(200)
    except Exception:
        pass

    page.wait_for_timeout(500)

    # Per-section screenshots
    sections = [
        ("tss_api",        "TSS Portal API"),
        ("email_smtp",     "Email / SMTP"),
        ("graph_mail",     "Inbound Email / Microsoft Graph"),
        ("auto_staging",   "Invoice Auto-Staging"),
        ("sdi_automation", "SDI / SupDec Automation"),
        ("validation",     "Validation Controls"),
        ("notifications",  "Email Automation Notifications"),
    ]

    cards = page.locator(".card, .accordion-item, section.settings-section").all()
    print(f"Found {len(cards)} sections on page")

    for name, title in sections:
        try:
            # Find section by heading text
            heading = page.locator(f"text={title}").first
            heading.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            # Screenshot the parent card/section
            parent = heading.locator("xpath=ancestor::*[contains(@class,'card') or contains(@class,'accordion-item')][1]")
            parent.screenshot(path=os.path.join(OUT, f"{name}.png"))
            print(f"{name}.png saved  [{title}]")
        except Exception as e:
            print(f"{name}: FAILED — {e}")

    # Also ingestion page
    page.goto(f"{BASE}/ingest/", wait_until="networkidle")
    page.screenshot(path=os.path.join(OUT, "ingestion_overview.png"), full_page=False)
    print("ingestion_overview.png saved")
    page.screenshot(path=os.path.join(OUT, "ingestion_full.png"), full_page=True)
    print("ingestion_full.png saved")

    browser.close()

print(f"\nDone. Screenshots: {OUT}")
