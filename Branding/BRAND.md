# Synovia Fusion — Brand Assets

Identity assets for Fusion Flow documents, decks and the serve/reporting layer.

## Logos

| File | Use |
|------|-----|
| `synovia_logo.jpg` | Primary Synovia wordmark (on light/white backgrounds). Document headers. |
| `synovia_logo_white.png` | White logo, transparent — for **dark** backgrounds (covers, dividers). |
| `synovia-flow-logo.png` | Synovia **Flow** mark (transparent). |
| `synovia_blue.png` | Synovia mark on brand blue. |
| `fusion_logo.jpg` | Fusion product mark (light backgrounds). |
| `clients/birkdale.png` | Client — Birkdale (BKD), pilot. |
| `clients/countrywide.png` | Client — Countrywide (CWD). |
| `clients/primeline-express.png` | Client — Primeline Express (PLE). |
| `clients/claritycargologo.png` | Client — Clarity Cargo. |

Backgrounds (route imagery etc.) live in `../assets/backgrounds/`.

## Palette

Sampled from the official marks. Use the deep royal as the primary brand colour,
flow-blue as the interactive/accent, and a restrained customs-gold only for
stage/seal markers in process material.

| Token | Hex | Use |
|-------|-----|-----|
**Flow 3 scheme** — midnight navy grounds, flow-aqua primary, fusion-coral secondary.
Canonical values live in `fusion_design_tokens.json` (mirrored by `liveWeb/`).

| Token | Hex | Use |
|-------|-----|-----|
| Abyss              | `#07152B` | Deepest ground — splash / dark hero |
| Navy (primary)     | `#0F2A4A` | Sidebar / nav pill / headers / dark surfaces |
| Navy 2             | `#143458` | Dark surface 2 / nav gradient end |
| Flow aqua (accent) | `#13DAC6` | **Primary brand accent** — active nav, links, charts |
| Flow deep          | `#0AB6A6` | Link default / hover / line stroke |
| Fusion coral       | `#FF7A45` | **Secondary accent** — "Powered by Fusion", attention |
| Fusion light       | `#FF9A6B` | Fusion gradient end |
| Sky                | `#3BA0FF` | Chart secondary / submitted state |
| Ink (navy text)    | `#132133` | Body text on light |
| Slate (secondary)  | `#5E7085` | Secondary text / labels |
| App background     | `#EEF3FA` | Navy-biased neutral ground |
| Panel / surface    | `#FFFFFF` | Cards / panels / tiles |
| Hairline           | `#DCE6F2` | Borders / rules |

Semantic (status): success `#17B26A` · warning `#F79009` · error `#F04438` · info `#7A5AF8`.

> Legacy royal/flow-blue values (`#17407F` / `#2F7FC4`) are superseded by the Flow 3
> scheme above; customs-gold `#C0871F` is retained only for stage/seal markers in process docs.

## Typography

- **Display / body:** Segoe UI (Windows-native; substitutes cleanly on macOS).
- **Data / mono:** Consolas — field names, codes, status pills, traces.

> These assets were recovered from the application's `app/static/` image set.
> The canonical source folders on the build machine are
> `…\Fusion_Flow_V3_QAS\Branding` and `…\Fusion_Flow_V3_QAS\assets`; this folder
> mirrors that identity so documents version with the codebase.
