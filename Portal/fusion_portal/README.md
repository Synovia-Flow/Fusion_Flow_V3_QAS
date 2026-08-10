# Fusion Portal Frontend

React + Vite dashboard surface for the Fusion Flow portal.

## Scripts

- `npm run dev` starts the local Vite server.
- `npm run build` creates a production build.
- `npm run preview` serves the production build locally.

## API Wiring

The frontend calls relative `/api/*` routes. In local Vite development those routes are proxied to:

`http://127.0.0.1:8000`

Start the backend from `Portal/fusion_api`:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

For deployed static hosting, set `VITE_API_BASE_URL` at build time when the API is hosted on a different origin.

If the API is unavailable, the portal keeps the current local sample data so the UI can still be reviewed.

## Status vocabulary

`GET /api/consignments/{clientCode}` returns a `statusVocabulary` array alongside the
rows, read from `CFG.Status_Vocabulary` (`ProcessName`, `ResultStatus`, `Meaning`,
`SortOrder`, `IsTerminal`, `IsException`). The View Consignments page uses it to:

- build the status filter tabs (ordered by `SortOrder`, only statuses with rows shown),
- show the `Meaning` as the tab tooltip.

If the API omits the field or returns an empty list (e.g. the table is missing),
the frontend falls back to `STATUS_VOCABULARY_FALLBACK` in `src/App.jsx` — keep that
list aligned with the seeded `CFG.Status_Vocabulary` rows when the vocabulary changes.

Filter tabs count rows by the **local** PRS status (`row.status`); the TSS mirror
status is shown separately per row and in the detail modal chips.

## Consignment detail modal

The modal shows a V2-style reference strip (Local / TSS / ENS / SFD / SDI), a
pipeline rail (PRS → Validation → STG → ENS → Consignment → Goods → SFD → SDI), and an
action grid. Only **Submit Consignment** runs a live call (the TSS route check via
`onQueueForTss`); the other actions are informational gates until their flows are
wired. Edits in the "Editable PRS fields" panel stay local until a commit/promotion
action exists. The **Delete** and **Excel** toolbar buttons on the list view are
disabled placeholders — no V3 endpoint yet.
