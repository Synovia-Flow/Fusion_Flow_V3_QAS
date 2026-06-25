/*
    FLOW V3 QAS DATABASE SETUP - FILE 3 OF 3

    Purpose:
        Load the initial QAS configuration for BKD, CWH and PLE.

    Run this after:
        001_create_schemas_and_tables.sql
        002_add_constraints_and_indexes.sql

    Safe to rerun:
        Yes. MERGE updates existing rows and inserts missing rows.

    Tenant state:
        BKD is active for the MVP vertical slice.
        CWH and PLE are configured but inactive until sender/source/templates are confirmed.

    Operational file store:
        \\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Integration_Layer

    Safety gates:
*/

/* Shared operational file store used by CFG path values. */
DECLARE @IntegrationLayerRoot nvarchar(1000) =
    N'\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Integration_Layer';
DECLARE @BKDRoot nvarchar(1000) = CONCAT(@IntegrationLayerRoot, N'\BKD');
DECLARE @CWHRoot nvarchar(1000) = CONCAT(@IntegrationLayerRoot, N'\CWH');
DECLARE @PLERoot nvarchar(1000) = CONCAT(@IntegrationLayerRoot, N'\PLE');
/* SECTION 1 - Tenants and default folders. */
MERGE CFG.Tenant AS target
USING (VALUES
    ('QAS', 'BKD', 'Birkdale', @BKDRoot, CONCAT(@BKDRoot, N'\Inbound\Sales_Order_files'), CONCAT(@BKDRoot, N'\Processed'), CONCAT(@BKDRoot, N'\Fails'), 1, 'Active first tenant. Email body is saved as ENS source text; generated API pack contains ENS PACK and DEC PACK sheets.'),
    ('QAS', 'CWH', 'Country Wide Homes', @CWHRoot, CONCAT(@CWHRoot, N'\Inbound\Sales_Order_files'), CONCAT(@CWHRoot, N'\Process'), CONCAT(@CWHRoot, N'\Fails'), 0, 'Tenant configured but inactive. Sender/source pending confirmation. MVP flow is existing ENS reference plus DEC PACK upload.'),
    ('QAS', 'PLE', 'Primeline Express', @PLERoot, CONCAT(@PLERoot, N'\Inbound\Sales_Order_files'), CONCAT(@PLERoot, N'\Process'), CONCAT(@PLERoot, N'\Fails'), 0, 'Tenant configured but inactive. Sender/source pending confirmation. MVP flow is existing ENS reference plus DEC PACK upload.')
) AS source (EnvCode, TenantCode, TenantName, IntegrationRoot, DefaultInboundFolder, ProcessFolder, FailFolder, IsActive, Notes)
ON target.EnvCode = source.EnvCode AND target.TenantCode = source.TenantCode
WHEN MATCHED THEN UPDATE SET
    TenantName = source.TenantName,
    IntegrationRoot = source.IntegrationRoot,
    DefaultInboundFolder = source.DefaultInboundFolder,
    ProcessFolder = source.ProcessFolder,
    FailFolder = source.FailFolder,
    IsActive = source.IsActive,
    Notes = source.Notes,
    UpdatedAt = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (EnvCode, TenantCode, TenantName, IntegrationRoot, DefaultInboundFolder, ProcessFolder, FailFolder, IsActive, Notes)
    VALUES (source.EnvCode, source.TenantCode, source.TenantName, source.IntegrationRoot, source.DefaultInboundFolder, source.ProcessFolder, source.FailFolder, source.IsActive, source.Notes);

/* SECTION 2 - Tenant runtime gates. */
MERGE CFG.TenantSetting AS target
USING (
    SELECT t.TenantID, v.*
    FROM (VALUES
    ) AS v (EnvCode, TenantCode, SettingKey, SettingValue, ValueType, IsActive, Notes)
    INNER JOIN CFG.Tenant t
      ON t.EnvCode = v.EnvCode
     AND t.TenantCode = v.TenantCode
) AS source
ON target.TenantID = source.TenantID AND target.SettingKey = source.SettingKey
WHEN MATCHED THEN UPDATE SET
    EnvCode = source.EnvCode,
    TenantCode = source.TenantCode,
    SettingValue = source.SettingValue,
    ValueType = source.ValueType,
    IsActive = source.IsActive,
    Notes = source.Notes,
    UpdatedAt = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (TenantID, EnvCode, TenantCode, SettingKey, SettingValue, ValueType, IsActive, Notes)
    VALUES (source.TenantID, source.EnvCode, source.TenantCode, source.SettingKey, source.SettingValue, source.ValueType, source.IsActive, source.Notes);

