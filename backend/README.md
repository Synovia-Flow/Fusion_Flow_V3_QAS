# Backend

This folder is the future Azure-deployable backend home for Fusion Flow V3 QAS.

Keep it simple:

- `app/` will contain Flask/API blueprints and reusable business services.
- `jobs/` will contain scheduled/operational jobs.
- Current working Graph and FLOW V3 scripts remain in the existing root folders until the app layer is brought into QAS.

Current source locations:

```text
Graph/
Integration_Layer/FLOW_V3/
Configuration_Layer/SQL/
```

Do not duplicate production logic here until the required blueprint/service has been reviewed and copied intentionally.
