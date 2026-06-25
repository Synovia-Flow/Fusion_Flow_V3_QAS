/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 3 OF 3
    =================================================
    Purpose : Seed the CFG layer for Release 1.

    Clients (confirmed):
        BKD  Birkdale          - ACTIVE pilot (Route A simplified procedure)
        CWD  CountryWide       - registered, inactive (sender/source pending)
        PLE  Primeline Express - agent (actAs), inactive (R2 fast-follow)

    Confirmed operational roots:
        Integration Layer : \\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Integration_Layer
                            with per-client subfolders, e.g. ...\Integration_Layer\BKD
        Reports/documents : \\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Documentation_Layer  (top level)

    Run after : 001_create_schemas.sql, 002_cfg_tables.sql
    Safe to rerun: Yes. Every block is a MERGE on the natural key.

    Secrets   : NO plaintext secrets here. CFG.Credentials stores the API username
                and a Key Vault secret REFERENCE only.

    >>> REVIEW BEFORE PROD: confirm the TSS API usernames, the PLE actAs id,
        and the CWD / PLE sender rules.
*/

/* Confirmed roots. */
DECLARE @IntRoot nvarchar(1000) = N'\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Integration_Layer';
DECLARE @DocRoot nvarchar(1000) = N'\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Documentation_Layer';
DECLARE @BKDRoot nvarchar(1000) = CONCAT(@IntRoot, N'\BKD');
DECLARE @CWDRoot nvarchar(1000) = CONCAT(@IntRoot, N'\CWD');
DECLARE @PLERoot nvarchar(1000) = CONCAT(@IntRoot, N'\PLE');

/* ================================================================== */
/* 1. Application parameters (all non-connection settings)             */
/* ================================================================== */
MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('DEFAULT_ENV',              'TEST',  'STRING', 'Default TSS environment (TEST|PROD).'),
    ('API_TIME_ZONE',            'UTC',   'STRING', 'All API datetimes are UTC.'),
    ('API_RATE_LIMIT_SECONDS',   '0.25',  'DECIMAL','Minimum seconds between production API calls (Rule 14).'),
    ('GMR_READ_WAIT_SECONDS',    '90',    'INT',    'Wait after GMR submit before reading gmr_id (Rule 19).'),
    ('API_VERSION_DEFAULT',      'NEW',   'STRING', 'Default API version for the New/Old switch.'),
    ('ARRIVAL_MAX_FUTURE_DAYS',  '14',    'INT',    'arrival_date_time max days in the future (Rule 4).'),
    ('SDI_DEADLINE_DAY',         '10',    'INT',    'Supplementary Declaration due by the 10th of month after arrival.'),
    ('INTEGRATION_LAYER_ROOT',   N'\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Integration_Layer', 'STRING', 'Root for per-client inbound/process/fail/archive subfolders.'),
    ('DOCUMENTATION_OUTPUT_ROOT',N'\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Documentation_Layer','STRING', 'Top-level output for reports/documents produced by scripts (Module 5).')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN MATCHED THEN UPDATE SET ParameterValue=s.ParameterValue, ValueType=s.ValueType, Description=s.Description, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);

/* ================================================================== */
/* 2. Clients                                                          */
/* ================================================================== */
MERGE CFG.Clients AS t
USING (VALUES
    ('BKD', 'Birkdale',          'BKD', 'BKD_', 'A', 0, NULL, 1, 'Active pilot client. Route A simplified procedure.'),
    ('CWD', 'CountryWide',       'CWD', 'CWD_', 'A', 0, NULL, 0, 'Registered, inactive. Sender/source pending confirmation.'),
    ('PLE', 'Primeline Express', 'PLE', 'PLE_', 'A', 1, NULL, 0, 'Agent (actAs) - R2 fast-follow. Inactive until actAs id confirmed.')
) AS s (ClientCode, ClientName, SchemaName, StgTablePrefix, DefaultRoute, IsAgent, ActAsSysId, IsActive, Notes)
ON t.ClientCode = s.ClientCode
WHEN MATCHED THEN UPDATE SET ClientName=s.ClientName, SchemaName=s.SchemaName, StgTablePrefix=s.StgTablePrefix,
    DefaultRoute=s.DefaultRoute, IsAgent=s.IsAgent, ActAsSysId=s.ActAsSysId, IsActive=s.IsActive, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, ClientName, SchemaName, StgTablePrefix, DefaultRoute, IsAgent, ActAsSysId, IsActive, Notes)
    VALUES (s.ClientCode, s.ClientName, s.SchemaName, s.StgTablePrefix, s.DefaultRoute, s.IsAgent, s.ActAsSysId, s.IsActive, s.Notes);

