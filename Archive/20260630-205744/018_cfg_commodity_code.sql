/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 18 OF N
    =================================================
    Purpose : Give commodity_code its own home. It is by far the largest choice
              set (~35k codes, with effective dates) and does not belong in the
              shared CFG.Choice_Value_Cache. A dedicated table + script keeps the
              general choice refresh fast and lets commodity carry its own
              metadata (effective dates).

              - Creates CFG.Commodity_Code_Cache (+ change-tracking + a changes view).
              - Removes commodity_code from the general flow: deactivates it in
                CFG.Choice_Field_Registry, deletes its rows from
                CFG.Choice_Value_Cache, and removes its CFG.Choice_Field_Map row
                (the processing engine resolves commodity against this table).
              - Registers the REF_FETCH_COMMODITY_CODES job.

    Run after : 014-016 (choice map + registry). Safe to rerun.
    Fed by : Modules/Global/fetch_commodity_codes.py
*/

/* ------------------------------------------------------------------ */
/* CFG.Commodity_Code_Cache                                            */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Commodity_Code_Cache', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Commodity_Code_Cache (
        CommodityID         bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Commodity_Code PRIMARY KEY,
        CommodityCode       varchar(20)  NOT NULL,
        Description         nvarchar(max) NULL,
        EffectiveFrom       date NULL,
        EffectiveTo         date NULL,
        ExtraJson           nvarchar(max) NULL,
        RowHash             char(64) NULL,
        ChangeStatus        varchar(12) NULL,          -- NEW | CHANGED | UNCHANGED | REMOVED
        IsActive            bit NOT NULL CONSTRAINT DF_CFG_Commodity_IsActive DEFAULT (1),
        FirstSeenAt         datetime2(3) NULL,
        LastSyncedAt        datetime2(3) NULL,
        LastSyncExecutionID bigint NULL,
        RetrievedAt         datetime2(3) NOT NULL CONSTRAINT DF_CFG_Commodity_Retrieved DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Commodity_Code UNIQUE (CommodityCode)
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_CFG_Commodity_Code_Change'
              AND object_id=OBJECT_ID('CFG.Commodity_Code_Cache'))
    CREATE INDEX IX_CFG_Commodity_Code_Change
        ON CFG.Commodity_Code_Cache (ChangeStatus, LastSyncExecutionID);
GO

CREATE OR ALTER VIEW CFG.vw_Commodity_Code_Changes AS
    SELECT CommodityCode, Description, ChangeStatus, IsActive,
           EffectiveFrom, EffectiveTo, FirstSeenAt, LastSyncedAt, LastSyncExecutionID
    FROM CFG.Commodity_Code_Cache
    WHERE ChangeStatus IN ('NEW', 'CHANGED', 'REMOVED');
GO

/* ------------------------------------------------------------------ */
/* Remove commodity_code from the general choice flow.                 */
/* ------------------------------------------------------------------ */
DELETE FROM CFG.Choice_Value_Cache  WHERE ChoiceField = 'commodity_code';
DELETE FROM CFG.Choice_Field_Map    WHERE ChoiceField = 'commodity_code';
UPDATE CFG.Choice_Field_Registry
   SET IsActive = 0, UsedBy = 'Goods Item (dedicated CFG.Commodity_Code_Cache)', UpdatedAt = SYSUTCDATETIME()
 WHERE ChoiceField = 'commodity_code';
GO

/* ------------------------------------------------------------------ */
/* Register the dedicated downloader job.                              */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
    MERGE CFG.Job AS t
    USING (VALUES
        ('REF_FETCH_COMMODITY_CODES', 'Reference - Download Commodity Codes', 'CONFIG', NULL, 'API', 'TASK', NULL, NULL,
         'Download the full commodity-code reference set (~35k, with effective dates) via GET /choice_values/commodity_code into CFG.Commodity_Code_Cache, with NEW/CHANGED/UNCHANGED/REMOVED change detection. Separate from the general choice refresh because of its size. No CLI - reuses CHOICE_VALUES_* controls.',
         'fetch_commodity_codes:run', 'TSS GET /choice_values/commodity_code', 'CFG.Commodity_Code_Cache',
         'Weekly / on tariff update', 1, 'Large reference set; batched commits.')
    ) AS s (JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
            Purpose, EntryPoint, InputSource, OutputTarget, Schedule, IsActive, Notes)
    ON t.JobCode = s.JobCode
    WHEN MATCHED THEN UPDATE SET JobName=s.JobName, Purpose=s.Purpose, EntryPoint=s.EntryPoint,
        InputSource=s.InputSource, OutputTarget=s.OutputTarget, Schedule=s.Schedule, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
    WHEN NOT MATCHED THEN INSERT (JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
            Purpose, EntryPoint, InputSource, OutputTarget, Schedule, IsActive, Notes)
        VALUES (s.JobCode, s.JobName, s.ModuleName, s.ClientCode, s.Channel, s.JobType, s.StepNo, s.ParentJobCode,
                s.Purpose, s.EntryPoint, s.InputSource, s.OutputTarget, s.Schedule, s.IsActive, s.Notes);
GO
