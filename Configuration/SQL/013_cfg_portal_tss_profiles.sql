/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 13 OF N
    ==================================================
    Purpose : Portal/TSS client profiles and upload file profiles.

              This is the contract used by the Fusion Portal to decide:
                * which tenant code the UI shows (PortalClientCode),
                * which CFG.Clients row owns the data (ClientCode),
                * which CFG.TSS_Credential row is used for TSS auth,
                * which uploaded file profile is mandatory for the client,
                * and the ordered TSS route rule: update consignments with ENS
                  before submit.

    Safe to rerun: Yes.
*/

/* ------------------------------------------------------------------ */
/* CFG.Portal_Client_Profile                                           */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Portal_Client_Profile', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Portal_Client_Profile (
        PortalClientCode          varchar(10) NOT NULL CONSTRAINT PK_CFG_Portal_Client_Profile PRIMARY KEY,
        ClientCode                char(3) NOT NULL,
        ClientName                nvarchar(120) NOT NULL,
        TssCredentialClientCode   char(3) NOT NULL,
        PreferredEnvCode          varchar(10) NOT NULL,
        UploadProfileCode         varchar(80) NOT NULL,
        RequiresEnsBeforeSubmit   bit NOT NULL CONSTRAINT DF_CFG_Portal_Profile_RequiresEns DEFAULT (1),
        IsActive                  bit NOT NULL CONSTRAINT DF_CFG_Portal_Profile_IsActive DEFAULT (1),
        Notes                     nvarchar(1000) NULL,
        UpdatedAt                 datetime2(3) NOT NULL CONSTRAINT DF_CFG_Portal_Profile_Updated DEFAULT (SYSUTCDATETIME())
    );
END;
GO

IF OBJECT_ID('CFG.FK_Portal_Profile_Client', 'F') IS NULL
    ALTER TABLE CFG.Portal_Client_Profile WITH CHECK ADD CONSTRAINT FK_Portal_Profile_Client
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO

/* ------------------------------------------------------------------ */
/* CFG.File_Profile                                                    */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.File_Profile', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.File_Profile (
        ProfileCode             varchar(80) NOT NULL CONSTRAINT PK_CFG_File_Profile PRIMARY KEY,
        PortalClientCode        varchar(10) NOT NULL,
        ClientCode              char(3) NOT NULL,
        FileRole                varchar(40) NOT NULL,
        RequiredFileOrdinal     int NOT NULL,
        FileDisplayName         nvarchar(200) NOT NULL,
        AcceptedExtensions      nvarchar(100) NOT NULL,
        TargetLandingTable      nvarchar(128) NOT NULL,
        TargetCanonicalRoot     nvarchar(128) NOT NULL,
        MappingStatus           varchar(40) NOT NULL,
        IsRequired              bit NOT NULL CONSTRAINT DF_CFG_File_Profile_IsRequired DEFAULT (1),
        IsActive                bit NOT NULL CONSTRAINT DF_CFG_File_Profile_IsActive DEFAULT (1),
        Notes                   nvarchar(1000) NULL,
        UpdatedAt               datetime2(3) NOT NULL CONSTRAINT DF_CFG_File_Profile_Updated DEFAULT (SYSUTCDATETIME())
    );
END;
GO

IF OBJECT_ID('CFG.FK_File_Profile_Portal_Profile', 'F') IS NULL
    ALTER TABLE CFG.File_Profile WITH CHECK ADD CONSTRAINT FK_File_Profile_Portal_Profile
        FOREIGN KEY (PortalClientCode) REFERENCES CFG.Portal_Client_Profile (PortalClientCode);
GO
IF OBJECT_ID('CFG.FK_File_Profile_Client', 'F') IS NULL
    ALTER TABLE CFG.File_Profile WITH CHECK ADD CONSTRAINT FK_File_Profile_Client
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO

/* Optional column-level map. It is intentionally empty until the two real
   attached files are available and column headers can be inspected. */
