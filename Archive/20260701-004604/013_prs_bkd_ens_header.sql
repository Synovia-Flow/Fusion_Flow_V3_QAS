/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 13 OF N
    =================================================
    Purpose : First client submission-staging table for ENS Declaration Headers,
              plus a parallel tracking / source table.

              PRS.BKD_ENS_Header_Submission
                  One row per ENS Declaration Header in the exact TSS field shape
                  (POST/GET /tss_api/headers). Values are resolved from the ING
                  raw rows + choice-value lookups. Carries our own Fusion_Status
                  (STAGED -> VALIDATED -> SUBMITTED) alongside the TSS-returned
                  status, so the submission payload and its lifecycle live together.

              PRS.BKD_ENS_Header_Tracking
                  The parallel control / source spine - one row per logical
                  movement. Records WHERE the record came from (ING lineage),
                  links to the submission row, and holds the authoritative
                  status + timeline (staged / validated / submitted). This is the
                  "overall tracker" that lets you see every header's position at a
                  glance without opening the payload table.

    Run after : 004 (EXC spine), 008 (ING.BKD_Raw_ENS).
    Safe to rerun: Yes (existence guards).

    Field types mirror the TSS Declaration Header (ENS) spec - max-lengths exact;
    alpha(2) -> char(2); yes/no -> varchar(3); datetime kept as the literal TSS
    string (dd/mm/yyyy hh:mm:ss) plus a derived UTC datetime for validation.

    Fusion_Status (our processing lifecycle):
        STAGED     - row written from ING + lookups, not yet validated
        VALIDATED  - passed mandatory/conditional + Critical-Rule checks
        SUBMITTED  - sent to TSS (declaration_number returned)
        REJECTED   - failed validation (see Fusion_Status_Reason)
        CANCELLED  - withdrawn before/after submit
*/

