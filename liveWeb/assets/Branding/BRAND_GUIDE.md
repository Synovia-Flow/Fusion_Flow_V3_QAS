# Synovia Fusion — Brand & Component Guide

**Version 1.1.0** · Canonical home: `Production/Common/Branding/`

This folder is the single source of truth for the Fusion look. When specifying
a new screen or report, reference components **by the names in this guide**
("use a *Tile* grid", "show statuses as *Status Pills*", "open with the
*Splash Screen*") and any deployment will land on-brand.

| File | What it is | Used by |
|---|---|---|
| `fusion_design_tokens.json` | Canonical tokens (colours, type scale, radii, shadows) — machine-readable | everything; generate other formats from this |
| `fusion_theme.css` | The light web theme — drop-in stylesheet implementing every component below | LiveWeb portal (deployed copy at `Production/LiveWeb/app/static/css/fusion_theme.css`) |
| `fusion_branding.py` | Python palette + openpyxl/pptx helpers so script output (Excel, decks) matches the portal | Utility_Scripts Excel exports, reports |
| `BRAND_GUIDE.md` | This document — the named component definitions | humans + future deployment prompts |

---

## 1. Brand basics

- **Font:** Montserrat (web), Segoe UI fallback for Excel/desktop. Mono accents: JetBrains Mono.
- **Logos:** `FusionLogo.jpg` (product — splash + sidebar brand), `SynoviaLogoHor.jpg` (company — "Powered by" footer). Always on a **white rounded panel** when placed over navy.
- **Framework:** Bootstrap 5.3.3 (optional) themed via `--bs-*` overrides; Bootstrap Icons 1.11.3.

### Palette

| Token | Hex | Use |
|---|---|---|
| `navy` | `#0B1D3A` | primary dark — sidebar, table header band, headings |
| `teal` | `#00C4B4` | primary brand accent — active nav, primary buttons, links |
| `teal_deep` | `#00A99B` | link default / hover on teal |
| `accent` / `amber` | `#F97316` | warning / attention |
| `sky` | `#0EA5E9` | secondary accent / charts |
| `green` | `#198754` | success / healthy |
| `rose` | `#DC3545` | failure / error |
| `violet` | `#7C3AED` | info / secondary category |
| `bg` | `#F0F4FF` | app background |
| `surface` | `#FFFFFF` | cards / panels |
| `text` | `#1E2A3A` | body text |
| `muted` | `#6B7A8D` | secondary text / labels |
| `border` | `#D8E0EE` | borders / dividers |

### Status → colour map (portal pills, Excel cells, deck shapes — identical everywhere)

| Status values | Colour |
|---|---|
| SUCCESS · OK · CONFIRMED · SENT · PROCESSED · LIVE | green `#198754` |
| PARTIAL · WARN · PENDING · IN PROGRESS | amber `#F97316` |
| FAILED · ERROR · TIMEOUT | rose `#DC3545` |
| INFO · ARCHIVED-secondary | violet `#7C3AED` |
| neutral / LEGACY / unknown | muted grey `#6B7A8D` |

---

## 2. Named components

### 2.1 Splash Screen  *(the one we keep — do not restyle)*

The entry/login screen for every Fusion web surface. Deliberately **dark** —
the only dark surface in the light theme — so the brand moment pops before
landing on the light portal.

**Definition:**
- Full-viewport **radial navy gradient**: `#14305c` at centre → `navy #0B1D3A` at 70%.
- Centred **Fusion logo** (`FusionLogo.jpg`, ~180px wide) on a **white rounded card**
  (12px radius, 14×18px padding) with a soft **teal glow** shadow `0 8px 30px rgba(0,196,180,.25)`.
- Below: app name in white Montserrat 38px/700, then the tagline in
  **teal uppercase, 13px, 3px letter-spacing**.
- **Three pulsing teal dots** (8px, staggered 0.15s) as the loader.
- Whole block fades in over 0.7s; version string bottom-centre at 35% white.
- Auto-redirects to the landing page after **1.6s** (`<meta http-equiv="refresh" content="1.6;url=/home">`).

**CSS classes:** `.splash-body`, `.splash`, `.splash-logo`, `.splash-title`,
`.splash-tagline`, `.splash-loader`, `.splash-version` (in `fusion_theme.css`).
Reference implementation: `Production/LiveWeb/app/templates/splash.html`.

```html
<body class="splash-body">
  <div class="splash">
    <img class="splash-logo" src="img/FusionLogo.jpg" alt="Fusion">
    <div class="splash-title">Fusion Consignment</div>
    <div class="splash-tagline">Production Console</div>
    <div class="splash-loader"><span></span><span></span><span></span></div>
  </div>
  <div class="splash-version">v1.0</div>
</body>
```

### 2.2 Portal Shell (sidebar + topbar)

The frame around every page: fixed **navy sidebar** (220px) with the Fusion
logo brand block, vertical nav (teal active state with 3px left bar), and a
"Powered by Synovia" footer; white **topbar** (56px, sticky) carrying the page
title; content area on the `bg #F0F4FF` canvas, max-width 1400px, 28px padding.

**Classes:** `.sidebar`, `.sidebar-brand`, `.nav-item` (+`.active`),
`.sidebar-footer`, `.main`, `.topbar`, `.page-content`.

### 2.3 Card

White surface panel for any content block: 1px `border` outline, 10px radius,
soft shadow, 20×24px padding. Title is a small uppercase muted label
(`.card-title`).

### 2.4 KPI Card

Headline-number card used in dashboard strips. White card with a **4px
coloured accent strip down the left edge** (gradient), a 44px tinted icon
chip, a 28px/700 value and an 11px uppercase muted label.

- Accent strip variants: `c-sky` · `c-green` · `c-amber` · `c-rose` · `c-violet`
- Icon chip tints: `teal` · `sky` · `navy` · `accent`
- Lay out in a `.kpi-grid` (auto-fill, min 220px).

```html
<div class="kpi-grid">
  <div class="kpi-card c-green">
    <div class="kpi-icon teal">✓</div>
    <div><div class="kpi-value">9,176</div><div class="kpi-label">Files Sent</div></div>
  </div>
</div>
```

### 2.5 Tile

Clickable launcher card (home page → solution areas). White card, **12px**
radius, 44px teal icon chip, navy 15px/700 title, muted description, teal
uppercase call-to-action footer. On hover: **lifts 2px**, deeper shadow, teal
border. Lay out in a `.tile-grid` (auto-fill, min 260px).

**Classes:** `.tile-grid`, `.tile`, `.tile-icon`, `.tile-title`, `.tile-desc`,
`.tile-cta` / `.tile-foot`.

### 2.6 Status Pill

The coloured rounded badge that shows a record's state — in tables, headers,
anywhere. 12px-radius pill, 11px/600 uppercase text, **tinted background with
strong text of the same hue** (never solid blocks in tables).

| Class | Meaning |
|---|---|
| `.badge.badge-green` | success / sent / processed |
| `.badge.badge-amber` | pending / partial / warning |
| `.badge.badge-red`   | failed / error |
| `.badge.badge-teal`  | active / live / brand-neutral positive |
| `.badge.badge-violet`| info |
| `.badge.badge-grey`  | neutral / legacy / archived |

```html
<span class="badge badge-green">SENT</span>
<span class="badge badge-amber">PENDING</span>
```

### 2.7 Pipeline (stage tracker)

Horizontal chain of pill-shaped stage chips showing a record's journey
(e.g. *Downloaded → Ingested → Filtered → Sent → Processed*), separated by
muted arrows. Stage states: **done** (green tint), **active** (teal tint),
**error** (rose tint), pending (plain white/muted).

```html
<div class="pipeline">
  <span class="pipeline-stage is-done">Downloaded</span><span class="pipeline-arrow">›</span>
  <span class="pipeline-stage is-done">Ingested</span><span class="pipeline-arrow">›</span>
  <span class="pipeline-stage is-active">Filtered</span><span class="pipeline-arrow">›</span>
  <span class="pipeline-stage">Sent</span>
</div>
```

### 2.8 Data Table + Header Band

Standard records table inside a Card. The **Header Band** is the signature:
**solid navy `#0B1D3A` header row with white 10px/700 uppercase text** — the
same band `fusion_branding.style_header_row()` paints in Excel exports, so
portal and workbook read as one product. Rows: 1px `border` dividers, teal-tint
hover; identifiers use `.mono` (JetBrains Mono). Wrap in `.table-wrap` for
horizontal scroll.

### 2.9 Toolbar, Buttons, Pager, Alert

- **Toolbar** (`.toolbar`): flex row above a table — filters/buttons left, `.row-count` pushed right.
- **Button** (`.btn`): white, 6px radius, teal border+text on hover. **Primary** (`.btn-primary`): solid teal, navy text.
- **Pager** (`.pager`): prev/next buttons + muted `.info` right-aligned.
- **Alert** (`.alert-error` rose tint / `.alert-info` sky tint): flash messages under the topbar.
- **Empty state** (`.empty-state`): centred muted message when a table has no rows.

---

## 3. Using the brand outside the web

```python
from fusion_branding import COLORS, STATUS_COLORS, style_header_row, xl_status_font
style_header_row(ws)                # navy Header Band + freeze + autofilter
xl_status_font(cell, "FAILED")      # rose bold text, mirroring the Status Pill
```

`fusion_branding.py` carries the identical palette and status map for Excel
(openpyxl) and decks (python-pptx). Font falls back to Segoe UI where
Montserrat isn't installed.

---

## 4. Layout tokens (quick reference)

- Radii: card **10px**, tile **12px**, pill **12px**, button 6px, small 4px.
- Shadows: card `0 1px 3px rgba(11,29,58,.06)`, hover `0 6px 18px rgba(11,29,58,.08)`.
- Motion: `transform 0.14s ease, box-shadow 0.14s ease, border-color 0.14s ease`.
- Sidebar width **220px**; topbar height 56px; page padding 28px.
- Type scale (px): KPI value 28 · page title 24 · card title 15 · body 13 · label 11 · small 10.
