/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 30 OF N
    =================================================
    Purpose : Register the "dump submitted ENS responses to JSON" analysis tool.

              Modules/Submission/fetch_submitted_json.py GETs each submitted header
              back from TSS and writes one JSON file per movement (full request +
              full response) to a folder, for offline analysis when designing the
              TSS.* live-mirror table and the next process step. Each call is also
              logged to API.Call. It is a READ (GET) - safe - so it contacts TSS
              regardless of SUBMISSION_DRY_RUN.

              Output folder: SUBMISSION_JSON_DIR when set, else <repo>/Development/json.

    Run after : 029 (submission controls/jobs). Safe to rerun. The script runs
                without this migration (folder defaults apply) - this only registers
                the job + the optional output-folder override.
*/

MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('SUBMISSION_JSON_DIR', '', 'STRING',
     'Output folder for fetch_submitted_json.py response dumps. Blank = <repo>/Development/json.')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO

IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
    MERGE CFG.Job AS t
    USING (VALUES
        ('SUB_FETCH_JSON_BKD_ENS', 'Fetch - Birkdale ENS responses to JSON', 'SUBMISSION', 'BKD', NULL, 'TASK', NULL, NULL,
         'GET each submitted BKD ENS header back from TSS and write one JSON file per movement (full request + response) to SUBMISSION_JSON_DIR (default <repo>/Development/json), for analysis when designing the TSS live-mirror table and the next process step. Logs each call to API.Call. Read-only - runs against TSS regardless of SUBMISSION_DRY_RUN.',
         'fetch_submitted_json:main', 'STG.BKD_ENS_Header (declaration_number set) + TSS API', 'JSON files + API.Call',
         'On demand', 1, 'Analysis helper; honours SUBMISSION_ENV / _MOVEMENT_KEY / _MAX_ROWS.')
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