IF OBJECT_ID('CFG.File_Profile_Column_Map', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.File_Profile_Column_Map (
        ColumnMapID       int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_File_Profile_Column_Map PRIMARY KEY,
        ProfileCode       varchar(80) NOT NULL,
        SourceColumn      nvarchar(255) NOT NULL,
        TargetTable       nvarchar(128) NOT NULL,
        TargetColumn      nvarchar(128) NOT NULL,
        IsRequired        bit NOT NULL CONSTRAINT DF_CFG_File_Profile_Column_IsRequired DEFAULT (0),
        TransformName     varchar(80) NULL,
        DefaultValue      nvarchar(500) NULL,
        Ordinal           int NULL,
        IsActive          bit NOT NULL CONSTRAINT DF_CFG_File_Profile_Column_IsActive DEFAULT (1),
        UpdatedAt         datetime2(3) NOT NULL CONSTRAINT DF_CFG_File_Profile_Column_Updated DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_File_Profile_Column UNIQUE (ProfileCode, SourceColumn, TargetTable, TargetColumn)
    );
END;
GO

IF OBJECT_ID('CFG.FK_File_Profile_Column_Profile', 'F') IS NULL
    ALTER TABLE CFG.File_Profile_Column_Map WITH CHECK ADD CONSTRAINT FK_File_Profile_Column_Profile
        FOREIGN KEY (ProfileCode) REFERENCES CFG.File_Profile (ProfileCode);
GO

/* ------------------------------------------------------------------ */
/* CFG.TSS_Submission_Route                                             */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.TSS_Submission_Route', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.TSS_Submission_Route (
        RouteID             int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_TSS_Submission_Route PRIMARY KEY,
        PortalClientCode    varchar(10) NOT NULL,
        ClientCode          char(3) NOT NULL,
        RouteCode           varchar(20) NOT NULL,
        StepNo              int NOT NULL,
        OperationCode       varchar(80) NOT NULL,
        ResourceName        varchar(80) NOT NULL,
        Endpoint            nvarchar(200) NOT NULL,
        HttpMethod          varchar(10) NOT NULL,
        OpType              varchar(30) NOT NULL,
        RequiresPrevious    varchar(80) NULL,
        IsActive            bit NOT NULL CONSTRAINT DF_CFG_TSS_Route_IsActive DEFAULT (1),
        Notes               nvarchar(1000) NULL,
        UpdatedAt           datetime2(3) NOT NULL CONSTRAINT DF_CFG_TSS_Route_Updated DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_TSS_Submission_Route UNIQUE (PortalClientCode, RouteCode, StepNo)
    );
END;
GO

IF OBJECT_ID('CFG.FK_TSS_Route_Portal_Profile', 'F') IS NULL
    ALTER TABLE CFG.TSS_Submission_Route WITH CHECK ADD CONSTRAINT FK_TSS_Route_Portal_Profile
        FOREIGN KEY (PortalClientCode) REFERENCES CFG.Portal_Client_Profile (PortalClientCode);
GO
IF OBJECT_ID('CFG.FK_TSS_Route_Client', 'F') IS NULL
    ALTER TABLE CFG.TSS_Submission_Route WITH CHECK ADD CONSTRAINT FK_TSS_Route_Client
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO

/* ------------------------------------------------------------------ */
/* Seed profiles from the current decision                             */
/* ------------------------------------------------------------------ */
MERGE CFG.Portal_Client_Profile AS t
USING (VALUES
    ('PLE', 'PLE', 'Primeline Express', 'PLE', 'PRD', 'PLE_PRIMELINE_CONSIGNMENT_UPLOAD', 1, 1,
     'Portal code PLE. Uses Primeline TSS credentials. Mandatory source file is attached file #1.'),
    ('CW',  'CWD', 'CountryWide',       'CWF', 'TST', 'CW_COUNTRYWIDE_CONSIGNMENT_UPLOAD', 1, 1,
     'Portal code CW. Data owner is CFG.Clients CWD; current TSS credential row is CWF. Mandatory source file is attached file #2.')
) AS s (PortalClientCode, ClientCode, ClientName, TssCredentialClientCode, PreferredEnvCode, UploadProfileCode,
        RequiresEnsBeforeSubmit, IsActive, Notes)
ON t.PortalClientCode = s.PortalClientCode
WHEN MATCHED THEN UPDATE SET
    ClientCode=s.ClientCode, ClientName=s.ClientName, TssCredentialClientCode=s.TssCredentialClientCode,
    PreferredEnvCode=s.PreferredEnvCode, UploadProfileCode=s.UploadProfileCode,
    RequiresEnsBeforeSubmit=s.RequiresEnsBeforeSubmit, IsActive=s.IsActive, Notes=s.Notes,
    UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
    (PortalClientCode, ClientCode, ClientName, TssCredentialClientCode, PreferredEnvCode, UploadProfileCode,
     RequiresEnsBeforeSubmit, IsActive, Notes)
    VALUES
    (s.PortalClientCode, s.ClientCode, s.ClientName, s.TssCredentialClientCode, s.PreferredEnvCode,
     s.UploadProfileCode, s.RequiresEnsBeforeSubmit, s.IsActive, s.Notes);
GO

