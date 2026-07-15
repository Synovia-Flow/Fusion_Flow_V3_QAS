/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 36 OF N
    ==================================================
    Purpose : [BPM] Business Partner Master - a global, multi-tenant partner
              master feeding the TSS Declaration API party blocks (consignor /
              consignee / importer / exporter / buyer / seller, Decl-Header
              carrier + holder-of-authorisation).

              Two layers: land raw (BPM.Stg_Customer_Master, all NVARCHAR +
              lineage) -> derive the typed/normalised master (BPM.BusinessPartner)
              via BPM.vw_BusinessPartner_Load. Roles are many-to-many
              (Ref_Role + BusinessPartnerRole bridge, optionally client-scoped);
              UKIMS/AEO authorisations are a child table.

              Key DQ rules carried in the model (see the design workbook in
              Documentation/Solution_Design/BPM_BusinessPartner_Schema_Design.xlsx):
                * EORI parsed into scheme-typed columns (XI/GB/EU) + validity
                  flags - consignor_eori rejects GB, importer accepts XI/EU/GB.
                * TSS party name/street/city cap at 35, postcode at 9 - *_TSS
                  shadow columns + *_Exceeds flags catch truncation pre-submit.
                * County canonicalised via Ref_County_Normalisation.
                * EORI_Unknown = 1 drives the Rule-13 Birkdale importer fallback.

    Adapted from the BPM proposal (BPM_Deploy.sql) for this platform:
      * BPM.Ref_Client seeds FROM CFG.Clients (single client registry - no
        hardcoded drift; picks up BKD/CWD/PLE and their IsActive flags).
      * BPM.Stg_Customer_Master carries ExecutionID/TransactionID so loads join
        the EXC execution spine like every other landing table.
      * The future loader job is registered (inactive) in CFG.Job.

    Run after : 002/003 (CFG), 012 (CFG.Job).
    Safe to rerun: Yes - tables guarded by OBJECT_ID, seeds via MERGE,
                   view CREATE OR ALTER, columns via COL_LENGTH guards.
*/

/* 1. Schema ------------------------------------------------------------------ */
IF SCHEMA_ID('BPM') IS NULL EXEC('CREATE SCHEMA [BPM]');
GO

/* 2. Ref_Client - mirror of CFG.Clients (module-local dimension) -------------- */
IF OBJECT_ID('[BPM].[Ref_Client]') IS NULL
CREATE TABLE [BPM].[Ref_Client]
(
    ClientCode CHAR(3) NOT NULL CONSTRAINT PK_Ref_Client PRIMARY KEY,
    ClientName NVARCHAR(80) NOT NULL,
    Is_Active  BIT NOT NULL CONSTRAINT DF_Ref_Client_Active DEFAULT (1)
);
GO
MERGE [BPM].[Ref_Client] AS t
USING (SELECT ClientCode, ClientName, IsActive FROM CFG.Clients) AS s
   ON t.ClientCode = s.ClientCode
WHEN NOT MATCHED THEN INSERT (ClientCode, ClientName, Is_Active)
     VALUES (s.ClientCode, s.ClientName, s.IsActive)
WHEN MATCHED AND (t.ClientName <> s.ClientName OR t.Is_Active <> s.IsActive)
     THEN UPDATE SET ClientName = s.ClientName, Is_Active = s.IsActive;
GO

