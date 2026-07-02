# Fusion Flow V3 QAS - ONE image for the whole platform.
# Contains the live portal + API (liveWeb/app.py) AND every Module job
# (Ingestion / Processing / Submission / Global) AND the background worker
# (Modules/Global/job_worker.py). The web service, the cron jobs and the worker in
# render.yaml all run FROM THIS SAME IMAGE - they differ only by their dockerCommand.
#
# Bundles the Microsoft ODBC Driver 18 so pyodbc can reach Azure SQL (not in the
# stock python image). Build context is the REPOSITORY ROOT so Modules/ is included -
# app.py imports the runners from ../Modules, and the crons/worker run them directly.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ACCEPT_EULA=Y

# Microsoft ODBC Driver 18 for SQL Server (+ build deps for pyodbc).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg apt-transport-https ca-certificates unixodbc-dev gcc g++ \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Default command = the web service (portal + API). Cron jobs and the worker override
# this with their own dockerCommand in render.yaml. Shell form so Render's $PORT
# expands; --chdir liveWeb makes `app:app` resolve while Modules/ stays importable at /app.
EXPOSE 8080
CMD gunicorn --chdir liveWeb --bind 0.0.0.0:${PORT:-8080} --workers 2 --timeout 120 app:app