/* ================================================================== */
/* 1. PRS.BKD_ENS_Header_Tracking  - control / source spine (parallel) */
/* ================================================================== */
IF OBJECT_ID('PRS.BKD_ENS_Header_Tracking', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.BKD_ENS_Header_Tracking (
        TrackingID         bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_BKD_ENS_Track PRIMARY KEY,
        ClientCode         char(3)       NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Track_Client DEFAULT ('BKD'),
        MovementKey        nvarchar(100) NOT NULL,            -- = ING DedupKey (DetailsDate|ICR)
        EntityType         varchar(20)   NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Track_Entity DEFAULT ('ENS_HEADER'),

        /* --- source / lineage (where it came from) --- */
        SourceChannel      varchar(20)   NULL,                -- EMAIL | SFTP | AS2 | API | FILE_DROP
        SourceEnsLoadID    bigint        NULL,                -- -> ING.BKD_Raw_ENS.LoadID (soft ref)
        SourceFile         nvarchar(1000) NULL,               -- originating CSV / email message id
        SourceReceivedUtc  datetime2(3)  NULL,
        DetailsDate        varchar(20)   NULL,
        ICR                nvarchar(40)  NULL,

        /* --- link to the submission payload row --- */
        SubmissionID       bigint        NULL,                -- -> PRS.BKD_ENS_Header_Submission (soft ref)

        /* --- authoritative status + TSS references --- */
        Fusion_Status      varchar(20)   NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Track_Status DEFAULT ('STAGED'),
        Tss_Status         varchar(40)   NULL,                -- TSS-returned: Draft|Submitted|...
        Declaration_Number nvarchar(40)  NULL,                -- ENS... returned by TSS
        RejectReason       nvarchar(2000) NULL,

        /* --- timeline --- */
        StagedAt           datetime2(3)  NULL,
        ValidatedAt        datetime2(3)  NULL,
        SubmittedAt        datetime2(3)  NULL,
        RetryCount         int           NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Track_Retry DEFAULT (0),
        LastExecutionID    bigint        NULL,
        LastTransactionID  uniqueidentifier NULL,

        CreatedAt          datetime2(3)  NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Track_Created DEFAULT (SYSUTCDATETIME()),
        UpdatedAt          datetime2(3)  NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Track_Updated DEFAULT (SYSUTCDATETIME()),

        CONSTRAINT UQ_PRS_BKD_ENS_Track_Movement UNIQUE (ClientCode, MovementKey),
        CONSTRAINT CK_PRS_BKD_ENS_Track_Status CHECK
            (Fusion_Status IN ('STAGED','VALIDATED','REJECTED','STG_MATERIALISED','READY','LINKED',
                               'SUBMITTING','SUBMITTED','ACKNOWLEDGED','IN_PROGRESS','RECONCILED',
                               'MISMATCH','ARCHIVED','ERROR','CANCELLED','ON_HOLD'))
    );
END;
GO

IF OBJECT_ID('PRS.FK_BKD_ENS_Track_Execution', 'F') IS NULL
    ALTER TABLE PRS.BKD_ENS_Header_Tracking WITH CHECK ADD CONSTRAINT FK_BKD_ENS_Track_Execution
        FOREIGN KEY (LastExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_BKD_ENS_Track_Status'
              AND object_id=OBJECT_ID('PRS.BKD_ENS_Header_Tracking'))
    CREATE INDEX IX_PRS_BKD_ENS_Track_Status ON PRS.BKD_ENS_Header_Tracking (ClientCode, Fusion_Status);
GO

/* ================================================================== */
/* 2. PRS.BKD_ENS_Header_Submission  - TSS Declaration Header payload   */
/* ================================================================== */
IF OBJECT_ID('PRS.BKD_ENS_Header_Submission', 'U') IS NULL
BEGIN
    CREATE TABLE PRS.BKD_ENS_Header_Submission (
        SubmissionID        bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_PRS_BKD_ENS_Sub PRIMARY KEY,

        /* --- lineage / linkage --- */
        ClientCode          char(3)       NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Sub_Client DEFAULT ('BKD'),
        MovementKey         nvarchar(100) NOT NULL,           -- = ING DedupKey (DetailsDate|ICR)
        TrackingID          bigint        NULL,               -- -> PRS.BKD_ENS_Header_Tracking
        SourceEnsLoadID     bigint        NULL,               -- -> ING.BKD_Raw_ENS.LoadID (soft ref)
        ExecutionID         bigint        NULL,               -- -> EXC.Execution
        TransactionID       uniqueidentifier NULL,

        /* --- TSS Declaration Header (ENS) fields - POST /tss_api/headers --- */
        op_type                              varchar(10)   NULL,   -- create | update | cancel
        declaration_number                   nvarchar(40)  NULL,   -- COND: required on update; ENS...
        movement_type                        nvarchar(40)  NULL,   -- choice (1a..7a)
        type_of_passive_transport            nvarchar(40)  NULL,   -- COND (3a); choice
        identity_no_of_transport             nvarchar(27)  NULL,
        nationality_of_transport             char(2)       NULL,   -- alpha(2); choice
        conveyance_ref                       nvarchar(35)  NULL,   -- COND (Air)
        arrival_date_time                    varchar(25)   NULL,   -- TSS literal dd/mm/yyyy hh:mm:ss (UTC)
        arrival_date_time_utc                datetime2(0)  NULL,   -- derived, for bounds validation
        arrival_port                         nvarchar(200) NULL,   -- choice (port) e.g. GBAUBELBELBEL
        place_of_loading                     nvarchar(33)  NULL,
        place_of_unloading                   nvarchar(33)  NULL,
        place_of_acceptance_same_as_loading  varchar(3)    NULL,   -- yes/no; COND (3a)
        place_of_acceptance                  nvarchar(33)  NULL,   -- COND
        place_of_delivery_same_as_unloading  varchar(3)    NULL,   -- yes/no; COND (3a)
        place_of_delivery                    nvarchar(33)  NULL,   -- COND
        seal_number                          nvarchar(20)  NULL,
        route                                nvarchar(20)  NULL,   -- READ ONLY (auto from arrival_port)
        transport_charges                    nvarchar(40)  NULL,   -- choice (Y/Z/...)
        carrier_eori                         nvarchar(200) NULL,   -- XI preferred; GB not accepted
        carrier_name                         nvarchar(35)  NULL,   -- COND (Maritime/RoRo)
        carrier_street_number                nvarchar(35)  NULL,   -- COND
        carrier_city                         nvarchar(35)  NULL,   -- COND
        carrier_postcode                     nvarchar(9)   NULL,   -- COND
        carrier_country                      char(2)       NULL,   -- COND; alpha(2); choice
        haulier_eori                         nvarchar(200) NULL,

        /* --- TSS read-only response fields --- */
        Tss_Status          varchar(40)   NULL,   -- 'status': Draft|Submitted|Processing|...
        Tss_Error_Message   nvarchar(max) NULL,   -- 'error_message'

        /* --- Fusion processing lifecycle --- */
        Fusion_Status        varchar(20)  NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Sub_Status DEFAULT ('STAGED'),
        Fusion_Status_Reason nvarchar(2000) NULL,
        StagedAt             datetime2(3) NULL CONSTRAINT DF_PRS_BKD_ENS_Sub_Staged DEFAULT (SYSUTCDATETIME()),
        ValidatedAt          datetime2(3) NULL,
        SubmittedAt          datetime2(3) NULL,

        CreatedAt           datetime2(3)  NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Sub_Created DEFAULT (SYSUTCDATETIME()),
        UpdatedAt           datetime2(3)  NOT NULL CONSTRAINT DF_PRS_BKD_ENS_Sub_Updated DEFAULT (SYSUTCDATETIME()),

        CONSTRAINT UQ_PRS_BKD_ENS_Sub_Movement UNIQUE (ClientCode, MovementKey),
        CONSTRAINT CK_PRS_BKD_ENS_Sub_Status CHECK
            (Fusion_Status IN ('STAGED','VALIDATED','REJECTED','STG_MATERIALISED','READY','LINKED',
                               'SUBMITTING','SUBMITTED','ACKNOWLEDGED','IN_PROGRESS','RECONCILED',
                               'MISMATCH','ARCHIVED','ERROR','CANCELLED','ON_HOLD'))
    );
END;
GO

/* Foreign keys: submission -> tracking (one direction), submission -> execution. */
IF OBJECT_ID('PRS.FK_BKD_ENS_Sub_Tracking', 'F') IS NULL
    ALTER TABLE PRS.BKD_ENS_Header_Submission WITH CHECK ADD CONSTRAINT FK_BKD_ENS_Sub_Tracking
        FOREIGN KEY (TrackingID) REFERENCES PRS.BKD_ENS_Header_Tracking (TrackingID);
GO
IF OBJECT_ID('PRS.FK_BKD_ENS_Sub_Execution', 'F') IS NULL
    ALTER TABLE PRS.BKD_ENS_Header_Submission WITH CHECK ADD CONSTRAINT FK_BKD_ENS_Sub_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_PRS_BKD_ENS_Sub_Status'
              AND object_id=OBJECT_ID('PRS.BKD_ENS_Header_Submission'))
    CREATE INDEX IX_PRS_BKD_ENS_Sub_Status ON PRS.BKD_ENS_Header_Submission (ClientCode, Fusion_Status);
GO
