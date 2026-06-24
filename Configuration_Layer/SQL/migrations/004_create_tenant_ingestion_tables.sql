/*
    Fusion_Flow_V3_QAS SQL migration.

    Purpose:
        Create tenant, tenant setting, route, pack rule, execution log and process-file tables.

    Run order:
        Execute files in numeric filename order. Scripts are idempotent
        where practical so QAS can be refreshed safely.
*/
/*
    Phase 2 MVP extension - tenant configuration, execution logs and
    folder-backed ingestion process records.

    Ownership rule:
        CFG = tenant/config/routing/pack rules and runtime gates.
        EXC = executions and logs only.
        ING = file/folder/process/fail intake records loaded from generated CSV/files.
        STG = validation/business staging.
        TSS = official API submission/mirror references.
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
        CsvFolder        nvarchar(1000) NULL,
        LoadTargetSchema sysname NOT NULL CONSTRAINT DF_CFG_IngestionPackRule_LoadTargetSchema DEFAULT ('ING'),
        LoadTargetTable  sysname NOT NULL CONSTRAINT DF_CFG_IngestionPackRule_LoadTargetTable DEFAULT ('ProcessFile'),
        IsActive         bit NOT NULL CONSTRAINT DF_CFG_IngestionPackRule_IsActive DEFAULT (1),
        Notes            nvarchar(1000) NULL,
        UpdatedAt        datetime2(3) NOT NULL CONSTRAINT DF_CFG_IngestionPackRule_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_IngestionPackRule UNIQUE (RouteID, PackCode, SourcePart)
    );
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
