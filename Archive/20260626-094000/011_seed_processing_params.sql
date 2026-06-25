/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 11 OF N
    =================================================
    Purpose : Seed the Module 2 (Data Processing) run-control parameters into
              CFG.Application_Parameters. The processing runner has NO CLI - the
              scheduler simply runs `python process_data.py` and the runner reads
              all of its behaviour from these parameters.

    Run after : 002 (CFG tables), 003 (CFG seed), 010 (PRS tables).
    Safe to rerun: Yes.

    NOTE on semantics: this seed is INSERT-ONLY (WHEN NOT MATCHED). Re-running it
    never overwrites a value an operator has changed in the table - so you can flip
    PROCESSING_DRY_RUN or repoint PROCESSING_CLIENT in the database and a later
    re-deploy will leave your value intact. (003 re-asserts its own defaults; these
    module controls are deliberately operator-owned once seeded.)
*/

MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('PROCESSING_CLIENT',           'BKD',    'STRING', 'Module 2: client code to process (3-letter code).'),
    ('PROCESSING_TRANSACTION_MODE', 'latest', 'STRING', 'Module 2: "latest" (unprocessed INGESTED rows) or an explicit EXC.Execution ExecutionID to reprocess.'),
    ('PROCESSING_DRY_RUN',          '0',      'BOOL',   'Module 2: 1/true = process + report only, write nothing; 0 = persist to PRS/EXC/LOG.')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO
