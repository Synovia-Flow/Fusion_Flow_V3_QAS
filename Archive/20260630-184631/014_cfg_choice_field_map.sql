/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 14 OF N
    =================================================
    Purpose : CFG.Choice_Field_Map - maps each TSS choice-value reference set
              (CFG.Choice_Field_Registry / CFG.Choice_Value_Cache.ChoiceField)
              to the actual column(s) in OUR schemas that it governs.

              This is the join the processing engine needs: our column name is
              often NOT the TSS choice-set name. e.g. our column movement_type is
              resolved against the CV set 'mode_of_transport'; nationality_of_transport
              and carrier_country both resolve against 'country'.

              MatchOn tells the engine how to resolve an incoming value:
                NAME  - incoming is the human label ("United Kingdom") -> match
                        Choice_Value_Cache.ChoiceName, output ChoiceValue ("GB").
                VALUE - incoming is already the code -> validate membership only.

    Run after : 002 (CFG tables), 003 (registry seed).
    Safe to rerun: Yes (MERGE; refreshes mapping metadata).

    Also: adds 'transport_charges' to the choice-field registry (it is a choice
    field per the spec) and seeds the choice-values downloader controls.
*/

/* ------------------------------------------------------------------ */
/* CFG.Choice_Field_Map                                                */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Choice_Field_Map', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Choice_Field_Map (
        MapID       int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Choice_Field_Map PRIMARY KEY,
        ChoiceField varchar(80)  NOT NULL,            -- the CV set to query (-> Choice_Field_Registry)
        SchemaName  varchar(20)  NOT NULL,            -- PRS | STG | ...
        TableName   varchar(80)  NOT NULL,            -- e.g. BKD_ENS_Header_Submission, Goods_Item
        ColumnName  varchar(64)  NOT NULL,            -- our column, e.g. movement_type
        EntityKind  varchar(20)  NOT NULL,            -- ENS_HEADER | CONSIGNMENT | GOODS_ITEM
        MatchOn     varchar(10)  NOT NULL CONSTRAINT DF_CFG_ChoiceMap_MatchOn DEFAULT ('NAME'),
        IsActive    bit          NOT NULL CONSTRAINT DF_CFG_ChoiceMap_IsActive DEFAULT (1),
        Notes       nvarchar(300) NULL,
        UpdatedAt   datetime2(3) NOT NULL CONSTRAINT DF_CFG_ChoiceMap_Updated DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Choice_Field_Map UNIQUE (SchemaName, TableName, ColumnName, ChoiceField),
        CONSTRAINT CK_CFG_Choice_Field_Map_MatchOn CHECK (MatchOn IN ('NAME','VALUE'))
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_CFG_Choice_Field_Map_Field'
              AND object_id=OBJECT_ID('CFG.Choice_Field_Map'))
    CREATE INDEX IX_CFG_Choice_Field_Map_Field ON CFG.Choice_Field_Map (ChoiceField, IsActive);
GO

/* The 35 canonical choice fields are seeded in 003. transport_charges /        */
/* controlled_goods_type / package_type are NOT /choice_values endpoints per the */
/* TSS Choice Fields reference - transport_charges is a fixed QAS value, so they  */
/* are intentionally NOT registered here.                                        */

