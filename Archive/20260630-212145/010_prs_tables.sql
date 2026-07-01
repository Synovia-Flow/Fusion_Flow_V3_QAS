/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 10 OF N
    ==================================================
    Purpose : Create the PRS (processing) canonical tables for Module 2
              (Data Processing). One logical movement materialises as
              1 PRS.ENS_Header -> many PRS.Consignment -> <=99 PRS.Goods_Item,
              each with nested TSS child arrays. Rows are produced by the
              NORMALISE -> ENRICH -> CONSTRUCT -> VALIDATE pipeline and carry
              the EXC execution linkage trio (ExecutionID / TransactionID /
              ClientCode) threaded from EXC.Execution.

    Source  : Module 2 PRS Processing Module Design - section 2
              (Header/Consignment/Goods + 10 nested child tables);
              R1 decisions Q3 (ENS-context only) / Q9 (READ-only NULL placeholders).

    Run after : 001_create_schemas.sql (schemas, incl. PRS),
                004_exc_log_tables.sql (EXC execution spine),
                005 / 008_ing_bkd_raw_tables.sql (ING landing tables).
    Safe to rerun: Yes.

    Notes:
      * Every CREATE TABLE is guarded by IF OBJECT_ID(...,'U') IS NULL; FKs and
        indexes carry their own existence guards. Each is followed by GO.
      * All FKs to EXC.Execution and between PRS tables are WITHOUT cascade -
        the audit/canonical rows must persist independently.
      * Provenance: header carries SourceEnsLoadID (-> ING.BKD_Raw_ENS.LoadID);
        goods carry SourceSalesOrderLoadID (-> ING.BKD_Raw_Sales_Orders.LoadID).
        These are soft references (no FK) so PRS rows survive raw-table pruning.
      * The "<=99 goods per consignment" rule (and ">=1 goods") is enforced by
        the runner at VALIDATE; NO CHECK constraint is added here, since rows
        are inserted incrementally mid-load and a CHECK would block the build.
      * SD-context goods columns and child rows are provisioned now but left
        NULL/unpopulated in R1 (design 2.2 / R1 Q3).
*/

/* ------------------------------------------------------------------ */
/* Defensive schema guard (PRS is normally created in 001)             */
/* ------------------------------------------------------------------ */
IF SCHEMA_ID('PRS') IS NULL EXEC('CREATE SCHEMA PRS');
GO

/* ================================================================== */
/* 2.1  TOP-LEVEL: ENS_Header / Consignment / Goods_Item               */
/* ================================================================== */

