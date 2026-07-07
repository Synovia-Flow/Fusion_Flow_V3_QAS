/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 34 OF N
    =================================================
    Purpose : Re-point CFG.Job.EntryPoint after the Module scripts were renamed with
              sequence prefixes for clarity (ING_/PRS_/SUB_/REF_/REP_). EntryPoint is
              documentation of "module:function"; dispatch itself is by JobCode, so this
              keeps the registry honest rather than changing behaviour.

              Renames (old module -> new module):
                run_ingestion         -> ING_00_run_cycle
                birkdale_sales_orders -> ING_01_acquire_email
                ens_headers           -> ING_02_parse_ens
                load_raw              -> ING_03_load_raw
                process_engine        -> PRS_01_engine
                reprocess_engine      -> PRS_02_reprocess
                promote_ens           -> SUB_01_promote
                submit_ens            -> SUB_02_submit
                mirror_ens            -> SUB_03_mirror
                update_ens            -> SUB_04_update
                cancel_ens            -> SUB_05_cancel
                fetch_submitted_json  -> SUB_06_fetch_json
                fetch_choice_values   -> REF_01_choice_values
                fetch_commodity_codes -> REF_02_commodity_codes
                export_db_snapshot    -> REP_01_db_snapshot
                export_reference_lists-> REP_02_reference_lists
                stage_bkd_ens_header  -> _retired/stage_bkd_ens_header (job already inactive)

    Run after : 033 (job queue). Safe to rerun (REPLACE only fires on the old prefix).
*/

IF OBJECT_ID('CFG.Job', 'U') IS NOT NULL
BEGIN
    -- Only the module part of "module:function" is rewritten; the :main/:run suffix is preserved.
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'run_ingestion:',          'ING_00_run_cycle:'),      UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'run_ingestion:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'birkdale_sales_orders:',  'ING_01_acquire_email:'),  UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'birkdale_sales_orders:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'ens_headers:',            'ING_02_parse_ens:'),      UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'ens_headers:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'load_raw:',               'ING_03_load_raw:'),       UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'load_raw:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'process_engine:',         'PRS_01_engine:'),         UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'process_engine:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'reprocess_engine:',       'PRS_02_reprocess:'),      UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'reprocess_engine:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'promote_ens:',            'SUB_01_promote:'),        UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'promote_ens:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'submit_ens:',             'SUB_02_submit:'),         UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'submit_ens:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'mirror_ens:',             'SUB_03_mirror:'),         UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'mirror_ens:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'update_ens:',             'SUB_04_update:'),         UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'update_ens:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'cancel_ens:',             'SUB_05_cancel:'),         UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'cancel_ens:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'fetch_submitted_json:',   'SUB_06_fetch_json:'),     UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'fetch_submitted_json:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'fetch_choice_values:',    'REF_01_choice_values:'),  UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'fetch_choice_values:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'fetch_commodity_codes:',  'REF_02_commodity_codes:'), UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'fetch_commodity_codes:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'export_db_snapshot:',     'REP_01_db_snapshot:'),    UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'export_db_snapshot:%';
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'export_reference_lists:', 'REP_02_reference_lists:'), UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'export_reference_lists:%';
    -- Retired job: script moved to Modules/_retired/ (job is already IsActive=0).
    UPDATE CFG.Job SET EntryPoint = REPLACE(EntryPoint, 'stage_bkd_ens_header:',   '_retired.stage_bkd_ens_header:'), UpdatedAt = SYSUTCDATETIME() WHERE EntryPoint LIKE 'stage_bkd_ens_header:%';
END;
GO
