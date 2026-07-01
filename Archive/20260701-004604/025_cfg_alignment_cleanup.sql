/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 25 OF N
    =================================================
    Purpose : Tighten alignment between the CFG reference data, the vocabulary and
              the solution. Removes duplication and reconciles the status model.

              1. JOBS - deactivate the two superseded BKD ENS processing jobs
                 (PRS_PROCESS_BKD, PRS_STAGE_BKD_ENS). PRS_ENGINE_BKD_ENS (config-
                 driven engine) + PRS_REPROCESS_BKD_ENS are the live path.

              2. CLIENTS - consolidate CountryWide to a SINGLE code: CWD. The DB
                 carried both CWD (original) and CWF (later duplicate) for the same
                 principal. CWF is removed across all CFG tables + its empty schema;
                 CWD is the canonical CountryWide client (schema CWD).

              3. ENVIRONMENT - standardise the env code: DEFAULT_ENV = 'TST' (was
                 'TEST', which did not match CFG.TSS_Environment codes PRD/TST).

              4. VOCABULARY / Fusion_Status - remove the 'STAGED' double-meaning and
                 make Fusion_Status a consistent subset of the vocabulary:
                   - 'STAGED' now = staged into the client submission table
                     (pre-validation), SortOrder 45.
                   - new 'STG_MATERIALISED' (SortOrder 60) = validated record
                     materialised into the STG schema (the old 'STAGED' meaning).
                   - PRS.BKD_ENS_Header_* Fusion_Status CHECK widened to the full
                     movement lifecycle so submission/monitoring/reconcile states
                     are representable.

    Run after : 003 (vocab/clients), 012 (jobs), 013 (BKD ENS tables). Safe to rerun.
*/

/* ================================================================== */
/* 1. Deactivate superseded processing jobs.                           */
/* ================================================================== */
IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
    UPDATE CFG.Job
       SET IsActive = 0,
           Notes = CONCAT(LEFT(ISNULL(Notes, ''), 300), ' [Deactivated 025: superseded by PRS_ENGINE_BKD_ENS.]'),
           UpdatedAt = SYSUTCDATETIME()
     WHERE JobCode IN ('PRS_PROCESS_BKD', 'PRS_STAGE_BKD_ENS') AND IsActive = 1;
GO

/* ================================================================== */
/* 2. Consolidate CountryWide -> CWD; remove the CWF duplicate.        */
/* ================================================================== */
IF SCHEMA_ID('CWD') IS NULL EXEC('CREATE SCHEMA CWD');
GO

/* Make sure the canonical CWD client row is correct (inactive CountryWide). */
IF OBJECT_ID('CFG.Clients', 'U') IS NOT NULL
    MERGE CFG.Clients AS t
    USING (VALUES ('CWD', 'CountryWide', 'CWD', 'CWD_', 'A', 0, NULL, 0,
                   'CountryWide - registered, inactive. Canonical code (CWF duplicate removed in 025).')) AS s
        (ClientCode, ClientName, SchemaName, StgTablePrefix, DefaultRoute, IsAgent, ActAsSysId, IsActive, Notes)
    ON t.ClientCode = s.ClientCode
    WHEN MATCHED THEN UPDATE SET ClientName=s.ClientName, SchemaName=s.SchemaName,
        StgTablePrefix=s.StgTablePrefix, DefaultRoute=s.DefaultRoute, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
    WHEN NOT MATCHED THEN INSERT (ClientCode, ClientName, SchemaName, StgTablePrefix, DefaultRoute, IsAgent, ActAsSysId, IsActive, Notes)
        VALUES (s.ClientCode, s.ClientName, s.SchemaName, s.StgTablePrefix, s.DefaultRoute, s.IsAgent, s.ActAsSysId, s.IsActive, s.Notes);
GO

/* Delete CWF rows from every CFG table that carries a ClientCode (children first). */
IF OBJECT_ID('CFG.API_Process_Map', 'U') IS NOT NULL DELETE FROM CFG.API_Process_Map WHERE ClientCode = 'CWF';
IF OBJECT_ID('CFG.API_Version', 'U')     IS NOT NULL DELETE FROM CFG.API_Version     WHERE ClientCode = 'CWF';
IF OBJECT_ID('CFG.Folder_Paths', 'U')    IS NOT NULL DELETE FROM CFG.Folder_Paths    WHERE ClientCode = 'CWF';
IF OBJECT_ID('CFG.Email_Rules', 'U')     IS NOT NULL DELETE FROM CFG.Email_Rules     WHERE ClientCode = 'CWF';
IF OBJECT_ID('CFG.TSS_Credential', 'U')  IS NOT NULL DELETE FROM CFG.TSS_Credential  WHERE ClientCode = 'CWF';
IF OBJECT_ID('CFG.Credentials', 'U')     IS NOT NULL DELETE FROM CFG.Credentials     WHERE ClientCode = 'CWF';
IF OBJECT_ID('CFG.Ingestion_Source', 'U')IS NOT NULL DELETE FROM CFG.Ingestion_Source WHERE ClientCode = 'CWF';
IF OBJECT_ID('CFG.Clients', 'U')         IS NOT NULL DELETE FROM CFG.Clients         WHERE ClientCode = 'CWF';
GO

