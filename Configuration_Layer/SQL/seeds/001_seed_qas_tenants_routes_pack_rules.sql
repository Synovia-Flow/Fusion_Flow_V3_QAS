/*
    Fusion_Flow_V3_QAS SQL migration.

    Purpose:
        Seed QAS tenant, route, pack-rule and runtime gate configuration for BKD, CWH and PLE.

    Run order:
        Execute files in numeric filename order. Scripts are idempotent
        where practical so QAS can be refreshed safely.
*/
MERGE CFG.Tenant AS target
USING (VALUES
    ('QAS', 'BKD', 'Birkdale', 'Integration_Layer\BKD', 'Integration_Layer\BKD\Inbound\Sales_Order_files', 'Integration_Layer\BKD\Process', 'Integration_Layer\BKD\Fails', 1, 'Active first tenant. Email body is saved as ENS source text; generated API pack contains ENS PACK and DEC PACK sheets.'),
    ('QAS', 'CWH', 'Country Wide Homes', 'Integration_Layer\CWH', 'Integration_Layer\CWH\Inbound\Sales_Order_files', 'Integration_Layer\CWH\Process', 'Integration_Layer\CWH\Fails', 0, 'Tenant configured but inactive. Sender/source pending confirmation. MVP flow is existing ENS reference plus DEC PACK upload.'),
    ('QAS', 'PLE', 'Primeline Express', 'Integration_Layer\PLE', 'Integration_Layer\PLE\Inbound\Sales_Order_files', 'Integration_Layer\PLE\Process', 'Integration_Layer\PLE\Fails', 0, 'Tenant configured but inactive. Sender/source pending confirmation. MVP flow is existing ENS reference plus DEC PACK upload.')
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
GO
MERGE CFG.TenantSetting AS target
USING (
    SELECT t.TenantID, v.*
    FROM (VALUES
        ('QAS', 'BKD', 'TSS_ENVIRONMENT', 'QAS', 'STRING', 1, 'QAS TSS target environment label.'),
        ('QAS', 'BKD', 'TSS_DRY_RUN', 'true', 'BOOLEAN', 1, 'QAS default. Submit jobs must dry-run unless explicitly changed.'),
        ('QAS', 'BKD', 'TSS_SUBMIT_ENABLED', 'false', 'BOOLEAN', 1, 'Live TSS submit gate. Must remain false until signed off.'),
        ('QAS', 'CWH', 'TSS_ENVIRONMENT', 'QAS', 'STRING', 1, 'QAS TSS target environment label.'),
        ('QAS', 'CWH', 'TSS_DRY_RUN', 'true', 'BOOLEAN', 1, 'QAS default. Submit jobs must dry-run unless explicitly changed.'),
        ('QAS', 'CWH', 'TSS_SUBMIT_ENABLED', 'false', 'BOOLEAN', 1, 'Live TSS submit gate. Must remain false until signed off.'),
        ('QAS', 'PLE', 'TSS_ENVIRONMENT', 'QAS', 'STRING', 1, 'QAS TSS target environment label.'),
        ('QAS', 'PLE', 'TSS_DRY_RUN', 'true', 'BOOLEAN', 1, 'QAS default. Submit jobs must dry-run unless explicitly changed.'),
        ('QAS', 'PLE', 'TSS_SUBMIT_ENABLED', 'false', 'BOOLEAN', 1, 'Live TSS submit gate. Must remain false until signed off.')
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
GO
MERGE CFG.IngestionRoute AS target
USING (
    SELECT t.TenantID, v.*
    FROM (VALUES
        ('QAS', 'BKD', 'GRAPH_SALES_ORDERS', 'GRAPH_EMAIL', 'nexus@synoviaflow.cloud', 'DOMAIN', 'birkdalesales.com', 'Integration_Layer\BKD\Inbound\Sales_Order_files', 'Integration_Layer\BKD\Process', 'Integration_Layer\BKD\Fails', '.xlsx', 'ACTIVE', 1, 'BKD active Graph route.'),
        ('QAS', 'CWH', 'GRAPH_SALES_ORDERS', 'GRAPH_EMAIL', 'nexus@synoviaflow.cloud', 'TBD', 'TBD', 'Integration_Layer\CWH\Inbound\Sales_Order_files', 'Integration_Layer\CWH\Process', 'Integration_Layer\CWH\Fails', '.xlsx,.csv', 'PENDING_SENDER_RULE', 0, 'Countrywide sender/source pending confirmation.'),
        ('QAS', 'PLE', 'GRAPH_SALES_ORDERS', 'GRAPH_EMAIL', 'nexus@synoviaflow.cloud', 'TBD', 'TBD', 'Integration_Layer\PLE\Inbound\Sales_Order_files', 'Integration_Layer\PLE\Process', 'Integration_Layer\PLE\Fails', '.xlsx,.csv', 'PENDING_SENDER_RULE', 0, 'Primeline sender/source pending confirmation.')
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
GO

MERGE CFG.IngestionPackRule AS target
USING (
    SELECT r.RouteID, v.*
    FROM (VALUES
        ('QAS', 'BKD', 'GRAPH_SALES_ORDERS', 'ENS_PACK', 'EMAIL_BODY', 'XLSX', 'BKD_API_PACK_{dd.MM.yyyy}_{message_id_short}.xlsx', 'ENS PACK', 'Integration_Layer\BKD\Process', 1, 'Email body saved as ENS text evidence and converted into the ENS PACK sheet.'),
        ('QAS', 'BKD', 'GRAPH_SALES_ORDERS', 'DEC_PACK', 'ATTACHMENT', 'XLSX', 'BKD_API_PACK_{dd.MM.yyyy}_{message_id_short}.xlsx', 'DEC PACK', 'Integration_Layer\BKD\Process', 1, 'Sales order attachment rows are copied into the DEC PACK sheet of the generated API pack.'),
        ('QAS', 'CWH', 'GRAPH_SALES_ORDERS', 'DEC_PACK', 'ATTACHMENT', 'XLSX', 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', 'DEC PACK', 'Integration_Layer\CWH\Process\DEC_PACK', 0, 'Pending source confirmation; expected existing ENS reference plus consignment/goods upload.'),
        ('QAS', 'PLE', 'GRAPH_SALES_ORDERS', 'DEC_PACK', 'ATTACHMENT', 'XLSX', 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', 'DEC PACK', 'Integration_Layer\PLE\Process\DEC_PACK', 0, 'Pending source confirmation; expected existing ENS reference plus consignment/goods upload.')
    ) AS v (EnvCode, TenantCode, RouteName, PackCode, SourcePart, OutputFormat, OutputFilePattern, SheetName, CsvFolder, IsActive, Notes)
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
    CsvFolder = source.CsvFolder,
    IsActive = source.IsActive,
    Notes = source.Notes,
    UpdatedAt = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (RouteID, PackCode, SourcePart, OutputFormat, OutputFilePattern, SheetName, CsvFolder, IsActive, Notes)
    VALUES (source.RouteID, source.PackCode, source.SourcePart, source.OutputFormat, source.OutputFilePattern, source.SheetName, source.CsvFolder, source.IsActive, source.Notes);
GO

MERGE CFG.Graph AS target
USING (
    SELECT t.TenantID, r.RouteID, v.*
    FROM (VALUES
        ('QAS', 'BKD', 'Birkdale', 'nexus@synoviaflow.cloud', 'birkdalesales.com', '.xlsx', 'Integration_Layer\BKD\Inbound\Sales_Order_files', 'Integration_Layer\BKD\Process', 'Integration_Layer\BKD\Fails', 'email_body', 'TEST_API_ONLY', 1, 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', 'ENS PACK', 'DEC PACK', 'Active first tenant. Body text plus attachment generate the BKD API pack.'),
        ('QAS', 'CWH', 'Country Wide Homes', 'nexus@synoviaflow.cloud', 'TBD', '.xlsx,.csv', 'Integration_Layer\CWH\Inbound\Sales_Order_files', 'Integration_Layer\CWH\Process', 'Integration_Layer\CWH\Fails', 'existing_ens_reference', 'TEST_API_ONLY', 0, 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', NULL, 'DEC PACK', 'Inactive until sender/source is confirmed.'),
        ('QAS', 'PLE', 'Primeline Express', 'nexus@synoviaflow.cloud', 'TBD', '.xlsx,.csv', 'Integration_Layer\PLE\Inbound\Sales_Order_files', 'Integration_Layer\PLE\Process', 'Integration_Layer\PLE\Fails', 'existing_ens_reference', 'TEST_API_ONLY', 0, 'Sales Orders Synovia_{dd.MM.yyyy}.xlsx', NULL, 'DEC PACK', 'Inactive until sender/source is confirmed.')
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
GO

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
GO
