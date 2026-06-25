/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 8 OF N
    =================================================
    Purpose : Birkdale raw landing tables for the two ingested file types.
              ING.BKD_Raw_ENS           - one row per ENS-Headers CSV row (typed).
              ING.BKD_Raw_Sales_Orders  - one row per Sales Order line (verbatim JSON).

    Run after : 001-005 (schemas + EXC spine).
    Safe to rerun: Yes.

    Notes:
      * ENS CSV columns are stable, so BKD_Raw_ENS is typed and keyed on DedupKey
        (DetailsDate|ICR) for idempotent loads / daily re-forward de-duplication.
      * Sales Order workbooks are templated/variable, so each row lands verbatim
        as JSON with provenance; keyed on (SourceFile, RowNumber).
*/

/* ------------------------------------------------------------------ */
/* ING.BKD_Raw_ENS - typed, from ENS_Headers_*.csv                     */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('ING.BKD_Raw_ENS', 'U') IS NULL
BEGIN
    CREATE TABLE ING.BKD_Raw_ENS (
        LoadID                              bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_ING_BKD_Raw_ENS PRIMARY KEY,
        ExecutionID                         bigint NULL,
        TransactionID                       uniqueidentifier NULL,
        DedupKey                            varchar(100) NOT NULL,       -- DetailsDate|ICR
        DetailsDate                         varchar(20) NULL,
        SourceReceivedUtc                   datetime2(3) NULL,
        SourceSender                        nvarchar(320) NULL,
        SourceSubject                       nvarchar(998) NULL,
        OriginalFrom                        nvarchar(320) NULL,
        OriginalSent                        nvarchar(100) NULL,
        movement_type                       nvarchar(100) NULL,
        type_of_passive_transport           nvarchar(100) NULL,
        identity_no_of_transport            nvarchar(60) NULL,
        nationality_of_transport            nvarchar(60) NULL,
        carrier_eori                        nvarchar(60) NULL,
        transport_document_number           nvarchar(60) NULL,
        arrival_date_time                   nvarchar(60) NULL,           -- raw (e.g. "Tomorrow's Date / 06:30"); resolved in M2
        arrival_port                        nvarchar(100) NULL,
        place_of_loading                    nvarchar(100) NULL,
        place_of_acceptance_same_as_loading nvarchar(10) NULL,
        place_of_unloading                  nvarchar(100) NULL,
        place_of_delivery_same_as_unloading nvarchar(10) NULL,
        transport_charges                   nvarchar(100) NULL,
        ParseStatus                         varchar(20) NULL,
        SourceFile                          nvarchar(1000) NULL,         -- internetMessageId of the source email
        SourceCsv                           nvarchar(500) NULL,          -- the CSV file name
        LoadedAt                            datetime2(3) NOT NULL CONSTRAINT DF_ING_BKD_Raw_ENS_LoadedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_ING_BKD_Raw_ENS_Dedup UNIQUE (DedupKey)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* ING.BKD_Raw_Sales_Orders - verbatim row JSON + provenance           */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('ING.BKD_Raw_Sales_Orders', 'U') IS NULL
BEGIN
    CREATE TABLE ING.BKD_Raw_Sales_Orders (
        LoadID        bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_ING_BKD_Raw_SO PRIMARY KEY,
        ExecutionID   bigint NULL,
        TransactionID uniqueidentifier NULL,
        SourceFile    nvarchar(500) NOT NULL,        -- e.g. 20260624_124739_Sales Orders Synovia (4).xlsx
        FileDate      date NULL,                     -- from the file-name date prefix
        SheetName     nvarchar(128) NULL,
        RowNumber     int NOT NULL,                  -- 1-based business row within the sheet
        RowHash       char(64) NULL,                 -- sha256 of the row payload (line-level dedup later)
        PayloadJson   nvarchar(max) NOT NULL,        -- the verbatim row as JSON
        Status        varchar(30) NOT NULL CONSTRAINT DF_ING_BKD_Raw_SO_Status DEFAULT ('INGESTED'),
        LoadedAt      datetime2(3) NOT NULL CONSTRAINT DF_ING_BKD_Raw_SO_LoadedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_ING_BKD_Raw_SO UNIQUE (SourceFile, RowNumber)
    );
END;
GO

/* Foreign keys to the execution spine + helpful indexes. */
IF OBJECT_ID('ING.FK_BKD_Raw_ENS_Execution', 'F') IS NULL
    ALTER TABLE ING.BKD_Raw_ENS WITH CHECK ADD CONSTRAINT FK_BKD_Raw_ENS_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF OBJECT_ID('ING.FK_BKD_Raw_SO_Execution', 'F') IS NULL
    ALTER TABLE ING.BKD_Raw_Sales_Orders WITH CHECK ADD CONSTRAINT FK_BKD_Raw_SO_Execution
        FOREIGN KEY (ExecutionID) REFERENCES EXC.Execution (ExecutionID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ING_BKD_Raw_SO_File' AND object_id=OBJECT_ID('ING.BKD_Raw_Sales_Orders'))
    CREATE INDEX IX_ING_BKD_Raw_SO_File ON ING.BKD_Raw_Sales_Orders (SourceFile);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ING_BKD_Raw_SO_Hash' AND object_id=OBJECT_ID('ING.BKD_Raw_Sales_Orders'))
    CREATE INDEX IX_ING_BKD_Raw_SO_Hash ON ING.BKD_Raw_Sales_Orders (RowHash);
GO
