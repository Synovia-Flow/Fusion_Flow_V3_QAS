/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 2 OF 3
    =================================================
    Purpose : Create the CFG configuration tables that drive all module behaviour.
              Onboarding a new client is a CFG exercise, not a code change.

    Source  : Fusion Flow R1 Functional Specification
              - 3.1 (CFG key tables), 3.3 (process/status model),
              - 3.4 (multi-tenant client model), 3.5 (API New/Old switch),
              - 4.1 (email/source rules), 6 (TSS API integration).

    Run after : 001_create_schemas.sql
    Run before: 003_seed_cfg.sql
    Safe to rerun: Yes. Every CREATE is guarded by an existence check.

    Tables:
        CFG.Application_Parameters global key/value runtime settings (all non-connection settings)
        CFG.Clients                principal registry (3-letter code, schema, prefix)
        CFG.Credentials            TSS API username + secret REFERENCE (no plaintext secret)
        CFG.Folder_Paths           per-client operational folders
        CFG.Email_Rules            per-client mailbox/sender/file-type ingestion rules
        CFG.API_Version            active API version (New/Old) + base URLs per client/resource
        CFG.API_Process_Map        ordered API operations per client/route (op_type + endpoint)
        CFG.Choice_Field_Registry  the choice fields to bootstrap from GET /choice_values
        CFG.Choice_Value_Cache     cached choice values (~57 reference sets)
        CFG.Status_Vocabulary      the single shared process/status vocabulary
*/

/* ------------------------------------------------------------------ */
/* CFG.Application_Parameters - global runtime settings.               */
/* The .ini holds ONLY the DB connection (bootstrap); every other       */
/* application setting lives here so it is centrally managed and audited.*/
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Application_Parameters', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Application_Parameters (
        ParameterID    int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Application_Parameters PRIMARY KEY,
        ParameterKey   varchar(100) NOT NULL,
        ParameterValue nvarchar(1000) NULL,
        ValueType      varchar(20) NOT NULL CONSTRAINT DF_CFG_AppParams_ValueType DEFAULT ('STRING'),
        Description    nvarchar(500) NULL,
        IsActive       bit NOT NULL CONSTRAINT DF_CFG_AppParams_IsActive DEFAULT (1),
        UpdatedAt      datetime2(3) NOT NULL CONSTRAINT DF_CFG_AppParams_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Application_Parameters UNIQUE (ParameterKey)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.Clients - principal registry                                    */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Clients', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Clients (
        ClientID        int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Clients PRIMARY KEY,
        ClientCode      char(3) NOT NULL,           -- 3-letter code, e.g. BKD
        ClientName      nvarchar(100) NOT NULL,
        SchemaName      sysname NOT NULL,           -- per-client schema, e.g. BKD
        StgTablePrefix  varchar(10) NOT NULL,       -- STG prefix, e.g. BKD_
        DefaultRoute    char(1) NOT NULL CONSTRAINT DF_CFG_Clients_Route DEFAULT ('A'),  -- A/B/C/D
        IsAgent         bit NOT NULL CONSTRAINT DF_CFG_Clients_IsAgent DEFAULT (0),
        ActAsSysId      varchar(64) NULL,           -- customer_account_sys_id for actAs agents
        IsActive        bit NOT NULL CONSTRAINT DF_CFG_Clients_IsActive DEFAULT (0),
        Notes           nvarchar(1000) NULL,
        UpdatedAt       datetime2(3) NOT NULL CONSTRAINT DF_CFG_Clients_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Clients_Code UNIQUE (ClientCode)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.Credentials - TSS API auth. Stores USERNAME + a secret REFERENCE */
