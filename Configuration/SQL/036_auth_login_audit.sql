/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 36 OF N
    =================================================
    Purpose : AUTH.Users + AUTH.LoginAudit - the portal login trail.

              Ported from Fusion_Flow_V2_BKD dev02 migration
              144_qas_auth_portal_access.sql, reduced to the two tables the V3
              portal actually writes to today, and re-pointed at the V3 model:
              CFG.TSS_Environment keys on EnvCode (V2: EnvironmentCode), and
              configuration lives in CFG.Application_Parameters (V2:
              CFG.AppConfiguration).

              Every login attempt against Integration_Layer/Portal/fusion_api
              (/api/auth/login, /api/auth/logout) is recorded here: who, which
              client/environment, from which address, success or failure and why.
              A success row and its logout row share CorrelationId.

              Additive and inert on its own. app/login_audit.py checks for
              AUTH.LoginAudit before every write and records nothing while the
              table is absent, so the portal behaves identically until this runs.

    NOT APPLIED. This file is a proposal for the team to review and deploy;
    nothing in this repo executes it.

    Security : no password material is stored here. AUTH.Users carries the hash
               columns only so a future portal-managed login has somewhere to go;
               the current V3 login still authenticates against
               CFG.TSS_Credential.

    Run after : 035. Safe to rerun.
*/

IF SCHEMA_ID('AUTH') IS NULL EXEC('CREATE SCHEMA AUTH');
GO

/* ------------------------------------------------------------------ */
/* AUTH.Users - global portal identity                                 */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('AUTH.Users', 'U') IS NULL
BEGIN
    CREATE TABLE AUTH.Users (
        UserId                bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_AUTH_Users PRIMARY KEY,
        Username              nvarchar(120) NOT NULL,
        NormalizedUsername    nvarchar(120) NOT NULL,   -- UPPER(Username); the login lookup key
        Email                 nvarchar(320)     NULL,
        NormalizedEmail       nvarchar(320)     NULL,
        DisplayName           nvarchar(200)     NULL,

        /* --- credential material (never plain text) --- */
        PasswordHash          nvarchar(255)     NULL,
        PasswordAlgo          nvarchar(40)      NULL,
        PasswordVersion       int           NOT NULL CONSTRAINT DF_AUTH_Users_PasswordVersion DEFAULT (1),
        MustResetPassword     bit           NOT NULL CONSTRAINT DF_AUTH_Users_MustReset DEFAULT (1),

        /* --- account state --- */
        IsActive              bit           NOT NULL CONSTRAINT DF_AUTH_Users_IsActive DEFAULT (1),
        IsLocked              bit           NOT NULL CONSTRAINT DF_AUTH_Users_IsLocked DEFAULT (0),
        FailedLoginCount      int           NOT NULL CONSTRAINT DF_AUTH_Users_FailedLoginCount DEFAULT (0),
        LockedUntil           datetime2(3)      NULL,
        LastPasswordChangedAt datetime2(3)      NULL,
        LastLoginAt           datetime2(3)      NULL,

        CreatedAt             datetime2(3)  NOT NULL CONSTRAINT DF_AUTH_Users_CreatedAt DEFAULT (SYSUTCDATETIME()),
        UpdatedAt             datetime2(3)  NOT NULL CONSTRAINT DF_AUTH_Users_UpdatedAt DEFAULT (SYSUTCDATETIME())
    );
END;
GO

IF OBJECT_ID('AUTH.Users', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.indexes
                   WHERE object_id = OBJECT_ID('AUTH.Users')
                     AND name = 'UX_AUTH_Users_NormalizedUsername')
    CREATE UNIQUE INDEX UX_AUTH_Users_NormalizedUsername
        ON AUTH.Users (NormalizedUsername);
GO

/* ------------------------------------------------------------------ */
/* AUTH.LoginAudit - one row per login attempt, success or failure     */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('AUTH.LoginAudit', 'U') IS NULL
BEGIN
    CREATE TABLE AUTH.LoginAudit (
        LoginAuditId  bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_AUTH_LoginAudit PRIMARY KEY,

        /* --- who --- */
        UserId        bigint            NULL,   -- resolved via NormalizedUsername when AUTH.Users has the row
        Email         nvarchar(320)     NULL,
        Username      nvarchar(120)     NULL,   -- always kept, even when no AUTH.Users row exists

        /* --- where --- */
        ClientCode    char(3)           NULL,
        EnvCode       varchar(10)       NULL,

        /* --- what happened --- */
        EventType     nvarchar(60)  NOT NULL,   -- LOGIN_SUCCESS | LOGIN_FAILURE | LOGOUT
        Success       bit           NOT NULL,
        FailureReason nvarchar(300)     NULL,   -- e.g. invalid_credentials

        /* --- request context --- */
        IpAddress     nvarchar(64)      NULL,   -- X-Forwarded-For first hop, else X-Real-IP, else peer
        UserAgent     nvarchar(500)     NULL,
        CorrelationId nvarchar(120)     NULL,   -- ties a LOGIN_SUCCESS row to its LOGOUT row

        CreatedAt     datetime2(3)  NOT NULL CONSTRAINT DF_AUTH_LoginAudit_CreatedAt DEFAULT (SYSUTCDATETIME()),

        CONSTRAINT FK_AUTH_LoginAudit_User
            FOREIGN KEY (UserId) REFERENCES AUTH.Users (UserId),
        CONSTRAINT FK_AUTH_LoginAudit_Client
            FOREIGN KEY (ClientCode) REFERENCES CFG.Clients (ClientCode),
        CONSTRAINT FK_AUTH_LoginAudit_Env
            FOREIGN KEY (EnvCode) REFERENCES CFG.TSS_Environment (EnvCode),
        CONSTRAINT CK_AUTH_LoginAudit_EventType
            CHECK (EventType IN ('LOGIN_SUCCESS', 'LOGIN_FAILURE', 'LOGOUT'))
    );
END;
GO

IF OBJECT_ID('AUTH.LoginAudit', 'U') IS NOT NULL
BEGIN
    /* The operational read: "who signed in to this client/env, most recent first". */
    IF NOT EXISTS (SELECT 1 FROM sys.indexes
                   WHERE object_id = OBJECT_ID('AUTH.LoginAudit')
                     AND name = 'IX_AUTH_LoginAudit_Client_Env_Date')
        CREATE INDEX IX_AUTH_LoginAudit_Client_Env_Date
            ON AUTH.LoginAudit (ClientCode, EnvCode, CreatedAt DESC)
            INCLUDE (EventType, Success, Username, Email, FailureReason);

    /* The security read: failed attempts for one username. */
    IF NOT EXISTS (SELECT 1 FROM sys.indexes
                   WHERE object_id = OBJECT_ID('AUTH.LoginAudit')
                     AND name = 'IX_AUTH_LoginAudit_Username_Date')
        CREATE INDEX IX_AUTH_LoginAudit_Username_Date
            ON AUTH.LoginAudit (Username, CreatedAt DESC)
            INCLUDE (EventType, Success, FailureReason, IpAddress);
END;
GO

/* ------------------------------------------------------------------ */
/* Postconditions                                                      */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('AUTH.Users', 'U') IS NULL OR OBJECT_ID('AUTH.LoginAudit', 'U') IS NULL
    THROW 51036, 'AUTH login audit postcondition failed: expected table missing.', 1;
GO

PRINT '036_auth_login_audit.sql done';
GO
