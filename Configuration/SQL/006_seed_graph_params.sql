/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 6 OF N
    =================================================
    Purpose : Seed the Microsoft Graph (mail ingestion) application parameters
              into CFG.Application_Parameters, taken from the registered app
              "Fusion Flow Mail Reader" (Inbound/Manifest_V2.json).

    Run after : 002_cfg_tables.sql (and ideally 003_seed_cfg.sql)
    Safe to rerun: Yes (MERGE on ParameterKey).

    SECURITY - read this:
      * The manifest does NOT contain the client secret (Azure exports
        passwordCredentials with secretText = null; only a hint "3w5"). So no
        secret value is - or should be - stored here.
      * GRAPH_CLIENT_SECRET_REF points at a Key Vault secret; the runtime
        resolves the actual secret from the vault (or the local Ingestion setup),
        never from this table. Do NOT paste the plaintext secret into the table.
      * GRAPH_TENANT_ID is not in the manifest; set the tenant GUID below.
*/

MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('GRAPH_APP_NAME',           'Fusion Flow Mail Reader',                  'STRING',     'Registered Entra app (display name).'),
    ('GRAPH_APP_OBJECT_ID',      'f46a9a16-ba01-4621-ac68-bc8a0450fd34',     'GUID',       'Entra application object id.'),
    ('GRAPH_CLIENT_ID',          '4cbaa4be-f78f-4e82-944a-bf9d4142a12e',     'GUID',       'Application (client) id - appId.'),
    ('GRAPH_TENANT_ID',          '<SET_TENANT_GUID>',                        'GUID',       'Entra tenant id (GUID). NOT in the manifest - set it.'),
    ('GRAPH_TENANT_DOMAIN',      'NETORG6546503.onmicrosoft.com',            'STRING',     'Publisher/tenant domain from the manifest.'),
    ('GRAPH_CLIENT_SECRET_REF',  'kv-fusionflow-qas/GRAPH-CLIENT-SECRET',    'SECRET_REF', 'Key Vault reference for the client secret. NOT the secret itself.'),
    ('GRAPH_SECRET_KEY_ID',      '63c5bb67-a862-46ff-b929-0159092045ac',     'GUID',       'Manifest passwordCredentials keyId (metadata only).'),
    ('GRAPH_SECRET_HINT',        '3w5',                                      'STRING',     'Secret hint from the manifest (metadata only).'),
    ('GRAPH_SECRET_EXPIRY',      '2028-05-04',                               'DATE',       'Client secret expiry - rotate before this date.'),
    ('GRAPH_PERMISSIONS',        'Mail.Read,Mail.ReadWrite',                 'STRING',     'Application permissions granted (admin consent required).'),
    ('GRAPH_MAILBOX',            'nexus@synoviaflow.cloud',                  'STRING',     'Mailbox (UPN) ingestion reads from.'),
    ('GRAPH_PROCESSED_FOLDER',   'Fusion_Processed',                         'STRING',     'Folder beneath Inbox to move scanned messages into.'),
    ('GRAPH_FORWARDERS',         'nexus@synoviaintegration.com,aidan.harrington@synoviadigital.com', 'STRING', 'Forwarder addresses whose true sender lives in the forwarded body.'),
    ('GRAPH_AUTHORITY',          'https://login.microsoftonline.com/',       'STRING',     'MSAL authority base (append tenant id).'),
    ('GRAPH_SCOPE',              'https://graph.microsoft.com/.default',     'STRING',     'App-only client-credentials scope.')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN MATCHED THEN UPDATE SET ParameterValue=s.ParameterValue, ValueType=s.ValueType, Description=s.Description, UpdatedAt=SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO
