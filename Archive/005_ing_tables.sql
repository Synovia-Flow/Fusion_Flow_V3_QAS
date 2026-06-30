/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 5 OF N
    =================================================
    Purpose : Create the ING (ingestion / raw landing) tables. Inbound artefacts
              are landed VERBATIM with full provenance; no transformation here.

    Source  : Fusion Flow R1 Functional Specification - 3.1, 4.1 (Module 1).

    Run after : 001-004
    Safe to rerun: Yes.

    Tables:
        ING.Inbound_File   one row per inbound file (any channel) with hash + provenance
        ING.Raw_Record     parsed rows landed verbatim (one row per source row)
        ING.Source_Email   email-channel provenance (mailbox/sender/subject/body)
*/

/* ------------------------------------------------------------------ */
/* ING.Inbound_File - one row per landed file                          */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('ING.Inbound_File', 'U') IS NULL
BEGIN
    CREATE TABLE ING.Inbound_File (
        FileID         bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_ING_Inbound_File PRIMARY KEY,
        ExecutionID    bigint NULL,
        TransactionID  uniqueidentifier NULL,
        ClientCode     char(3) NOT NULL,
        SourceChannel  varchar(20) NOT NULL,          -- FILE_DROP | EMAIL | SFTP | REST | MANUAL
        SourceName     nvarchar(500) NOT NULL,        -- original filename
        SourcePath     nvarchar(1000) NULL,
        Mailbox        nvarchar(320) NULL,
        Sender         nvarchar(320) NULL,
        ReceivedUtc    datetime2(3) NULL,
        FileHash       char(64) NOT NULL,             -- sha256 hex, for idempotent dedup
        SizeBytes      bigint NULL,
        ContentType    nvarchar(255) NULL,
        RowsLanded     int NULL,
        Status         varchar(30) NOT NULL,          -- INGESTED | QUARANTINED | FAILED | DUPLICATE
        FailReason     nvarchar(2000) NULL,
        CreatedAt      datetime2(3) NOT NULL CONSTRAINT DF_ING_Inbound_File_Created DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_ING_Inbound_File_Hash UNIQUE (ClientCode, FileHash)   -- dedup natural key
    );
END;
GO

/* ------------------------------------------------------------------ */
/* ING.Raw_Record - verbatim parsed rows                               */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('ING.Raw_Record', 'U') IS NULL
BEGIN
    CREATE TABLE ING.Raw_Record (
        RawID         bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_ING_Raw_Record PRIMARY KEY,
        FileID        bigint NOT NULL,
        ExecutionID   bigint NULL,
        ClientCode    char(3) NOT NULL,
        RowOrdinal    int NOT NULL,                  -- 1-based position in the source
        RowHash       char(64) NULL,                 -- sha256 of the row natural key (dedup)
        PayloadJson   nvarchar(max) NOT NULL,        -- the verbatim row as JSON
        Status        varchar(30) NOT NULL CONSTRAINT DF_ING_Raw_Record_Status DEFAULT ('INGESTED'),
        CreatedAt     datetime2(3) NOT NULL CONSTRAINT DF_ING_Raw_Record_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* ING.Source_Email - email-channel provenance                         */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('ING.Source_Email', 'U') IS NULL
BEGIN
    CREATE TABLE ING.Source_Email (
        EmailID           bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_ING_Source_Email PRIMARY KEY,
        ExecutionID       bigint NULL,
        TransactionID     uniqueidentifier NULL,
        ClientCode        char(3) NULL,
        Mailbox           nvarchar(320) NULL,
        GraphMessageID    nvarchar(450) NULL,
        InternetMessageID nvarchar(1000) NULL,
        Sender            nvarchar(320) NULL,
        SenderDomain      nvarchar(320) NULL,
        Subject           nvarchar(998) NULL,
        ReceivedUtc       datetime2(3) NULL,
        HasAttachments    bit NOT NULL CONSTRAINT DF_ING_Source_Email_HasAtt DEFAULT (0),
        BodyText          nvarchar(max) NULL,
        Status            varchar(30) NOT NULL,
        CreatedAt         datetime2(3) NOT NULL CONSTRAINT DF_ING_Source_Email_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* Foreign keys + indexes                                              */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('ING.FK_Inbound_File_Execution', 'F') IS NULL
    ALTER TABLE ING.Inbound_File WITH CHECK ADD CONSTRAINT FK_Inbound_File_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('ING.FK_Inbound_File_Clients', 'F') IS NULL
    ALTER TABLE ING.Inbound_File WITH CHECK ADD CONSTRAINT FK_Inbound_File_Clients
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO
IF OBJECT_ID('ING.FK_Raw_Record_Inbound_File', 'F') IS NULL
    ALTER TABLE ING.Raw_Record WITH CHECK ADD CONSTRAINT FK_Raw_Record_Inbound_File
        FOREIGN KEY (FileID) REFERENCES ING.Inbound_File (FileID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ING_Raw_Record_File' AND object_id=OBJECT_ID('ING.Raw_Record'))
    CREATE INDEX IX_ING_Raw_Record_File ON ING.Raw_Record (FileID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ING_Inbound_File_Client_Status' AND object_id=OBJECT_ID('ING.Inbound_File'))
    CREATE INDEX IX_ING_Inbound_File_Client_Status ON ING.Inbound_File (ClientCode, Status);
GO