/* Drop the now-orphaned CWF schema if it holds no objects. */
IF SCHEMA_ID('CWF') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.objects WHERE schema_id = SCHEMA_ID('CWF'))
    EXEC('DROP SCHEMA CWF');
GO

/* ================================================================== */
/* 3. Standardise the environment code to match CFG.TSS_Environment.   */
/* ================================================================== */
IF OBJECT_ID('CFG.Application_Parameters', 'U') IS NOT NULL
    UPDATE CFG.Application_Parameters
       SET ParameterValue = 'TST',
           Description = 'Default TSS environment code, matching CFG.TSS_Environment (PRD|TST).',
           UpdatedAt = SYSUTCDATETIME()
     WHERE ParameterKey = 'DEFAULT_ENV' AND ParameterValue <> 'TST';
GO

/* ================================================================== */
/* 4. Vocabulary: resolve the STAGED double-meaning + add STG step.    */
/* ================================================================== */
IF OBJECT_ID('CFG.Status_Vocabulary', 'U') IS NOT NULL
BEGIN
    /* 'STAGED' now means staged into the client submission table (pre-validation). */
    UPDATE CFG.Status_Vocabulary
       SET ProcessName = 'STAGING',
           Meaning = 'Movement staged into the client submission table (pre-validation).',
           SortOrder = 45
     WHERE ResultStatus = 'STAGED';

    /* The old 'STAGED' meaning (materialised into the STG schema) gets its own status. */
    MERGE CFG.Status_Vocabulary AS t
    USING (VALUES ('STG_LOAD', 'STG_MATERIALISED', 'Validated record materialised into the STG schema.', 60, 0, 0))
        AS s (ProcessName, ResultStatus, Meaning, SortOrder, IsTerminal, IsException)
    ON t.ResultStatus = s.ResultStatus
    WHEN MATCHED THEN UPDATE SET ProcessName=s.ProcessName, Meaning=s.Meaning, SortOrder=s.SortOrder,
        IsTerminal=s.IsTerminal, IsException=s.IsException
    WHEN NOT MATCHED THEN INSERT (ProcessName, ResultStatus, Meaning, SortOrder, IsTerminal, IsException)
        VALUES (s.ProcessName, s.ResultStatus, s.Meaning, s.SortOrder, s.IsTerminal, s.IsException);
END;
GO

/* Widen the BKD ENS Fusion_Status checks to the movement lifecycle (vocabulary subset). */
IF OBJECT_ID('PRS.CK_PRS_BKD_ENS_Track_Status', 'C') IS NOT NULL
    ALTER TABLE PRS.BKD_ENS_Header_Tracking DROP CONSTRAINT CK_PRS_BKD_ENS_Track_Status;
IF OBJECT_ID('PRS.CK_PRS_BKD_ENS_Sub_Status', 'C') IS NOT NULL
    ALTER TABLE PRS.BKD_ENS_Header_Submission DROP CONSTRAINT CK_PRS_BKD_ENS_Sub_Status;
GO

IF OBJECT_ID('PRS.BKD_ENS_Header_Tracking', 'U') IS NOT NULL
    ALTER TABLE PRS.BKD_ENS_Header_Tracking WITH NOCHECK ADD CONSTRAINT CK_PRS_BKD_ENS_Track_Status CHECK
        (Fusion_Status IN ('STAGED','VALIDATED','REJECTED','STG_MATERIALISED','READY','LINKED',
                           'SUBMITTING','SUBMITTED','ACKNOWLEDGED','IN_PROGRESS','RECONCILED',
                           'MISMATCH','ARCHIVED','ERROR','CANCELLED','ON_HOLD'));
IF OBJECT_ID('PRS.BKD_ENS_Header_Submission', 'U') IS NOT NULL
    ALTER TABLE PRS.BKD_ENS_Header_Submission WITH NOCHECK ADD CONSTRAINT CK_PRS_BKD_ENS_Sub_Status CHECK
        (Fusion_Status IN ('STAGED','VALIDATED','REJECTED','STG_MATERIALISED','READY','LINKED',
                           'SUBMITTING','SUBMITTED','ACKNOWLEDGED','IN_PROGRESS','RECONCILED',
                           'MISMATCH','ARCHIVED','ERROR','CANCELLED','ON_HOLD'));
GO
