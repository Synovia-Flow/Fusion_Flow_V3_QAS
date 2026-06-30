/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 12 OF N
    =================================================
    Purpose : CFG.Job - the canonical registry of scheduled jobs across the
              platform. Each row describes ONE schedulable unit of work: its
              purpose, the module it belongs to, the client/channel it serves,
              its order within a cycle, and the code entry point that runs it.

              The Ingestion runner reads this table to drive the cycle (no CLI),
              so the job list is authoritative data, not buried in code.

    Run after : 002 (CFG tables), 003 (CFG seed).
    Safe to rerun: Yes (MERGE). Re-running refreshes job metadata but PRESERVES
                   the IsActive flag you have set in the database.

    JobType:  ORCHESTRATOR - runs an ordered set of STEP jobs (the cycle entry).
              STEP         - one ordered step within a parent orchestrator.
              ACQUIRE      - a stand-alone acquisition route (channel framework).
              TASK         - a single self-contained job (e.g. processing run).
*/

/* ------------------------------------------------------------------ */
/* CFG.Job                                                             */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Job', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Job (
        JobID         int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Job PRIMARY KEY,
        JobCode       varchar(50)  NOT NULL,
        JobName       nvarchar(120) NOT NULL,
        ModuleName    varchar(40)  NOT NULL,             -- INGESTION, DATA_PROCESSING, ...
        ClientCode    char(3)      NULL,                 -- NULL = all / generic
        Channel       varchar(20)  NULL,                 -- EMAIL | SFTP | AS2 | API | FILE_DROP
        JobType       varchar(20)  NOT NULL,             -- ORCHESTRATOR | STEP | ACQUIRE | TASK
        StepNo        int          NULL,                 -- order within the parent cycle
        ParentJobCode varchar(50)  NULL,                 -- soft ref to the orchestrator JobCode
        Purpose       nvarchar(600) NOT NULL,
        EntryPoint    nvarchar(200) NULL,                -- module:function  (e.g. load_raw:run)
        InputSource   nvarchar(300) NULL,
        OutputTarget  nvarchar(300) NULL,
        Schedule      nvarchar(120) NULL,                -- human description of cadence
        IsActive      bit NOT NULL CONSTRAINT DF_CFG_Job_IsActive DEFAULT (1),
        Notes         nvarchar(600) NULL,
        CreatedAt     datetime2(3) NOT NULL CONSTRAINT DF_CFG_Job_Created DEFAULT (SYSUTCDATETIME()),
        UpdatedAt     datetime2(3) NOT NULL CONSTRAINT DF_CFG_Job_Updated DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Job_JobCode UNIQUE (JobCode)
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_Job_Module_Client_Step'
              AND object_id = OBJECT_ID('CFG.Job'))
    CREATE INDEX IX_CFG_Job_Module_Client_Step
        ON CFG.Job (ModuleName, ClientCode, IsActive, StepNo);
GO

