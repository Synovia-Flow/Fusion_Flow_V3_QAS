# SQL Layer

This folder holds the Fusion Flow V3 QAS database setup.

## Run Order

1. Run every file in `migrations/` in numeric filename order.
2. Run every file in `seeds/` in numeric filename order after the tables and constraints exist.

The migration files define database shape. The seed files preload QAS configuration such as tenants, Graph routes, pack rules, and QAS submit gates.

## Schema Ownership

| Schema | Owns |
| --- | --- |
| `CFG` | Tenant, route, pack-rule and runtime configuration. |
| `EXC` | Execution rows and process logs. |
| `ING` | Inbound email/file/process records and raw loaded rows. |
| `STG` | Validation and business staging objects. |
| `TSS` | TSS API submit/status mirrors and references. |

## Current MVP Files

| File | Purpose |
| --- | --- |
| `migrations/001_create_core_schemas.sql` | Creates `CFG`, `EXC`, `ING`, `STG`, and `TSS`. |
| `migrations/002_create_mvp_graph_pipeline_tables.sql` | Creates the first Graph intake, staging, and TSS mirror tables. |
| `migrations/003_add_mvp_graph_pipeline_constraints.sql` | Adds the original MVP foreign keys and indexes. |
| `migrations/004_create_tenant_ingestion_tables.sql` | Adds tenant, tenant-setting, route, pack-rule, process-file, load-row, and log tables. |
| `migrations/005_extend_graph_tables_for_tenant_ingestion.sql` | Adds tenant/folder/pack metadata columns to `CFG.Graph` and `ING.Graph`. |
| `migrations/006_add_tenant_ingestion_constraints.sql` | Adds tenant ingestion foreign keys and indexes, including `CFG.TenantSetting`. |
| `seeds/001_seed_qas_tenants_routes_pack_rules.sql` | Seeds BKD, Country Wide Homes, Primeline Express, and default QAS TSS gates. |

The old single bootstrap file was split so future DB changes can be reviewed and applied one step at a time.
## Runtime Gates

TSS execution gates live in `CFG.TenantSetting`, not only in `.env`. The seed keeps QAS safe by default with `TSS_SUBMIT_ENABLED=false` and `TSS_DRY_RUN=true` for each tenant.

Real credentials still belong in `.env` or secure deployment configuration, never in seed data.