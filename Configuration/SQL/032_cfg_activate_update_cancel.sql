/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 32 OF N
    =================================================
    Purpose : Activate the TSS-layer UPDATE and CANCEL jobs now that they are built.

              SUB_UPDATE_BKD_ENS  -> Modules/Submission/update_ens.py  (full-replacement
                                     update, Rule 16, op_type=update + declaration_number)
              SUB_CANCEL_BKD_ENS  -> Modules/Submission/cancel_ens.py  (op_type=cancel +
                                     declaration_number; marks CANCELLED + mirror not-live)

              Both operate on the TSS live mirror by Declaration_Number, log every call
              to API.Call, advance the EXC spine, and honour SUBMISSION_DRY_RUN /
              SUBMISSION_ENV / SUBMISSION_MOVEMENT_KEY / SUBMISSION_MAX_ROWS.

    Run after : 029 (submission jobs registered). Safe to rerun.
*/

IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
BEGIN
    UPDATE CFG.Job
       SET IsActive = 1,
           Purpose = 'Full-replacement UPDATE (Rule 16) of a live ENS declaration header: POST /headers with op_type=update + declaration_number + all fields, against STG rows that have a declaration_number. Logs to API.Call, advances EXC, refreshes tracking Tss_Status. Re-run mirror after. Dry-run safe; scope with SUBMISSION_MOVEMENT_KEY / _MAX_ROWS.',
           EntryPoint = 'update_ens:main', Notes = 'TSS layer - update.', UpdatedAt = SYSUTCDATETIME()
     WHERE JobCode = 'SUB_UPDATE_BKD_ENS';

    UPDATE CFG.Job
       SET IsActive = 1,
           Purpose = 'CANCEL a live ENS declaration header: POST /headers with op_type=cancel + declaration_number. On success sets STG + tracking Fusion_Status=CANCELLED and marks the TSS mirror not-live (IsLive=0, CancelledAt). Logs to API.Call, advances EXC. Destructive: requires SUBMISSION_MOVEMENT_KEY or SUBMISSION_MAX_ROWS. Dry-run safe.',
           EntryPoint = 'cancel_ens:main', Notes = 'TSS layer - cancel.', UpdatedAt = SYSUTCDATETIME()
     WHERE JobCode = 'SUB_CANCEL_BKD_ENS';
END;
GO
