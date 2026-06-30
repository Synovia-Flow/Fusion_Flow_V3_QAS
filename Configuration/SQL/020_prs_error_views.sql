/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 20 OF N
    =================================================
    Purpose : Views to SEE processing errors / rejections at a glance.

              SRV.vw_Processing_Errors      - client-agnostic. Every DATA_PROCESSING
                                              error logged to LOG.Error_Log, joined to
                                              its EXC.Execution (newest first).
              PRS.vw_BKD_ENS_Header_Status  - one row per BKD ENS movement: Fusion_Status,
                                              the reject reason, and the offending values
                                              (movement_type, arrival_port, ...).
              PRS.vw_BKD_ENS_Header_Rejected- the REJECTED subset of the above.
              PRS.vw_BKD_ENS_Header_Reasons - one row PER individual reason (STRING_SPLIT),
                                              so you can GROUP BY to see the most common
                                              failures across a run.

    Run after : 004 (EXC/LOG), 013 (PRS BKD ENS tables). Safe to rerun.
*/

/* ------------------------------------------------------------------ */
/* All processing errors (every client) - from LOG.Error_Log + EXC.    */
/* ------------------------------------------------------------------ */
CREATE OR ALTER VIEW SRV.vw_Processing_Errors AS
    SELECT  e.ClientCode,
            e.ExecutionID,
            e.ModuleName,
            e.ProcessName,
            e.Status        AS ExecutionStatus,
            e.StartedAt,
            l.StepName,
            l.ErrorType,
            l.Message,
            l.TransactionID,
            l.CreatedAt     AS LoggedAt
    FROM LOG.Error_Log l
    JOIN EXC.Execution e ON e.ExecutionID = l.ExecutionID
    WHERE e.ModuleName = 'DATA_PROCESSING';
GO

/* ------------------------------------------------------------------ */
/* BKD ENS header - status + offending values per movement.            */
/* ------------------------------------------------------------------ */
CREATE OR ALTER VIEW PRS.vw_BKD_ENS_Header_Status AS
    SELECT  s.ClientCode,
            s.MovementKey,
            s.Fusion_Status,
            s.Fusion_Status_Reason,
            s.op_type,
            s.movement_type,
            s.arrival_port,
            s.arrival_date_time,
            s.transport_charges,
            s.carrier_eori,
            s.carrier_name,
            s.ExecutionID,
            s.SubmissionID,
            s.CreatedAt,
            t.SourceEnsLoadID,
            t.SourceFile,
            t.StagedAt,
            t.ValidatedAt
    FROM PRS.BKD_ENS_Header_Submission s
    LEFT JOIN PRS.BKD_ENS_Header_Tracking t ON t.SubmissionID = s.SubmissionID;
GO

CREATE OR ALTER VIEW PRS.vw_BKD_ENS_Header_Rejected AS
    SELECT * FROM PRS.vw_BKD_ENS_Header_Status WHERE Fusion_Status = 'REJECTED';
GO

/* ------------------------------------------------------------------ */
/* One row PER reason - GROUP BY Reason to rank the commonest failures. */
/* ------------------------------------------------------------------ */
CREATE OR ALTER VIEW PRS.vw_BKD_ENS_Header_Reasons AS
    SELECT  s.ClientCode,
            s.MovementKey,
            s.ExecutionID,
            LTRIM(RTRIM(r.value)) AS Reason
    FROM PRS.BKD_ENS_Header_Submission s
    CROSS APPLY STRING_SPLIT(ISNULL(s.Fusion_Status_Reason, ''), ';') r
    WHERE s.Fusion_Status = 'REJECTED'
      AND NULLIF(LTRIM(RTRIM(r.value)), '') IS NOT NULL;
GO