/* 3. Ref_Role ---------------------------------------------------------------- */
IF OBJECT_ID('[BPM].[Ref_Role]') IS NULL
CREATE TABLE [BPM].[Ref_Role]
(
    RoleCode         CHAR(3) NOT NULL CONSTRAINT PK_Ref_Role PRIMARY KEY,
    RoleName         NVARCHAR(40) NOT NULL,
    TSS_Party_Prefix NVARCHAR(20) NULL,
    EORI_Scheme_Rule NVARCHAR(40) NULL,
    Is_Active        BIT NOT NULL CONSTRAINT DF_Ref_Role_Active DEFAULT (1)
);
GO
MERGE [BPM].[Ref_Role] AS t
USING (VALUES
 ('CNR',N'Consignor',N'consignor',N'XI/EU only (GB rejected)'),
 ('CNE',N'Consignee',N'consignee',N'XI/EU'),
 ('IMP',N'Importer',N'importer',N'XI/EU/GB'),
 ('EXP',N'Exporter',N'exporter',N'Any'),
 ('BUY',N'Buyer',N'buyer',N'Any'),
 ('SEL',N'Seller',N'seller',N'Any'),
 ('HAU',N'Haulier',NULL,N'GB/XI (Decl Header / GVMS)'),
 ('CAR',N'Carrier',N'carrier',N'GB/XI'),
 ('AGT',N'Agent',N'declarant',N'Per authorisation'),
 ('DEC',N'Declarant',N'declarant',N'Per authorisation'),
 ('REP',N'Representative',N'representative',N'Per authorisation'),
 ('HOA',N'Holder of Authorisation',N'holder_of_authorisation',N'XI')
) AS s(RoleCode,RoleName,TSS_Party_Prefix,EORI_Scheme_Rule)
   ON t.RoleCode = s.RoleCode
WHEN NOT MATCHED THEN INSERT (RoleCode,RoleName,TSS_Party_Prefix,EORI_Scheme_Rule)
     VALUES (s.RoleCode,s.RoleName,s.TSS_Party_Prefix,s.EORI_Scheme_Rule);
GO

/* 4. Ref_County_Normalisation ----------------------------------------------- */
IF OBJECT_ID('[BPM].[Ref_County_Normalisation]') IS NULL
CREATE TABLE [BPM].[Ref_County_Normalisation]
(
    County_Raw_Upper NVARCHAR(60) NOT NULL CONSTRAINT PK_Ref_County PRIMARY KEY,
    County_Canonical NVARCHAR(20) NULL,
    Needs_Review     BIT NOT NULL CONSTRAINT DF_Ref_County_Review DEFAULT (0)
);
GO
MERGE [BPM].[Ref_County_Normalisation] AS t
USING (VALUES
 (N'ANTRIM',N'Antrim',0),(N'CO ANTRIM',N'Antrim',0),(N'COUNTY ANTRIM',N'Antrim',0),
 (N'ARMAGH',N'Armagh',0),(N'CO ARMAGH',N'Armagh',0),(N'CO. ARMAGH',N'Armagh',0),(N'COUNTY ARMAGH',N'Armagh',0),
 (N'CO DOWN',N'Down',0),(N'CO.DOWN',N'Down',0),(N'COUNTY DOWN',N'Down',0),
 (N'CO FERMANAGH',N'Fermanagh',0),(N'CO. FERMANAGH',N'Fermanagh',0),(N'COUNTY FERMANAGH',N'Fermanagh',0),
 (N'CO TYRONE',N'Tyrone',0),(N'CO. TYRONE',N'Tyrone',0),(N'COUNTY TYRONE',N'Tyrone',0),
 (N'CO DERRY',N'Londonderry',0),(N'CO LONDONDERRY',N'Londonderry',0),(N'COUNTY LONDONDERRY',N'Londonderry',0),
 (N'BELFAST',N'Antrim',1),(N'BALLYMENA',N'Antrim',1),(N'CARRICKMORE',N'Tyrone',1),(N'NEWRY',N'Down',1),
 (N'N IRELAND',NULL,1),(N'NORTHERN IRELAND',NULL,1)
) AS s(County_Raw_Upper,County_Canonical,Needs_Review)
   ON t.County_Raw_Upper = s.County_Raw_Upper
WHEN NOT MATCHED THEN INSERT (County_Raw_Upper,County_Canonical,Needs_Review)
     VALUES (s.County_Raw_Upper,s.County_Canonical,s.Needs_Review);
