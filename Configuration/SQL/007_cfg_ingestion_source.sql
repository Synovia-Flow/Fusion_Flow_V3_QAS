/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 7 OF N
    =================================================
    Purpose : CFG.Ingestion_Source - the DB-driven registry of acquisition
              channels per client. The ingestion module reads this to decide
              which sources to run (EMAIL now; SFTP / AS2 / API to follow), so
              adding a channel is configuration, not code.

    Run after : 002_cfg_tables.sql (+ 003 for the BKD client row)
    Safe to rerun: Yes (MERGE on ClientCode + Channel).
*/

IF OBJECT_ID('CFG.Ingestion_Source', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Ingestion_Source (
        SourceID           int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Ingestion_Source PRIMARY KEY,
        ClientCode         char(3) NOT NULL,
        Channel            varchar(20) NOT NULL,        -- EMAIL | SFTP | AS2 | API | FILE_DROP
        IsActive           bit NOT NULL CONSTRAINT DF_CFG_Ingestion_Source_IsActive DEFAULT (0),
        ProcessedSubfolder varchar(100) NULL,           -- e.g. BKD (subfolder under Fusion_Processed for EMAIL)
        ConfigJson         nvarchar(max) NULL,          -- channel-specific settings (host/path/endpoint/etc.)
        Notes              nvarchar(500) NULL,
        UpdatedAt          datetime2(3) NOT NULL CONSTRAINT DF_CFG_Ingestion_Source_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Ingestion_Source UNIQUE (ClientCode, Channel)
    );
END;
GO

IF OBJECT_ID('CFG.FK_Ingestion_Source_Clients', 'F') IS NULL
    ALTER TABLE CFG.Ingestion_Source WITH CHECK ADD CONSTRAINT FK_Ingestion_Source_Clients
        FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode);
GO

/* Seed BKD channels: EMAIL active now; SFTP / AS2 / API registered but inactive. */
MERGE CFG.Ingestion_Source AS t
USING (VALUES
    ('BKD', 'EMAIL', 1, 'BKD',  N'{"mailbox_param":"GRAPH_MAILBOX","sender_domain":"birkdalesales.com","relevant_ext":[".xlsx",".xls",".csv",".pdf",".doc",".docx",".txt"]}', 'Microsoft Graph mailbox. Processed mail moved to Inbox/Fusion_Processed/BKD.'),
    ('BKD', 'SFTP',  0, 'BKD',  N'{"host":"","port":22,"username":"","remote_dir":"","secret_ref":""}', 'Registered, inactive - pending SFTP endpoint.'),
    ('BKD', 'AS2',   0, 'BKD',  N'{"as2_id":"","partner_id":"","inbox_dir":""}', 'Registered, inactive - pending AS2 setup.'),
    ('BKD', 'API',   0, 'BKD',  N'{"base_url":"","auth":"","endpoint":""}', 'Registered, inactive - pending API source.')
) AS s (ClientCode, Channel, IsActive, ProcessedSubfolder, ConfigJson, Notes)
ON t.ClientCode = s.ClientCode AND t.Channel = s.Channel
WHEN MATCHED THEN UPDATE SET IsActive=s.IsActive, ProcessedSubfolder=s.ProcessedSubfolder,
    ConfigJson=s.ConfigJson, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, Channel, IsActive, ProcessedSubfolder, ConfigJson, Notes)
    VALUES (s.ClientCode, s.Channel, s.IsActive, s.ProcessedSubfolder, s.ConfigJson, s.Notes);
GO