MERGE CFG.File_Profile AS t
USING (VALUES
    ('PLE_PRIMELINE_CONSIGNMENT_UPLOAD', 'PLE', 'PLE', 'CONSIGNMENT_UPLOAD', 1, 'Primeline mandatory consignment file', '.xlsx,.xls,.csv',
     'ING.Inbound_File / ING.Raw_Record', 'PRS.Consignment / PRS.Goods_Item', 'AWAITING_SAMPLE_COLUMNS', 1, 1,
     'Map only the first attached file for Primeline. Column map waits for the real attachment headers.'),
    ('CW_COUNTRYWIDE_CONSIGNMENT_UPLOAD', 'CW', 'CWD', 'CONSIGNMENT_UPLOAD', 2, 'Countrywide mandatory consignment file', '.xlsx,.xls,.csv',
     'ING.Inbound_File / ING.Raw_Record', 'PRS.Consignment / PRS.Goods_Item', 'AWAITING_SAMPLE_COLUMNS', 1, 1,
     'Map only the second attached file for Countrywide. Column map waits for the real attachment headers.')
) AS s (ProfileCode, PortalClientCode, ClientCode, FileRole, RequiredFileOrdinal, FileDisplayName, AcceptedExtensions,
        TargetLandingTable, TargetCanonicalRoot, MappingStatus, IsRequired, IsActive, Notes)
ON t.ProfileCode = s.ProfileCode
WHEN MATCHED THEN UPDATE SET
    PortalClientCode=s.PortalClientCode, ClientCode=s.ClientCode, FileRole=s.FileRole,
    RequiredFileOrdinal=s.RequiredFileOrdinal, FileDisplayName=s.FileDisplayName,
    AcceptedExtensions=s.AcceptedExtensions, TargetLandingTable=s.TargetLandingTable,
    TargetCanonicalRoot=s.TargetCanonicalRoot, MappingStatus=s.MappingStatus,
    IsRequired=s.IsRequired, IsActive=s.IsActive, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
    (ProfileCode, PortalClientCode, ClientCode, FileRole, RequiredFileOrdinal, FileDisplayName, AcceptedExtensions,
     TargetLandingTable, TargetCanonicalRoot, MappingStatus, IsRequired, IsActive, Notes)
    VALUES
    (s.ProfileCode, s.PortalClientCode, s.ClientCode, s.FileRole, s.RequiredFileOrdinal, s.FileDisplayName,
     s.AcceptedExtensions, s.TargetLandingTable, s.TargetCanonicalRoot, s.MappingStatus,
     s.IsRequired, s.IsActive, s.Notes);
GO

MERGE CFG.TSS_Submission_Route AS t
USING (VALUES
    ('PLE', 'PLE', 'ENS_TO_SUBMIT', 1, 'UPDATE_CONSIGNMENT_WITH_ENS', 'Consignment', '/consignments', 'POST', 'update', NULL,
     'ENS/declaration_number must be present on the consignment update before submit.'),
    ('PLE', 'PLE', 'ENS_TO_SUBMIT', 2, 'SUBMIT_CONSIGNMENT', 'Consignment', '/consignments', 'POST', 'submit', 'UPDATE_CONSIGNMENT_WITH_ENS',
     'Submit only after UPDATE_CONSIGNMENT_WITH_ENS.'),
    ('CW', 'CWD', 'ENS_TO_SUBMIT', 1, 'UPDATE_CONSIGNMENT_WITH_ENS', 'Consignment', '/consignments', 'POST', 'update', NULL,
     'ENS/declaration_number must be present on the consignment update before submit.'),
    ('CW', 'CWD', 'ENS_TO_SUBMIT', 2, 'SUBMIT_CONSIGNMENT', 'Consignment', '/consignments', 'POST', 'submit', 'UPDATE_CONSIGNMENT_WITH_ENS',
     'Submit only after UPDATE_CONSIGNMENT_WITH_ENS.')
) AS s (PortalClientCode, ClientCode, RouteCode, StepNo, OperationCode, ResourceName, Endpoint, HttpMethod, OpType, RequiresPrevious, Notes)
ON t.PortalClientCode = s.PortalClientCode AND t.RouteCode = s.RouteCode AND t.StepNo = s.StepNo
WHEN MATCHED THEN UPDATE SET
    ClientCode=s.ClientCode, OperationCode=s.OperationCode, ResourceName=s.ResourceName,
    Endpoint=s.Endpoint, HttpMethod=s.HttpMethod, OpType=s.OpType, RequiresPrevious=s.RequiresPrevious,
    Notes=s.Notes, IsActive=1, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
    (PortalClientCode, ClientCode, RouteCode, StepNo, OperationCode, ResourceName, Endpoint, HttpMethod, OpType, RequiresPrevious, Notes)
    VALUES
    (s.PortalClientCode, s.ClientCode, s.RouteCode, s.StepNo, s.OperationCode, s.ResourceName,
     s.Endpoint, s.HttpMethod, s.OpType, s.RequiresPrevious, s.Notes);
GO