/* ------------------------------------------------------------------ */
/* Seed the field -> column map.                                       */
/* Header rows target the BKD submission table (013); goods/consignment */
/* rows target the canonical PRS tables (010).                          */
/* ------------------------------------------------------------------ */
MERGE CFG.Choice_Field_Map AS t
USING (VALUES
    /* --- ENS Declaration Header (PRS.BKD_ENS_Header_Submission) --- */
    ('movement_type',            'PRS', 'BKD_ENS_Header_Submission', 'movement_type',             'ENS_HEADER',  'NAME',  'Header movement_type resolves against CV movement_type (TSS Choice Fields ref; mode_of_transport is FFD/IMMI only).'),
    ('passive_transport_types',  'PRS', 'BKD_ENS_Header_Submission', 'type_of_passive_transport', 'ENS_HEADER',  'NAME',  NULL),
    ('country',                  'PRS', 'BKD_ENS_Header_Submission', 'nationality_of_transport',  'ENS_HEADER',  'NAME',  'Country name -> alpha-2 code.'),
    ('country',                  'PRS', 'BKD_ENS_Header_Submission', 'carrier_country',           'ENS_HEADER',  'NAME',  NULL),
    ('port',                     'PRS', 'BKD_ENS_Header_Submission', 'arrival_port',              'ENS_HEADER',  'NAME',  'Port name -> location code (e.g. GBAUBELBELBEL).'),
    /* transport_charges is a fixed QAS value (BKD=Y), not a /choice_values field - no map row. */

    /* --- Consignment (PRS.Consignment) --- */
    ('goods_domestic_status',    'PRS', 'Consignment', 'goods_domestic_status', 'CONSIGNMENT', 'VALUE', 'Single char (e.g. D).'),
    ('no_sfd_reason',            'PRS', 'Consignment', 'no_sfd_reason',         'CONSIGNMENT', 'VALUE', NULL),
    ('sfd_declaration_choice',   'PRS', 'Consignment', 'declaration_choice',    'CONSIGNMENT', 'VALUE', 'H1/H2/H3/H4.'),

    /* --- Goods Item (PRS.Goods_Item) --- */
    ('procedure_code',           'PRS', 'Goods_Item', 'procedure_code',           'GOODS_ITEM', 'VALUE', NULL),
    ('additional_procedure_code','PRS', 'Goods_Item', 'additional_procedure_code','GOODS_ITEM', 'VALUE', NULL),
    ('preference',               'PRS', 'Goods_Item', 'preference',               'GOODS_ITEM', 'VALUE', NULL),
    ('currency',                 'PRS', 'Goods_Item', 'item_invoice_currency',    'GOODS_ITEM', 'VALUE', NULL),
    ('valuation_method',         'PRS', 'Goods_Item', 'valuation_method',         'GOODS_ITEM', 'VALUE', NULL),
    ('valuation_indicator',      'PRS', 'Goods_Item', 'valuation_indicator',      'GOODS_ITEM', 'VALUE', NULL),
    ('nature_of_transaction',    'PRS', 'Goods_Item', 'nature_of_transaction',    'GOODS_ITEM', 'VALUE', NULL),
    ('commodity_code',           'PRS', 'Goods_Item', 'commodity_code',           'GOODS_ITEM', 'VALUE', NULL),
    ('country',                  'PRS', 'Goods_Item', 'country_of_origin',        'GOODS_ITEM', 'VALUE', NULL),
    ('country',                  'PRS', 'Goods_Item', 'country_of_preferential_origin', 'GOODS_ITEM', 'VALUE', NULL),
    ('ni_additional_information_code', 'PRS', 'Goods_Item', 'ni_additional_information_codes', 'GOODS_ITEM', 'VALUE', NULL)
) AS s (ChoiceField, SchemaName, TableName, ColumnName, EntityKind, MatchOn, Notes)
ON t.SchemaName = s.SchemaName AND t.TableName = s.TableName AND t.ColumnName = s.ColumnName AND t.ChoiceField = s.ChoiceField
WHEN MATCHED THEN UPDATE SET EntityKind=s.EntityKind, MatchOn=s.MatchOn, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ChoiceField, SchemaName, TableName, ColumnName, EntityKind, MatchOn, Notes)
    VALUES (s.ChoiceField, s.SchemaName, s.TableName, s.ColumnName, s.EntityKind, s.MatchOn, s.Notes);
GO

/* ------------------------------------------------------------------ */
/* Downloader controls (insert-only; operator-owned once set).         */
/* ------------------------------------------------------------------ */
MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('CHOICE_VALUES_ENV',     'PRD', 'STRING', 'Choice-values downloader: which TSS environment (CFG.TSS_Environment) to query. TST and PRD both serve choice_values; PRD is the authoritative reference set.'),
    ('CHOICE_VALUES_CLIENT',  'BKD', 'STRING', 'Choice-values downloader: which client credential (CFG.TSS_Credential) to authenticate with (reference data is client-agnostic; must be active for the chosen env).'),
    ('CHOICE_VALUES_PATH',    '/x_fhmrc_tss_api/v1/choice_values', 'STRING', 'Choice-values resource path appended to BaseUrl (which ends in /api). Per TSS API Reference v2.9.5: <base>/x_fhmrc_tss_api/v1/choice_values/<field>.'),
    ('CHOICE_VALUES_DRY_RUN', '0',   'BOOL',   'Choice-values downloader: 1/true = fetch + report only, write nothing.')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO

/* ------------------------------------------------------------------ */
/* Register the downloader as a job (if CFG.Job exists - file 12).     */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
    MERGE CFG.Job AS t
    USING (VALUES
        ('REF_FETCH_CHOICE_VALUES', 'Reference - Download TSS Choice Values', 'CONFIG', NULL, 'API', 'TASK', NULL, NULL,
         'Download every active TSS choice-value reference set (CFG.Choice_Field_Registry) via GET /choice_values/<field> and cache it in CFG.Choice_Value_Cache. CFG.Choice_Field_Map maps each set to the schema columns it governs. No CLI - controls from CFG.Application_Parameters (CHOICE_VALUES_*).',
         'fetch_choice_values:run', 'TSS GET /choice_values/<field>', 'CFG.Choice_Value_Cache',
         'Weekly + on demand before processing', 1, 'Reference-data bootstrap; client-agnostic.')
    ) AS s (JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
            Purpose, EntryPoint, InputSource, OutputTarget, Schedule, IsActive, Notes)
    ON t.JobCode = s.JobCode
    WHEN MATCHED THEN UPDATE SET JobName=s.JobName, Purpose=s.Purpose, EntryPoint=s.EntryPoint,
        InputSource=s.InputSource, OutputTarget=s.OutputTarget, Schedule=s.Schedule, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
    WHEN NOT MATCHED THEN INSERT (JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
            Purpose, EntryPoint, InputSource, OutputTarget, Schedule, IsActive, Notes)
        VALUES (s.JobCode, s.JobName, s.ModuleName, s.ClientCode, s.Channel, s.JobType, s.StepNo, s.ParentJobCode,
                s.Purpose, s.EntryPoint, s.InputSource, s.OutputTarget, s.Schedule, s.IsActive, s.Notes);
GO
