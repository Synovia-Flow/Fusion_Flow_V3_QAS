/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 28 OF N
    =================================================
    Purpose : API schema - the authoritative log of EVERY TSS API call.

              API.Call captures one row per call: the EXC execution/transaction it
              belongs to, the process (shared vocabulary), the route/step/resource,
              the FULL request (url + body + redacted headers) and the FULL response
              (body + status + duration), whether it was a dry-run, and the TSS
              reference returned. Nothing is silent - every create/get/update/cancel
              is recorded here and tied back to the EXC spine.

              (LOG.API_Trace from file 004 remains as a lightweight legacy trace;
              API.Call is the rich, authoritative record going forward.)

    Run after : 004 (EXC/LOG). Safe to rerun.
*/

IF SCHEMA_ID('API') IS NULL EXEC('CREATE SCHEMA API');
GO

IF OBJECT_ID('API.Call', 'U') IS NULL
BEGIN
    CREATE TABLE API.Call (
        CallID          bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_API_Call PRIMARY KEY,

        /* --- EXC spine + business linkage --- */
        ExecutionID     bigint        NULL,
        TransactionID   uniqueidentifier NULL,
        ClientCode      char(3)       NULL,
        EntityType      varchar(40)   NULL,            -- e.g. ENS_HEADER
        MovementKey     nvarchar(100) NULL,
        Declaration_Number nvarchar(40) NULL,

        /* --- what was called --- */
        ModuleName      varchar(40)   NULL,            -- SUBMISSION | MONITOR | ...
        ProcessName     varchar(30)   NULL,            -- shared vocabulary (SUBMITTING, RECONCILING, ...)
        RouteCode       char(1)       NULL,
        StepNo          int           NULL,
        ResourceName    varchar(50)   NULL,            -- Declaration Header, ...
        OpType          varchar(20)   NULL,            -- create|read|update|cancel|submit
        EnvCode         varchar(10)   NULL,            -- PRD | TST
        HttpMethod      varchar(10)   NULL,
        RequestUrl      nvarchar(1000) NULL,

        /* --- full request / response --- */
        RequestHeaders  nvarchar(max) NULL,            -- Authorization redacted
        RequestJson     nvarchar(max) NULL,
        ResponseHeaders nvarchar(max) NULL,
        ResponseJson    nvarchar(max) NULL,

        /* --- outcome --- */
        StatusCode      int           NULL,
        Success         bit           NOT NULL CONSTRAINT DF_API_Call_Success DEFAULT (0),
        DurationMs      int           NULL,
        IsDryRun        bit           NOT NULL CONSTRAINT DF_API_Call_DryRun DEFAULT (0),
        ErrorMessage    nvarchar(2000) NULL,

        CreatedAt       datetime2(3)  NOT NULL CONSTRAINT DF_API_Call_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

IF OBJECT_ID('API.FK_Call_Execution', 'F') IS NULL AND OBJECT_ID('EXC.Execution','U') IS NOT NULL
    ALTER TABLE API.Call WITH NOCHECK ADD CONSTRAINT API_FK_Call_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_API_Call_Execution'
              AND object_id=OBJECT_ID('API.Call'))
    CREATE INDEX IX_API_Call_Execution ON API.Call (ExecutionID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_API_Call_Movement'
              AND object_id=OBJECT_ID('API.Call'))
    CREATE INDEX IX_API_Call_Movement ON API.Call (ClientCode, MovementKey);
GO

/* Convenience views. */
CREATE OR ALTER VIEW API.vw_Call_Log AS
    SELECT c.CallID, c.CreatedAt, c.ClientCode, c.MovementKey, c.Declaration_Number,
           c.ModuleName, c.ProcessName, c.ResourceName, c.OpType, c.EnvCode,
           c.HttpMethod, c.RequestUrl, c.StatusCode, c.Success, c.IsDryRun,
           c.DurationMs, c.ErrorMessage, c.ExecutionID
    FROM API.Call c;
GO

CREATE OR ALTER VIEW API.vw_Call_Errors AS
    SELECT * FROM API.vw_Call_Log WHERE Success = 0 AND IsDryRun = 0;
GO
