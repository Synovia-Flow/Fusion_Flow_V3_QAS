/*
    030_cfg_product_master.sql

    Purpose
    -------
    Product/item master data used by PRS enrichment.

    Ownership
    ---------
    CFG owns reusable masterdata. ING remains verbatim source evidence and PRS
    stores the processed/enriched movement payload.

    Fed by
    ------
    Modules/Global/load_product_masterdata.py
*/

IF OBJECT_ID('CFG.Product_Master', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Product_Master
    (
        ProductMasterID                 bigint IDENTITY(1,1) NOT NULL
            CONSTRAINT PK_CFG_Product_Master PRIMARY KEY,

        ClientCode                      char(3) NOT NULL,
        CustomerCode                    nvarchar(30) NULL,
        SourceTable                     nvarchar(128) NULL,
        SourceID                        nvarchar(80) NULL,

        SKU                             nvarchar(100) NOT NULL,
        ProductCode                     nvarchar(100) NULL,
        Barcode                         nvarchar(100) NULL,
        ProductName                     nvarchar(500) NULL,
        GoodsDescription                nvarchar(500) NULL,

        CommodityCode                   nvarchar(10) NULL,
        CountryOfOrigin                 char(2) NULL,
        PackageType                     nvarchar(40) NULL,
        PackageMarks                    nvarchar(140) NULL,
        ProcedureCode                   nvarchar(4) NULL,
        AdditionalProcedureCode         nvarchar(3) NULL,
        ValuationMethod                 nvarchar(2) NULL,
        ValuationIndicator              nvarchar(4) NULL,
        PreferenceCode                  nvarchar(4) NULL,
        NiAdditionalInfoCode            nvarchar(40) NULL,
        NatureOfTransaction             nvarchar(20) NULL,
        CountryOfPreferentialOrigin     char(2) NULL,
        TaricCode                       nvarchar(20) NULL,
        CusCode                         nvarchar(8) NULL,
        NationalAdditionalCode          nvarchar(4) NULL,
        QuotaOrderNumber                nvarchar(6) NULL,
        ControlledGoodsType             nvarchar(40) NULL,
        SdiNotes                        nvarchar(1000) NULL,

        GrossWeightKg                   decimal(15,3) NULL,
        NetWeightKg                     decimal(15,3) NULL,
        WeightSource                    nvarchar(200) NULL,
        WeightSampleCount               int NULL,
        UnitValue                       decimal(18,4) NULL,
        Currency                        nvarchar(8) NULL,
        StatisticalUnit                 nvarchar(50) NULL,
        ControlledGoods                 bit NULL,
        RequiresSupplementaryUnit       bit NULL,

        IsActive                        bit NOT NULL
            CONSTRAINT DF_CFG_Product_Master_IsActive DEFAULT (1),
        Notes                           nvarchar(1000) NULL,
        SourceCreatedAt                 datetime2(3) NULL,
        SourceUpdatedAt                 datetime2(3) NULL,
        RowHash                         char(64) NOT NULL,
        ImportedAt                      datetime2(3) NOT NULL
            CONSTRAINT DF_CFG_Product_Master_ImportedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt                       datetime2(3) NOT NULL
            CONSTRAINT DF_CFG_Product_Master_UpdatedAt DEFAULT SYSUTCDATETIME()
    );

    ALTER TABLE CFG.Product_Master
        ADD CONSTRAINT UQ_CFG_Product_Master_Client_SKU
        UNIQUE (ClientCode, SKU);

    CREATE INDEX IX_CFG_Product_Master_Active_SKU
        ON CFG.Product_Master (ClientCode, IsActive, SKU)
        INCLUDE (ProductCode, CommodityCode, GrossWeightKg, NetWeightKg, ControlledGoods);
END;
GO
