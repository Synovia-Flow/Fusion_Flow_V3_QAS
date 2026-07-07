/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 33 OF N
    =================================================
    Purpose : EXC.Job_Queue - the request queue for the background worker (Pattern B).

              The portal (POST /api/enqueue/<verb>) or any process INSERTs a PENDING
              row here; Modules/Global/job_worker.py claims it (atomic UPDATE with
              READPAST so multiple workers are safe), runs the SAME runner the portal
              runs in-process - promote/submit/mirror/update/cancel/reprocess - scoped
              to the one MovementKey via per-run overrides (never mutating shared CFG),
              then records the exit code + outcome. Everything the runner itself does is
              still tracked in EXC.Execution / API.Call / LOG as usual.

    Run after : 032 (update/cancel jobs activated). Safe to rerun.
*/

IF SCHEMA_ID('EXC') IS NULL EXEC('CREATE SCHEMA EXC');
GO

IF OBJECT_ID('EXC.Job_Queue', 'U') IS NULL
BEGIN
    CREATE TABLE EXC.Job_Queue (
        QueueID        bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_EXC_Job_Queue PRIMARY KEY,

        /* --- what to run --- */
        Verb           varchar(20)   NOT NULL,   -- promote|submit|mirror|update|cancel|reprocess
        MovementKey    varchar(100)  NOT NULL,   -- scopes the run to one movement
        ClientCode     char(3)           NULL,

        /* --- lifecycle --- */
        Status         varchar(20)   NOT NULL CONSTRAINT DF_EXC_Job_Queue_Status    DEFAULT 'PENDING',  -- PENDING|RUNNING|DONE|FAILED
        Attempts       int           NOT NULL CONSTRAINT DF_EXC_Job_Queue_Attempts  DEFAULT 0,
        RequestedBy    varchar(100)      NULL,
        RequestedAt    datetime2(0)  NOT NULL CONSTRAINT DF_EXC_Job_Queue_Requested DEFAULT SYSUTCDATETIME(),
        StartedAt      datetime2(0)      NULL,
        FinishedAt     datetime2(0)      NULL,

        /* --- outcome --- */
        ExecutionID    bigint            NULL,   -- the EXC.Execution the runner opened (if captured)
        ExitCode       int               NULL,   -- runner return code (0 = ok)
        ResultMessage  nvarchar(2000)    NULL
    );

    -- The worker's claim query filters on Status and takes the oldest first.
    CREATE INDEX IX_EXC_Job_Queue_Status ON EXC.Job_Queue (Status, QueueID);
END;
GO
