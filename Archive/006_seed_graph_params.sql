/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 6 OF N
    =================================================
    Purpose : Seed the Microsoft Graph (mail ingestion) application parameters
              into CFG.Application_Parameters, from the registered app
              "Fusion Flow Mail Reader" (Inbound/Manifest_V2.json).

    Run after : 002_cfg_tables.sql (and ideally 003_seed_cfg.sql)
    Safe to rerun: Yes.

    DESIGN DECISION:
      All Graph configuration - including the client secret and tenant id - is
      held in CFG.Application_Parameters (single config surface). This committed
      script seeds only PLACEHOLDERS for the secret and tenant; set the real
      values in the database (see the UPDATE statements at the bottom). The
      operator-managed rows are seeded ONLY IF MISSING and are never overwritten
      on re-deploy, so re-running this script will not clobber the real values.
*/

/* 1) Stable Graph metadata - safe to refresh on every deploy. */
MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('GRAPH_APP_NAME',           'Fusion Flow Mail Reader',                  'STRING',  'Registered Entra app (display name).'),
    ('GRAPH_APP_OBJECT_ID',      'f46a9a16-ba01-4621-ac68-bc8a0450fd34',     'GUID',    'Entra application object id.'),
    ('GRAPH_CLIENT_ID',          '4cbaa4be-f78f-4e82-944a-bf9d4142a12e',     'GUID',    'Application (client) id - appId.'),
    ('GRAPH_TENANT_DOMAIN',      'NETORG6546503.onmicrosoft.com',            'STRING',  'Publisher/tenant domain from the manifest.'),
    ('GRAPH_SECRET_KEY_ID',      '63c5bb67-a862-46ff-b929-0159092045ac',     'GUID',    'Manifest passwordCredentials keyId (metadata only).'),
    ('GRAPH_SECRET_HINT',        '3w5',                                      'STRING',  'Secret hint from the manifest (metadata only).'),
    ('GRAPH_SECRET_EXPIRY',      '2028-05-04',                               'DATE',    'Client secret expiry - rotate before this date.'),
    ('GRAPH_PERMISSIONS',        'Mail.Read,Mail.ReadWrite',                 'STRING',  'Application permissions granted (admin consent required).'),
    ('GRAPH_MAILBOX',            'nexus@synoviaflow.cloud',                  'STRING',  'Mailbox (UPN) ingestion reads from.'),
    ('GRAPH_PROCESSED_FOLDER',   'Fusion_Processed',                         'STRING',  'Folder beneath Inbox to move scanned messages into.'),
    ('GRAPH_FORWARDERS',         'nexus@synoviaintegration.com,aidan.harrington@synoviadigital.com', 'STRING', 'Forwarder addresses whose true sender lives in the forwarded body.'),
    ('GRAPH_AUTHORITY',          'https://login.microsoftonline.com/',       'STRING',  'MSAL authority base (append tenant id).'),
    ('GRAPH_SCOPE',              'https://graph.microsoft.com/.default',     'STRING',  'App-only client-credentials scope.')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN MATCHED THEN UPDATE SET ParameterValue=s.ParameterValue, ValueType=s.ValueType, Description=s.Description, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO

/* 2) Operator-managed values: seed PLACEHOLDERS only if the row does not exist.
      No WHEN MATCHED clause -> a re-deploy never overwrites a real value you set. */
MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('GRAPH_TENANT_ID',      '<SET_TENANT_GUID>',    'GUID',   'Entra tenant id (GUID or *.onmicrosoft.com). Set the real value in the DB.'),
    ('GRAPH_CLIENT_SECRET',  '<SET_CLIENT_SECRET>',  'SECRET', 'Graph app client secret (stored in the table by design). Set the real value in the DB.'),
    ('GRAPH_CLIENT_SECRET_REF','kv-fusionflow-qas/GRAPH-CLIENT-SECRET', 'SECRET_REF', 'Optional Key Vault fallback if GRAPH_CLIENT_SECRET is not set.')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO

/*
    SET THE REAL VALUES (run once, in the database):

    UPDATE CFG.Application_Parameters SET ParameterValue='<tenant-guid-or-domain>', UpdatedAt=SYSUTCDATETIME()
    WHERE ParameterKey='GRAPH_TENANT_ID';

    UPDATE CFG.Application_Parameters SET ParameterValue='<the-real-client-secret>', UpdatedAt=SYSUTCDATETIME()
    WHERE ParameterKey='GRAPH_CLIENT_SECRET';
*/
