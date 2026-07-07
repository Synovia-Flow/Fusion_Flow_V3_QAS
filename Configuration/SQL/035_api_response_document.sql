/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 35 OF N
    =================================================
    Purpose : API.Response_Document - DB store for the full TSS response documents that
              SUB_06_fetch_json used to write to disk (Development/json). Everything is
              now DB-driven: no files. One row is appended per fetch (an audit log of
              every response pulled back for a submitted movement), so history is kept.

              The full request+response record is in DocumentJson; ResponseJson holds the
              parsed body; StatusCode/Success make failures (e.g. a 400 rejection) easy to
              query. Per-call detail is still in API.Call; step logs in LOG.Process_Log;
              failures in LOG.Error_Log.

    Run after : 034. Safe to rerun.
*/

IF SCHEMA_ID('API') IS NULL EXEC('CREATE SCHEMA API');
GO

IF OBJECT_ID('API.Response_Document', 'U') IS NULL
BEGIN
    CREATE TABLE API.Response_Document (
        DocID              bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_API_Response_Document PRIMARY KEY,

        /* --- linkage --- */
        ExecutionID        bigint            NULL,   -- the EXC.Execution that fetched it
        ClientCode         char(3)           NULL,
        MovementKey        varchar(100)  NOT NULL,
        Declaration_Number varchar(50)       NULL,
        EnvCode            varchar(10)       NULL,

        /* --- outcome --- */
        StatusCode         int               NULL,   -- HTTP status from TSS (200, 400, ...)
        Success            bit               NULL,

        /* --- payload --- */
        ResponseJson       nvarchar(max)     NULL,   -- the parsed TSS response body
        DocumentJson       nvarchar(max)     NULL,   -- full record: request + response + meta

        FetchedAt          datetime2(3)  NOT NULL CONSTRAINT DF_API_Response_Document_Fetched DEFAULT SYSUTCDATETIME()
    );

    -- Newest response per movement is the common lookup.
    CREATE INDEX IX_API_Response_Document_Movement ON API.Response_Document (ClientCode, MovementKey, DocID DESC);
    CREATE INDEX IX_API_Response_Document_Decl     ON API.Response_Document (Declaration_Number);
END;
GO