/* SECTION 3 - Graph email routes. */
MERGE CFG.IngestionRoute AS target
USING (
    SELECT t.TenantID, v.*
    FROM (VALUES
        ('QAS', 'BKD', 'GRAPH_SALES_ORDERS', 'GRAPH_EMAIL', 'nexus@synoviaflow.cloud', 'DOMAIN', 'birkdalesales.com', CONCAT(@BKDRoot, N'\Inbound\Sales_Order_files'), CONCAT(@BKDRoot, N'\Processed'), CONCAT(@BKDRoot, N'\Fails'), '.xlsx', 'ACTIVE', 1, 'BKD active Graph route.'),
        ('QAS', 'CWH', 'GRAPH_SALES_ORDERS', 'GRAPH_EMAIL', 'nexus@synoviaflow.cloud', 'TBD', 'TBD', CONCAT(@CWHRoot, N'\Inbound\Sales_Order_files'), CONCAT(@CWHRoot, N'\Process'), CONCAT(@CWHRoot, N'\Fails'), '.xlsx,.csv', 'PENDING_SENDER_RULE', 0, 'Countrywide sender/source pending confirmation.'),
        ('QAS', 'PLE', 'GRAPH_SALES_ORDERS', 'GRAPH_EMAIL', 'nexus@synoviaflow.cloud', 'TBD', 'TBD', CONCAT(@PLERoot, N'\Inbound\Sales_Order_files'), CONCAT(@PLERoot, N'\Process'), CONCAT(@PLERoot, N'\Fails'), '.xlsx,.csv', 'PENDING_SENDER_RULE', 0, 'Primeline sender/source pending confirmation.')
    ) AS v (EnvCode, TenantCode, RouteName, SourceType, Mailbox, SenderRuleType, SenderRule, DestinationFolder, ProcessFolder, FailFolder, AllowedFileTypes, RouteStatus, IsActive, Notes)
    INNER JOIN CFG.Tenant t
      ON t.EnvCode = v.EnvCode
     AND t.TenantCode = v.TenantCode
) AS source
ON target.EnvCode = source.EnvCode AND target.TenantCode = source.TenantCode AND target.RouteName = source.RouteName
WHEN MATCHED THEN UPDATE SET
    TenantID = source.TenantID,
    SourceType = source.SourceType,
    Mailbox = source.Mailbox,
    SenderRuleType = source.SenderRuleType,
    SenderRule = source.SenderRule,
    DestinationFolder = source.DestinationFolder,
    ProcessFolder = source.ProcessFolder,
    FailFolder = source.FailFolder,
    AllowedFileTypes = source.AllowedFileTypes,
    RouteStatus = source.RouteStatus,
    IsActive = source.IsActive,
    Notes = source.Notes,
    UpdatedAt = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (TenantID, EnvCode, TenantCode, RouteName, SourceType, Mailbox, SenderRuleType, SenderRule, DestinationFolder, ProcessFolder, FailFolder, AllowedFileTypes, RouteStatus, IsActive, Notes)
    VALUES (source.TenantID, source.EnvCode, source.TenantCode, source.RouteName, source.SourceType, source.Mailbox, source.SenderRuleType, source.SenderRule, source.DestinationFolder, source.ProcessFolder, source.FailFolder, source.AllowedFileTypes, source.RouteStatus, source.IsActive, source.Notes);

