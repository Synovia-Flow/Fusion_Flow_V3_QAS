/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 26 OF N
    =================================================
    Purpose : STG staging table for BKD ENS headers - the submission-ready copy.

              A VALIDATED PRS.BKD_ENS_Header_Submission row is PROMOTED into
              STG.BKD_ENS_Header (Fusion_Status STG_MATERIALISED). The submit job
              reads STG, POSTs to TSS, and advances the row through the lifecycle:

                STG_MATERIALISED -> READY -> SUBMITTING -> SUBMITTED
                    -> (get-back / mirror) -> RECONCILED         (complete)
                    -> ERROR | CANCELLED                          (exception)

              STG holds the full TSS Declaration Header payload (same field shape as
              the PRS submission table) plus the returned Declaration_Number and the
              links used by the get-back / update / cancel jobs.

    Run after : 013 (PRS submission table), 025 (widened status vocabulary). Safe to rerun.
*/

IF SCHEMA_ID('STG') IS NULL EXEC('CREATE SCHEMA STG');
GO

IF OBJECT_ID('STG.BKD_ENS_Header', 'U') IS NULL
BEGIN
    CREATE TABLE STG.BKD_ENS_Header (
        StgID               bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_STG_BKD_ENS PRIMARY KEY,

        /* --- lineage --- */
        ClientCode          char(3)       NOT NULL CONSTRAINT DF_STG_BKD_ENS_Client DEFAULT ('BKD'),
        MovementKey         nvarchar(100) NOT NULL,
        SubmissionID        bigint        NULL,               -- -> PRS.BKD_ENS_Header_Submission
        TrackingID          bigint        NULL,               -- -> PRS.BKD_ENS_Header_Tracking
        SourceEnsLoadID     bigint        NULL,

        /* --- TSS Declaration Header (ENS) payload - same shape as PRS submission --- */
        op_type                              varchar(10)   NULL,
        declaration_number                   nvarchar(40)  NULL,   -- ENS... (set on/after create)
        movement_type                        nvarchar(40)  NULL,
        type_of_passive_transport            nvarchar(40)  NULL,
        identity_no_of_transport             nvarchar(27)  NULL,
        nationality_of_transport             char(2)       NULL,
        conveyance_ref                       nvarchar(35)  NULL,
        arrival_date_time                    varchar(25)   NULL,
        arrival_date_time_utc                datetime2(0)  NULL,
        arrival_port                         nvarchar(200) NULL,
        place_of_loading                     nvarchar(33)  NULL,
        place_of_unloading                   nvarchar(33)  NULL,
        place_of_acceptance_same_as_loading  varchar(3)    NULL,
        place_of_acceptance                  nvarchar(33)  NULL,
        place_of_delivery_same_as_unloading  varchar(3)    NULL,
        place_of_delivery                    nvarchar(33)  NULL,
        seal_number                          nvarchar(20)  NULL,
        route                                nvarchar(20)  NULL,
        transport_charges                    nvarchar(40)  NULL,
        carrier_eori                         nvarchar(200) NULL,
        carrier_name                         nvarchar(35)  NULL,
        carrier_street_number                nvarchar(35)  NULL,
        carrier_city                         nvarchar(35)  NULL,
        carrier_postcode                     nvarchar(9)   NULL,
        carrier_country                      char(2)       NULL,
        haulier_eori                         nvarchar(200) NULL,

        /* --- submission lifecycle --- */
        Fusion_Status        varchar(20)   NOT NULL CONSTRAINT DF_STG_BKD_ENS_Status DEFAULT ('STG_MATERIALISED'),
        Tss_Status           varchar(40)   NULL,
        Tss_Error_Message    nvarchar(max) NULL,

        PromoteExecutionID   bigint        NULL,
        SubmitExecutionID    bigint        NULL,
        MirrorExecutionID    bigint        NULL,

        PromotedAt           datetime2(3)  NULL CONSTRAINT DF_STG_BKD_ENS_Promoted DEFAULT (SYSUTCDATETIME()),
        SubmittedAt          datetime2(3)  NULL,
        ReconciledAt         datetime2(3)  NULL,               -- "stage data complete"
        CreatedAt            datetime2(3)  NOT NULL CONSTRAINT DF_STG_BKD_ENS_Created DEFAULT (SYSUTCDATETIME()),
        UpdatedAt            datetime2(3)  NOT NULL CONSTRAINT DF_STG_BKD_ENS_Updated DEFAULT (SYSUTCDATETIME()),

        CONSTRAINT UQ_STG_BKD_ENS_Movement UNIQUE (ClientCode, MovementKey),
        CONSTRAINT CK_STG_BKD_ENS_Status CHECK
            (Fusion_Status IN ('STG_MATERIALISED','READY','SUBMITTING','SUBMITTED','RECONCILED',
                               'MISMATCH','ERROR','CANCELLED'))
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_STG_BKD_ENS_Status'
              AND object_id=OBJECT_ID('STG.BKD_ENS_Header'))
    CREATE INDEX IX_STG_BKD_ENS_Status ON STG.BKD_ENS_Header (ClientCode, Fusion_Status);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_STG_BKD_ENS_Decl'
              AND object_id=OBJECT_ID('STG.BKD_ENS_Header'))
    CREATE INDEX IX_STG_BKD_ENS_Decl ON STG.BKD_ENS_Header (declaration_number);
GO
