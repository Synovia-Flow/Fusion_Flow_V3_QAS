/*
    031_cfg_partner_master.sql

    Purpose
    -------
    Partner/customer master data used by PRS enrichment.

    Ownership
    ---------
    CFG owns reusable masterdata. ING remains verbatim source evidence and PRS
    stores the processed/enriched movement payload.

    Fed by
    ------
    Modules/Global/sync_partner_masterdata_from_prd.py
*/

IF OBJECT_ID('CFG.Partner_Master', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Partner_Master
    (
        PartnerMasterID        bigint IDENTITY(1,1) NOT NULL
            CONSTRAINT PK_CFG_Partner_Master PRIMARY KEY,

        ClientCode             char(3) NOT NULL,
        SourceDatabase         sysname NULL,
        SourceSchema           sysname NULL,
        SourceTable            sysname NULL,
        SourceID               nvarchar(80) NOT NULL,

        PartnerType            nvarchar(40) NULL,
        PartnerName            nvarchar(300) NOT NULL,
        NormalizedPartnerName  nvarchar(300) NOT NULL,
        EORI                   nvarchar(30) NULL,
        EORIGB                 nvarchar(30) NULL,
        VATNumber              nvarchar(40) NULL,
        AccountRef             nvarchar(80) NULL,

        AddressLine1           nvarchar(300) NULL,
        AddressLine2           nvarchar(300) NULL,
        City                   nvarchar(120) NULL,
        County                 nvarchar(120) NULL,
        Postcode               nvarchar(30) NULL,
        Country                char(2) NULL,

        ContactName            nvarchar(200) NULL,
        ContactEmail           nvarchar(320) NULL,
        ContactPhone           nvarchar(80) NULL,
        EnvCode                nvarchar(10) NULL,
        SourceSystem           nvarchar(128) NULL,
        SourceRecordID         nvarchar(80) NULL,

        IsActive               bit NOT NULL
            CONSTRAINT DF_CFG_Partner_Master_IsActive DEFAULT (1),
        Notes                  nvarchar(1000) NULL,
        SourceCreatedAt        datetime2(3) NULL,
        SourceUpdatedAt        datetime2(3) NULL,
        SourceLoadedAt         datetime2(3) NULL,
        RowHash                char(64) NOT NULL,
        ImportedAt             datetime2(3) NOT NULL
            CONSTRAINT DF_CFG_Partner_Master_ImportedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt              datetime2(3) NOT NULL
            CONSTRAINT DF_CFG_Partner_Master_UpdatedAt DEFAULT SYSUTCDATETIME()
    );

    ALTER TABLE CFG.Partner_Master
        ADD CONSTRAINT UQ_CFG_Partner_Master_Source
        UNIQUE (ClientCode, SourceTable, SourceID);

    CREATE INDEX IX_CFG_Partner_Master_Name
        ON CFG.Partner_Master (ClientCode, IsActive, PartnerType, NormalizedPartnerName)
        INCLUDE (PartnerName, EORI, AddressLine1, AddressLine2, City, Postcode, Country);

    CREATE INDEX IX_CFG_Partner_Master_EORI
        ON CFG.Partner_Master (ClientCode, IsActive, EORI)
        INCLUDE (PartnerType, PartnerName, AddressLine1, City, Postcode, Country);
END;
GO
