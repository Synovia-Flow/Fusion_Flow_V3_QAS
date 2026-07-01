/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 24 OF N
    =================================================
    Purpose : Register the reference / option-lists export.

              Modules/Global/export_reference_lists.py exports the curated CFG
              "option" lists (the enumerations / lookups that define allowed
              values) to a SEPARATE workbook, one tab per list - Vocabulary
              (CFG.Status_Vocabulary), Clients, Jobs, Choice Fields, Choice Field
              Map, Translations, Processing Profiles, Field Map, Carriers,
              Ingestion Sources, API Versions/Process Map, TSS Environments,
              Parameters - plus an Index tab of links. Secret-ish values masked.
              Output folder as per the DB snapshot (DB_SNAPSHOT_OUTPUT_DIR ->
              DOCUMENTATION_OUTPUT_ROOT).

    Run after : 003 (Application_Parameters), 012 (CFG.Job). Safe to rerun.
    Script    : Modules/Global/export_reference_lists.py  (no CLI; optional arg = output dir)
*/

IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
    MERGE CFG.Job AS t
    USING (VALUES
        ('REP_REFERENCE_LISTS_XLSX', 'Report - Reference / option lists to Excel', 'REPORTING', NULL, NULL, 'TASK', NULL, NULL,
         'Export the curated CFG option lists (Status_Vocabulary as "Vocabulary", Clients, Jobs, Choice Fields/Map, Translations, Processing Profiles/Field Map, Carriers, Ingestion Sources, API Versions/Process Map, TSS Environments, Parameters) to a separate .xlsx - one tab per list + an Index of links, secret values masked. For analysis of the allowed-value model.',
         'export_reference_lists:main', 'Curated CFG reference/lookup tables', 'Reference_Lists_<db>_<stamp>.xlsx',
         'On demand', 1, 'Companion to REP_DB_SNAPSHOT_XLSX; reuses its helpers + output folder.')
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
