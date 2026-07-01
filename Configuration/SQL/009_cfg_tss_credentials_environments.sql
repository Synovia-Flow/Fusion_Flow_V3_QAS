/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 9 OF N
    =================================================
    Purpose : TSS connectivity configuration -
              CFG.TSS_Environment  - one row per environment (PRD/TST) + base URL.
              CFG.TSS_Credential   - one row per client/env: username, password,
                                     active flag and the last verification result.

    Run after : 002 (CFG schema).
    Safe to rerun: Yes.

    SECURITY:
      The committed seed sets everything EXCEPT the passwords (usernames, active
      flags, verification results, environment URLs are not secret). Passwords are
      seeded as a placeholder and set separately in the database (run-once UPDATEs
      supplied out of band) - they are NOT committed to git. On re-deploy the
      MERGE never overwrites a password you have set.

    Client codes are aligned to CWD (CountryWide) across CFG.Clients and these
    tables. Not FK-bound to CFG.Clients so the verification matrix can be seeded
    independently of client onboarding order.
*/

/* ------------------------------------------------------------------ */
/* CFG.TSS_Environment                                                 */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.TSS_Environment', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.TSS_Environment (
        EnvCode      varchar(10) NOT NULL CONSTRAINT PK_CFG_TSS_Environment PRIMARY KEY,
        EnvName      nvarchar(50) NOT NULL,
        BaseUrl      nvarchar(200) NOT NULL,
        Description  nvarchar(500) NULL,
        DatabaseName nvarchar(128) NULL,
        Notes        nvarchar(500) NULL,
        IsActive     bit NOT NULL CONSTRAINT DF_CFG_TSS_Environment_IsActive DEFAULT (1),
        CreatedAt    datetime2(3) NOT NULL CONSTRAINT DF_CFG_TSS_Environment_Created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* ------------------------------------------------------------------ */
/* CFG.TSS_Credential (password held in the DB, by design)             */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.TSS_Credential', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.TSS_Credential (
        ClientCode   char(3) NOT NULL,
        EnvCode      varchar(10) NOT NULL,
        TssUsername  varchar(64) NOT NULL,
        TssPassword  nvarchar(256) NULL,             -- set in the DB; not committed
        IsActive     bit NOT NULL CONSTRAINT DF_CFG_TSS_Credential_IsActive DEFAULT (1),
        LastVerified datetime2(7) NULL,
        LastStatus   varchar(10) NULL,               -- PASS | FAIL
        HttpStatus   int NULL,
        UpdatedAt    datetime2(3) NOT NULL CONSTRAINT DF_CFG_TSS_Credential_Updated DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_CFG_TSS_Credential PRIMARY KEY (ClientCode, EnvCode),
        CONSTRAINT FK_TSS_Credential_Environment FOREIGN KEY (EnvCode) REFERENCES CFG.TSS_Environment (EnvCode)
    );
END;
GO

/* ------------------------------------------------------------------ */
/* Seed environments (all values non-secret)                           */
/* ------------------------------------------------------------------ */
MERGE CFG.TSS_Environment AS t
USING (VALUES
    ('PRD', 'Production', 'https://api.tradersupportservice.co.uk/api',
     'Live HMRC submissions. Use only after go-live approval. Outbound email active.', 'Fusion_TSS_PRD', 'Live HMRC submissions', 1),
    ('TST', 'Test', 'https://api.tsstestenv.co.uk/api',
     'Integrated to HMRC CDS Trader Dress Rehearsal. Outbound email disabled.', 'Fusion_TSS', 'CDS Trader Dress Rehearsal', 1)
) AS s (EnvCode, EnvName, BaseUrl, Description, DatabaseName, Notes, IsActive)
ON t.EnvCode = s.EnvCode
WHEN MATCHED THEN UPDATE SET EnvName=s.EnvName, BaseUrl=s.BaseUrl, Description=s.Description,
    DatabaseName=s.DatabaseName, Notes=s.Notes, IsActive=s.IsActive
WHEN NOT MATCHED THEN INSERT (EnvCode, EnvName, BaseUrl, Description, DatabaseName, Notes, IsActive)
    VALUES (s.EnvCode, s.EnvName, s.BaseUrl, s.Description, s.DatabaseName, s.Notes, s.IsActive);
GO

/* ------------------------------------------------------------------ */
/* Seed credentials - everything EXCEPT the password. The password is  */
/* set separately in the DB and is never overwritten on re-deploy.     */
/* ------------------------------------------------------------------ */
MERGE CFG.TSS_Credential AS t
USING (VALUES
    ('BKD', 'PRD', 'API.TSS0012045', 1, CONVERT(datetime2(7), '2026-04-06 12:25:27.5607268'), 'PASS', 200),
    ('BKD', 'TST', 'API.TSS0012045', 1, CONVERT(datetime2(7), '2026-04-06 12:25:27.8057282'), 'PASS', 200),
    ('CWD', 'PRD', 'API.TSS0014334', 0, CONVERT(datetime2(7), '2026-04-02 20:07:00.0000000'), 'FAIL', 401),
    ('CWD', 'TST', 'API.TSS0014334', 1, CONVERT(datetime2(7), '2026-04-06 12:25:28.0188937'), 'PASS', 200),
    ('PLE', 'PRD', 'API.TSS0011141', 1, CONVERT(datetime2(7), '2026-04-06 12:25:28.2304062'), 'PASS', 200),
    ('PLE', 'TST', 'API.TSS0011141', 0, CONVERT(datetime2(7), '2026-04-02 20:07:00.0000000'), 'FAIL', 401)
) AS s (ClientCode, EnvCode, TssUsername, IsActive, LastVerified, LastStatus, HttpStatus)
ON t.ClientCode = s.ClientCode AND t.EnvCode = s.EnvCode
WHEN MATCHED THEN UPDATE SET TssUsername=s.TssUsername, IsActive=s.IsActive,
    LastVerified=s.LastVerified, LastStatus=s.LastStatus, HttpStatus=s.HttpStatus, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, EnvCode, TssUsername, TssPassword, IsActive, LastVerified, LastStatus, HttpStatus)
    VALUES (s.ClientCode, s.EnvCode, s.TssUsername, '<SET_IN_DB>', s.IsActive, s.LastVerified, s.LastStatus, s.HttpStatus);
GO

/*
    SET THE REAL PASSWORDS (run once, in the database - NOT committed):

    UPDATE CFG.TSS_Credential SET TssPassword = '<bkd-secret>', UpdatedAt=SYSUTCDATETIME() WHERE ClientCode='BKD';
    UPDATE CFG.TSS_Credential SET TssPassword = '<cwd-secret>', UpdatedAt=SYSUTCDATETIME() WHERE ClientCode='CWD';
    UPDATE CFG.TSS_Credential SET TssPassword = '<ple-secret>', UpdatedAt=SYSUTCDATETIME() WHERE ClientCode='PLE';
*/
