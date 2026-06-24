/*
    FLOW V3 QAS DATABASE SETUP - FILE 1 OF 3

    Purpose:
        Create the empty database structure: schemas, tables and additive columns.

    Run this after:
        The SQL Server database `Fusion_Flow_V3_QAS` exists and is selected.

    Run this before:
        002_add_constraints_and_indexes.sql
        003_seed_qas_config.sql

    Safe to rerun:
        Yes. Every CREATE/ALTER block checks whether the object or column exists.

    What this file does NOT do:
        - It does not create the database itself.
        - It does not insert BKD/CWH/PLE config data.
        - It does not store secrets or credentials.
*/

/* SECTION 1 - Ingestion ownership schemas: CFG, EXC and ING. */
IF SCHEMA_ID('CFG') IS NULL EXEC('CREATE SCHEMA CFG');
IF SCHEMA_ID('EXC') IS NULL EXEC('CREATE SCHEMA EXC');
IF SCHEMA_ID('ING') IS NULL EXEC('CREATE SCHEMA ING');
GO

/*
    SECTION 2 - Base MVP pipeline tables.

    CFG.Graph      = legacy/simple Graph route config used by the current worker.
    EXC.Graph      = one execution/run of the Graph intake worker.
    ING.Graph      = inbound email/message/file trace.
    Later validation/submission phases may add separate schemas; this setup is ingestion-only.
*/
IF OBJECT_ID('CFG.Graph', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Graph (
        ConfigID                    bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Graph PRIMARY KEY,
        EnvCode                     varchar(20) NOT NULL,
        TenantCode                  varchar(10) NOT NULL,
        TenantName                  nvarchar(100) NOT NULL,
        Mailbox                     nvarchar(320) NOT NULL,
        SenderRule                  nvarchar(500) NOT NULL,
        AllowedFileTypes            nvarchar(200) NULL,
        DestinationFolder           nvarchar(1000) NOT NULL,
        BodySourceForEns            varchar(50) NULL,
        ProcessingEnvironment       varchar(30) NOT NULL CONSTRAINT DF_CFG_Graph_ProcessingEnvironment DEFAULT ('TEST_API_ONLY'),
        IsActive                    bit NOT NULL CONSTRAINT DF_CFG_Graph_IsActive DEFAULT (1),
        Notes                       nvarchar(500) NULL,
        UpdatedAt                   datetime2(3) NOT NULL CONSTRAINT DF_CFG_Graph_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Graph_Tenant UNIQUE (EnvCode, TenantCode)
    );
END;
GO

IF OBJECT_ID('EXC.Graph', 'U') IS NULL
BEGIN
    CREATE TABLE EXC.Graph (
        ExecutionID     bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_EXC_Graph PRIMARY KEY,
        EnvCode         varchar(20) NOT NULL,
        ProcessName     varchar(100) NOT NULL,
        RunMode         varchar(30) NULL,
        StartedAt       datetime2(3) NOT NULL CONSTRAINT DF_EXC_Graph_StartedAt DEFAULT (SYSUTCDATETIME()),
        EndedAt         datetime2(3) NULL,
        Status          varchar(30) NOT NULL,
        ItemsFound      int NULL,
        ItemsProcessed  int NULL,
        ErrorMessage    nvarchar(2000) NULL
    );
END;
GO

IF OBJECT_ID('ING.Graph', 'U') IS NULL
BEGIN
    CREATE TABLE ING.Graph (
        GraphID             bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_ING_Graph PRIMARY KEY,
        ExecutionID         bigint NULL,
        ConfigID            bigint NULL,
        EnvCode             varchar(20) NOT NULL,
        TenantCode          varchar(10) NULL,
        Mailbox             nvarchar(320) NOT NULL,
        GraphMessageID      nvarchar(450) NULL,
        InternetMessageID   nvarchar(1000) NULL,
        SenderEmail         nvarchar(320) NULL,
        SenderDomain        nvarchar(320) NULL,
        Subject             nvarchar(998) NULL,
        ReceivedAt          datetime2(3) NULL,
        HasAttachments      bit NOT NULL CONSTRAINT DF_ING_Graph_HasAttachments DEFAULT (0),
        OriginalFileName    nvarchar(500) NULL,
        SavedFileName       nvarchar(500) NULL,
        SavedPath           nvarchar(1000) NULL,
        ContentType         nvarchar(255) NULL,
        SizeBytes           bigint NULL,
        FileHash            char(64) NULL,
        Status              varchar(30) NOT NULL,
        CreatedAt           datetime2(3) NOT NULL CONSTRAINT DF_ING_Graph_CreatedAt DEFAULT (SYSUTCDATETIME())
    );
END;
GO



/*
    SECTION 3 - Tenant-driven ingestion tables.

    CFG.Tenant            = tenant identity and default folders.
    CFG.TenantSetting     = ingestion runtime settings; submit gates belong to a later phase.
    CFG.IngestionRoute    = mailbox/sender/folder route per tenant.
    CFG.IngestionPackRule = ENS_PACK / DEC_PACK output rules.
    EXC.ExecutionLog      = detailed technical process log rows.
    ING.ProcessFile       = saved/generated file trace.
    ING.LoadRow           = raw rows loaded from generated process files.
*/

IF OBJECT_ID('CFG.Tenant', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Tenant (
        TenantID              bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Tenant PRIMARY KEY,
        EnvCode               varchar(20) NOT NULL,
        TenantCode            varchar(10) NOT NULL,
        TenantName            nvarchar(100) NOT NULL,
        IntegrationRoot       nvarchar(1000) NOT NULL,
        DefaultInboundFolder  nvarchar(1000) NOT NULL,
        ProcessFolder         nvarchar(1000) NOT NULL,
        FailFolder            nvarchar(1000) NOT NULL,
        IsActive              bit NOT NULL CONSTRAINT DF_CFG_Tenant_IsActive DEFAULT (1),
        Notes                 nvarchar(1000) NULL,
        UpdatedAt             datetime2(3) NOT NULL CONSTRAINT DF_CFG_Tenant_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Tenant UNIQUE (EnvCode, TenantCode)
    );
END;
GO

IF OBJECT_ID('CFG.TenantSetting', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.TenantSetting (
        TenantSettingID  bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_TenantSetting PRIMARY KEY,
        TenantID         bigint NOT NULL,
        EnvCode          varchar(20) NOT NULL,
        TenantCode       varchar(10) NOT NULL,
        SettingKey       varchar(100) NOT NULL,
        SettingValue     nvarchar(1000) NOT NULL,
        ValueType        varchar(30) NOT NULL CONSTRAINT DF_CFG_TenantSetting_ValueType DEFAULT ('STRING'),
        IsActive         bit NOT NULL CONSTRAINT DF_CFG_TenantSetting_IsActive DEFAULT (1),
        Notes            nvarchar(1000) NULL,
        UpdatedAt        datetime2(3) NOT NULL CONSTRAINT DF_CFG_TenantSetting_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_TenantSetting UNIQUE (TenantID, SettingKey)
    );
END;
GO

IF OBJECT_ID('CFG.IngestionRoute', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.IngestionRoute (
        RouteID             bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_IngestionRoute PRIMARY KEY,
        TenantID            bigint NOT NULL,
        EnvCode             varchar(20) NOT NULL,
        TenantCode          varchar(10) NOT NULL,
        RouteName           varchar(100) NOT NULL,
        SourceType          varchar(50) NOT NULL,
        Mailbox             nvarchar(320) NULL,
        SenderRuleType      varchar(30) NOT NULL,
        SenderRule          nvarchar(500) NOT NULL,
        DestinationFolder   nvarchar(1000) NOT NULL,
        ProcessFolder       nvarchar(1000) NOT NULL,
        FailFolder          nvarchar(1000) NOT NULL,
        AllowedFileTypes    nvarchar(200) NULL,
        RouteStatus         varchar(40) NOT NULL CONSTRAINT DF_CFG_IngestionRoute_RouteStatus DEFAULT ('PENDING'),
        IsActive            bit NOT NULL CONSTRAINT DF_CFG_IngestionRoute_IsActive DEFAULT (0),
        Notes               nvarchar(1000) NULL,
        UpdatedAt           datetime2(3) NOT NULL CONSTRAINT DF_CFG_IngestionRoute_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_IngestionRoute UNIQUE (EnvCode, TenantCode, RouteName)
    );
END;
GO

IF OBJECT_ID('CFG.IngestionPackRule', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.IngestionPackRule (
        PackRuleID       bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_IngestionPackRule PRIMARY KEY,
        RouteID          bigint NOT NULL,
        PackCode         varchar(30) NOT NULL,
        SourcePart       varchar(30) NOT NULL,
        OutputFormat     varchar(20) NOT NULL,
        OutputFilePattern nvarchar(255) NOT NULL,
        SheetName        nvarchar(128) NULL,
        OutputFolder     nvarchar(1000) NULL,
        LoadTargetSchema sysname NOT NULL CONSTRAINT DF_CFG_IngestionPackRule_LoadTargetSchema DEFAULT ('ING'),
        LoadTargetTable  sysname NOT NULL CONSTRAINT DF_CFG_IngestionPackRule_LoadTargetTable DEFAULT ('ProcessFile'),
        IsActive         bit NOT NULL CONSTRAINT DF_CFG_IngestionPackRule_IsActive DEFAULT (1),
        Notes            nvarchar(1000) NULL,
        UpdatedAt        datetime2(3) NOT NULL CONSTRAINT DF_CFG_IngestionPackRule_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_IngestionPackRule UNIQUE (RouteID, PackCode, SourcePart)
    );
END;
GO

/* Compatibility for databases created before CsvFolder was renamed to OutputFolder. */
IF OBJECT_ID('CFG.IngestionPackRule', 'U') IS NOT NULL
   AND COL_LENGTH('CFG.IngestionPackRule', 'OutputFolder') IS NULL
   AND COL_LENGTH('CFG.IngestionPackRule', 'CsvFolder') IS NOT NULL
BEGIN
    EXEC sp_rename 'CFG.IngestionPackRule.CsvFolder', 'OutputFolder', 'COLUMN';
END;
GO
IF OBJECT_ID('EXC.ExecutionLog', 'U') IS NULL
BEGIN
    CREATE TABLE EXC.ExecutionLog (
        LogID        bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_EXC_ExecutionLog PRIMARY KEY,
        ExecutionID  bigint NULL,
        EnvCode      varchar(20) NOT NULL,
        TenantCode   varchar(10) NULL,
        ProcessName  varchar(100) NOT NULL,
        StepName     varchar(100) NULL,
        LogLevel     varchar(20) NOT NULL,
        Message      nvarchar(2000) NOT NULL,
        DetailJson   nvarchar(max) NULL,
        CreatedAt    datetime2(3) NOT NULL CONSTRAINT DF_EXC_ExecutionLog_CreatedAt DEFAULT (SYSUTCDATETIME())
    );
END;
GO

IF OBJECT_ID('ING.ProcessFile', 'U') IS NULL
BEGIN
    CREATE TABLE ING.ProcessFile (
        ProcessFileID    bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_ING_ProcessFile PRIMARY KEY,
        GraphID          bigint NULL,
        ExecutionID      bigint NULL,
        TenantID         bigint NULL,
        RouteID          bigint NULL,
        EnvCode          varchar(20) NOT NULL,
        TenantCode       varchar(10) NOT NULL,
        PackCode         varchar(30) NULL,
        SourcePart       varchar(30) NULL,
        SourceFolder     nvarchar(1000) NULL,
        ProcessFolder    nvarchar(1000) NULL,
        FailFolder       nvarchar(1000) NULL,
        OriginalFileName nvarchar(500) NULL,
        SavedFileName    nvarchar(500) NULL,
        SavedPath        nvarchar(1000) NULL,
        GeneratedCsvPath nvarchar(1000) NULL,
        SheetName        nvarchar(128) NULL,
        FileHash         char(64) NULL,
        Status           varchar(40) NOT NULL,
        ErrorMessage     nvarchar(2000) NULL,
        CreatedAt        datetime2(3) NOT NULL CONSTRAINT DF_ING_ProcessFile_CreatedAt DEFAULT (SYSUTCDATETIME())
    );
END;
GO

IF OBJECT_ID('ING.LoadRow', 'U') IS NULL
BEGIN
    CREATE TABLE ING.LoadRow (
        LoadRowID      bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_ING_LoadRow PRIMARY KEY,
        ProcessFileID  bigint NOT NULL,
        RowNumber      int NOT NULL,
        PayloadJson    nvarchar(max) NOT NULL,
        Status         varchar(40) NOT NULL,
        ErrorMessage   nvarchar(2000) NULL,
        CreatedAt      datetime2(3) NOT NULL CONSTRAINT DF_ING_LoadRow_CreatedAt DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/*
    SECTION 4 - Compatibility columns on existing Graph tables.

    These columns connect the older CFG.Graph/ING.Graph trace to the newer
    tenant/route/pack model without dropping existing MVP tables.
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
