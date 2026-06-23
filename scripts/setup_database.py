"""Run all SQL migrations against the target database."""
import os, sys, pyodbc, glob
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string


def main():
    load_dotenv()
    conn = pyodbc.connect(build_connection_string(timeout=30), autocommit=True)
    cursor = conn.cursor()
    migration_dir = os.path.join(os.path.dirname(__file__), '..', 'migrations')
    for sql_file in sorted(glob.glob(os.path.join(migration_dir, '*.sql'))):
        print(f"Running: {os.path.basename(sql_file)}")
        with open(sql_file) as f:
            sql = f.read()
        for batch in sql.split('\nGO\n'):
            batch = batch.strip()
            if batch:
                try:
                    cursor.execute(batch)
                except pyodbc.Error as e:
                    print(f"  Warning: {e}")
    conn.close()
    print("All migrations complete.")

if __name__ == '__main__':
    main()
