/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 22 OF N
    =================================================
    Purpose : Local value translation / override - let SPECIFIC FILES (or a whole
              client/field) DEFINE the translation of an incoming value to an output
              code, deterministically, instead of relying only on the fuzzy
              choice-value resolver.

              CFG.Value_Translation
                  Per client (+ optional entity, + optional SOURCE FILE) maps an
                  incoming raw value for a target field to the exact output value the
                  engine must emit. Consulted BEFORE the choice-value cache resolver,
                  so a configured translation always wins.

                  Examples seeded for BKD ENS headers:
                      arrival_port  'Belfast Port'           -> GBAUBELBELBEL
                      movement_type 'RoRo Accompanied ICS2'  -> 3a

              Scoping / precedence (most specific wins):
                  1. exact SourceFile match  (this file defines the translation)
                  2. SourceFile = '*'        (applies to every file for the client)
                  and within that, EntityKind match beats EntityKind = '*'.

              MatchMode controls how IncomingValue is compared to the raw value:
                  CI     - case-insensitive, trimmed   (default)
                  EXACT  - byte-for-byte
                  NORM   - bracket/punctuation/whitespace-insensitive (engine _norm)

    Run after : 013 (PRS BKD ENS tables), 019 (processing config). Safe to rerun.
    Engine    : Modules/Processing/process_engine.py (TranslationResolver, applied
                in _transform before the choice resolver).
*/

/* ================================================================== */
/* CFG.Value_Translation - local, file-aware translation overrides.    */
/* ================================================================== */
IF OBJECT_ID('CFG.Value_Translation', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Value_Translation (
        TranslationID  bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Value_Translation PRIMARY KEY,
        ClientCode     char(3)        NOT NULL,
        EntityKind     varchar(40)    NOT NULL CONSTRAINT DF_CFG_ValXlate_Entity DEFAULT ('*'),  -- '*' = any entity
        TargetField    nvarchar(128)  NOT NULL,                                                  -- schema column, e.g. arrival_port
        SourceFile     nvarchar(400)  NOT NULL CONSTRAINT DF_CFG_ValXlate_File   DEFAULT ('*'),  -- '*' = any file; else file name (basename matched)
        IncomingValue  nvarchar(400)  NOT NULL,                                                  -- raw value as it arrives
        OutputValue    nvarchar(400)  NULL,                                                      -- forced output value/code
        MatchMode      varchar(10)    NOT NULL CONSTRAINT DF_CFG_ValXlate_Match  DEFAULT ('CI'), -- CI | EXACT | NORM
        IsActive       bit            NOT NULL CONSTRAINT DF_CFG_ValXlate_Active  DEFAULT (1),
        Notes          nvarchar(400)  NULL,
        CreatedAt      datetime2(3)   NOT NULL CONSTRAINT DF_CFG_ValXlate_Created DEFAULT (SYSUTCDATETIME()),
        UpdatedAt      datetime2(3)   NOT NULL CONSTRAINT DF_CFG_ValXlate_Updated DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Value_Translation UNIQUE (ClientCode, EntityKind, TargetField, SourceFile, IncomingValue),
        CONSTRAINT CK_CFG_ValXlate_Match CHECK (MatchMode IN ('CI','EXACT','NORM'))
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_Value_Translation_Lookup'
              AND object_id = OBJECT_ID('CFG.Value_Translation'))
    CREATE INDEX IX_CFG_Value_Translation_Lookup
        ON CFG.Value_Translation (ClientCode, EntityKind, TargetField, IsActive);
GO

/* ------------------------------------------------------------------ */
/* Seed the BKD ENS translations (apply to every BKD ENS file: '*').   */
/* To make a translation apply to ONE file only, add a row with that   */
/* file's name in SourceFile - it takes precedence over the '*' row.   */
/* ------------------------------------------------------------------ */
MERGE CFG.Value_Translation AS t
USING (VALUES
    ('BKD', 'ENS_HEADER', 'arrival_port',  '*', 'Belfast Port',          'GBAUBELBELBEL', 'CI', 'Local translation: Belfast Port -> UN/LOCODE choice code.'),
    ('BKD', 'ENS_HEADER', 'movement_type', '*', 'RoRo Accompanied ICS2', '3a',            'CI', 'Local translation: RoRo Accompanied (ICS2) -> movement_type 3a.')
) AS s (ClientCode, EntityKind, TargetField, SourceFile, IncomingValue, OutputValue, MatchMode, Notes)
ON  t.ClientCode    = s.ClientCode
AND t.EntityKind    = s.EntityKind
AND t.TargetField   = s.TargetField
AND t.SourceFile    = s.SourceFile
AND t.IncomingValue = s.IncomingValue
WHEN MATCHED THEN UPDATE SET OutputValue = s.OutputValue, MatchMode = s.MatchMode,
    Notes = s.Notes, IsActive = 1, UpdatedAt = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, EntityKind, TargetField, SourceFile, IncomingValue, OutputValue, MatchMode, Notes)
    VALUES (s.ClientCode, s.EntityKind, s.TargetField, s.SourceFile, s.IncomingValue, s.OutputValue, s.MatchMode, s.Notes);
GO