GO

/* 5. Stg_Customer_Master (landing; view depends on it) ----------------------- */
IF OBJECT_ID('[BPM].[Stg_Customer_Master]') IS NULL
CREATE TABLE [BPM].[Stg_Customer_Master]
(
    Stg_Id          BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Stg_Customer_Master PRIMARY KEY CLUSTERED,
    ExecutionID     BIGINT NULL,                    -- EXC spine lineage (platform standard)
    TransactionID   UNIQUEIDENTIFIER NULL,
    [No]            NVARCHAR(20)  NULL,
    [Name]          NVARCHAR(100) NULL,
    [Address]       NVARCHAR(100) NULL,
    [Address_2]     NVARCHAR(100) NULL,
    [City]          NVARCHAR(60)  NULL,
    [County]        NVARCHAR(60)  NULL,
    [Postcode]      NVARCHAR(20)  NULL,
    [Country_Region] NVARCHAR(10) NULL,
    [Phone_No]      NVARCHAR(30)  NULL,
    [EORI_Number]   NVARCHAR(40)  NULL,
    Source_File     NVARCHAR(260) NOT NULL,
    Source_Sheet    NVARCHAR(60)  NOT NULL CONSTRAINT DF_Stg_CM_Sheet DEFAULT ('Report'),
    Source_Row      INT           NULL,
    Source_Client   CHAR(3)       NULL,
    Source_Company  NVARCHAR(60)  NULL,
    Ingest_Batch_Id UNIQUEIDENTIFIER NOT NULL,
    Ingested_At_UTC DATETIME2(3)  NOT NULL CONSTRAINT DF_Stg_CM_Ingested DEFAULT (SYSUTCDATETIME())
);
GO
/* If the table pre-exists from the standalone proposal script, add the lineage. */
IF COL_LENGTH('[BPM].[Stg_Customer_Master]', 'ExecutionID') IS NULL
    ALTER TABLE [BPM].[Stg_Customer_Master] ADD ExecutionID BIGINT NULL;
IF COL_LENGTH('[BPM].[Stg_Customer_Master]', 'TransactionID') IS NULL
    ALTER TABLE [BPM].[Stg_Customer_Master] ADD TransactionID UNIQUEIDENTIFIER NULL;
GO

