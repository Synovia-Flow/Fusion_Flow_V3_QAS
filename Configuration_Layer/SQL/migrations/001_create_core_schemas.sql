/*
    Fusion_Flow_V3_QAS SQL migration.

    Purpose:
        Create the ownership schemas: CFG, EXC, ING, STG and TSS.

    Run order:
        Execute files in numeric filename order. Scripts are idempotent
        where practical so QAS can be refreshed safely.
*/
IF SCHEMA_ID('CFG') IS NULL EXEC('CREATE SCHEMA CFG');
IF SCHEMA_ID('EXC') IS NULL EXEC('CREATE SCHEMA EXC');
IF SCHEMA_ID('ING') IS NULL EXEC('CREATE SCHEMA ING');
IF SCHEMA_ID('STG') IS NULL EXEC('CREATE SCHEMA STG');
IF SCHEMA_ID('TSS') IS NULL EXEC('CREATE SCHEMA TSS');
GO