/* ------------------------------------------------------------------ */
/* PRS.ENS_Header - one per logical movement                           */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.ENS_Header', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.ENS_Header (
        EnsHeaderRowID                      bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_ENS_Header PRIMARY KEY,
        -- execution linkage trio
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        -- lifecycle
        Status                              varchar(30) NULL,
        RejectReason                        nvarchar(2000) NULL,
        -- movement business key + provenance
        MovementKey                         nvarchar(100) NULL,
        SourceEnsLoadID                     bigint NULL,                 -- soft ref -> ING.BKD_Raw_ENS.LoadID
        -- TSS fields
        movement_type                       nvarchar(40) NULL,
        type_of_passive_transport           nvarchar(40) NULL,
        identity_no_of_transport            nvarchar(27) NULL,
        nationality_of_transport            char(2) NULL,
        conveyance_ref                      nvarchar(35) NULL,
        arrival_date_time                   nvarchar(20) NULL,           -- strict DD/MM/YYYY HH:MM:SS string
        arrival_date_time_utc               datetime2(0) NULL,
        arrival_port                        nvarchar(200) NULL,
        place_of_loading                    nvarchar(33) NULL,
        place_of_unloading                  nvarchar(33) NULL,
        place_of_acceptance_same_as_loading varchar(3) NULL,
        place_of_acceptance                 nvarchar(33) NULL,
        place_of_delivery_same_as_unloading varchar(3) NULL,
        place_of_delivery                   nvarchar(33) NULL,
        seal_number                         nvarchar(20) NULL,
        transport_charges                   nvarchar(40) NULL,
        carrier_eori                        nvarchar(200) NULL,
        carrier_name                        nvarchar(35) NULL,
        carrier_street_number               nvarchar(35) NULL,
        carrier_city                        nvarchar(35) NULL,
        carrier_postcode                    nvarchar(9) NULL,
        carrier_country                     char(2) NULL,
        haulier_eori                        nvarchar(200) NULL,
        declaration_number                  nvarchar(40) NULL,           -- NULL until submission
        route                               nvarchar(20) NULL,           -- READ-only placeholder (R1 Q9)
        -- audit
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_ENS_Header_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_ENS_Header_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_ENS_Header_Movement UNIQUE (ClientCode, MovementKey)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Consignment - child of ENS_Header, 1->many                      */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Consignment', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Consignment (
        ConsignmentRowID                    bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Consignment PRIMARY KEY,
        EnsHeaderRowID                      bigint NOT NULL,
        ConsignmentOrdinal                  int NOT NULL,
        -- execution linkage trio
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        -- lifecycle
        Status                              varchar(30) NULL,
        RejectReason                        nvarchar(2000) NULL,
        -- propagated movement key
        MovementKey                         nvarchar(100) NULL,
        -- TSS fields
        declaration_number                  nvarchar(40) NULL,
        consignment_number                  nvarchar(40) NULL,
        no_sfd_reason                       nvarchar(4) NULL,
        goods_description                   nvarchar(254) NULL,
        trader_reference                    nvarchar(100) NULL,
        transport_document_number           nvarchar(35) NULL,
        controlled_goods                    varchar(3) NULL,
        goods_domestic_status               nvarchar(1) NULL,
        destination_country                 char(2) NULL,
        supervising_customs_office          nvarchar(8) NULL,
        customs_warehouse_identifier        nvarchar(18) NULL,
        ducr                                nvarchar(35) NULL,
        -- party block: consignor
        consignor_eori                      nvarchar(200) NULL,
        consignor_name                      nvarchar(35) NULL,
        consignor_street_number             nvarchar(35) NULL,
        consignor_city                      nvarchar(35) NULL,
        consignor_postcode                  nvarchar(35) NULL,
        consignor_country                   char(2) NULL,
        -- party block: consignee
        consignee_eori                      nvarchar(200) NULL,
        consignee_name                      nvarchar(35) NULL,
        consignee_street_number             nvarchar(35) NULL,
        consignee_city                      nvarchar(35) NULL,
        consignee_postcode                  nvarchar(35) NULL,
        consignee_country                   char(2) NULL,
        -- party block: importer
        importer_eori                       nvarchar(200) NULL,
        importer_name                       nvarchar(35) NULL,
        importer_street_number              nvarchar(35) NULL,
        importer_city                       nvarchar(35) NULL,
        importer_postcode                   nvarchar(35) NULL,
        importer_country                    char(2) NULL,
        -- party block: exporter
        exporter_eori                       nvarchar(200) NULL,
        exporter_name                       nvarchar(35) NULL,
        exporter_street_number              nvarchar(35) NULL,
        exporter_city                       nvarchar(35) NULL,
        exporter_postcode                   nvarchar(35) NULL,
        exporter_country                    char(2) NULL,
        -- party block: buyer (street uses buyer_street_and_number)
        buyer_eori                          nvarchar(200) NULL,
        buyer_name                          nvarchar(35) NULL,
        buyer_street_and_number             nvarchar(35) NULL,
        buyer_city                          nvarchar(35) NULL,
        buyer_postcode                      nvarchar(35) NULL,
        buyer_country                       char(2) NULL,
        -- party block: seller (street uses seller_street_and_number)
        seller_eori                         nvarchar(200) NULL,
        seller_name                         nvarchar(35) NULL,
        seller_street_and_number            nvarchar(35) NULL,
        seller_city                         nvarchar(35) NULL,
        seller_postcode                     nvarchar(35) NULL,
        seller_country                      char(2) NULL,
        -- remaining TSS fields
        align_ukims                         varchar(3) NULL,
        importer_parent_organisation_eori   nvarchar(40) NULL,
        use_importer_sde                    varchar(3) NULL,
        declaration_choice                  nvarchar(2) NULL,
        generate_SD                         varchar(3) NULL,
        container_indicator                 varchar(1) NULL,
        buyer_same_as_importer              varchar(3) NULL,
        seller_same_as_exporter             varchar(3) NULL,
        -- audit
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Consignment_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Consignment_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Consignment_Ordinal UNIQUE (EnsHeaderRowID, ConsignmentOrdinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Goods_Item - child of Consignment, 1->many (<=99, runner-checked)*/
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Goods_Item', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Goods_Item (
        GoodsItemRowID                      bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Goods_Item PRIMARY KEY,
        ConsignmentRowID                    bigint NOT NULL,
        GoodsItemOrdinal                    int NOT NULL,
        -- execution linkage trio
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        -- lifecycle
        Status                              varchar(30) NULL,
        RejectReason                        nvarchar(2000) NULL,
        -- propagated movement key + provenance
        MovementKey                         nvarchar(100) NULL,
        SourceSalesOrderLoadID              bigint NULL,                 -- soft ref -> ING.BKD_Raw_Sales_Orders.LoadID
        -- TSS fields
        consignment_number                  nvarchar(40) NULL,
        goods_id                            nvarchar(32) NULL,
        equipment_number                    nvarchar(17) NULL,
        un_dangerous_goods_code             nvarchar(4) NULL,
        type_of_packages                    nvarchar(40) NULL,
        number_of_packages                  int NULL,
        number_of_individual_pieces         int NULL,
        package_marks                       nvarchar(140) NULL,
        gross_mass_kg                       decimal(15,2) NULL,
        net_mass_kg                         decimal(15,2) NULL,
        goods_description                   nvarchar(255) NULL,
        controlled_goods                    varchar(3) NULL,
        controlled_goods_type               nvarchar(40) NULL,
        commodity_code                      nvarchar(10) NULL,
        preference                          nvarchar(4) NULL,
        country_of_origin                   char(2) NULL,
        country_of_preferential_origin      char(2) NULL,
        item_invoice_amount                 nvarchar(13) NULL,
        item_invoice_currency               nvarchar(8) NULL,
        procedure_code                      nvarchar(4) NULL,
        additional_procedure_code           nvarchar(3) NULL,
        taric_code                          nvarchar(20) NULL,
        cus_code                            nvarchar(8) NULL,
        national_additional_code            nvarchar(4) NULL,
        ni_additional_information_codes     nvarchar(40) NULL,
        supplementary_units                 decimal(15,3) NULL,
        quota_order_number                  nvarchar(6) NULL,
        valuation_method                    nvarchar(2) NULL,
        valuation_indicator                 nvarchar(4) NULL,
        invoice_number                      nvarchar(35) NULL,
        nature_of_transaction               nvarchar(40) NULL,
        statistical_value                   nvarchar(17) NULL,
        tax_type                            nvarchar(16) NULL,
        tax_base_unit                       nvarchar(4) NULL,
        tax_base_quantity                   nvarchar(16) NULL,
        payable_tax_amount                  nvarchar(9) NULL,
        payable_tax_currency                nvarchar(3) NULL,
        -- audit
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_Item_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_Item_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Goods_Item_Ordinal UNIQUE (ConsignmentRowID, GoodsItemOrdinal)
    );