/* 6. BusinessPartner --------------------------------------------------------- */
IF OBJECT_ID('[BPM].[BusinessPartner]') IS NULL
CREATE TABLE [BPM].[BusinessPartner]
(
    BusinessPartnerId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_BusinessPartner PRIMARY KEY CLUSTERED,
    PartnerCode NVARCHAR(20) NOT NULL CONSTRAINT UQ_BusinessPartner_PartnerCode UNIQUE,
    PartnerName NVARCHAR(100) NULL,
    PartnerName_Normalised NVARCHAR(100) NULL,
    AddressLine1 NVARCHAR(100) NULL,
    AddressLine2 NVARCHAR(100) NULL,
    City NVARCHAR(60) NULL,
    County_Raw NVARCHAR(60) NULL,
    County_Normalised NVARCHAR(20) NULL,
    County_Needs_Review BIT NOT NULL CONSTRAINT DF_BP_CountyReview DEFAULT (0),
    Postcode_Raw NVARCHAR(20) NULL,
    Postcode_Normalised NVARCHAR(10) NULL,
    Is_BT_Postcode AS (CASE WHEN Postcode_Normalised LIKE N'BT%' THEN CONVERT(BIT,1) ELSE CONVERT(BIT,0) END) PERSISTED,
    CountryRegionCode CHAR(2) NULL,
    Is_Expected_GB AS (CASE WHEN CountryRegionCode='GB' THEN CONVERT(BIT,1) ELSE CONVERT(BIT,0) END) PERSISTED,
    PhoneNo NVARCHAR(30) NULL,
    Email NVARCHAR(40) NULL,
    Trader_Reference NVARCHAR(100) NULL,
    EORI_Raw NVARCHAR(40) NULL,
    EORI_XI NVARCHAR(20) NULL,
    EORI_GB NVARCHAR(20) NULL,
    EORI_EU NVARCHAR(20) NULL,
    EORI_Unknown BIT NOT NULL CONSTRAINT DF_BP_EORIUnknown DEFAULT (0),
    Has_Valid_XI_EORI AS (CASE WHEN EORI_XI LIKE 'XI[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' AND LEN(EORI_XI)=14 THEN CONVERT(BIT,1) ELSE CONVERT(BIT,0) END) PERSISTED,
    Has_Valid_GB_EORI AS (CASE WHEN EORI_GB LIKE 'GB[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' AND LEN(EORI_GB)=14 THEN CONVERT(BIT,1) ELSE CONVERT(BIT,0) END) PERSISTED,
    Parent_Organisation_EORI NVARCHAR(40) NULL,
    TSS_Registered BIT NOT NULL CONSTRAINT DF_BP_TSSReg DEFAULT (0),
    TSS_Registered_Checked_UTC DATETIME2(3) NULL,
    Name_TSS NVARCHAR(35) NULL,
    Name_Exceeds AS (CASE WHEN LEN(PartnerName)>35 THEN CONVERT(BIT,1) ELSE CONVERT(BIT,0) END) PERSISTED,
    Street_And_Number_TSS NVARCHAR(35) NULL,
    Street_Exceeds BIT NOT NULL CONSTRAINT DF_BP_StreetExceeds DEFAULT (0),
    City_TSS NVARCHAR(35) NULL,
    City_Exceeds AS (CASE WHEN LEN(City)>35 THEN CONVERT(BIT,1) ELSE CONVERT(BIT,0) END) PERSISTED,
    Postcode_TSS NVARCHAR(9) NULL,
    Postcode_Exceeds AS (CASE WHEN LEN(Postcode_Normalised)>9 THEN CONVERT(BIT,1) ELSE CONVERT(BIT,0) END) PERSISTED,
    Client_BKD BIT NOT NULL CONSTRAINT DF_BP_ClientBKD DEFAULT (0),
    Client_PLE BIT NOT NULL CONSTRAINT DF_BP_ClientPLE DEFAULT (0),
    Source_File NVARCHAR(260) NULL,
    Ingest_Batch_Id UNIQUEIDENTIFIER NULL,
    Created_At_UTC DATETIME2(3) NOT NULL CONSTRAINT DF_BP_Created DEFAULT (SYSUTCDATETIME()),
    Modified_At_UTC DATETIME2(3) NOT NULL CONSTRAINT DF_BP_Modified DEFAULT (SYSUTCDATETIME()),
    Is_Active BIT NOT NULL CONSTRAINT DF_BP_Active DEFAULT (1)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_BusinessPartner_Postcode' AND object_id=OBJECT_ID('[BPM].[BusinessPartner]'))
    CREATE INDEX IX_BusinessPartner_Postcode ON [BPM].[BusinessPartner] (Postcode_Normalised);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_BusinessPartner_Name' AND object_id=OBJECT_ID('[BPM].[BusinessPartner]'))
    CREATE INDEX IX_BusinessPartner_Name ON [BPM].[BusinessPartner] (PartnerName_Normalised);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_BusinessPartner_EORI_XI' AND object_id=OBJECT_ID('[BPM].[BusinessPartner]'))
    CREATE INDEX IX_BusinessPartner_EORI_XI ON [BPM].[BusinessPartner] (EORI_XI) WHERE EORI_XI IS NOT NULL;
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_BusinessPartner_EORI_GB' AND object_id=OBJECT_ID('[BPM].[BusinessPartner]'))
    CREATE INDEX IX_BusinessPartner_EORI_GB ON [BPM].[BusinessPartner] (EORI_GB) WHERE EORI_GB IS NOT NULL;
GO

/* 7. BusinessPartnerRole ----------------------------------------------------- */
IF OBJECT_ID('[BPM].[BusinessPartnerRole]') IS NULL
CREATE TABLE [BPM].[BusinessPartnerRole]
(
    BusinessPartnerId INT NOT NULL,
    RoleCode CHAR(3) NOT NULL,
    ClientCode CHAR(3) NOT NULL CONSTRAINT DF_BPR_Client DEFAULT ('*'),   -- '*' = all clients
    Is_Active BIT NOT NULL CONSTRAINT DF_BPR_Active DEFAULT (1),
    Created_At_UTC DATETIME2(3) NOT NULL CONSTRAINT DF_BPR_Created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_BusinessPartnerRole PRIMARY KEY (BusinessPartnerId,RoleCode,ClientCode),
    CONSTRAINT FK_BPR_Partner FOREIGN KEY (BusinessPartnerId) REFERENCES [BPM].[BusinessPartner](BusinessPartnerId),
    CONSTRAINT FK_BPR_Role FOREIGN KEY (RoleCode) REFERENCES [BPM].[Ref_Role](RoleCode)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_BPR_Role' AND object_id=OBJECT_ID('[BPM].[BusinessPartnerRole]'))
    CREATE INDEX IX_BPR_Role ON [BPM].[BusinessPartnerRole] (RoleCode,ClientCode) WHERE Is_Active=1;
GO

/* 8. BusinessPartnerAuthorisation ------------------------------------------- */
IF OBJECT_ID('[BPM].[BusinessPartnerAuthorisation]') IS NULL
CREATE TABLE [BPM].[BusinessPartnerAuthorisation]
(
    AuthorisationId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_BP_Auth PRIMARY KEY,
    BusinessPartnerId INT NOT NULL,
    Auth_Type_Code NVARCHAR(5) NULL,
    Auth_Role_Id NVARCHAR(17) NULL,
    Auth_Role_Type NVARCHAR(3) NULL,
    Valid_From DATE NULL,
    Valid_To DATE NULL,
    Is_Active BIT NOT NULL CONSTRAINT DF_BP_Auth_Active DEFAULT (1),
    CONSTRAINT FK_BPAuth_Partner FOREIGN KEY (BusinessPartnerId) REFERENCES [BPM].[BusinessPartner](BusinessPartnerId)
);
GO

/* 9. Load view (CREATE OR ALTER = idempotent) ------------------------------- */
CREATE OR ALTER VIEW [BPM].[vw_BusinessPartner_Load]
AS
WITH s AS (
    SELECT st.*,
        UPPER(REPLACE(REPLACE(REPLACE(LTRIM(RTRIM(st.[EORI_Number])),' ',''),CHAR(9),''),CHAR(160),'')) AS Eori_Clean,
        UPPER(LTRIM(RTRIM(st.[Postcode]))) AS Pc_Upper,
        UPPER(LTRIM(RTRIM(st.[County])))   AS County_Key,
        LTRIM(RTRIM(CONCAT(st.[Address], CASE WHEN NULLIF(LTRIM(RTRIM(st.[Address_2])),'') IS NULL THEN '' ELSE ' ' + st.[Address_2] END))) AS Street_Full
    FROM [BPM].[Stg_Customer_Master] st
    WHERE ISNULL(st.[No],'') <> 'Total'
)
SELECT
    LTRIM(RTRIM(s.[No])) AS PartnerCode,
    LTRIM(RTRIM(s.[Name])) AS PartnerName,
    NULLIF(UPPER(LTRIM(RTRIM(s.[Name]))),'') AS PartnerName_Normalised,
    LTRIM(RTRIM(s.[Address])) AS AddressLine1,
    LTRIM(RTRIM(s.[Address_2])) AS AddressLine2,
    LTRIM(RTRIM(s.[City])) AS City,
    s.[County] AS County_Raw,
    r.County_Canonical AS County_Normalised,
    ISNULL(r.Needs_Review, CASE WHEN s.County_Key IS NULL THEN 0 ELSE 1 END) AS County_Needs_Review,
    s.[Postcode] AS Postcode_Raw,
    NULLIF(LTRIM(RTRIM(REPLACE(REPLACE(REPLACE(s.Pc_Upper,'  ',' '),'  ',' '),'  ',' '))),'') AS Postcode_Normalised,
    NULLIF(UPPER(LTRIM(RTRIM(s.[Country_Region]))),'') AS CountryRegionCode,
    LTRIM(RTRIM(s.[Phone_No])) AS PhoneNo,
    s.[EORI_Number] AS EORI_Raw,
    CASE WHEN s.Eori_Clean LIKE 'XI%' THEN s.Eori_Clean END AS EORI_XI,
    CASE WHEN s.Eori_Clean LIKE 'GB%' THEN s.Eori_Clean END AS EORI_GB,
    CAST(NULL AS NVARCHAR(20)) AS EORI_EU,
    CASE WHEN NULLIF(s.Eori_Clean,'') IS NULL THEN 1 ELSE 0 END AS EORI_Unknown,
    LEFT(NULLIF(LTRIM(RTRIM(s.[Name])),''),35) AS Name_TSS,
    LEFT(NULLIF(s.Street_Full,''),35) AS Street_And_Number_TSS,
    CASE WHEN LEN(s.Street_Full)>35 THEN 1 ELSE 0 END AS Street_Exceeds,
    LEFT(NULLIF(LTRIM(RTRIM(s.[City])),''),35) AS City_TSS,
    LEFT(NULLIF(LTRIM(RTRIM(s.Pc_Upper)),''),9) AS Postcode_TSS,
    s.Source_File, s.Ingest_Batch_Id, s.Source_Client
FROM s
LEFT JOIN [BPM].[Ref_County_Normalisation] r ON r.County_Raw_Upper = s.County_Key;
GO

/* 10. Register the (future) loader job - documentation-first, inactive ------- */
MERGE CFG.Job AS t
USING (VALUES
    ('BPM_LOAD_CUSTOMER_MASTER',
     N'Load - Customer Master to Business Partner',
     'MASTER_DATA', NULL, 'FILE_DROP', 'TASK', NULL, NULL,
     N'Land the D365 Business Central Customer export (xlsx, Report sheet) into BPM.Stg_Customer_Master with EXC lineage, then upsert the typed master via BPM.vw_BusinessPartner_Load (EORI scheme-typing, county normalisation, TSS length shadows). Drops the "Total" footer row.',
     NULL,
     N'Customer Master xlsx drop (per client INBOUND)',
     N'BPM.Stg_Customer_Master / BPM.BusinessPartner',
     N'On drop / on demand',
     N'Registered ahead of the loader build (Release 4). Activate once the loader script exists.')
) AS s(JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
       Purpose, EntryPoint, InputSource, OutputTarget, Schedule, Notes)
   ON t.JobCode = s.JobCode
WHEN NOT MATCHED THEN
    INSERT (JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
            Purpose, EntryPoint, InputSource, OutputTarget, Schedule, IsActive, Notes)
    VALUES (s.JobCode, s.JobName, s.ModuleName, s.ClientCode, s.Channel, s.JobType, s.StepNo, s.ParentJobCode,
            s.Purpose, s.EntryPoint, s.InputSource, s.OutputTarget, s.Schedule, 0, s.Notes)
WHEN MATCHED THEN
    UPDATE SET JobName = s.JobName, ModuleName = s.ModuleName, Purpose = s.Purpose,
               InputSource = s.InputSource, OutputTarget = s.OutputTarget,
               Schedule = s.Schedule, Notes = s.Notes, UpdatedAt = SYSUTCDATETIME();
GO
