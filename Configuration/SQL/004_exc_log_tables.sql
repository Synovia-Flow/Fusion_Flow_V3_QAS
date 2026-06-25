/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 4 OF N
    =================================================
    Purpose : Create the cross-cutting EXC (execution spine) and LOG (trace)
              tables. Every module opens one EXC.Execution per run carrying a
              Transaction_ID that threads through ING/PRS/STG/API/BKD/CTL.

    Source  : Fusion Flow R1 Functional Specification - 3.1, 3.3, 7.1.

    Run after : 001_create_schemas.sql, 002_cfg_tables.sql
    Safe to rerun: Yes.
*/

/* ------------------------------------------------------------------ */
/* EXC.Execution - master execution/transaction spine                  */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('EXC.Execution', 'U') IS NULL
BEGIN
    CREATE TABLE EXC.Execution (
        ExecutionID     bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_EXC_Execution PRIMARY KEY,
        TransactionID   uniqueidentifier NOT NULL CONSTRAINT DF_EXC_Execution_Txn DEFAULT (NEWID()),
        EnvCode         varchar(10) NOT NULL CONSTRAINT DF_EXC_Execution_Env DEFAULT ('TEST'),
        ClientCode      char(3) NULL,
        ModuleName      varchar(40) NOT NULL,         -- INGESTION | DATA_PROCESSING | SUBMISSION | MONITOR | REPORTING
        ProcessName     varchar(30) NOT NULL,         -- shared vocabulary, e.g. INGESTING
        RunMode         varchar(30) NULL,             -- e.g. daily | historic | manual | dry-run
        StartedAt       datetime2(3) NOT NULL CONSTRAINT DF_EXC_Execution_Started DEFAULT (SYSUTCDATETIME()),
        EndedAt         datetime2(3) NULL,
        Status          varchar(30) NOT NULL,         -- shared vocabulary result, e.g. INGESTED
        ItemsFound      int NULL,
        ItemsProcessed  int NULL,
        ItemsFailed     int NULL,
        ErrorMessage    nvarchar(2000) NULL,
        CreatedAt       datetime2(3) NOT NULL CONSTRAINT DF_EXC_Execution_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* EXC.Transaction - per-entity transitions within an execution        */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('EXC.[Transaction]', 'U') IS NULL
BEGIN
    CREATE TABLE EXC.[Transaction] (
        TransactionRowID bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_EXC_Transaction PRIMARY KEY,
        ExecutionID      bigint NOT NULL,
        TransactionID    uniqueidentifier NOT NULL,
        ClientCode       char(3) NULL,
        EntityType       varchar(40) NULL,            -- e.g. INBOUND_FILE, ENS_HEADER
        EntityRef        nvarchar(100) NULL,
        ProcessName      varchar(30) NULL,
        Status           varchar(30) NULL,
        CreatedAt        datetime2(3) NOT NULL CONSTRAINT DF_EXC_Transaction_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* EXC.Error - execution-level errors                                  */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('EXC.Error', 'U') IS NULL
BEGIN
    CREATE TABLE EXC.Error (
        ErrorID       bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_EXC_Error PRIMARY KEY,
        ExecutionID   bigint NULL,
        TransactionID uniqueidentifier NULL,
        ClientCode    char(3) NULL,
        Severity      varchar(20) NOT NULL CONSTRAINT DF_EXC_Error_Sev DEFAULT ('ERROR'),
        ErrorCode     varchar(50) NULL,
        Message       nvarchar(2000) NOT NULL,
        Context       nvarchar(max) NULL,
        CreatedAt     datetime2(3) NOT NULL CONSTRAINT DF_EXC_Error_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* EXC.Data_Processing_Enhancement - field-level change audit          */
/* (written by Module 2; created here as part of the spine)            */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('EXC.Data_Processing_Enhancement', 'U') IS NULL
BEGIN
    CREATE TABLE EXC.Data_Processing_Enhancement (
        EnhancementID bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_EXC_DPE PRIMARY KEY,
        ExecutionID   bigint NULL,
        TransactionID uniqueidentifier NULL,
        ClientCode    char(3) NULL,
        SchemaName    sysname NULL,
        TableName     sysname NULL,
        ColumnName    sysname NULL,
        EntityRef     nvarchar(100) NULL,
        OldValue      nvarchar(max) NULL,
        NewValue      nvarchar(max) NULL,
        RuleApplied   nvarchar(200) NULL,
        CreatedAt     datetime2(3) NOT NULL CONSTRAINT DF_EXC_DPE_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* LOG.Process_Log - detailed technical/process log                    */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('LOG.Process_Log', 'U') IS NULL
BEGIN
    CREATE TABLE LOG.Process_Log (
        LogID         bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_LOG_Process_Log PRIMARY KEY,
        ExecutionID   bigint NULL,
        TransactionID uniqueidentifier NULL,
        ClientCode    char(3) NULL,
        ModuleName    varchar(40) NULL,
        StepName      varchar(100) NULL,
        LogLevel      varchar(20) NOT NULL CONSTRAINT DF_LOG_Process_Log_Level DEFAULT ('INFO'),
        Message       nvarchar(2000) NOT NULL,
        DetailJson    nvarchar(max) NULL,
        CreatedAt     datetime2(3) NOT NULL CONSTRAINT DF_LOG_Process_Log_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* LOG.Error_Log - dedicated error log (deeper than EXC.Error)         */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('LOG.Error_Log', 'U') IS NULL
BEGIN
    CREATE TABLE LOG.Error_Log (
        ErrorLogID    bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_LOG_Error_Log PRIMARY KEY,
        ExecutionID   bigint NULL,
        TransactionID uniqueidentifier NULL,
        ClientCode    char(3) NULL,
        ModuleName    varchar(40) NULL,
        StepName      varchar(100) NULL,
        ErrorType     varchar(100) NULL,
        Message       nvarchar(2000) NOT NULL,
        StackTrace    nvarchar(max) NULL,
        CreatedAt     datetime2(3) NOT NULL CONSTRAINT DF_LOG_Error_Log_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* LOG.API_Trace - full API request/response trace (Module 3)          */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('LOG.API_Trace', 'U') IS NULL
BEGIN
    CREATE TABLE LOG.API_Trace (
        TraceID       bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_LOG_API_Trace PRIMARY KEY,
        ExecutionID   bigint NULL,
        TransactionID uniqueidentifier NULL,
        ClientCode    char(3) NULL,
        ResourceName  varchar(50) NULL,
        Endpoint      varchar(100) NULL,
        HttpMethod    varchar(10) NULL,
        RequestJson   nvarchar(max) NULL,
        ResponseJson  nvarchar(max) NULL,
        StatusCode    int NULL,
        DurationMs    int NULL,
        CreatedAt     datetime2(3) NOT NULL CONSTRAINT DF_LOG_API_Trace_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* Foreign keys + indexes (no cascade deletes - audit must persist)    */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('EXC.FK_Transaction_Execution', 'F') IS NULL
    ALTER TABLE EXC.[Transaction] WITH CHECK ADD CONSTRAINT FK_Transaction_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_EXC_Execution_Txn' AND object_id=OBJECT_ID('EXC.Execution'))
    CREATE INDEX IX_EXC_Execution_Txn ON EXC.Execution (TransactionID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_EXC_Execution_Client_Status' AND object_id=OBJECT_ID('EXC.Execution'))
    CREATE INDEX IX_EXC_Execution_Client_Status ON EXC.Execution (ClientCode, Status);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_LOG_Process_Log_Exec' AND object_id=OBJECT_ID('LOG.Process_Log'))
    CREATE INDEX IX_LOG_Process_Log_Exec ON LOG.Process_Log (ExecutionID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_LOG_Error_Log_Exec' AND object_id=OBJECT_ID('LOG.Error_Log'))
    CREATE INDEX IX_LOG_Error_Log_Exec ON LOG.Error_Log (ExecutionID);
GO
