/*
    Fusion_Flow_V3_QAS SQL migration.

    Purpose:
        Extend CFG.Graph and ING.Graph with tenant route and folder-backed ingestion columns.

    Run order:
        Execute files in numeric filename order. Scripts are idempotent
        where practical so QAS can be refreshed safely.
*/
IF COL_LENGTH('CFG.Graph', 'TenantID') IS NULL ALTER TABLE CFG.Graph ADD TenantID bigint NULL;
IF COL_LENGTH('CFG.Graph', 'RouteID') IS NULL ALTER TABLE CFG.Graph ADD RouteID bigint NULL;
IF COL_LENGTH('CFG.Graph', 'ProcessFolder') IS NULL ALTER TABLE CFG.Graph ADD ProcessFolder nvarchar(1000) NULL;
IF COL_LENGTH('CFG.Graph', 'FailFolder') IS NULL ALTER TABLE CFG.Graph ADD FailFolder nvarchar(1000) NULL;
IF COL_LENGTH('CFG.Graph', 'OutputFilePattern') IS NULL ALTER TABLE CFG.Graph ADD OutputFilePattern nvarchar(255) NULL;
IF COL_LENGTH('CFG.Graph', 'EnsSheetName') IS NULL ALTER TABLE CFG.Graph ADD EnsSheetName nvarchar(128) NULL;
IF COL_LENGTH('CFG.Graph', 'DecSheetName') IS NULL ALTER TABLE CFG.Graph ADD DecSheetName nvarchar(128) NULL;
GO

IF COL_LENGTH('ING.Graph', 'RouteID') IS NULL ALTER TABLE ING.Graph ADD RouteID bigint NULL;
IF COL_LENGTH('ING.Graph', 'PackCode') IS NULL ALTER TABLE ING.Graph ADD PackCode varchar(30) NULL;
IF COL_LENGTH('ING.Graph', 'SourcePart') IS NULL ALTER TABLE ING.Graph ADD SourcePart varchar(30) NULL;
IF COL_LENGTH('ING.Graph', 'ProcessFolder') IS NULL ALTER TABLE ING.Graph ADD ProcessFolder nvarchar(1000) NULL;
IF COL_LENGTH('ING.Graph', 'FailFolder') IS NULL ALTER TABLE ING.Graph ADD FailFolder nvarchar(1000) NULL;
IF COL_LENGTH('ING.Graph', 'GeneratedCsvPath') IS NULL ALTER TABLE ING.Graph ADD GeneratedCsvPath nvarchar(1000) NULL;
IF COL_LENGTH('ING.Graph', 'LoadStatus') IS NULL ALTER TABLE ING.Graph ADD LoadStatus varchar(40) NULL;
IF COL_LENGTH('ING.Graph', 'FailReason') IS NULL ALTER TABLE ING.Graph ADD FailReason nvarchar(2000) NULL;
GO
