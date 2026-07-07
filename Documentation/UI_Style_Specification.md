# Synovia Flow 3 — UI / Brand Specification

A build-ready spec to replicate the **Flow 3 portal** look, fonts and components on
another website. This is the *live portal* theme (`liveWeb/index.html`) — a hand-rolled,
framework-free system. (Note: the older `fusion_design_tokens.json` / `fusion_theme.css`
in `Branding/` describe a **different, Bootstrap-based light theme** from the previous
app — do not mix them; this document is the current portal.)

---

## 1. Tooling / stack

| Concern | Choice |
|---|---|
| Markup/logic | **Vanilla HTML + CSS + JS** — no framework, no build step, no external JS |
| CSS | Single inline `<style>`; CSS custom properties (`:root`) as the token layer |
| Font | **Montserrat** via Google Fonts `@import` (weights 300–800 + italic 500); fallback `Segoe UI, system-ui, -apple-system, sans-serif` |
| Icons | Inline emoji / hand-drawn SVG (no icon font in the live portal) |
| Charts | Hand-rolled inline **SVG** (no chart lib) |
| Motion | CSS transitions/keyframes; respects `prefers-reduced-motion` |
| Theme feel | Light app surface, **navy + flow-aqua** brand, coral accent; big rounded cards, floating pill nav, gradient primary buttons |

Font import (top of stylesheet):
```css
@import url('https://fonts.googleapis.com/css2?family=Montserrat:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;1,500&display=swap');
```

---

## 2. Design tokens (`:root`)

Drop this block in and everything else composes from it.

```css
:root{
  /* brand darks */
  --abyss:#07152B; --navy:#0F2A4A; --navy2:#143458;
  /* brand accents */
  --flow:#13DAC6; --flow-deep:#0AB6A6;      /* flow-aqua — primary accent */
  --fusion:#FF7A45; --fusion2:#FF9A6B;      /* fusion coral — "powered by" / submit */
  --sky:#3BA0FF;
  /* surfaces */
  --bg:#EEF3FA; --surface:#FFFFFF; --surface2:#F7FAFF;
  /* ink */
  --ink:#132133; --muted:#5E7085; --hairline:#DCE6F2;
  /* status */
  --good:#17B26A; --warn:#F79009; --bad:#F04438; --info:#7A5AF8;
  /* shape */
  --radius:16px; --radius-sm:11px; --pill:999px;
  --shadow:0 1px 2px rgba(15,42,74,.06), 0 8px 24px rgba(15,42,74,.06);
  --shadow-lg:0 18px 50px rgba(7,21,43,.16);
  --font:'Montserrat','Segoe UI',system-ui,-apple-system,sans-serif;
}
```

### Colour usage
| Token | Hex | Use |
|---|---|---|
| `--abyss` / `--navy` / `--navy2` | `#07152B` / `#0F2A4A` / `#143458` | splash gradient, nav bar, sheet headers, dark chips |
| `--flow` / `--flow-deep` | `#13DAC6` / `#0AB6A6` | **primary accent** — active nav, primary buttons, links, done states |
| `--fusion` / `--fusion2` | `#FF7A45` / `#FF9A6B` | "Powered by Fusion", the Submit action, avatar |
| `--sky` | `#3BA0FF` | secondary/info chips, charts |
| `--bg` / `--surface` / `--surface2` | `#EEF3FA` / `#FFFFFF` / `#F7FAFF` | app bg / cards / subtle fills & hovers |
| `--ink` / `--muted` / `--hairline` | `#132133` / `#5E7085` / `#DCE6F2` | body text / secondary text / borders |
| `--good`/`--warn`/`--bad`/`--info` | `#17B26A`/`#F79009`/`#F04438`/`#7A5AF8` | status |

---

## 3. Typography

- **Family:** Montserrat everywhere; headings `font-weight:800; letter-spacing:-.01em; text-wrap:balance`.
- **Body:** 13–13.5px, `line-height:1.45`, `-webkit-font-smoothing:antialiased`.
- **Numbers:** `.tnum`/`.mono` use `font-variant-numeric:tabular-nums`.

