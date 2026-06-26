# SQL Layer

This folder is the easiest way to build the Fusion Flow V3 QAS database model.

## Run These 3 Files

Run them against the selected `Fusion_Flow_V3_QAS` database in this order:

1. `001_create_schemas_and_tables.sql`
2. `002_add_constraints_and_indexes.sql`
3. `003_seed_qas_config.sql`

The files are intentionally split by job:

| File | Plain-English job |
| --- | --- |
| `001_create_schemas_and_tables.sql` | Make the empty boxes: schemas, tables and extra columns. |
| `002_add_constraints_and_indexes.sql` | Connect the boxes: foreign keys and indexes. |
| `003_seed_qas_config.sql` | Put starting config in the boxes: BKD active, CWH/PLE inactive and pack rules. |

## What Each Schema Means

| Schema | Meaning |
| --- | --- |
| `CFG` | What should happen: tenants, routes and pack rules. |
| `EXC` | What happened: execution runs and logs. |
| `ING` | What came in: emails, files, process records and loaded rows. |

## Safety Notes

- These scripts do not create the database itself; create/select `Fusion_Flow_V3_QAS` first.
- They do not store credentials or secrets.
- CWH and PLE are present for design/testing but inactive until sender/source/templates are confirmed.

## Smoke Test After Running

After the 3 files run, validate the chain exists with one dummy path:

```text
CFG.Tenant
-> CFG.Graph
-> ING.Graph
-> ING.ProcessFile
-> ING.LoadRow
```
