/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 21 OF N
    =================================================
    Purpose : Support a separate REPROCESS job that re-runs already-processed
              records and CLOSES OFF their errors.

              - Adds resolution tracking to PRS.BKD_ENS_Header_Tracking:
                  ReprocessCount        - times the movement has been reprocessed
                  ResolvedAt            - when a prior rejection was cleared
                  ResolvedByExecutionID - the reprocess run that resolved it
              - PRS.vw_BKD_ENS_Header_Resolved - movements rejected then resolved.
              - Seeds the reprocess controls + registers the PRS_REPROCESS_BKD_ENS job.

    Run after : 013 (tracking table), 019 (processing config). Safe to rerun.
    Engine    : Modules/Processing/reprocess_engine.py  (process_engine REPROCESS mode)
*/

IF COL_LENGTH('PRS.BKD_ENS_Header_Tracking', 'ReprocessCount') IS NULL
    ALTER TABLE PRS.BKD_ENS_Header_Tracking ADD ReprocessCount int NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Track_Reproc DEFAULT (0);
GO
IF COL_LENGTH('PRS.BKD_ENS_Header_Tracking', 'ResolvedAt') IS NULL
    ALTER TABLE PRS.BKD_ENS_Header_Tracking ADD ResolvedAt datetime2(3) NULL;
GO
IF COL_LENGTH('PRS.BKD_ENS_Header_Tracking', 'ResolvedByExecutionID') IS NULL
    ALTER TABLE PRS.BKD_ENS_Header_Tracking ADD ResolvedByExecutionID bigint NULL;
GO

CREATE OR ALTER VIEW PRS.vw_BKD_ENS_Header_Resolved AS
    SELECT ClientCode, MovementKey, Fusion_Status, ReprocessCount,
           ResolvedAt, ResolvedByExecutionID, ValidatedAt, SubmissionID
    FROM PRS.BKD_ENS_Header_Tracking
    WHERE ResolvedAt IS NOT NULL;
GO

/* ------------------------------------------------------------------ */
/* Reprocess controls (insert-only; operator-owned once set).          */
/* ------------------------------------------------------------------ */
MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('PROCESSING_MODE',            'NEW',      'STRING', 'Processing engine mode: NEW (untracked rows) or REPROCESS (re-run tracked rows). The reprocess job forces REPROCESS.'),
    ('PROCESSING_REPROCESS_SCOPE', 'REJECTED', 'STRING', 'Reprocess scope: REJECTED (only failed) or ALL (every tracked movement).'),
    ('PROCESSING_MOVEMENT_KEY',    '',         'STRING', 'Optional: reprocess a single MovementKey (blank = all in scope).')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO

/* ------------------------------------------------------------------ */
/* Register the reprocess job.                                         */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
    MERGE CFG.Job AS t
    USING (VALUES
        ('PRS_REPROCESS_BKD_ENS', 'Reprocess - Birkdale ENS Header', 'DATA_PROCESSING', 'BKD', NULL, 'TASK', NULL, NULL,
         'Re-run already-processed BKD ENS movements through the engine in REPROCESS mode (scope REJECTED by default), updating them in place and closing off resolved errors (clears RejectReason, stamps ResolvedAt). Run after a config/data fix. Controls: PROCESSING_REPROCESS_SCOPE / PROCESSING_MOVEMENT_KEY.',
         'reprocess_engine:main', 'PRS.BKD_ENS_Header_Tracking (rejected/all) + ING.BKD_Raw_ENS', 'PRS.BKD_ENS_Header_Submission / _Tracking',
         'On demand / after fixes', 1, 'Separate job; same engine as PRS_ENGINE_BKD_ENS in REPROCESS mode.')
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