| Role | Size / weight |
|---|---|
| Splash H1 | `clamp(34px,5vw,58px)` / 800 |
| Page title (`h2`) | 25px / 800 |
| KPI value | 34px / 800, `letter-spacing:-.02em` |
| Panel title (`h3`) | 15px / 800 |
| Body | 13–13.5px / 400–600 |
| Eyebrow / label | 11px / 700, `letter-spacing:.14em`, `text-transform:uppercase`, `--muted` |
| Small / job hint | 10–10.5px |

Eyebrow helper:
```css
.eyebrow{font-size:11px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
```

---

## 4. Layout

- **Page width:** `main{max-width:1220px;margin:0 auto;padding:26px 30px 70px}`.
- **Top bar:** sticky, translucent, blurred — `background:rgba(238,243,250,.82);backdrop-filter:blur(12px);border-bottom:1px solid var(--hairline)`.
- **Floating pill nav:** centered, sticky under the top bar, navy gradient capsule; active item = flow gradient.
- **Grids:** KPI grid `repeat(auto-fit,minmax(190px,1fr))`; two-column content `1.6fr 1fr` collapsing to 1 col ≤900px.
- **View transition:** `@keyframes fade` (opacity + 8px rise, .35s).

Nav recipe:
```css
.nav{display:flex;gap:4px;padding:6px;border-radius:var(--pill);
  background:linear-gradient(135deg,var(--navy),var(--navy2));box-shadow:var(--shadow-lg);
  border:1px solid rgba(255,255,255,.06)}
.nav button{border:none;background:transparent;color:#AFC3E0;font-weight:600;font-size:13px;
  padding:9px 17px;border-radius:var(--pill);transition:color .15s,background .15s}
.nav button:hover{color:#fff}
.nav button.active{background:linear-gradient(135deg,var(--flow),var(--flow-deep));
  color:#04241f;box-shadow:0 6px 16px rgba(19,218,198,.35)}
```

---

## 5. Core components (copy-paste recipes)

**Card / panel & KPI tile**
```css
.panel{background:var(--surface);border:1px solid var(--hairline);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:20px}
.tile{background:var(--surface);border:1px solid var(--hairline);border-radius:var(--radius);
  padding:18px;box-shadow:var(--shadow);position:relative;overflow:hidden;
  transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease}
.tile:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg);border-color:rgba(19,218,198,.4)}
.tile .edge{position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--flow)} /* accent rail */
.kpi .label{font-size:11.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.kpi .val{font-size:34px;font-weight:800;letter-spacing:-.02em;margin-top:6px}
```

**Buttons** — primary is a flow gradient with dark-teal ink; variants for submit/reprocess/secondary/danger.
```css
.act{border:none;border-radius:11px;padding:12px 18px;font-weight:700;font-size:13px;color:#04241f;
  background:linear-gradient(135deg,var(--flow),var(--flow-deep));
  display:flex;flex-direction:column;align-items:flex-start;gap:1px;transition:transform .12s,box-shadow .2s}
.act:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(19,218,198,.32)}
.act.submit{background:linear-gradient(135deg,var(--fusion),var(--fusion2));color:#3a1400}
.act.reproc{background:linear-gradient(135deg,#F79009,#FBB040);color:#3a2400}
.act.secondary{background:var(--surface2);color:var(--ink);border:1px solid var(--hairline)}
.act.danger{background:linear-gradient(135deg,var(--bad),#F97066);color:#fff}
.act:disabled{opacity:.45;cursor:not-allowed}
/* full-width form CTA */
.btn{width:100%;padding:13px;border:none;border-radius:11px;font-weight:700;font-size:14px;
  background:linear-gradient(135deg,var(--flow),var(--flow-deep));color:#04241f;transition:transform .12s,box-shadow .2s}
.btn:hover{transform:translateY(-1px);box-shadow:0 10px 26px rgba(19,218,198,.35)}
```

