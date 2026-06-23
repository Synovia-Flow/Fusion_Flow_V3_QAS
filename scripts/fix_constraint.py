# NOT FOR PRD: references BKD.Staging* tables removed by migration 078.
# Use STG.BKD_* or ING.BKD_* for new pipeline work.
from dotenv import load_dotenv
load_dotenv()
import os, pyodbc, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string

conn = pyodbc.connect(build_connection_string(timeout=30), autocommit=True)
cur = conn.cursor()

# Find and drop old constraint
cur.execute("""
    SELECT name FROM sys.check_constraints
    WHERE parent_object_id = OBJECT_ID('BKD.StagingDeclarations')
    AND definition LIKE '%status%'
""")
for row in cur.fetchall():
    print(f"Dropping: {row[0]}")
    cur.execute(f"ALTER TABLE BKD.StagingDeclarations DROP CONSTRAINT [{row[0]}]")

# Add new with all statuses
cur.execute("""
    ALTER TABLE BKD.StagingDeclarations ADD CONSTRAINT CK_Staging_Status_V2
    CHECK (status IN (
        'Inserted','Validated','Validation_Error',
        'Submitted','Submit_Error','Resubmit',
        'Draft','Queued','Processing','Success','Failed',
        'Cancelled','Error'
    ))
""")
print("New constraint added")
conn.close()
print("Done")
