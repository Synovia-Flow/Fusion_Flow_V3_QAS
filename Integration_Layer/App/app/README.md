# App Layer

This is the new QAS app layer. It starts intentionally small.

Current scope:

- Provide a local Flask app entrypoint for Azure deployment readiness.
- Add FLOW V3 blueprints/services only when we rebuild them for QAS.
- Do not depend on `FUSION_FLOW_APP_ROOT` or the V2 repository at runtime.

Current endpoint:

```text
GET /health
```
