"""Take SOP screenshots using playwright and save to docs/sop/screenshots/."""
import os, sys, subprocess

# Install playwright if needed
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "-q"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"])
    from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000"
OUT  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "sop", "screenshots")
os.makedirs(OUT, exist_ok=True)

LIST_PAGES = [
    ("01_login",        "/auth/login"),
    ("02_dashboard",    "/dashboard/"),
    ("03_ens_list",     "/ens/"),
    ("04_cons_list",    "/consignments/"),
    ("05_sdi_list",     "/supdec/"),
    ("06_sfd_list",     "/sfd/"),
    ("07_ingestion",    "/ingest/"),
    ("08_master_data",  "/master-data/"),
    ("09_settings",     "/admin/settings"),
    ("10_technical",    "/technical/"),
]

# Detail pages — navigated after login; IDs from live PRODUCTION data
DETAIL_PAGES = [
    ("11_ens_detail",   "/ens/30"),
    ("12_cons_detail",  "/consignments/171"),
    ("13_sfd_detail",   "/sfd/DEC000000017101580/detail"),
    ("14_sdi_detail",   "/sdi/SUP000000007548953/detail"),
]

def snap(page, name):
    path = os.path.join(OUT, f"{name}.png")
    page.screenshot(path=path, full_page=False)
    print(f"{name}.png saved")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()

    # Login
    page.goto(f"{BASE}/auth/login")
    page.fill("input[name='username']", "birkdale")
    page.fill("input[name='password']", "admin")
    snap(page, "01_login")
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")
    snap(page, "02_dashboard")

    for name, path in LIST_PAGES[2:]:
        try:
            page.goto(f"{BASE}{path}", wait_until="networkidle", timeout=15000)
            snap(page, name)
        except Exception as e:
            print(f"{name}: ERROR {e}")

    for name, path in DETAIL_PAGES:
        try:
            page.goto(f"{BASE}{path}", wait_until="networkidle", timeout=15000)
            snap(page, name)
        except Exception as e:
            print(f"{name}: ERROR {e}")

    browser.close()

print(f"\nAll screenshots saved to: {OUT}")
