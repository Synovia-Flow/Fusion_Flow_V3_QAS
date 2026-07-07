/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 27 OF N
    =================================================
    Purpose : NEW schema that reflects WHAT IS LIVE IN TSS.

              After the create call returns an ENS Declaration_Number, the get-back
              job calls TSS again (GET the header by number) and inserts the
              authoritative record into TSS.BKD_ENS_Header - the live mirror. The
              full TSS response is kept verbatim in RawJson; the parsed fields sit
              alongside for querying. The update / cancel jobs operate against this
              mirror (by Declaration_Number), and the STG row is marked complete.

    Run after : 026 (STG). Safe to rerun.
*/

IF SCHEMA_ID('TSS') IS NULL EXEC('CREATE SCHEMA TSS');
GO

IF OBJECT_ID('TSS.BKD_ENS_Header', 'U') IS NULL
BEGIN
    CREATE TABLE TSS.BKD_ENS_Header (
        MirrorID            bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_TSS_BKD_ENS PRIMARY KEY,

        /* --- identity / linkage --- */
        Declaration_Number  nvarchar(40)  NOT NULL,           -- ENS... (TSS business key)
        ClientCode          char(3)       NOT NULL CONSTRAINT DF_TSS_BKD_ENS_Client DEFAULT ('BKD'),
        MovementKey         nvarchar(100) NULL,
        StgID               bigint        NULL,               -- -> STG.BKD_ENS_Header
        SubmissionID        bigint        NULL,               -- -> PRS.BKD_ENS_Header_Submission

        /* --- header fields AS RETURNED BY TSS --- */
        op_type                              varchar(10)   NULL,
        movement_type                        nvarchar(40)  NULL,
        type_of_passive_transport            nvarchar(40)  NULL,
        identity_no_of_transport             nvarchar(27)  NULL,
        nationality_of_transport             char(2)       NULL,
        conveyance_ref                       nvarchar(35)  NULL,
        arrival_date_time                    varchar(25)   NULL,
        arrival_port                         nvarchar(200) NULL,
        place_of_loading                     nvarchar(33)  NULL,
        place_of_unloading                   nvarchar(33)  NULL,
        seal_number                          nvarchar(20)  NULL,
        route                                nvarchar(20)  NULL,
        transport_charges                    nvarchar(40)  NULL,
        carrier_eori                         nvarchar(200) NULL,
        carrier_name                         nvarchar(35)  NULL,
        carrier_country                      char(2)       NULL,
        haulier_eori                         nvarchar(200) NULL,

        /* --- TSS state --- */
        Tss_Status          varchar(40)   NULL,               -- Draft|Submitted|Processing|...
        Tss_Error_Message   nvarchar(max) NULL,
        RawJson             nvarchar(max) NULL,               -- full TSS record, verbatim

        /* --- mirror control --- */
        IsLive              bit           NOT NULL CONSTRAINT DF_TSS_BKD_ENS_Live DEFAULT (1),
        FetchExecutionID    bigint        NULL,
        FetchedAt           datetime2(3)  NULL,
        CancelledAt         datetime2(3)  NULL,
        CreatedAt           datetime2(3)  NOT NULL CONSTRAINT DF_TSS_BKD_ENS_Created DEFAULT (SYSUTCDATETIME()),
        UpdatedAt           datetime2(3)  NOT NULL CONSTRAINT DF_TSS_BKD_ENS_Updated DEFAULT (SYSUTCDATETIME()),

        CONSTRAINT UQ_TSS_BKD_ENS_Decl UNIQUE (Declaration_Number)
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_TSS_BKD_ENS_Movement'
              AND object_id=OBJECT_ID('TSS.BKD_ENS_Header'))
    CREATE INDEX IX_TSS_BKD_ENS_Movement ON TSS.BKD_ENS_Header (ClientCode, MovementKey);
GO