/* (Key Vault name / secret id). Never the plaintext password.         */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Credentials', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Credentials (
        CredentialID  int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Credentials PRIMARY KEY,
        ClientCode    char(3) NOT NULL,
        EnvCode       varchar(10) NOT NULL,         -- TEST | PROD
        AuthType      varchar(20) NOT NULL CONSTRAINT DF_CFG_Credentials_AuthType DEFAULT ('BASIC'),
        ApiUsername   varchar(64) NOT NULL,         -- format API.TSSnnnnnnn
        SecretRef     nvarchar(256) NULL,           -- Key Vault secret name / reference, NOT the secret
        IsActive      bit NOT NULL CONSTRAINT DF_CFG_Credentials_IsActive DEFAULT (1),
        Notes         nvarchar(500) NULL,
        UpdatedAt     datetime2(3) NOT NULL CONSTRAINT DF_CFG_Credentials_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Credentials UNIQUE (ClientCode, EnvCode)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.Folder_Paths - per-client operational folders                   */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Folder_Paths', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Folder_Paths (
        PathID      int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Folder_Paths PRIMARY KEY,
        ClientCode  char(3) NOT NULL,
        PathType    varchar(30) NOT NULL,           -- INBOUND | PROCESS | FAIL | ARCHIVE | ENS_SOURCE
        PathValue   nvarchar(1000) NOT NULL,
        IsActive    bit NOT NULL CONSTRAINT DF_CFG_Folder_Paths_IsActive DEFAULT (1),
        UpdatedAt   datetime2(3) NOT NULL CONSTRAINT DF_CFG_Folder_Paths_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Folder_Paths UNIQUE (ClientCode, PathType)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.Email_Rules - per-client mailbox/sender/file-type ingestion      */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Email_Rules', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Email_Rules (
        RuleID            int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Email_Rules PRIMARY KEY,
        ClientCode        char(3) NOT NULL,
        Mailbox           nvarchar(320) NOT NULL,
        SenderRuleType    varchar(20) NOT NULL,     -- DOMAIN | ADDRESS
        SenderRule        nvarchar(500) NOT NULL,
        AllowedFileTypes  nvarchar(200) NULL,       -- e.g. .xlsx,.csv
        IsActive          bit NOT NULL CONSTRAINT DF_CFG_Email_Rules_IsActive DEFAULT (0),
        Notes             nvarchar(500) NULL,
        UpdatedAt         datetime2(3) NOT NULL CONSTRAINT DF_CFG_Email_Rules_UpdatedAt DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.API_Version - active version (New/Old switch) + base URLs        */
/* ResourceName '*' = applies to all resources for the client.          */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.API_Version', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.API_Version (
        VersionID    int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_API_Version PRIMARY KEY,
        ClientCode   char(3) NOT NULL,
        ResourceName varchar(50) NOT NULL CONSTRAINT DF_CFG_API_Version_Resource DEFAULT ('*'),
        ApiVersion   varchar(10) NOT NULL CONSTRAINT DF_CFG_API_Version_Ver DEFAULT ('NEW'),  -- NEW | OLD
        BaseUrlTest  nvarchar(200) NOT NULL,
        BaseUrlProd  nvarchar(200) NOT NULL,
        IsActive     bit NOT NULL CONSTRAINT DF_CFG_API_Version_IsActive DEFAULT (1),
        UpdatedAt    datetime2(3) NOT NULL CONSTRAINT DF_CFG_API_Version_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_API_Version UNIQUE (ClientCode, ResourceName)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.API_Process_Map - ordered API operations per client/route        */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.API_Process_Map', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.API_Process_Map (
        MapID        int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_API_Process_Map PRIMARY KEY,
        ClientCode   char(3) NOT NULL,
        RouteCode    char(1) NOT NULL,              -- A/B/C/D
        StepNo       int NOT NULL,                  -- execution order within the route
        ResourceName varchar(50) NOT NULL,          -- e.g. Declaration Header, Consignment
        Endpoint     varchar(100) NOT NULL,         -- e.g. /headers (NOT /declaration_headers)
        HttpMethod   varchar(10) NOT NULL,          -- GET | POST
        OpType       varchar(20) NULL,              -- create|update|submit|cancel|read|lookup|...
        WaitSeconds  int NOT NULL CONSTRAINT DF_CFG_API_Process_Map_Wait DEFAULT (0),  -- e.g. 90 after GMR submit
        IsActive     bit NOT NULL CONSTRAINT DF_CFG_API_Process_Map_IsActive DEFAULT (1),
        Notes        nvarchar(500) NULL,
        UpdatedAt    datetime2(3) NOT NULL CONSTRAINT DF_CFG_API_Process_Map_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_API_Process_Map UNIQUE (ClientCode, RouteCode, StepNo)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.Choice_Field_Registry - which choice fields to bootstrap         */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Choice_Field_Registry', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Choice_Field_Registry (
        FieldID      int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Choice_Field_Registry PRIMARY KEY,
        ChoiceField  varchar(80) NOT NULL,          -- e.g. movement_type
        Description  nvarchar(300) NULL,
        ApiPath      varchar(150) NOT NULL,          -- GET /choice_values/<field>
        IsActive     bit NOT NULL CONSTRAINT DF_CFG_Choice_Field_Registry_IsActive DEFAULT (1),
        UpdatedAt    datetime2(3) NOT NULL CONSTRAINT DF_CFG_Choice_Field_Registry_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Choice_Field_Registry UNIQUE (ChoiceField)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.Choice_Value_Cache - cached GET /choice_values results           */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Choice_Value_Cache', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Choice_Value_Cache (
        ChoiceID      bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Choice_Value_Cache PRIMARY KEY,
        ChoiceField   varchar(80) NOT NULL,         -- e.g. movement_type
        ChoiceValue   nvarchar(100) NOT NULL,       -- the code/value sent to TSS
        ChoiceName    nvarchar(400) NULL,           -- display/description (column name varies per CV table - see Rule 9)
        ExtraJson     nvarchar(max) NULL,           -- metadata: ens_allowed, ffd_allowed, effective dates, etc.
        EffectiveFrom date NULL,
        EffectiveTo   date NULL,
        IsActive      bit NOT NULL CONSTRAINT DF_CFG_Choice_Value_Cache_IsActive DEFAULT (1),
        RetrievedAt   datetime2(3) NOT NULL CONSTRAINT DF_CFG_Choice_Value_Cache_RetrievedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Choice_Value_Cache UNIQUE (ChoiceField, ChoiceValue)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.Status_Vocabulary - single shared process/status model (3.3)     */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Status_Vocabulary', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Status_Vocabulary (
        VocabID       int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Status_Vocabulary PRIMARY KEY,
        ProcessName   varchar(30) NULL,             -- the action, e.g. INGESTING (null for pure exception states)
        ResultStatus  varchar(30) NOT NULL,         -- the resulting status, e.g. INGESTED
        Meaning       nvarchar(300) NULL,
        SortOrder     int NOT NULL CONSTRAINT DF_CFG_Status_Vocabulary_Sort DEFAULT (0),
        IsTerminal    bit NOT NULL CONSTRAINT DF_CFG_Status_Vocabulary_Terminal DEFAULT (0),
        IsException   bit NOT NULL CONSTRAINT DF_CFG_Status_Vocabulary_Exception DEFAULT (0),
        CONSTRAINT UQ_CFG_Status_Vocabulary UNIQUE (ResultStatus)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* Foreign keys - everything client-scoped references CFG.Clients       */