/* ------------------------------------------------------------------ */
/* Seed the canonical job list.                                        */
/* MERGE refreshes metadata on re-deploy but does NOT touch IsActive   */
/* (operator-owned) for jobs that already exist.                       */
/* ------------------------------------------------------------------ */
MERGE CFG.Job AS t
USING (VALUES
    /* ---- INGESTION : Birkdale (BKD) email route - the active cycle ---- */
    ('ING_BKD_CYCLE', 'Birkdale Ingestion Cycle', 'INGESTION', 'BKD', NULL, 'ORCHESTRATOR', NULL, NULL,
     'Run the full Birkdale ingestion cycle in order: acquire email attachments -> parse ENS headers -> load raw tables. Opens one EXC.Execution per step; returns non-zero if any step fails so the scheduler can alert.',
     'run_ingestion:main', 'CFG.Job (active INGESTION steps for the client)', 'ING raw tables + EXC/LOG',
     'Every 30 min, 06:00-20:00 UK business days', 1,
     'Scheduler entry point. No CLI - reads INGESTION_CLIENT / INGESTION_DRY_RUN from CFG.Application_Parameters.'),

    ('ING_BKD_ACQUIRE_EMAIL', 'Acquire - Birkdale email attachments', 'INGESTION', 'BKD', 'EMAIL', 'STEP', 1, 'ING_BKD_CYCLE',
     'Download relevant attachments (xlsx/xls/csv/pdf/doc/txt) from the Birkdale mailbox where the sender domain is birkdalesales.com, via Microsoft Graph. Save each with an emaildate_emailtime_filename prefix, move the processed message to Inbox/Fusion_Processed/BKD, and land file provenance to ING.',
     'birkdale_sales_orders:run', 'MS Graph mailbox (Inbox + sub-folders)', 'BKD Inbound folder + ING.Inbound_File / ING.Source_Email',
     'Step 1 of ING_BKD_CYCLE', 1,
     'Sales Order workbooks + forwarded TSS Details mails both arrive here.'),

    ('ING_BKD_PARSE_ENS', 'Parse - ENS headers to CSV', 'INGESTION', 'BKD', 'EMAIL', 'STEP', 2, 'ING_BKD_CYCLE',
     'Parse the forwarded TSS "Details for <date>" email bodies into a timestamped ENS CSV (ENS_Headers_<stamp>.csv) in the client ENS_Source folder. Each row is keyed on DetailsDate|ICR so the daily re-forward never duplicates.',
     'ens_headers:run_from_graph', 'MS Graph (Inbox + Fusion_Processed/BKD, sender-domain filtered)', 'BKD ENS_Source / ENS_Headers_<stamp>.csv',
     'Step 2 of ING_BKD_CYCLE', 1,
     'Body is fetched only for sender-domain matches to keep the scan fast.'),

    ('ING_BKD_LOAD_RAW', 'Load - raw ENS + Sales Orders', 'INGESTION', 'BKD', NULL, 'STEP', 3, 'ING_BKD_CYCLE',
     'Load the ENS CSV -> ING.BKD_Raw_ENS (skipping any existing DedupKey) and the Sales Order workbooks -> ING.BKD_Raw_Sales_Orders (verbatim row JSON; FileDate from the name prefix). Move processed files to the Processed sub-folder.',
     'load_raw:run', 'BKD ENS_Source CSV + Inbound Sales Order workbooks', 'ING.BKD_Raw_ENS / ING.BKD_Raw_Sales_Orders',
     'Step 3 of ING_BKD_CYCLE', 1,
     'Idempotent: ENS rows dedupe on DedupKey; Sales Orders delete-then-insert per source file.'),

    /* ---- INGESTION : other channels - registered, INACTIVE (modular, future) ---- */
    ('ING_ACQUIRE_FILE_DROP', 'Acquire - watched INBOUND folder', 'INGESTION', NULL, 'FILE_DROP', 'ACQUIRE', NULL, NULL,
     'Acquire via a watched client INBOUND folder: list new files and land them verbatim to ING. Framework route - implement discover/fetch/parse_rows on FileDropChannel to activate.',
     'ingest:FileDropChannel', 'Client INBOUND folder', 'ING.Inbound_File / ING.Raw_Record',
     'On demand / folder watch', 0, 'Channel stub - see CFG.Ingestion_Source.'),

    ('ING_ACQUIRE_SFTP', 'Acquire - SFTP drop', 'INGESTION', NULL, 'SFTP', 'ACQUIRE', NULL, NULL,
     'Acquire via SFTP: list and get files from the client SFTP drop, then land verbatim to ING. Framework route - implement SftpChannel to activate.',
     'ingest:SftpChannel', 'Client SFTP endpoint', 'ING.Inbound_File / ING.Raw_Record',
     'Scheduled poll', 0, 'Channel stub - see CFG.Ingestion_Source.'),

    ('ING_ACQUIRE_AS2', 'Acquire - AS2 exchange', 'INGESTION', NULL, 'AS2', 'ACQUIRE', NULL, NULL,
     'Acquire via AS2 message exchange: receive payloads and land them verbatim to ING. Planned route.',
     NULL, 'AS2 partner endpoint', 'ING.Inbound_File / ING.Raw_Record',
     'Event-driven', 0, 'Planned - no implementation yet.'),

    ('ING_ACQUIRE_API', 'Acquire - REST pull', 'INGESTION', NULL, 'API', 'ACQUIRE', NULL, NULL,
     'Acquire via REST pull from the client API: page results and land verbatim to ING. Framework route - implement RestChannel to activate.',
     'ingest:RestChannel', 'Client REST API', 'ING.Inbound_File / ING.Raw_Record',
     'Scheduled poll', 0, 'Channel stub - see CFG.Ingestion_Source.'),

    /* ---- DATA PROCESSING : the existing scheduled job (registry is module-wide) ---- */
    ('PRS_PROCESS_BKD', 'Data Processing - Birkdale (PRS)', 'DATA_PROCESSING', 'BKD', NULL, 'TASK', NULL, NULL,
     'Transform INGESTED ING rows into validated canonical PRS objects (normalise -> enrich -> construct -> validate); log every field change to EXC.Data_Processing_Enhancement. No CLI - controls from CFG.Application_Parameters (PROCESSING_*).',
     'process_data:run', 'ING.BKD_Raw_ENS / ING.BKD_Raw_Sales_Orders', 'PRS.ENS_Header / Consignment / Goods_Item (+ nested)',
     'After each ingestion cycle', 1, 'Module 2 runner - companion to the ingestion jobs.'),

    ('PRS_STAGE_BKD_ENS', 'Stage - Birkdale ENS Headers', 'DATA_PROCESSING', 'BKD', NULL, 'TASK', NULL, NULL,
     'Stage raw ING.BKD_Raw_ENS rows into the TSS-shaped PRS.BKD_ENS_Header_Submission (+ parallel PRS.BKD_ENS_Header_Tracking). Each field is normalised / reformatted / replaced by lookup; every change is logged old->new to EXC.Data_Processing_Enhancement. Sets Fusion_Status=STAGED. No CLI - PROCESSING_DRY_RUN from CFG.Application_Parameters.',
     'stage_bkd_ens_header:run', 'ING.BKD_Raw_ENS', 'PRS.BKD_ENS_Header_Submission / PRS.BKD_ENS_Header_Tracking',
     'After each ingestion cycle', 1, 'First per-table staging job; ENS Declaration Header.')
) AS s (JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
        Purpose, EntryPoint, InputSource, OutputTarget, Schedule, IsActive, Notes)
