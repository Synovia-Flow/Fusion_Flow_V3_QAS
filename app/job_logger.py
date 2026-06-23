"""
Lightweight job run logger for standalone maintenance scripts.

Writes best-effort entries to TSS.BKD_JobRuns.
Failures are non-fatal so scripts can still run on databases without the migration.
"""
import logging
import os
import time
import traceback

logger = logging.getLogger(__name__)


def _is_missing_job_run_log_error(exc) -> bool:
    message = str(exc)
    return (
        'BKD_JobRuns' in message
        and ('Invalid object name' in message or '(208)' in message)
    )


class JobRun:
    def __init__(self, job_name: str, triggered_by: str = 'manual'):
        self.job_name = job_name
        self.triggered_by = triggered_by
        self.rows_processed = None
        self.log_lines = []
        self._run_id = None
        self._conn = None
        self._started_at = 0.0

    def __enter__(self):
        self._started_at = time.time()
        client_code = os.environ.get('CLIENT_CODE', os.environ.get('TENANT_CODE', 'BKD'))
        try:
            from app.db import get_standalone_connection

            self._conn = get_standalone_connection()
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO [TSS].[BKD_JobRuns] (ClientCode, JobName, TriggeredBy, Status, StartedAt)
                OUTPUT INSERTED.JobRunId
                VALUES (?, ?, ?, 'running', SYSUTCDATETIME())
                """,
                client_code,
                self.job_name,
                self.triggered_by,
            )
            row = cursor.fetchone()
            self._run_id = row[0] if row else None
            self._conn.commit()
            cursor.close()
        except Exception as exc:
            if _is_missing_job_run_log_error(exc):
                logger.info('TSS.BKD_JobRuns table unavailable; job run insert skipped.')
            else:
                logger.warning('Job run insert skipped: %s', exc)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = int((time.time() - self._started_at) * 1000)
        status = 'error' if exc_type else 'ok'
        error_message = None
        if exc_type:
            error_message = ''.join(traceback.format_exception(exc_type, exc_val, exc_tb))
        log_output = '\n'.join(self.log_lines) if self.log_lines else None

        if self._conn and self._run_id:
            try:
                cursor = self._conn.cursor()
                cursor.execute(
                    """
                    UPDATE [TSS].[BKD_JobRuns]
                    SET FinishedAt    = SYSUTCDATETIME(),
                        DurationMs    = ?,
                        Status        = ?,
                        RowsProcessed = ?,
                        ErrorMessage  = ?,
                        LogOutput     = ?
                    WHERE JobRunId = ?
                    """,
                    duration_ms,
                    status,
                    self.rows_processed,
                    error_message,
                    log_output,
                    self._run_id,
                )
                self._conn.commit()
                cursor.close()
            except Exception as exc:
                logger.warning('Job run update skipped: %s', exc)
            finally:
                try:
                    self._conn.close()
                except Exception:
                    pass

        return False
