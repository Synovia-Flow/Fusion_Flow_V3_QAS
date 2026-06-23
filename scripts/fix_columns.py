# NOT FOR PRD: references BKD.Staging* tables removed by migration 078.
# Use STG.BKD_* or ING.BKD_* for new pipeline work.
from dotenv import load_dotenv
load_dotenv()
import os, pyodbc, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string

conn = pyodbc.connect(build_connection_string(timeout=30), autocommit=True)
cur = conn.cursor()

columns = [
    ("api_http_status", "INT NULL"),
    ("api_response_status", "VARCHAR(50) NULL"),
    ("api_process_message", "VARCHAR(500) NULL"),
    ("api_error_message", "NVARCHAR(MAX) NULL"),
    ("api_error_details", "NVARCHAR(MAX) NULL"),
    ("api_duration_ms", "INT NULL"),
    ("api_called_at", "DATETIME2 NULL"),
    ("api_request_json", "NVARCHAR(MAX) NULL"),
    ("api_response_json", "NVARCHAR(MAX) NULL"),
    ("external_status", "VARCHAR(50) NULL"),
    ("external_route", "VARCHAR(20) NULL"),
    ("external_error_message", "NVARCHAR(MAX) NULL"),
    ("source", "VARCHAR(30) NULL DEFAULT 'App_Form'"),
    ("identity_no_of_transport", "VARCHAR(27) NULL"),
    ("nationality_of_transport", "VARCHAR(2) NULL"),
    ("type_of_passive_transport", "VARCHAR(40) NULL"),
    ("conveyance_ref", "VARCHAR(35) NULL"),
    ("seal_number", "VARCHAR(20) NULL"),
    ("transport_charges", "VARCHAR(40) NULL"),
    ("place_of_loading", "VARCHAR(33) NULL"),
    ("place_of_unloading", "VARCHAR(33) NULL"),
    ("place_of_acceptance_same", "VARCHAR(3) NULL"),
    ("place_of_acceptance", "VARCHAR(33) NULL"),
    ("place_of_delivery_same", "VARCHAR(3) NULL"),
    ("place_of_delivery", "VARCHAR(33) NULL"),
    ("carrier_street_number", "VARCHAR(35) NULL"),
    ("carrier_city", "VARCHAR(35) NULL"),
    ("carrier_postcode", "VARCHAR(9) NULL"),
    ("carrier_country", "VARCHAR(2) NULL"),
    ("haulier_eori", "VARCHAR(200) NULL"),
    ("completed_at", "DATETIME2 NULL"),
]

for col_name, col_def in columns:
    try:
        cur.execute(f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA='BKD' AND TABLE_NAME='StagingDeclarations' AND COLUMN_NAME='{col_name}')
            ALTER TABLE BKD.StagingDeclarations ADD [{col_name}] {col_def}
        """)
        print(f"  OK: {col_name}")
    except Exception as e:
        print(f"  Skip: {col_name} ({e})")

conn.close()
print("Done")