**Status pills & chips**
```css
.pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;padding:3px 10px;border-radius:var(--pill)}
.pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.p-good{background:rgba(23,178,106,.12);color:var(--good)}
.p-sky{background:rgba(59,160,255,.13);color:#1E7FD6}
.p-bad{background:rgba(240,68,56,.12);color:var(--bad)}
.p-flow{background:rgba(19,218,198,.14);color:var(--flow-deep)}
.p-muted{background:rgba(94,112,133,.13);color:var(--muted)}
.p-fusion{background:rgba(255,122,69,.14);color:#D9531F}
.chip{font-size:11px;font-weight:700;color:var(--muted);background:var(--surface2);
  border:1px solid var(--hairline);padding:3px 9px;border-radius:var(--pill)}
```

**Tables**
```css
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);padding:11px 12px;border-bottom:1px solid var(--hairline)}
td{padding:12px;border-bottom:1px solid var(--hairline)}
tbody tr:hover{background:var(--surface2)}
.tablewrap{overflow-x:auto}
```

**Modal / sheet** — centered card with a navy gradient header.
```css
.modal{position:fixed;inset:0;z-index:90;display:none;align-items:flex-start;justify-content:center;
  padding:38px 16px;overflow:auto;background:rgba(7,21,43,.55);backdrop-filter:blur(5px)}
.modal.open{display:flex}
.sheet{width:min(1060px,96vw);background:var(--surface);border-radius:20px;box-shadow:var(--shadow-lg);overflow:hidden}
.sheet-head{padding:22px 24px;background:linear-gradient(135deg,var(--navy),var(--navy2));color:#fff}
.sheet-body{padding:24px}
```

**Toast**
```css
.toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(20px);z-index:120;
  background:var(--navy);color:#fff;padding:12px 20px;border-radius:12px;box-shadow:var(--shadow-lg);
  font-weight:600;font-size:13px;opacity:0;transition:.28s}
.toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
```

Other signature elements: **step/stage rail** (numbered circular nodes joined by a bar; done=flow, current=navy w/ flow ring, reject=red), **dropdown menu** (rounded, `--shadow-lg`, `pop` keyframe), **splash screen** (layered radial gradients over navy + faint route-background image + login card with `backdrop-filter:blur(14px)`).

---

## 6. Motion

- Standard transition: `transform .12–.16s ease, box-shadow .2s`.
- Keyframes: `fade` (view enter), `pop` (menus), `pulse` (stage node).
- Always wrap: `@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}`.

---

## 7. Branding assets

Repo: `liveWeb/assets/Branding/` (and canonical `Branding/`).

| Asset | Use |
|---|---|
| `SynoviaFlowLogo.png` | Synovia Flow wordmark — splash / top bar |
| `SynoviaFlowJustLogo.png` | mark only — **favicon** (`<link rel="icon">`) & compact |
| `SynoviaFlowWhite.png` | white wordmark for dark backgrounds |
| `FusionLogo.jpg` | Fusion product mark — "Powered by Fusion" |
| `assets/backgrounds/synovia-route-background.png` | faint splash backdrop (`opacity:.13`, `object-fit:cover`) |

Rules: logos placed over navy sit on a **white rounded panel**; product tagline **"Next-Generation Integration"**; "**Powered by Fusion**" pill uses the coral tokens (`rgba(255,122,69,.14)` bg, `--fusion2` text, glowing coral dot).

---

## 8. Minimal starter for a new site

1. Add the Montserrat `@import` and the `:root` block (§2).
2. `body{font-family:var(--font);background:var(--bg);color:var(--ink);line-height:1.45}`.
3. Compose pages from `.panel` / `.tile` / `.act` / `.pill` / `.chip` / `table` (§5) and the sticky top bar + floating `.nav` (§4).
4. Use `--flow` for primary/interactive, `--fusion` for the single hero/CTA accent, navy for structure, and the status tokens for state.

Keep it framework-free and token-driven and any new screen will read as the same product.
