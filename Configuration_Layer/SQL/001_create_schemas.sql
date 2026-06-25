/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 1 OF 3
    =================================================
    Purpose : Provision the 11 Release 1 schemas inside the Fusion_Flow_V3_QAS
              database. Schemas only - no tables here.

    Source  : Fusion Flow R1 Functional Specification (Section 3.1 Schema layer model).

    Run after : the Azure SQL database `Fusion_Flow_V3_QAS` exists and is selected.
    Run before: 002_cfg_tables.sql, 003_seed_cfg.sql
    Safe to rerun: Yes. Each CREATE is guarded by an existence check.

    Schema layer model (R1):
        CFG  Configuration        - settings, clients, credentials, paths, rules, choice cache
        ING  Ingestion (raw)      - inbound artefacts landed verbatim
        EXC  Execution            - master execution/transaction spine + enhancement log
        LOG  Log                  - technical/process/error/API traces
        PRS  Processing           - canonical TSS-shaped base objects
        STG  Staging              - submission-ready, client-prefixed
        API  API                  - full request/response capture
        CTL  Control              - authoritative latest status + views
        ARC  Archive              - shared archived reconciled records
        SRV  Serve                - reporting & analytics views/facts
        BKD  Client schema        - per-client pulled-back/reconciled records (Birkdale pilot)

    Note: BKD is the first per-client schema. Each principal added later receives
          its own schema named after its 3-letter CFG.Clients code.
*/

IF SCHEMA_ID('CFG') IS NULL EXEC('CREATE SCHEMA CFG');
GO
IF SCHEMA_ID('ING') IS NULL EXEC('CREATE SCHEMA ING');
GO
IF SCHEMA_ID('EXC') IS NULL EXEC('CREATE SCHEMA EXC');
GO
IF SCHEMA_ID('LOG') IS NULL EXEC('CREATE SCHEMA LOG');
GO
IF SCHEMA_ID('PRS') IS NULL EXEC('CREATE SCHEMA PRS');
GO
IF SCHEMA_ID('STG') IS NULL EXEC('CREATE SCHEMA STG');
GO
IF SCHEMA_ID('API') IS NULL EXEC('CREATE SCHEMA API');
GO
IF SCHEMA_ID('CTL') IS NULL EXEC('CREATE SCHEMA CTL');
GO
IF SCHEMA_ID('ARC') IS NULL EXEC('CREATE SCHEMA ARC');
GO
IF SCHEMA_ID('SRV') IS NULL EXEC('CREATE SCHEMA SRV');
GO
IF SCHEMA_ID('BKD') IS NULL EXEC('CREATE SCHEMA BKD');
GO