/* ================================================================== */
/* 3. Credentials (placeholders + Key Vault references; NO secrets)     */
/* ================================================================== */
MERGE CFG.Credentials AS t
USING (VALUES
    ('BKD', 'TEST', 'BASIC', 'API.TSS0000000', 'kv-fusionflow-qas/BKD-TSS-TEST', 1, 'Placeholder - set real username; secret lives in Key Vault.'),
    ('BKD', 'PROD', 'BASIC', 'API.TSS0000000', 'kv-fusionflow-prd/BKD-TSS-PROD', 0, 'Placeholder - enable at cutover.'),
    ('CWD', 'TEST', 'BASIC', 'API.TSS0000000', 'kv-fusionflow-qas/CWD-TSS-TEST', 0, 'Placeholder - inactive.'),
    ('PLE', 'TEST', 'BASIC', 'API.TSS0000000', 'kv-fusionflow-qas/PLE-TSS-TEST', 0, 'Placeholder - agent, inactive.')
) AS s (ClientCode, EnvCode, AuthType, ApiUsername, SecretRef, IsActive, Notes)
ON t.ClientCode = s.ClientCode AND t.EnvCode = s.EnvCode
WHEN MATCHED THEN UPDATE SET AuthType=s.AuthType, ApiUsername=s.ApiUsername, SecretRef=s.SecretRef, IsActive=s.IsActive, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, EnvCode, AuthType, ApiUsername, SecretRef, IsActive, Notes)
    VALUES (s.ClientCode, s.EnvCode, s.AuthType, s.ApiUsername, s.SecretRef, s.IsActive, s.Notes);

/* ================================================================== */
/* 4. Folder paths - per-client subfolders under the Integration root   */
/* ================================================================== */
MERGE CFG.Folder_Paths AS t
USING (VALUES
    ('BKD', 'INBOUND',    CONCAT(@BKDRoot, N'\Inbound\Sales_Order_files')),
    ('BKD', 'ENS_SOURCE', CONCAT(@BKDRoot, N'\Inbound\ENS_Source')),
    ('BKD', 'PROCESS',    CONCAT(@BKDRoot, N'\Process')),
    ('BKD', 'FAIL',       CONCAT(@BKDRoot, N'\Fails')),
    ('BKD', 'ARCHIVE',    CONCAT(@BKDRoot, N'\Archive')),
    ('CWD', 'INBOUND',    CONCAT(@CWDRoot, N'\Inbound\Sales_Order_files')),
    ('CWD', 'PROCESS',    CONCAT(@CWDRoot, N'\Process')),
    ('CWD', 'FAIL',       CONCAT(@CWDRoot, N'\Fails')),
    ('CWD', 'ARCHIVE',    CONCAT(@CWDRoot, N'\Archive')),
    ('PLE', 'INBOUND',    CONCAT(@PLERoot, N'\Inbound\Sales_Order_files')),
    ('PLE', 'PROCESS',    CONCAT(@PLERoot, N'\Process')),
    ('PLE', 'FAIL',       CONCAT(@PLERoot, N'\Fails')),
    ('PLE', 'ARCHIVE',    CONCAT(@PLERoot, N'\Archive'))
) AS s (ClientCode, PathType, PathValue)
ON t.ClientCode = s.ClientCode AND t.PathType = s.PathType
WHEN MATCHED THEN UPDATE SET PathValue=s.PathValue, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, PathType, PathValue) VALUES (s.ClientCode, s.PathType, s.PathValue);

/* ================================================================== */
/* 5. Email rules                                                      */
/* ================================================================== */
MERGE CFG.Email_Rules AS t
USING (VALUES
    ('BKD', N'nexus@synoviaflow.cloud', 'DOMAIN', N'birkdalesales.com', '.xlsx',      1, 'Active BKD inbound route.'),
    ('CWD', N'nexus@synoviaflow.cloud', 'DOMAIN', N'TBD',               '.xlsx,.csv', 0, 'Pending sender confirmation.'),
    ('PLE', N'nexus@synoviaflow.cloud', 'DOMAIN', N'TBD',               '.xlsx,.csv', 0, 'Pending sender confirmation.')
) AS s (ClientCode, Mailbox, SenderRuleType, SenderRule, AllowedFileTypes, IsActive, Notes)
ON t.ClientCode = s.ClientCode AND t.SenderRule = s.SenderRule
WHEN MATCHED THEN UPDATE SET Mailbox=s.Mailbox, SenderRuleType=s.SenderRuleType, AllowedFileTypes=s.AllowedFileTypes, IsActive=s.IsActive, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, Mailbox, SenderRuleType, SenderRule, AllowedFileTypes, IsActive, Notes)
    VALUES (s.ClientCode, s.Mailbox, s.SenderRuleType, s.SenderRule, s.AllowedFileTypes, s.IsActive, s.Notes);

