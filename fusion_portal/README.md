# Render compatibility root

The real portal lives in `Portal/fusion_portal`.

This small wrapper exists only because the existing Render service may still
have `fusion_portal` saved as its Root Directory. Its build delegates to the
canonical portal and copies the generated `dist` here. New services should use
`Portal/fusion_portal`, as declared in `render.yaml`.