END;
GO

/* ================================================================== */
/* 2.2  NESTED CHILD TABLES                                            */
/*  All carry: PK ...RowID, FK to parent, Ordinal (UQ with parent),    */
/*  RowAction DEFAULT('create'), execution-linkage trio, CreatedAt/    */
/*  UpdatedAt. SD-only arrays provisioned now, populated later (R1 Q3).*/
/* ================================================================== */

/* ------------------------------------------------------------------ */
/* PRS.Consignment_PreviousDocument (parent PRS.Consignment)           */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Consignment_PreviousDocument', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Consignment_PreviousDocument (
        ConsignmentPreviousDocumentRowID    bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Consignment_PreviousDocument PRIMARY KEY,
        ConsignmentRowID                    bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Consignment_PreviousDocument_RowAction DEFAULT ('create'),
        previous_document_ref               nvarchar(35) NULL,
        previous_document_class             nvarchar(1) NULL,
        previous_document_type              nvarchar(3) NULL,
        previous_document_item_identifier   int NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Consignment_PreviousDocument_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Consignment_PreviousDocument_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Consignment_PreviousDocument_Ordinal UNIQUE (ConsignmentRowID, Ordinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Consignment_HolderOfAuthorisation (parent PRS.Consignment)      */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Consignment_HolderOfAuthorisation', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Consignment_HolderOfAuthorisation (
        ConsignmentHolderOfAuthorisationRowID bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Consignment_HolderOfAuthorisation PRIMARY KEY,
        ConsignmentRowID                    bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Consignment_HolderOfAuthorisation_RowAction DEFAULT ('create'),
        auth_role_id                        nvarchar(17) NULL,
        auth_role_type                      nvarchar(3) NULL,
        auth_type_code                      nvarchar(5) NULL,
        eori                                nvarchar(200) NULL,
        eori_unknown                        bit NULL,
        name                                nvarchar(35) NULL,
        street_and_number                   nvarchar(35) NULL,
        country                             char(2) NULL,
        postcode                            nvarchar(9) NULL,
        city                                nvarchar(35) NULL,
        phone_number                        nvarchar(50) NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Consignment_HolderOfAuthorisation_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Consignment_HolderOfAuthorisation_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Consignment_HolderOfAuthorisation_Ordinal UNIQUE (ConsignmentRowID, Ordinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Goods_AdditionalProcedure (parent PRS.Goods_Item)               */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Goods_AdditionalProcedure', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Goods_AdditionalProcedure (
        GoodsAdditionalProcedureRowID       bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Goods_AdditionalProcedure PRIMARY KEY,
        GoodsItemRowID                      bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Goods_AdditionalProcedure_RowAction DEFAULT ('create'),
        additional_procedure_code           nvarchar(3) NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_AdditionalProcedure_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_AdditionalProcedure_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Goods_AdditionalProcedure_Ordinal UNIQUE (GoodsItemRowID, Ordinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Goods_DocumentReference (parent PRS.Goods_Item)                 */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Goods_DocumentReference', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Goods_DocumentReference (
        GoodsDocumentReferenceRowID         bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Goods_DocumentReference PRIMARY KEY,
        GoodsItemRowID                      bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Goods_DocumentReference_RowAction DEFAULT ('create'),
        document_reference                  nvarchar(35) NULL,
        document_code                       nvarchar(4) NULL,
        document_status                     nvarchar(2) NULL,
        document_part                       nvarchar(5) NULL,
        document_reason                     nvarchar(35) NULL,
        date_of_validity                    nvarchar(10) NULL,
        issuing_authority                   nvarchar(70) NULL,
        amount                              decimal(15,3) NULL,
        currency                            nvarchar(3) NULL,
        measurement_unit                    nvarchar(4) NULL,
        quantity                            nvarchar(16) NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_DocumentReference_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_DocumentReference_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Goods_DocumentReference_Ordinal UNIQUE (GoodsItemRowID, Ordinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Goods_AdditionalInformation (parent PRS.Goods_Item)             */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Goods_AdditionalInformation', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Goods_AdditionalInformation (
        GoodsAdditionalInformationRowID     bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Goods_AdditionalInformation PRIMARY KEY,
        GoodsItemRowID                      bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Goods_AdditionalInformation_RowAction DEFAULT ('create'),
        additional_info_code                nvarchar(5) NULL,
        additional_info_description         nvarchar(512) NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_AdditionalInformation_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_AdditionalInformation_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Goods_AdditionalInformation_Ordinal UNIQUE (GoodsItemRowID, Ordinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Goods_PreviousDocument (parent PRS.Goods_Item)                  */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Goods_PreviousDocument', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Goods_PreviousDocument (
        GoodsPreviousDocumentRowID          bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Goods_PreviousDocument PRIMARY KEY,
        GoodsItemRowID                      bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Goods_PreviousDocument_RowAction DEFAULT ('create'),
        previous_document_ref               nvarchar(35) NULL,
        previous_document_class             nvarchar(1) NULL,
        previous_document_type              nvarchar(3) NULL,
        previous_document_item_identifier   int NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_PreviousDocument_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_PreviousDocument_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Goods_PreviousDocument_Ordinal UNIQUE (GoodsItemRowID, Ordinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Goods_ItemAddDed (parent PRS.Goods_Item)                        */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Goods_ItemAddDed', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Goods_ItemAddDed (
        GoodsItemAddDedRowID                bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Goods_ItemAddDed PRIMARY KEY,
        GoodsItemRowID                      bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Goods_ItemAddDed_RowAction DEFAULT ('create'),
        item_add_ded_code                   nvarchar(2) NULL,
        item_add_ded_value                  decimal(15,3) NULL,
        item_add_ded_currency               nvarchar(3) NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_ItemAddDed_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_ItemAddDed_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Goods_ItemAddDed_Ordinal UNIQUE (GoodsItemRowID, Ordinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Goods_NationalAdditionalCode (parent PRS.Goods_Item)            */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Goods_NationalAdditionalCode', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Goods_NationalAdditionalCode (
        GoodsNationalAdditionalCodeRowID    bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Goods_NationalAdditionalCode PRIMARY KEY,
        GoodsItemRowID                      bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Goods_NationalAdditionalCode_RowAction DEFAULT ('create'),
        national_additional_code            nvarchar(4) NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_NationalAdditionalCode_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_NationalAdditionalCode_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Goods_NationalAdditionalCode_Ordinal UNIQUE (GoodsItemRowID, Ordinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Goods_TaxBase (parent PRS.Goods_Item)                           */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Goods_TaxBase', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Goods_TaxBase (
        GoodsTaxBaseRowID                   bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Goods_TaxBase PRIMARY KEY,
        GoodsItemRowID                      bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Goods_TaxBase_RowAction DEFAULT ('create'),
        tax_base_unit                       nvarchar(4) NULL,
        tax_base_quantity                   nvarchar(16) NULL,
        payable_tax_amount                  nvarchar(9) NULL,
        payable_tax_currency                nvarchar(3) NULL,
        tax_type                            nvarchar(16) NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_TaxBase_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_TaxBase_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Goods_TaxBase_Ordinal UNIQUE (GoodsItemRowID, Ordinal)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* PRS.Goods_AdditionalParties (parent PRS.Goods_Item, SD only)        */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('PRS.Goods_AdditionalParties', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.Goods_AdditionalParties (
        GoodsAdditionalPartiesRowID         bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_Goods_AdditionalParties PRIMARY KEY,
        GoodsItemRowID                      bigint NOT NULL,
        Ordinal                             int NOT NULL,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        ClientCode                          char(3) NULL,
        RowAction                           varchar(10) NOT NULL CONSTRAINT DF_PRS_Goods_AdditionalParties_RowAction DEFAULT ('create'),
        auth_role_id                        nvarchar(17) NULL,
        auth_role_code                      nvarchar(3) NULL,
        auth_role_type                      nvarchar(5) NULL,
        eori                                nvarchar(17) NULL,
        eori_unknown                        bit NULL,
        name                                nvarchar(70) NULL,
        street_and_number                   nvarchar(70) NULL,
        country                             char(2) NULL,
        postcode                            nvarchar(9) NULL,
        city                                nvarchar(35) NULL,
        phone_number                        nvarchar(50) NULL,
        CreatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_AdditionalParties_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt                           datetime2(3) NOT NULL CONSTRAINT DF_PRS_Goods_AdditionalParties_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_PRS_Goods_AdditionalParties_Ordinal UNIQUE (GoodsItemRowID, Ordinal)
    );
END;
GO

/* ================================================================== */
/* FOREIGN KEYS - to EXC.Execution and PRS parent rows (no cascade)    */
/* ================================================================== */

/* --- FKs to EXC.Execution (one per table) ------------------------- */
IF OBJECT_ID('PRS.FK_PRS_ENS_Header_Execution', 'F') IS NULL
    ALTER TABLE PRS.ENS_Header WITH CHECK ADD CONSTRAINT FK_PRS_ENS_Header_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Consignment_Execution', 'F') IS NULL
    ALTER TABLE PRS.Consignment WITH CHECK ADD CONSTRAINT FK_PRS_Consignment_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_Item_Execution', 'F') IS NULL
    ALTER TABLE PRS.Goods_Item WITH CHECK ADD CONSTRAINT FK_PRS_Goods_Item_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Consignment_PreviousDocument_Execution', 'F') IS NULL
    ALTER TABLE PRS.Consignment_PreviousDocument WITH CHECK ADD CONSTRAINT FK_PRS_Consignment_PreviousDocument_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Consignment_HolderOfAuthorisation_Execution', 'F') IS NULL
    ALTER TABLE PRS.Consignment_HolderOfAuthorisation WITH CHECK ADD CONSTRAINT FK_PRS_Consignment_HolderOfAuthorisation_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_AdditionalProcedure_Execution', 'F') IS NULL
    ALTER TABLE PRS.Goods_AdditionalProcedure WITH CHECK ADD CONSTRAINT FK_PRS_Goods_AdditionalProcedure_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_DocumentReference_Execution', 'F') IS NULL
    ALTER TABLE PRS.Goods_DocumentReference WITH CHECK ADD CONSTRAINT FK_PRS_Goods_DocumentReference_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_AdditionalInformation_Execution', 'F') IS NULL
    ALTER TABLE PRS.Goods_AdditionalInformation WITH CHECK ADD CONSTRAINT FK_PRS_Goods_AdditionalInformation_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_PreviousDocument_Execution', 'F') IS NULL
    ALTER TABLE PRS.Goods_PreviousDocument WITH CHECK ADD CONSTRAINT FK_PRS_Goods_PreviousDocument_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_ItemAddDed_Execution', 'F') IS NULL
    ALTER TABLE PRS.Goods_ItemAddDed WITH CHECK ADD CONSTRAINT FK_PRS_Goods_ItemAddDed_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_NationalAdditionalCode_Execution', 'F') IS NULL
    ALTER TABLE PRS.Goods_NationalAdditionalCode WITH CHECK ADD CONSTRAINT FK_PRS_Goods_NationalAdditionalCode_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_TaxBase_Execution', 'F') IS NULL
    ALTER TABLE PRS.Goods_TaxBase WITH CHECK ADD CONSTRAINT FK_PRS_Goods_TaxBase_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_AdditionalParties_Execution', 'F') IS NULL
    ALTER TABLE PRS.Goods_AdditionalParties WITH CHECK ADD CONSTRAINT FK_PRS_Goods_AdditionalParties_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO

/* --- FKs enforcing the canonical hierarchy ------------------------ */
IF OBJECT_ID('PRS.FK_PRS_Consignment_Header', 'F') IS NULL
    ALTER TABLE PRS.Consignment WITH CHECK ADD CONSTRAINT FK_PRS_Consignment_Header
        FOREIGN KEY (EnsHeaderRowID) REFERENCES PRS.ENS_Header (EnsHeaderRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_Consignment', 'F') IS NULL
    ALTER TABLE PRS.Goods_Item WITH CHECK ADD CONSTRAINT FK_PRS_Goods_Consignment
        FOREIGN KEY (ConsignmentRowID) REFERENCES PRS.Consignment (ConsignmentRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Consignment_PreviousDocument_Consignment', 'F') IS NULL
    ALTER TABLE PRS.Consignment_PreviousDocument WITH CHECK ADD CONSTRAINT FK_PRS_Consignment_PreviousDocument_Consignment
        FOREIGN KEY (ConsignmentRowID) REFERENCES PRS.Consignment (ConsignmentRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Consignment_HolderOfAuthorisation_Consignment', 'F') IS NULL
    ALTER TABLE PRS.Consignment_HolderOfAuthorisation WITH CHECK ADD CONSTRAINT FK_PRS_Consignment_HolderOfAuthorisation_Consignment
        FOREIGN KEY (ConsignmentRowID) REFERENCES PRS.Consignment (ConsignmentRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_AdditionalProcedure_Goods', 'F') IS NULL
    ALTER TABLE PRS.Goods_AdditionalProcedure WITH CHECK ADD CONSTRAINT FK_PRS_Goods_AdditionalProcedure_Goods
        FOREIGN KEY (GoodsItemRowID) REFERENCES PRS.Goods_Item (GoodsItemRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_DocumentReference_Goods', 'F') IS NULL
    ALTER TABLE PRS.Goods_DocumentReference WITH CHECK ADD CONSTRAINT FK_PRS_Goods_DocumentReference_Goods
        FOREIGN KEY (GoodsItemRowID) REFERENCES PRS.Goods_Item (GoodsItemRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_AdditionalInformation_Goods', 'F') IS NULL
    ALTER TABLE PRS.Goods_AdditionalInformation WITH CHECK ADD CONSTRAINT FK_PRS_Goods_AdditionalInformation_Goods
        FOREIGN KEY (GoodsItemRowID) REFERENCES PRS.Goods_Item (GoodsItemRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_PreviousDocument_Goods', 'F') IS NULL
    ALTER TABLE PRS.Goods_PreviousDocument WITH CHECK ADD CONSTRAINT FK_PRS_Goods_PreviousDocument_Goods
        FOREIGN KEY (GoodsItemRowID) REFERENCES PRS.Goods_Item (GoodsItemRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_ItemAddDed_Goods', 'F') IS NULL
    ALTER TABLE PRS.Goods_ItemAddDed WITH CHECK ADD CONSTRAINT FK_PRS_Goods_ItemAddDed_Goods
        FOREIGN KEY (GoodsItemRowID) REFERENCES PRS.Goods_Item (GoodsItemRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_NationalAdditionalCode_Goods', 'F') IS NULL
    ALTER TABLE PRS.Goods_NationalAdditionalCode WITH CHECK ADD CONSTRAINT FK_PRS_Goods_NationalAdditionalCode_Goods
        FOREIGN KEY (GoodsItemRowID) REFERENCES PRS.Goods_Item (GoodsItemRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_TaxBase_Goods', 'F') IS NULL
    ALTER TABLE PRS.Goods_TaxBase WITH CHECK ADD CONSTRAINT FK_PRS_Goods_TaxBase_Goods
        FOREIGN KEY (GoodsItemRowID) REFERENCES PRS.Goods_Item (GoodsItemRowID);
GO
IF OBJECT_ID('PRS.FK_PRS_Goods_AdditionalParties_Goods', 'F') IS NULL
    ALTER TABLE PRS.Goods_AdditionalParties WITH CHECK ADD CONSTRAINT FK_PRS_Goods_AdditionalParties_Goods
        FOREIGN KEY (GoodsItemRowID) REFERENCES PRS.Goods_Item (GoodsItemRowID);
GO

/* ================================================================== */
/* INDEXES - FKs + (ClientCode, MovementKey) / TransactionID lookups   */
/* (mirrors 008 index style)                                          */
/* ================================================================== */

/* --- top-level: movement / transaction lookups -------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_ENS_Header_Client_Movement' AND object_id=OBJECT_ID('PRS.ENS_Header'))
    CREATE INDEX IX_PRS_ENS_Header_Client_Movement ON PRS.ENS_Header (ClientCode, MovementKey);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_ENS_Header_Txn' AND object_id=OBJECT_ID('PRS.ENS_Header'))
    CREATE INDEX IX_PRS_ENS_Header_Txn ON PRS.ENS_Header (TransactionID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Consignment_Header' AND object_id=OBJECT_ID('PRS.Consignment'))
    CREATE INDEX IX_PRS_Consignment_Header ON PRS.Consignment (EnsHeaderRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Consignment_Client_Movement' AND object_id=OBJECT_ID('PRS.Consignment'))
    CREATE INDEX IX_PRS_Consignment_Client_Movement ON PRS.Consignment (ClientCode, MovementKey);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Consignment_Txn' AND object_id=OBJECT_ID('PRS.Consignment'))
    CREATE INDEX IX_PRS_Consignment_Txn ON PRS.Consignment (TransactionID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_Item_Consignment' AND object_id=OBJECT_ID('PRS.Goods_Item'))
    CREATE INDEX IX_PRS_Goods_Item_Consignment ON PRS.Goods_Item (ConsignmentRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_Item_Client_Movement' AND object_id=OBJECT_ID('PRS.Goods_Item'))
    CREATE INDEX IX_PRS_Goods_Item_Client_Movement ON PRS.Goods_Item (ClientCode, MovementKey);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_Item_Txn' AND object_id=OBJECT_ID('PRS.Goods_Item'))
    CREATE INDEX IX_PRS_Goods_Item_Txn ON PRS.Goods_Item (TransactionID);
GO

/* --- nested child: parent-FK lookups ------------------------------ */
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Consignment_PreviousDocument_Parent' AND object_id=OBJECT_ID('PRS.Consignment_PreviousDocument'))
    CREATE INDEX IX_PRS_Consignment_PreviousDocument_Parent ON PRS.Consignment_PreviousDocument (ConsignmentRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Consignment_HolderOfAuthorisation_Parent' AND object_id=OBJECT_ID('PRS.Consignment_HolderOfAuthorisation'))
    CREATE INDEX IX_PRS_Consignment_HolderOfAuthorisation_Parent ON PRS.Consignment_HolderOfAuthorisation (ConsignmentRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_AdditionalProcedure_Parent' AND object_id=OBJECT_ID('PRS.Goods_AdditionalProcedure'))
    CREATE INDEX IX_PRS_Goods_AdditionalProcedure_Parent ON PRS.Goods_AdditionalProcedure (GoodsItemRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_DocumentReference_Parent' AND object_id=OBJECT_ID('PRS.Goods_DocumentReference'))
    CREATE INDEX IX_PRS_Goods_DocumentReference_Parent ON PRS.Goods_DocumentReference (GoodsItemRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_AdditionalInformation_Parent' AND object_id=OBJECT_ID('PRS.Goods_AdditionalInformation'))
    CREATE INDEX IX_PRS_Goods_AdditionalInformation_Parent ON PRS.Goods_AdditionalInformation (GoodsItemRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_PreviousDocument_Parent' AND object_id=OBJECT_ID('PRS.Goods_PreviousDocument'))
    CREATE INDEX IX_PRS_Goods_PreviousDocument_Parent ON PRS.Goods_PreviousDocument (GoodsItemRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_ItemAddDed_Parent' AND object_id=OBJECT_ID('PRS.Goods_ItemAddDed'))
    CREATE INDEX IX_PRS_Goods_ItemAddDed_Parent ON PRS.Goods_ItemAddDed (GoodsItemRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_NationalAdditionalCode_Parent' AND object_id=OBJECT_ID('PRS.Goods_NationalAdditionalCode'))
    CREATE INDEX IX_PRS_Goods_NationalAdditionalCode_Parent ON PRS.Goods_NationalAdditionalCode (GoodsItemRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_TaxBase_Parent' AND object_id=OBJECT_ID('PRS.Goods_TaxBase'))
    CREATE INDEX IX_PRS_Goods_TaxBase_Parent ON PRS.Goods_TaxBase (GoodsItemRowID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_Goods_AdditionalParties_Parent' AND object_id=OBJECT_ID('PRS.Goods_AdditionalParties'))
    CREATE INDEX IX_PRS_Goods_AdditionalParties_Parent ON PRS.Goods_AdditionalParties (GoodsItemRowID);
GO

/* ================================================================== */
/* SUMMARY                                                            */
/* ------------------------------------------------------------------ */
/* Tables created (13):                                               */
/*   Top-level (3): PRS.ENS_Header, PRS.Consignment, PRS.Goods_Item   */
/*   Nested  (10): Consignment_PreviousDocument,                      */
/*                 Consignment_HolderOfAuthorisation,                 */
/*                 Goods_AdditionalProcedure, Goods_DocumentReference,*/
/*                 Goods_AdditionalInformation, Goods_PreviousDocument,*/
/*                 Goods_ItemAddDed, Goods_NationalAdditionalCode,    */
/*                 Goods_TaxBase, Goods_AdditionalParties.            */
/*                                                                    */
/* Hierarchy enforced by FKs (no cascade): ENS_Header 1->* Consignment*/
/*   1->* Goods_Item 1->* (each nested array). Per-parent UNIQUE on   */
/*   ordinals. The <=99 (and >=1) goods-per-consignment rule is       */
/*   enforced by the runner at VALIDATE - intentionally NOT a CHECK,  */
/*   so incremental mid-load inserts are not blocked.                 */
/*                                                                    */
/* HANDOFF: PRS holds the validated canonical objects. The later STG  */
/*   module materialises VALIDATED PRS movements into submission-     */
/*   shaped staging structures; STG tables are NOT created here.      */
/* ================================================================== */
