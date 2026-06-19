# Fusion Flow V3 QAS

Version-controlled starting point for the Fusion Flow V3 QAS ingestion work.

Current scope:

- Microsoft Graph mailbox ingestion.
- Minimum configuration needed to read customer emails and save inbound files.
- Simple database shape for `CFG -> EXC -> ING -> STG -> TSS`.

The operational Excel configuration lives on the shared Quality drive:

```text
\\pl-az-sdf-plint\Fusion_Production\Synovia_Flow_Quality\Documentation_Layer\Base_Ingestion_Configuration.xlsx
```

Do not commit real Graph secrets to this repository. Use the Excel workbook,
database `CFG.Graph`, environment variables, or secure deployment configuration
for real values.

## Structure

```text
Graph/
Synovia_Flow_Quality/Documentation_Layer/
Synovia_Flow_Production/Configuration_Layer/SQL/
```

## First Graph Scope

Initial confirmed tenant: `BKD` / Birkdale.

- Mailbox: `nexus@synoviaflow.cloud`
- Sender rule: domain `birkdalesales.com`
- Current behaviour: save file attachments only
- Destination: tenant folder under the Integration Layer
- Body extraction: pending Aidan confirmation