/* SECTION 4 - Pack rules: ENS_PACK and DEC_PACK. */
MERGE CFG.IngestionPackRule AS target
USING (
    SELECT r.RouteID, v.*
    FROM (VALUES
        ('QAS', 'BKD', 'GRAPH_SALES_ORDERS', 'ENS_PACK', 'EMAIL_BODY', 'XLSX', 'BKD_API_PACK_{dd.MM.yyyy}.xlsx', 'ENS PACK', CONCAT(@BKDRoot, N'\Processed'), 1, 'Email body saved as ENS text evidence and converted into the ENS PACK sheet.'),
        ('QAS', 'BKD', 'GRAPH_SALES_ORDERS', 'DEC_PACK', 'ATTACHMENT', 'XLSX', 'BKD_API_PACK_{dd.MM.yyyy}.xlsx', 'DEC PACK', CONCAT(@BKDRoot, N'\Processed'), 1, 'Sales order attachment rows are copied into the DEC PACK sheet of the generated API pack.'),
        ('QAS', 'CWH', 'GRAPH_SALES_ORDERS', 'DEC_PACK', 'ATTACHMENT', 'XLSX', 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', 'DEC PACK', CONCAT(@CWHRoot, N'\Process\DEC_PACK'), 0, 'Pending source confirmation; expected existing ENS reference plus consignment/goods upload.'),
        ('QAS', 'PLE', 'GRAPH_SALES_ORDERS', 'DEC_PACK', 'ATTACHMENT', 'XLSX', 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', 'DEC PACK', CONCAT(@PLERoot, N'\Process\DEC_PACK'), 0, 'Pending source confirmation; expected existing ENS reference plus consignment/goods upload.')
    ) AS v (EnvCode, TenantCode, RouteName, PackCode, SourcePart, OutputFormat, OutputFilePattern, SheetName, OutputFolder, IsActive, Notes)
    INNER JOIN CFG.IngestionRoute r
      ON r.EnvCode = v.EnvCode
     AND r.TenantCode = v.TenantCode
     AND r.RouteName = v.RouteName
) AS source
ON target.RouteID = source.RouteID AND target.PackCode = source.PackCode AND target.SourcePart = source.SourcePart
WHEN MATCHED THEN UPDATE SET
    OutputFormat = source.OutputFormat,
    OutputFilePattern = source.OutputFilePattern,
    SheetName = source.SheetName,
    OutputFolder = source.OutputFolder,
    IsActive = source.IsActive,
    Notes = source.Notes,
    UpdatedAt = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (RouteID, PackCode, SourcePart, OutputFormat, OutputFilePattern, SheetName, OutputFolder, IsActive, Notes)
    VALUES (source.RouteID, source.PackCode, source.SourcePart, source.OutputFormat, source.OutputFilePattern, source.SheetName, source.OutputFolder, source.IsActive, source.Notes);

/* SECTION 5 - Compatibility rows for the current Graph worker. */
MERGE CFG.Graph AS target
USING (
    SELECT t.TenantID, r.RouteID, v.*
    FROM (VALUES
        ('QAS', 'BKD', 'Birkdale', 'nexus@synoviaflow.cloud', 'birkdalesales.com', '.xlsx', CONCAT(@BKDRoot, N'\Inbound\Sales_Order_files'), CONCAT(@BKDRoot, N'\Processed'), CONCAT(@BKDRoot, N'\Fails'), 'email_body', 'TEST_API_ONLY', 1, 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', 'ENS PACK', 'DEC PACK', 'Active first tenant. Body text plus attachment generate the BKD API pack.'),
        ('QAS', 'CWH', 'Country Wide Homes', 'nexus@synoviaflow.cloud', 'TBD', '.xlsx,.csv', CONCAT(@CWHRoot, N'\Inbound\Sales_Order_files'), CONCAT(@CWHRoot, N'\Process'), CONCAT(@CWHRoot, N'\Fails'), 'existing_ens_reference', 'TEST_API_ONLY', 0, 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', NULL, 'DEC PACK', 'Inactive until sender/source is confirmed.'),
        ('QAS', 'PLE', 'Primeline Express', 'nexus@synoviaflow.cloud', 'TBD', '.xlsx,.csv', CONCAT(@PLERoot, N'\Inbound\Sales_Order_files'), CONCAT(@PLERoot, N'\Process'), CONCAT(@PLERoot, N'\Fails'), 'existing_ens_reference', 'TEST_API_ONLY', 0, 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', NULL, 'DEC PACK', 'Inactive until sender/source is confirmed.')
    ) AS v (EnvCode, TenantCode, TenantName, Mailbox, SenderRule, AllowedFileTypes, DestinationFolder, ProcessFolder, FailFolder, BodySourceForEns, ProcessingEnvironment, IsActive, OutputFilePattern, EnsSheetName, DecSheetName, Notes)
    INNER JOIN CFG.Tenant t
      ON t.EnvCode = v.EnvCode
     AND t.TenantCode = v.TenantCode
    INNER JOIN CFG.IngestionRoute r
      ON r.EnvCode = v.EnvCode
     AND r.TenantCode = v.TenantCode
     AND r.RouteName = 'GRAPH_SALES_ORDERS'
) AS source
ON target.EnvCode = source.EnvCode AND target.TenantCode = source.TenantCode
WHEN MATCHED THEN UPDATE SET
    TenantID = source.TenantID,
    RouteID = source.RouteID,
    TenantName = source.TenantName,
    Mailbox = source.Mailbox,
    SenderRule = source.SenderRule,
    AllowedFileTypes = source.AllowedFileTypes,
    DestinationFolder = source.DestinationFolder,
    ProcessFolder = source.ProcessFolder,
    FailFolder = source.FailFolder,
    BodySourceForEns = source.BodySourceForEns,
    ProcessingEnvironment = source.ProcessingEnvironment,
    IsActive = source.IsActive,
    OutputFilePattern = source.OutputFilePattern,
    EnsSheetName = source.EnsSheetName,
    DecSheetName = source.DecSheetName,
    Notes = source.Notes,
    UpdatedAt = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (TenantID, RouteID, EnvCode, TenantCode, TenantName, Mailbox, SenderRule, AllowedFileTypes, DestinationFolder, ProcessFolder, FailFolder, BodySourceForEns, ProcessingEnvironment, IsActive, OutputFilePattern, EnsSheetName, DecSheetName, Notes)
    VALUES (source.TenantID, source.RouteID, source.EnvCode, source.TenantCode, source.TenantName, source.Mailbox, source.SenderRule, source.AllowedFileTypes, source.DestinationFolder, source.ProcessFolder, source.FailFolder, source.BodySourceForEns, source.ProcessingEnvironment, source.IsActive, source.OutputFilePattern, source.EnsSheetName, source.DecSheetName, source.Notes);

/* SECTION 6 - Backfill compatibility links after MERGE. */
UPDATE g
SET TenantID = t.TenantID,
    RouteID = r.RouteID
FROM CFG.Graph g
INNER JOIN CFG.Tenant t
  ON t.EnvCode = g.EnvCode
 AND t.TenantCode = g.TenantCode
LEFT JOIN CFG.IngestionRoute r
  ON r.EnvCode = g.EnvCode
 AND r.TenantCode = g.TenantCode
 AND r.RouteName = 'GRAPH_SALES_ORDERS';
