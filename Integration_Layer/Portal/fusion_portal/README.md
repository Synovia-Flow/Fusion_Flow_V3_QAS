# Fusion Portal Frontend

React + Vite dashboard surface for the Fusion Flow portal.

## Scripts

- `npm run dev` starts the local Vite server.
- `npm run build` creates a production build.
- `npm run preview` serves the production build locally.

## API Wiring

The frontend calls relative `/api/*` routes. In local Vite development those routes are proxied to:

`http://127.0.0.1:8000`

Start the backend from `Integration_Layer/Portal/fusion_api`:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

For deployed static hosting, set `VITE_API_BASE_URL` at build time when the API is hosted on a different origin.

If the API is unavailable, the portal keeps the current local sample data so the UI can still be reviewed.
