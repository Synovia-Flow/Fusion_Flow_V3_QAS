/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 19 OF N
    =================================================
    Purpose : Config-driven processing - so the SAME engine processes every
              principal even though their ING source tables and rules differ.

              CFG.Processing_Profile    - per client+entity: which ING table is the
                                          source, which PRS tables are the targets,
                                          the source key -> MovementKey.
              CFG.Processing_Field_Map  - per client+entity+target field: where it
                                          comes from and how it is transformed
                                          (CONST / PASSTHROUGH / CODE / YESNO /
                                          DATE_UTC / CHOICE / MASTER_ENRICH / QAS /
                                          READONLY / API_RETURN / DERIVE), whether it
                                          is mandatory (and the condition), and its
                                          max length.
              CFG.Carrier_Master        - carrier address block keyed by EORI, for
                                          MASTER_ENRICH (e.g. carrier_name/city...).

              Seeds the BKD ENS_HEADER profile + field map from the agreed mapping.

    Run after : 013 (PRS BKD ENS tables), 014-016 (choice map).
    Safe to rerun: Yes (MERGE).
    Engine : Modules/Processing/process_engine.py
*/

/* ================================================================== */
/* 1. CFG.Processing_Profile                                           */
/* ================================================================== */
IF OBJECT_ID('CFG.Processing_Profile', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Processing_Profile (
        ProfileID       int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Proc_Profile PRIMARY KEY,
        ClientCode      char(3)       NOT NULL,
        EntityKind      varchar(20)   NOT NULL,          -- ENS_HEADER | CONSIGNMENT | GOODS_ITEM
        SourceSchema    varchar(20)   NOT NULL,          -- ING
        SourceTable     nvarchar(128) NOT NULL,          -- BKD_Raw_ENS
        SourceKeyColumn nvarchar(64)  NOT NULL,          -- DedupKey -> MovementKey
        SourceIdColumn  nvarchar(64)  NOT NULL,          -- LoadID (provenance / dedup)
        TargetSchema    varchar(20)   NOT NULL,          -- PRS
        TargetTable     nvarchar(128) NOT NULL,          -- BKD_ENS_Header_Submission
        TrackingTable   nvarchar(128) NOT NULL,          -- BKD_ENS_Header_Tracking
        IsActive        bit NOT NULL CONSTRAINT DF_CFG_Proc_Profile_IsActive DEFAULT (1),
        Notes           nvarchar(300) NULL,
        UpdatedAt       datetime2(3) NOT NULL CONSTRAINT DF_CFG_Proc_Profile_Updated DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Proc_Profile UNIQUE (ClientCode, EntityKind)
    );
END;
GO

/* ================================================================== */
/* 2. CFG.Processing_Field_Map                                         */
/* ================================================================== */
IF OBJECT_ID('CFG.Processing_Field_Map', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Processing_Field_Map (
        MapID         int IDENTITY(1,1) NOT NULL CONSTRAINT PK_CFG_Proc_Field PRIMARY KEY,
        ClientCode    char(3)       NOT NULL,
        EntityKind    varchar(20)   NOT NULL,
        TargetField   varchar(64)   NOT NULL,            -- TSS field / PRS column
        SourceColumn  varchar(64)   NULL,                -- ING column (NULL for CONST/MASTER_ENRICH/DERIVE)
        TransformType varchar(20)   NOT NULL,            -- see CHECK below
        ChoiceField   varchar(80)   NULL,                -- CV set for CHOICE / master name for MASTER_ENRICH
        ConstValue    nvarchar(200) NULL,                -- for CONST / QAS
        LookupKey     varchar(64)   NULL,                -- join field for MASTER_ENRICH (e.g. carrier_eori)
        MatchOn       varchar(10)   NULL,                -- NAME | VALUE (for CHOICE)
        Mandatory     varchar(4)    NOT NULL CONSTRAINT DF_CFG_Proc_Field_Mand DEFAULT ('NO'),  -- YES|COND|NO|READ
        CondExpression nvarchar(200) NULL,               -- e.g. movement_type=3a  /  movement_type IN (1a,3a,3b)
        MaxLen        int           NULL,
        StepNo        int           NOT NULL CONSTRAINT DF_CFG_Proc_Field_Step DEFAULT (100),
        RuleRef       varchar(40)   NULL,
        IsActive      bit NOT NULL CONSTRAINT DF_CFG_Proc_Field_IsActive DEFAULT (1),
        Notes         nvarchar(300) NULL,
        UpdatedAt     datetime2(3) NOT NULL CONSTRAINT DF_CFG_Proc_Field_Updated DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_CFG_Proc_Field UNIQUE (ClientCode, EntityKind, TargetField),
        CONSTRAINT CK_CFG_Proc_Field_Transform CHECK (TransformType IN
            ('CONST','PASSTHROUGH','CODE','YESNO','DATE_UTC','CHOICE','MASTER_ENRICH','QAS','READONLY','API_RETURN','DERIVE')),
        CONSTRAINT CK_CFG_Proc_Field_Mand CHECK (Mandatory IN ('YES','COND','NO','READ'))
    );
END;
GO

/* ================================================================== */
/* 3. CFG.Carrier_Master (for MASTER_ENRICH of the carrier block)      */
/* ================================================================== */
IF OBJECT_ID('CFG.Carrier_Master', 'U') IS NULL
BEGIN
    CREATE TABLE CFG.Carrier_Master (
        Eori            varchar(20)  NOT NULL CONSTRAINT PK_CFG_Carrier_Master PRIMARY KEY,
        CarrierName     nvarchar(35) NULL,
        StreetAndNumber nvarchar(35) NULL,
        City            nvarchar(35) NULL,
        Postcode        nvarchar(9)  NULL,
        Country         char(2)      NULL,
        IsActive        bit NOT NULL CONSTRAINT DF_CFG_Carrier_IsActive DEFAULT (1),
        UpdatedAt       datetime2(3) NOT NULL CONSTRAINT DF_CFG_Carrier_Updated DEFAULT (SYSUTCDATETIME())
    );
END;
GO

/* Seed known carrier (from the worked example). Add BKD's carrier when known. */
MERGE CFG.Carrier_Master AS t
USING (VALUES
    ('XI894542681000', 'Primeline Express Limited', 'Unit 14, Eagle Park Drive', 'Warrington', 'WA2 8JA', 'GB')
) AS s (Eori, CarrierName, StreetAndNumber, City, Postcode, Country)
ON t.Eori = s.Eori
WHEN MATCHED THEN UPDATE SET CarrierName=s.CarrierName, StreetAndNumber=s.StreetAndNumber,
    City=s.City, Postcode=s.Postcode, Country=s.Country, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (Eori, CarrierName, StreetAndNumber, City, Postcode, Country)
    VALUES (s.Eori, s.CarrierName, s.StreetAndNumber, s.City, s.Postcode, s.Country);
GO

/* ================================================================== */
/* 4. BKD ENS_HEADER profile                                           */
/* ================================================================== */
MERGE CFG.Processing_Profile AS t
USING (VALUES
    ('BKD', 'ENS_HEADER', 'ING', 'BKD_Raw_ENS', 'DedupKey', 'LoadID',
     'PRS', 'BKD_ENS_Header_Submission', 'BKD_ENS_Header_Tracking', 'Birkdale ENS Declaration Header')
) AS s (ClientCode, EntityKind, SourceSchema, SourceTable, SourceKeyColumn, SourceIdColumn,
        TargetSchema, TargetTable, TrackingTable, Notes)
ON t.ClientCode = s.ClientCode AND t.EntityKind = s.EntityKind
WHEN MATCHED THEN UPDATE SET SourceSchema=s.SourceSchema, SourceTable=s.SourceTable,
    SourceKeyColumn=s.SourceKeyColumn, SourceIdColumn=s.SourceIdColumn, TargetSchema=s.TargetSchema,
    TargetTable=s.TargetTable, TrackingTable=s.TrackingTable, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, EntityKind, SourceSchema, SourceTable, SourceKeyColumn, SourceIdColumn,
        TargetSchema, TargetTable, TrackingTable, Notes)
    VALUES (s.ClientCode, s.EntityKind, s.SourceSchema, s.SourceTable, s.SourceKeyColumn, s.SourceIdColumn,
        s.TargetSchema, s.TargetTable, s.TrackingTable, s.Notes);
GO

/* ================================================================== */
/* 5. BKD ENS_HEADER field map (from the agreed mapping)               */
/* ================================================================== */
MERGE CFG.Processing_Field_Map AS t
USING (VALUES
 -- TargetField, SourceColumn, TransformType, ChoiceField, ConstValue, LookupKey, MatchOn, Mandatory, CondExpression, MaxLen, StepNo, RuleRef
 ('op_type',                             NULL,                                  'CONST',        NULL,                     'create', NULL, NULL, 'YES',  NULL,                                    10, 10, NULL),
 ('declaration_number',                  NULL,                                  'API_RETURN',   NULL,                     NULL,     NULL, NULL, 'COND', 'op_type=update',                        40, 20, 'Rule 17'),
 ('movement_type',                       'movement_type',                       'CHOICE',       'movement_type',          NULL,     NULL, 'NAME','YES',  NULL,                                    40, 30, NULL),
 ('type_of_passive_transport',           'type_of_passive_transport',           'CHOICE',       'passive_transport_types',NULL,     NULL, 'NAME','COND', 'movement_type=3a',                      40, 40, 'Rule 3'),
 ('identity_no_of_transport',            'identity_no_of_transport',            'PASSTHROUGH',  NULL,                     NULL,     NULL, NULL, 'YES',  NULL,                                    27, 50, NULL),
 ('nationality_of_transport',            'nationality_of_transport',            'CHOICE',       'country',                NULL,     NULL, 'NAME','YES',  NULL,                                     2, 60, NULL),
 ('conveyance_ref',                      'conveyance_ref',                      'PASSTHROUGH',  NULL,                     NULL,     NULL, NULL, 'COND', 'movement_type=2a',                      35, 70, NULL),
 ('arrival_date_time',                   'arrival_date_time',                   'DATE_UTC',     NULL,                     NULL,     NULL, NULL, 'YES',  NULL,                                    25, 80, 'Rule 4'),
 ('arrival_port',                        'arrival_port',                        'CHOICE',       'port',                   NULL,     NULL, 'NAME','YES',  NULL,                                   200, 90, 'Rule 12'),
 ('place_of_loading',                    'place_of_loading',                    'PASSTHROUGH',  NULL,                     NULL,     NULL, NULL, 'YES',  NULL,                                    33,100, NULL),
 ('place_of_unloading',                  'place_of_unloading',                  'PASSTHROUGH',  NULL,                     NULL,     NULL, NULL, 'YES',  NULL,                                    33,110, NULL),
 ('place_of_acceptance_same_as_loading', 'place_of_acceptance_same_as_loading', 'YESNO',        NULL,                     NULL,     NULL, NULL, 'COND', 'movement_type=3a',                       3,120, 'Rule 3'),
 ('place_of_acceptance',                 'place_of_acceptance',                 'PASSTHROUGH',  NULL,                     NULL,     NULL, NULL, 'COND', 'place_of_acceptance_same_as_loading=no', 33,130, NULL),
 ('place_of_delivery_same_as_unloading', 'place_of_delivery_same_as_unloading', 'YESNO',        NULL,                     NULL,     NULL, NULL, 'COND', 'movement_type=3a',                       3,140, 'Rule 3'),
 ('place_of_delivery',                   'place_of_delivery',                   'PASSTHROUGH',  NULL,                     NULL,     NULL, NULL, 'COND', 'place_of_delivery_same_as_unloading=no', 33,150, NULL),
 ('seal_number',                         'seal_number',                         'PASSTHROUGH',  NULL,                     NULL,     NULL, NULL, 'NO',   NULL,                                    20,160, NULL),
 ('route',                               NULL,                                  'READONLY',     NULL,                     NULL,     NULL, NULL, 'READ', NULL,                                    20,170, NULL),
 ('transport_charges',                   NULL,                                  'QAS',          NULL,                     'Y',      NULL, NULL, 'YES',  NULL,                                    40,180, 'Rule 11'),
 ('carrier_eori',                        'carrier_eori',                        'CODE',         NULL,                     NULL,     NULL, NULL, 'YES',  NULL,                                   200,190, NULL),
 ('carrier_name',                        NULL,                                  'MASTER_ENRICH','Carrier_Master',         'CarrierName',     'carrier_eori', NULL, 'COND', 'movement_type IN (1a,3a,3b)',  35,200, NULL),
 ('carrier_street_number',               NULL,                                  'MASTER_ENRICH','Carrier_Master',         'StreetAndNumber', 'carrier_eori', NULL, 'COND', 'movement_type IN (1a,3a,3b)',  35,210, NULL),
 ('carrier_city',                        NULL,                                  'MASTER_ENRICH','Carrier_Master',         'City',            'carrier_eori', NULL, 'COND', 'movement_type IN (1a,3a,3b)',  35,220, NULL),
 ('carrier_postcode',                    NULL,                                  'MASTER_ENRICH','Carrier_Master',         'Postcode',        'carrier_eori', NULL, 'COND', 'movement_type IN (1a,3a,3b)',   9,230, NULL),
 ('carrier_country',                     NULL,                                  'MASTER_ENRICH','Carrier_Master',         'Country',         'carrier_eori', NULL, 'COND', 'movement_type IN (1a,3a,3b)',   2,240, NULL),
 ('haulier_eori',                        'haulier_eori',                        'CODE',         NULL,                     NULL,     NULL, NULL, 'NO',   NULL,                                   200,250, NULL),
 ('Tss_Status',                          NULL,                                  'READONLY',     NULL,                     NULL,     NULL, NULL, 'READ', NULL,                                    40,260, NULL)
) AS s (TargetField, SourceColumn, TransformType, ChoiceField, ConstValue, LookupKey, MatchOn, Mandatory, CondExpression, MaxLen, StepNo, RuleRef)
ON t.ClientCode = 'BKD' AND t.EntityKind = 'ENS_HEADER' AND t.TargetField = s.TargetField
WHEN MATCHED THEN UPDATE SET SourceColumn=s.SourceColumn, TransformType=s.TransformType, ChoiceField=s.ChoiceField,
    ConstValue=s.ConstValue, LookupKey=s.LookupKey, MatchOn=s.MatchOn, Mandatory=s.Mandatory,
    CondExpression=s.CondExpression, MaxLen=s.MaxLen, StepNo=s.StepNo, RuleRef=s.RuleRef, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, EntityKind, TargetField, SourceColumn, TransformType, ChoiceField,
    ConstValue, LookupKey, MatchOn, Mandatory, CondExpression, MaxLen, StepNo, RuleRef)
    VALUES ('BKD', 'ENS_HEADER', s.TargetField, s.SourceColumn, s.TransformType, s.ChoiceField,
    s.ConstValue, s.LookupKey, s.MatchOn, s.Mandatory, s.CondExpression, s.MaxLen, s.StepNo, s.RuleRef);
GO

/* ================================================================== */
/* 6. Register the engine as a job                                     */
/* ================================================================== */
IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
    MERGE CFG.Job AS t
    USING (VALUES
        ('PRS_ENGINE_BKD_ENS', 'Process - Birkdale ENS Header (engine)', 'DATA_PROCESSING', 'BKD', NULL, 'TASK', NULL, NULL,
         'Config-driven processing engine: reads CFG.Processing_Profile + CFG.Processing_Field_Map for BKD/ENS_HEADER, transforms ING.BKD_Raw_ENS into PRS.BKD_ENS_Header_Submission (choice lookups, dates, QAS, carrier enrich), validates, and tracks every change in EXC.Data_Processing_Enhancement. Reusable for any client by seeding its profile + field map.',
         'process_engine:run', 'ING.BKD_Raw_ENS (per profile)', 'PRS.BKD_ENS_Header_Submission / _Tracking',
         'After each ingestion cycle', 1, 'Supersedes the hard-coded stage_bkd_ens_header.py.')
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
