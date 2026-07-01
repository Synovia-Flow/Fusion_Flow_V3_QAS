/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 23 OF N
    =================================================
    Purpose : Register the full-database snapshot-to-Excel report.

              Modules/Global/export_db_snapshot.py downloads every base table to
              one .xlsx: one worksheet per populated table, a single "Zero Records"
              tab for empty tables, a "Summary" tab (row/column counts per table)
              and a "Column Analysis" tab (per-column type/keys/nullability/
              % populated). Output folder comes from CFG (below), default the
              Documentation_Layer share.

    Run after : 003 (Application_Parameters), 012 (CFG.Job). Safe to rerun.
    Script    : Modules/Global/export_db_snapshot.py  (no CLI; optional arg = output dir)
*/

/* ------------------------------------------------------------------ */
/* Output folder for the snapshot (defaults to DOCUMENTATION_OUTPUT_ROOT */
/* when this row is blank/absent).                                      */
/* ------------------------------------------------------------------ */
MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('DB_SNAPSHOT_OUTPUT_DIR', N'\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Documentation_Layer',
     'STRING', 'Output folder for the full DB snapshot workbook (export_db_snapshot.py). Blank = use DOCUMENTATION_OUTPUT_ROOT.')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO

/* ------------------------------------------------------------------ */
/* Register the job.                                                    */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
    MERGE CFG.Job AS t
    USING (VALUES
        ('REP_DB_SNAPSHOT_XLSX', 'Report - Full DB snapshot to Excel', 'REPORTING', NULL, NULL, 'TASK', NULL, NULL,
         'Download every base table to a single .xlsx: one worksheet per populated table, a single "Zero Records" tab for empty tables, a Summary tab (row/column counts) and a Column Analysis tab (per-column types/keys/nullability/% populated). Output to DB_SNAPSHOT_OUTPUT_DIR (default Documentation_Layer share).',
         'export_db_snapshot:main', 'All base tables (every schema)', 'DB_Snapshot_<db>_<stamp>.xlsx',
         'On demand', 1, 'Cross-schema reporting; no CLI. Optional positional arg overrides the output folder.')
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
