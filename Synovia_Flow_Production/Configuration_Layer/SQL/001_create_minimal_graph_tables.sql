/*
    Fusion_Flow_V3_QAS - minimal Graph ingestion database model.

    Goal:
        Keep the first database layer small and easy to understand.
        No detailed log tables are created here.

    Flow:
        CFG.Graph -> EXC.Graph -> ING.Graph -> STG.SalesOrder -> TSS.Submission
*/

IF SCHEMA_ID('CFG') IS NULL EXEC('CREATE SCHEMA CFG');
IF SCHEMA_ID('EXC') IS NULL EXEC('CREATE SCHEMA EXC');
IF SCHEMA_ID('ING') IS NULL EXEC('CREATE SCHEMA ING');
IF SCHEMA_ID('STG') IS NULL EXEC('CREATE SCHEMA STG');
IF SCHEMA_ID('TSS') IS NULL EXEC('CREATE SCHEMA TSS');
GO

IF OBJECT_ID('CFG.Graph', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Graph (
        ConfigID        bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Graph PRIMARY KEY,
        EnvCode         varchar(20) NOT NULL,
        ClientCode      varchar(10) NOT NULL,
        ConfigGroup     varchar(50) NOT NULL,
        ConfigKey       varchar(100) NOT NULL,
        ConfigValue     nvarchar(2000) NULL,
        IsSecret        bit NOT NULL CONSTRAINT DF_CFG_Graph_IsSecret DEFAULT (0),
        IsActive        bit NOT NULL CONSTRAINT DF_CFG_Graph_IsActive DEFAULT (1),
        Notes           nvarchar(500) NULL,
        UpdatedAt       datetime2(3) NOT NULL CONSTRAINT DF_CFG_Graph_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Graph UNIQUE (EnvCode, ClientCode, ConfigGroup, ConfigKey)
    );
END;
GO

IF OBJECT_ID('EXC.Graph', 'U') IS NULL
BEGIN
    CREATE TABLE EXC.Graph (
        ExecutionID     bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_EXC_Graph PRIMARY KEY,
        EnvCode         varchar(20) NOT NULL,
        ClientCode      varchar(10) NOT NULL,
        ProcessName     varchar(100) NOT NULL,
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
        EnvCode             varchar(20) NOT NULL,
        ClientCode          varchar(10) NOT NULL,
        Mailbox             nvarchar(320) NOT NULL,
        GraphMessageID      nvarchar(450) NULL,
        InternetMessageID   nvarchar(1000) NULL,
        SenderEmail         nvarchar(320) NULL,
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

IF OBJECT_ID('STG.SalesOrder', 'U') IS NULL
BEGIN
    CREATE TABLE STG.SalesOrder (
        SalesOrderID    bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_STG_SalesOrder PRIMARY KEY,
        GraphID         bigint NULL,
        EnvCode         varchar(20) NOT NULL,
        ClientCode      varchar(10) NOT NULL,
        SourceRowNum    int NULL,
        OrderReference  nvarchar(100) NULL,
        OrderDate       date NULL,
        CustomerCode    nvarchar(100) NULL,
        PayloadJson     nvarchar(max) NULL,
        Status          varchar(30) NOT NULL,
        UpdatedAt       datetime2(3) NOT NULL CONSTRAINT DF_STG_SalesOrder_UpdatedAt DEFAULT (SYSUTCDATETIME())
    );
END;
GO

IF OBJECT_ID('TSS.Submission', 'U') IS NULL
BEGIN
    CREATE TABLE TSS.Submission (
        SubmissionID    bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_TSS_Submission PRIMARY KEY,
        SalesOrderID    bigint NULL,
        EnvCode         varchar(20) NOT NULL,
        ClientCode      varchar(10) NOT NULL,
        TssEntity       varchar(50) NULL,
        TssReference    nvarchar(100) NULL,
        TssStatus       nvarchar(100) NULL,
        SubmittedAt     datetime2(3) NULL,
        LastCheckedAt   datetime2(3) NULL,
        StatusMessage   nvarchar(2000) NULL
    );
END;
GO