/* ================================================================== */
/* 6. API version (New/Old switch) + base URLs                          */
/* ================================================================== */
MERGE CFG.API_Version AS t
USING (VALUES
    ('BKD', '*', 'NEW', N'https://api.tsstestenv.co.uk', N'https://api.tradersupportservice.co.uk'),
    ('CWD', '*', 'NEW', N'https://api.tsstestenv.co.uk', N'https://api.tradersupportservice.co.uk'),
    ('PLE', '*', 'NEW', N'https://api.tsstestenv.co.uk', N'https://api.tradersupportservice.co.uk')
) AS s (ClientCode, ResourceName, ApiVersion, BaseUrlTest, BaseUrlProd)
ON t.ClientCode = s.ClientCode AND t.ResourceName = s.ResourceName
WHEN MATCHED THEN UPDATE SET ApiVersion=s.ApiVersion, BaseUrlTest=s.BaseUrlTest, BaseUrlProd=s.BaseUrlProd, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, ResourceName, ApiVersion, BaseUrlTest, BaseUrlProd)
    VALUES (s.ClientCode, s.ResourceName, s.ApiVersion, s.BaseUrlTest, s.BaseUrlProd);

/* ================================================================== */
/* 7. API process map - BKD Route A ordered sequence                    */
/*    (Rule 1: /headers not /declaration_headers; Rule 19: GMR 2 calls   */
/*     + 90s wait; Rule 2: SFD lookup by consignment_number.)            */
/* ================================================================== */
MERGE CFG.API_Process_Map AS t
USING (VALUES
    ('BKD','A', 0,'Permission Grant',     '/permission_grant',                  'GET', 'read',   0,  'Pre-flight: validate importer EORI (result is a LIST - Rule 7).'),
    ('BKD','A', 1,'Declaration Header',    '/headers',                           'POST','create', 0,  'ENS000... Use /headers, never /declaration_headers (Rule 1).'),
    ('BKD','A', 2,'Consignment',           '/consignments',                      'POST','create', 0,  'DEC000... consignment_number blank on create.'),
    ('BKD','A', 3,'Goods Item',            '/goods',                             'POST','create', 0,  'ENS context. Loop up to 99 goods.'),
    ('BKD','A', 4,'Consignment',           '/consignments',                      'POST','submit', 0,  'Submits ENS; locks header; SFD auto-generates.'),
    ('BKD','A', 5,'SFD Consignment',       '/simplified_frontier_declarations',  'GET', 'lookup', 0,  'Lookup by consignment_number, NOT consignment_reference (Rule 2).'),
    ('BKD','A', 6,'GVMS GMR',              '/gvms_gmr',                          'POST','create', 0,  'GMR000...'),
    ('BKD','A', 7,'GVMS GMR',              '/gvms_gmr',                          'POST','submit', 0,  'Always create then submit (Rule 19).'),
    ('BKD','A', 8,'GVMS GMR',              '/gvms_gmr',                          'GET', 'read',   90, 'Read gmr_id after ~90s wait (Rule 19).'),
    ('BKD','A', 9,'Supplementary Dec.',    '/supplementary_declarations',        'GET', 'lookup', 0,  'Lookup SUP000... from sfd_number after ~10min post-arrival.'),
    ('BKD','A',10,'Goods Item',            '/goods',                             'GET', 'lookup', 0,  'SDI context - NEW goods_id, differs from ENS (Rule 15).'),
    ('BKD','A',11,'Goods Item',            '/goods',                             'POST','update', 0,  'Full customs valuation per line. Full-replacement payload (Rule 16).'),
    ('BKD','A',12,'Supplementary Dec.',    '/supplementary_declarations',        'POST','submit', 0,  'Submit SDI by the 10th of month after arrival.')
) AS s (ClientCode, RouteCode, StepNo, ResourceName, Endpoint, HttpMethod, OpType, WaitSeconds, Notes)
ON t.ClientCode = s.ClientCode AND t.RouteCode = s.RouteCode AND t.StepNo = s.StepNo
WHEN MATCHED THEN UPDATE SET ResourceName=s.ResourceName, Endpoint=s.Endpoint, HttpMethod=s.HttpMethod,
    OpType=s.OpType, WaitSeconds=s.WaitSeconds, Notes=s.Notes, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ClientCode, RouteCode, StepNo, ResourceName, Endpoint, HttpMethod, OpType, WaitSeconds, Notes)
    VALUES (s.ClientCode, s.RouteCode, s.StepNo, s.ResourceName, s.Endpoint, s.HttpMethod, s.OpType, s.WaitSeconds, s.Notes);

