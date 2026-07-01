/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 0 OF N
    =================================================
    Purpose : Create the CHG (change management) schema and its audit tables.
              Every deployment run and every DDL script applied is logged here,
              with the operator's description.

    Run first (the Python deploy tool also self-bootstraps this).
    Safe to rerun: Yes.

    Tables:
        CHG.Deployment  one row per deploy run (description, server, who, counts, status)
        CHG.Change_Log  one row per DDL script applied (name, hash, status, archive path)
*/

IF SCHEMA_ID('CHG') IS NULL EXEC('CREATE SCHEMA CHG');
GO

IF OBJECT_ID('CHG.Deployment', 'U') IS NULL
BEGIN
    CREATE TABLE CHG.Deployment (
        DeploymentID    bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CHG_Deployment PRIMARY KEY,
        RunStamp        varchar(20) NOT NULL,            -- yyyyMMdd-HHmmss
        Description     nvarchar(1000) NOT NULL,         -- prompted at deploy time
        ServerName      nvarchar(200) NULL,
        DatabaseName    nvarchar(128) NULL,
        AppliedBy       nvarchar(200) NULL,              -- OS / login user
        ScriptCount     int NOT NULL CONSTRAINT DF_CHG_Deployment_ScriptCount DEFAULT (0),
        SucceededCount  int NOT NULL CONSTRAINT DF_CHG_Deployment_Succeeded DEFAULT (0),
        FailedCount     int NOT NULL CONSTRAINT DF_CHG_Deployment_Failed DEFAULT (0),
        Status          varchar(20) NOT NULL CONSTRAINT DF_CHG_Deployment_Status DEFAULT ('RUNNING'),
        StartedAt       datetime2(3) NOT NULL CONSTRAINT DF_CHG_Deployment_Started DEFAULT (SYSUTCDATETIME()),
        EndedAt         datetime2(3) NULL
    );
END;
GO

IF OBJECT_ID('CHG.Change_Log', 'U') IS NULL
BEGIN
    CREATE TABLE CHG.Change_Log (
        ChangeID      bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CHG_Change_Log PRIMARY KEY,
        DeploymentID  bigint NOT NULL,
        ScriptName    nvarchar(500) NOT NULL,
        ScriptHash    char(64) NULL,                     -- sha256 of the script text
        BatchCount    int NULL,                          -- GO-separated batches executed
        Status        varchar(20) NOT NULL,              -- SUCCESS | FAILED | SKIPPED
        ErrorMessage  nvarchar(max) NULL,
        ArchivePath   nvarchar(1000) NULL,               -- where the DDL was moved on success
        AppliedAt     datetime2(3) NOT NULL CONSTRAINT DF_CHG_Change_Log_Applied DEFAULT (SYSUTCDATETIME())
    );
END;
GO

IF OBJECT_ID('CHG.FK_Change_Log_Deployment', 'F') IS NULL
    ALTER TABLE CHG.Change_Log WITH CHECK ADD CONSTRAINT FK_Change_Log_Deployment
        FOREIGN KEY (DeploymentID) REFERENCES CHG.Deployment (DeploymentID);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_CHG_Change_Log_Deployment' AND object_id=OBJECT_ID('CHG.Change_Log'))
    CREATE INDEX IX_CHG_Change_Log_Deployment ON CHG.Change_Log (DeploymentID);
GO