/* (No cascade deletes: config/audit rows must not vanish silently.)    */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.FK_Credentials_Clients', 'F') IS NULL
    ALTER TABLE CFG.Credentials WITH CHECK ADD CONSTRAINT FK_Credentials_Clients
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO
IF OBJECT_ID('CFG.FK_Folder_Paths_Clients', 'F') IS NULL
    ALTER TABLE CFG.Folder_Paths WITH CHECK ADD CONSTRAINT FK_Folder_Paths_Clients
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO
IF OBJECT_ID('CFG.FK_Email_Rules_Clients', 'F') IS NULL
    ALTER TABLE CFG.Email_Rules WITH CHECK ADD CONSTRAINT FK_Email_Rules_Clients
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO
IF OBJECT_ID('CFG.FK_API_Version_Clients', 'F') IS NULL
    ALTER TABLE CFG.API_Version WITH CHECK ADD CONSTRAINT FK_API_Version_Clients
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO
IF OBJECT_ID('CFG.FK_API_Process_Map_Clients', 'F') IS NULL
    ALTER TABLE CFG.API_Process_Map WITH CHECK ADD CONSTRAINT FK_API_Process_Map_Clients
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO

/* ------------------------------------------------------------------ */
/* Helpful non-unique indexes                                          */
/* ------------------------------------------------------------------ */
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_Email_Rules_Client' AND object_id = OBJECT_ID('CFG.Email_Rules'))
    CREATE INDEX IX_CFG_Email_Rules_Client ON CFG.Email_Rules (ClientCode, IsActive);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_API_Process_Map_Client_Route' AND object_id = OBJECT_ID('CFG.API_Process_Map'))
    CREATE INDEX IX_CFG_API_Process_Map_Client_Route ON CFG.API_Process_Map (ClientCode, RouteCode, StepNo);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_Choice_Value_Cache_Field' AND object_id = OBJECT_ID('CFG.Choice_Value_Cache'))
    CREATE INDEX IX_CFG_Choice_Value_Cache_Field ON CFG.Choice_Value_Cache (ChoiceField, IsActive);
GO