ON t.JobCode = s.JobCode
WHEN MATCHED THEN UPDATE SET
    JobName=s.JobName, ModuleName=s.ModuleName, ClientCode=s.ClientCode, Channel=s.Channel,
    JobType=s.JobType, StepNo=s.StepNo, ParentJobCode=s.ParentJobCode, Purpose=s.Purpose,
    EntryPoint=s.EntryPoint, InputSource=s.InputSource, OutputTarget=s.OutputTarget,
    Schedule=s.Schedule, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()   -- IsActive deliberately preserved
WHEN NOT MATCHED THEN INSERT
    (JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
     Purpose, EntryPoint, InputSource, OutputTarget, Schedule, IsActive, Notes)
    VALUES (s.JobCode, s.JobName, s.ModuleName, s.ClientCode, s.Channel, s.JobType, s.StepNo, s.ParentJobCode,
            s.Purpose, s.EntryPoint, s.InputSource, s.OutputTarget, s.Schedule, s.IsActive, s.Notes);
GO

/* ------------------------------------------------------------------ */
/* Ingestion runner controls (insert-only; operator-owned once set).   */
/* ------------------------------------------------------------------ */
MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('INGESTION_CLIENT',  'BKD', 'STRING', 'Ingestion runner: client code whose active CFG.Job steps to run.'),
    ('INGESTION_DRY_RUN', '0',   'BOOL',   'Ingestion runner: 1/true = run + report only, write nothing; 0 = land to ING.')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO
