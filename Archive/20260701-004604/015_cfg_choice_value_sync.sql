/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 15 OF N
    =================================================
    Purpose : Make CFG.Choice_Value_Cache refreshable with easy change detection.

              The downloader (Modules/Global/fetch_choice_values.py) runs an
              initial load and then regular refreshes. Each refresh classifies
              every value as NEW / CHANGED / UNCHANGED / REMOVED relative to what
              is already cached, so changes are trivial to find - filter
              ChangeStatus, or use CFG.vw_Choice_Value_Changes /
              CFG.vw_Choice_Sync_Summary.

              Adds the SYNCING / SYNCED / SYNC_FAILED process statuses. Each run is
              one EXC.Execution (SYNCING -> COMPLETED); each field is one
              EXC.Transaction; failures go to EXC.Error.

    Run after : 002 (Choice tables), 003 (status vocab), 014 (field map).
    Safe to rerun: Yes (column + view guards).
*/

/* ------------------------------------------------------------------ */
/* Change-tracking columns on CFG.Choice_Value_Cache                   */
/* ------------------------------------------------------------------ */
IF COL_LENGTH('CFG.Choice_Value_Cache', 'RowHash') IS NULL
    ALTER TABLE CFG.Choice_Value_Cache ADD RowHash char(64) NULL;
GO
IF COL_LENGTH('CFG.Choice_Value_Cache', 'ChangeStatus') IS NULL
    ALTER TABLE CFG.Choice_Value_Cache ADD ChangeStatus varchar(12) NULL;   -- NEW|CHANGED|UNCHANGED|REMOVED
GO
IF COL_LENGTH('CFG.Choice_Value_Cache', 'FirstSeenAt') IS NULL
    ALTER TABLE CFG.Choice_Value_Cache ADD FirstSeenAt datetime2(3) NULL;
GO
IF COL_LENGTH('CFG.Choice_Value_Cache', 'LastSyncedAt') IS NULL
    ALTER TABLE CFG.Choice_Value_Cache ADD LastSyncedAt datetime2(3) NULL;
GO
IF COL_LENGTH('CFG.Choice_Value_Cache', 'LastSyncExecutionID') IS NULL
    ALTER TABLE CFG.Choice_Value_Cache ADD LastSyncExecutionID bigint NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_CFG_Choice_Value_Cache_Change'
              AND object_id=OBJECT_ID('CFG.Choice_Value_Cache'))
    CREATE INDEX IX_CFG_Choice_Value_Cache_Change
        ON CFG.Choice_Value_Cache (ChangeStatus, LastSyncExecutionID);
GO

/* ------------------------------------------------------------------ */
/* SYNCING / SYNCED / SYNC_FAILED process statuses                     */
/* ------------------------------------------------------------------ */
MERGE CFG.Status_Vocabulary AS t
USING (VALUES
    ('SYNCING', 'SYNCING',     'Reference-data refresh in progress.',  5, 0, 0),
    ('SYNCING', 'SYNCED',      'Reference-data set refreshed.',        6, 0, 0),
    ('SYNCING', 'SYNC_FAILED', 'Reference-data refresh failed.',       7, 0, 1)
) AS s (ProcessName, ResultStatus, Meaning, SortOrder, IsTerminal, IsException)
ON t.ResultStatus = s.ResultStatus
WHEN MATCHED THEN UPDATE SET ProcessName=s.ProcessName, Meaning=s.Meaning, SortOrder=s.SortOrder,
    IsTerminal=s.IsTerminal, IsException=s.IsException
WHEN NOT MATCHED THEN INSERT (ProcessName, ResultStatus, Meaning, SortOrder, IsTerminal, IsException)
    VALUES (s.ProcessName, s.ResultStatus, s.Meaning, s.SortOrder, s.IsTerminal, s.IsException);
GO

/* ------------------------------------------------------------------ */
/* Views to surface changes easily                                     */
/* ------------------------------------------------------------------ */
CREATE OR ALTER VIEW CFG.vw_Choice_Value_Changes AS
    /* Every value that changed at its last sync - new/changed/removed only. */
    SELECT ChoiceField, ChoiceValue, ChoiceName, ChangeStatus, IsActive,
           FirstSeenAt, LastSyncedAt, LastSyncExecutionID
    FROM CFG.Choice_Value_Cache
    WHERE ChangeStatus IN ('NEW', 'CHANGED', 'REMOVED');
GO

CREATE OR ALTER VIEW CFG.vw_Choice_Sync_Summary AS
    /* Per-field counts - active total + this-cycle deltas. */
    SELECT ChoiceField,
           COUNT(*)                                                   AS TotalValues,
           SUM(CASE WHEN IsActive = 1 THEN 1 ELSE 0 END)              AS ActiveValues,
           SUM(CASE WHEN ChangeStatus = 'NEW'     THEN 1 ELSE 0 END)  AS NewCount,
           SUM(CASE WHEN ChangeStatus = 'CHANGED' THEN 1 ELSE 0 END)  AS ChangedCount,
           SUM(CASE WHEN ChangeStatus = 'REMOVED' THEN 1 ELSE 0 END)  AS RemovedCount,
           MAX(LastSyncedAt)                                          AS LastSyncedAt,
           MAX(LastSyncExecutionID)                                   AS LastSyncExecutionID
    FROM CFG.Choice_Value_Cache
    GROUP BY ChoiceField;
GO