/* ================================================================== */
/* 8. Choice field registry - bootstrap list (GET /choice_values/<f>)   */
/* ================================================================== */
MERGE CFG.Choice_Field_Registry AS t
USING (VALUES
    ('country'),('movement_type'),('port'),('procedure_code'),('additional_procedure_code'),
    ('commodity_code'),('document_code'),('document_status'),('auth_type_code'),('previous_document_type'),
    ('additional_info_code'),('currency'),('sd_declaration_choice'),('ffd_declaration_choice'),('sfd_declaration_choice'),
    ('declaration_category'),('goods_domestic_status'),('incoterm'),('mode_of_transport'),('method_of_payment'),
    ('valuation_method'),('valuation_indicator'),('nature_of_transaction'),('no_sfd_reason'),('gvms_routes'),
    ('transport_document_type'),('passive_transport_types'),('load_type'),('cargo_or_consignment'),('final_destination_location_code'),
    ('guarantee_type'),('tax_base_unit'),('tax_type'),('preference'),('ni_additional_information_code')
) AS s (ChoiceField)
ON t.ChoiceField = s.ChoiceField
WHEN NOT MATCHED THEN INSERT (ChoiceField, ApiPath)
    VALUES (s.ChoiceField, CONCAT('/choice_values/', s.ChoiceField));

/* ================================================================== */
/* 9. Shared process/status vocabulary (Spec 3.3)                       */
/* ================================================================== */
MERGE CFG.Status_Vocabulary AS t
USING (VALUES
    ('INGESTING',   'INGESTED',     'Raw artefact landed verbatim in ING.',                  10, 0, 0),
    ('NORMALISING', 'NORMALISED',   'Formats/dates/codes standardised in PRS.',              20, 0, 0),
    ('ENRICHING',   'ENRICHED',     'Lookups, choice values and client QAS rules applied.',  30, 0, 0),
    ('CONSTRUCTING','CONSTRUCTED',  'Canonical base submission object assembled in PRS.',     40, 0, 0),
    ('VALIDATING',  'VALIDATED',    'Mandatory/conditional rules passed.',                    50, 0, 0),
    ('VALIDATING',  'REJECTED',     'Validation failed with reason.',                         51, 0, 1),
    ('STAGING',     'STAGED',       'Materialised into STG.',                                 60, 0, 0),
    ('STAGING',     'READY',        'Staged and ready to submit.',                            61, 0, 0),
    ('LINKING',     'LINKED',       'Goods -> consignment -> header bound.',                  70, 0, 0),
    ('SUBMITTING',  'SUBMITTING',   'TSS API calls in progress.',                             80, 0, 0),
    ('SUBMITTING',  'SUBMITTED',    'TSS API calls executed; reference returned.',            81, 0, 0),
    ('MONITORING',  'ACKNOWLEDGED', 'TSS acknowledged; downstream artefacts appearing.',      90, 0, 0),
    ('MONITORING',  'IN_PROGRESS',  'Polling TSS for downstream artefacts.',                  91, 0, 0),
    ('RECONCILING', 'RECONCILED',   'Pulled-back record matches submitted payload.',         100, 0, 0),
    ('RECONCILING', 'MISMATCH',     'Pulled-back record diverges from submitted payload.',   101, 0, 1),
    ('ARCHIVING',   'ARCHIVED',     'Reconciled terminal record copied to ARC.',             110, 1, 0),
    (NULL,          'ERROR',        'Exception state; requires operator action.',            900, 0, 1),
    (NULL,          'CANCELLED',    'Movement cancelled.',                                   910, 1, 1),
    (NULL,          'ON_HOLD',      'Held pending operator action.',                         920, 0, 1)
) AS s (ProcessName, ResultStatus, Meaning, SortOrder, IsTerminal, IsException)
ON t.ResultStatus = s.ResultStatus
WHEN MATCHED THEN UPDATE SET ProcessName=s.ProcessName, Meaning=s.Meaning, SortOrder=s.SortOrder,
    IsTerminal=s.IsTerminal, IsException=s.IsException
WHEN NOT MATCHED THEN INSERT (ProcessName, ResultStatus, Meaning, SortOrder, IsTerminal, IsException)
    VALUES (s.ProcessName, s.ResultStatus, s.Meaning, s.SortOrder, s.IsTerminal, s.IsException);
GO
