/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 29 OF N
    =================================================
    Purpose : Submission run-control parameters + the submission jobs (Module 3).

              Controls (CFG.Application_Parameters) - no CLI:
                SUBMISSION_CLIENT       client to submit         (default BKD)
                SUBMISSION_ENTITY       entity                   (default ENS_HEADER)
                SUBMISSION_ENV          TSS environment code     (default TST - safe)
                SUBMISSION_DRY_RUN      1 = build + log the request but DO NOT call
                                        (default 1 - safe; set 0 to actually submit)
                SUBMISSION_MOVEMENT_KEY optional single movement (blank = all in scope)
                SUBMISSION_API_BASE_PATH resource path prefix appended to the env
                                        BaseUrl (default /x_fhmrc_tss_api/v1)

              Jobs (Module SUBMISSION):
                PRS_PROMOTE_BKD_ENS  VALIDATED PRS rows -> STG.BKD_ENS_Header
                SUB_CREATE_BKD_ENS   POST /headers (create); capture ENS number
                SUB_MIRROR_BKD_ENS   GET header back -> TSS.BKD_ENS_Header; mark complete
                SUB_UPDATE_BKD_ENS   (stub) full-replacement update (Rule 16)
                SUB_CANCEL_BKD_ENS   (stub) cancel a live declaration

    Run after : 012 (CFG.Job), 026/027/028. Safe to rerun.
*/

MERGE CFG.Application_Parameters AS t
USING (VALUES
    ('SUBMISSION_CLIENT',        'BKD',                   'STRING', 'Submission: client code to submit.'),
    ('SUBMISSION_ENTITY',        'ENS_HEADER',            'STRING', 'Submission: entity to submit.'),
    ('SUBMISSION_ENV',           'TST',                   'STRING', 'Submission: TSS environment code (CFG.TSS_Environment: PRD|TST). Default TST.'),
    ('SUBMISSION_DRY_RUN',       '1',                     'BOOL',   'Submission: 1 = build + log the request but do NOT call TSS (safe default). 0 = actually submit.'),
    ('SUBMISSION_MOVEMENT_KEY',  '',                      'STRING', 'Submission: optional single MovementKey (blank = all in scope).'),
    ('SUBMISSION_MAX_ROWS',      '0',                     'INT',    'Submission: cap the number of rows submitted/mirrored per run (0 = no cap). Set e.g. 3 to send a few.'),
    ('SUBMISSION_API_BASE_PATH', '/x_fhmrc_tss_api/v1',   'STRING', 'Submission: resource path prefix appended to the env BaseUrl before the endpoint (e.g. /headers).')
) AS s (ParameterKey, ParameterValue, ValueType, Description)
ON t.ParameterKey = s.ParameterKey
WHEN NOT MATCHED THEN INSERT (ParameterKey, ParameterValue, ValueType, Description)
    VALUES (s.ParameterKey, s.ParameterValue, s.ValueType, s.Description);
GO

IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
    MERGE CFG.Job AS t
    USING (VALUES
        ('PRS_PROMOTE_BKD_ENS', 'Promote - Birkdale ENS Header to STG', 'SUBMISSION', 'BKD', NULL, 'TASK', NULL, NULL,
         'Promote VALIDATED PRS.BKD_ENS_Header_Submission rows into STG.BKD_ENS_Header (Fusion_Status STG_MATERIALISED -> READY), the submission-ready staging copy. Sets the PRS tracking status to STG_MATERIALISED.',
         'promote_ens:main', 'PRS.BKD_ENS_Header_Submission (VALIDATED)', 'STG.BKD_ENS_Header', 'After processing', 1,
         'Module 3 step 1. No CLI; controls SUBMISSION_*.'),
        ('SUB_CREATE_BKD_ENS', 'Submit - Birkdale ENS Header (create)', 'SUBMISSION', 'BKD', NULL, 'TASK', NULL, NULL,
         'POST /headers to TSS for READY STG rows (create). Logs the full request/response to API.Call, advances EXC SUBMITTING -> SUBMITTED, and captures the returned ENS Declaration_Number onto the STG row. Honours SUBMISSION_DRY_RUN (default 1 = no live call) and SUBMISSION_ENV (default TST).',
         'submit_ens:main', 'STG.BKD_ENS_Header (READY)', 'TSS API /headers + API.Call + STG', 'After promote', 1,
         'Module 3 step 2. Rule 1 (/headers), Rule 14 (rate limit). Dry-run safe.'),
        ('SUB_MIRROR_BKD_ENS', 'Mirror - Birkdale ENS Header from TSS', 'SUBMISSION', 'BKD', NULL, 'TASK', NULL, NULL,
         'For SUBMITTED STG rows with a Declaration_Number, GET the header back from TSS and upsert the authoritative live record into TSS.BKD_ENS_Header (the live mirror). Marks the STG row RECONCILED (complete). Logs to API.Call + EXC RECONCILING -> RECONCILED.',
         'mirror_ens:main', 'STG.BKD_ENS_Header (SUBMITTED) + TSS API', 'TSS.BKD_ENS_Header + STG', 'After create', 1,
         'Module 3 step 3. The live mirror the update/cancel jobs link to.'),
        ('SUB_UPDATE_BKD_ENS', 'Update - Birkdale ENS Header (stub)', 'SUBMISSION', 'BKD', NULL, 'TASK', NULL, NULL,
         'STUB - full-replacement update of a live declaration (Rule 16), operating against TSS.BKD_ENS_Header by Declaration_Number. To be implemented.',
         'update_ens:main', 'TSS.BKD_ENS_Header (live)', 'TSS API /headers (update) + API.Call', 'On demand', 0,
         'Registered stub; not yet implemented.'),
        ('SUB_CANCEL_BKD_ENS', 'Cancel - Birkdale ENS Header (stub)', 'SUBMISSION', 'BKD', NULL, 'TASK', NULL, NULL,
         'STUB - cancel a live declaration, operating against TSS.BKD_ENS_Header by Declaration_Number. To be implemented.',
         'cancel_ens:main', 'TSS.BKD_ENS_Header (live)', 'TSS API (cancel) + API.Call', 'On demand', 0,
         'Registered stub; not yet implemented.')
    ) AS s (JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
            Purpose, EntryPoint, InputSource, OutputTarget, Schedule, IsActive, Notes)
    ON t.JobCode = s.JobCode
    WHEN MATCHED THEN UPDATE SET JobName=s.JobName, Purpose=s.Purpose, EntryPoint=s.EntryPoint,
        InputSource=s.InputSource, OutputTarget=s.OutputTarget, Schedule=s.Schedule, Notes=s.Notes,
        IsActive=s.IsActive, UpdatedAt=SYSUTCDATETIME()
    WHEN NOT MATCHED THEN INSERT (JobCode, JobName, ModuleName, ClientCode, Channel, JobType, StepNo, ParentJobCode,
            Purpose, EntryPoint, InputSource, OutputTarget, Schedule, IsActive, Notes)
        VALUES (s.JobCode, s.JobName, s.ModuleName, s.ClientCode, s.Channel, s.JobType, s.StepNo, s.ParentJobCode,
                s.Purpose, s.EntryPoint, s.InputSource, s.OutputTarget, s.Schedule, s.IsActive, s.Notes);
GO
