"""Take focused UI element screenshots for SOP button/nav documentation."""
import os, sys, subprocess

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "-q"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"])
    from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000"
OUT  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "sop", "screenshots", "ui")
os.makedirs(OUT, exist_ok=True)


def snap(page, name, selector=None, full=False, padding=8):
    path = os.path.join(OUT, f"{name}.png")
    if selector:
        try:
            el = page.locator(selector).first
            el.wait_for(timeout=3000)
            el.screenshot(path=path)
            print(f"  {name}.png  [{selector}]")
            return
        except Exception as e:
            print(f"  {name}: selector failed ({e}) — falling back to full page clip")
    page.screenshot(path=path, full_page=full)
    print(f"  {name}.png")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()

    # ── Login ────────────────────────────────────────────────────────────────
    page.goto(f"{BASE}/auth/login")
    page.fill("input[name='username']", "birkdale")
    page.fill("input[name='password']", "admin")
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")

    # ── Nav bar ───────────────────────────────────────────────────────────────
    print("\n[NAV]")
    page.goto(f"{BASE}/ens/", wait_until="networkidle")
    snap(page, "nav_bar", "nav, .navbar, header")
    # Open hamburger if present
    try:
        page.locator(".navbar-toggler").click(timeout=1000)
        page.wait_for_timeout(400)
        snap(page, "nav_mobile_open", ".navbar-collapse")
    except Exception:
        pass

    # ── Dashboard ─────────────────────────────────────────────────────────────
    print("\n[DASHBOARD]")
    page.goto(f"{BASE}/dashboard/", wait_until="networkidle")
    snap(page, "dash_stat_cards", ".row.g-3, .row.g-2, .stats-row, .card-group, .dashboard-cards")
    snap(page, "dash_search_bar", "form.search-form, .search-wrapper, input[placeholder*='Search']")

    # ── ENS list ──────────────────────────────────────────────────────────────
    print("\n[ENS LIST]")
    page.goto(f"{BASE}/ens/", wait_until="networkidle")
    snap(page, "ens_page_header", "h1, .page-header, .d-flex.align-items-center")
    snap(page, "ens_action_buttons", ".btn-toolbar, .page-actions, h1 + div, .d-flex.gap-2")
    snap(page, "ens_status_tabs", ".nav-tabs, .tab-bar, [role='tablist']")
    snap(page, "ens_table_header", "table thead, .table thead")
    # Grab first 3 rows to show badges
    snap(page, "ens_rows_badges", "table tbody, .table tbody")

    # ── SDI list ──────────────────────────────────────────────────────────────
    print("\n[SDI LIST]")
    page.goto(f"{BASE}/supdec/", wait_until="networkidle")
    snap(page, "sdi_action_buttons", ".btn-toolbar, .page-actions, .d-flex.gap-2, .d-flex.align-items-center.gap-2")
    snap(page, "sdi_deadline_banner", ".alert-warning, .deadline-banner, .border-warning")
    snap(page, "sdi_status_tabs", ".nav-tabs, [role='tablist']")
    snap(page, "sdi_rows_badges", "table tbody, .table tbody")

    # ── SDI detail ────────────────────────────────────────────────────────────
    print("\n[SDI DETAIL]")
    page.goto(f"{BASE}/sdi/SUP000000007548953/detail", wait_until="networkidle")
    snap(page, "sdi_detail_header_btns", ".d-flex.gap-2, .btn-group, .page-actions")
    snap(page, "sdi_blocked_alert", ".alert-danger, .alert-warning, .blocked-banner")
    snap(page, "sdi_dec_chain", ".declaration-chain, .card:has(.chain), section")

    # Try a DRAFT SDI for better submit button visibility
    page.goto(f"{BASE}/supdec/?status=DRAFT", wait_until="networkidle")
    try:
        first_draft_link = page.locator("a[href*='/sdi/SUP']").first
        first_draft_link.wait_for(timeout=3000)
        href = first_draft_link.get_attribute("href")
        page.goto(f"{BASE}{href}", wait_until="networkidle")
        snap(page, "sdi_draft_action_btns", ".d-flex.gap-2, .btn-toolbar")
    except Exception as e:
        print(f"  No DRAFT SDI found: {e}")

    # ── Consignment detail ────────────────────────────────────────────────────
    print("\n[CONSIGNMENT DETAIL]")
    page.goto(f"{BASE}/consignments/171", wait_until="networkidle")
    snap(page, "cons_detail_header", "h1, .page-title")
    snap(page, "cons_detail_action_btns", ".d-flex.gap-2, .btn-toolbar, .page-actions")
    snap(page, "cons_goods_table", "table, .goods-table")

    # ── SFD detail ────────────────────────────────────────────────────────────
    print("\n[SFD DETAIL]")
    page.goto(f"{BASE}/sfd/DEC000000017101580/detail", wait_until="networkidle")
    snap(page, "sfd_detail_action_btns", ".d-flex.gap-2, .btn-group, .btn-toolbar")
    snap(page, "sfd_detail_fields", ".card-body, .detail-grid, dl")

    # ── ENS detail ────────────────────────────────────────────────────────────
    print("\n[ENS DETAIL]")
    page.goto(f"{BASE}/ens/30", wait_until="networkidle")
    snap(page, "ens_detail_action_btns", ".d-flex.gap-2, .btn-toolbar")
    snap(page, "ens_detail_cons_panel", ".consignments-panel, section:has(h2)")

    # ── Status badge legend ───────────────────────────────────────────────────
    # Capture a row showing multiple status badge colours from ENS list
    print("\n[STATUS BADGES]")
    page.goto(f"{BASE}/ens/", wait_until="networkidle")
    snap(page, "status_badges_ens", "table, .table")

    page.goto(f"{BASE}/supdec/", wait_until="networkidle")
    snap(page, "status_badges_sdi", "table, .table")

    browser.close()

print(f"\nDone. UI screenshots saved to: {OUT}")
