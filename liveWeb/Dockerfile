# Synovia Flow 3 — live portal + API (Render web service, Frankfurt).
# Bundles the Microsoft ODBC driver so pyodbc can reach Azure SQL.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 ACCEPT_EULA=Y

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

# The image ships ODBC Driver 18; set DB_DRIVER accordingly in the environment:
#   DB_DRIVER={ODBC Driver 18 for SQL Server}
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "60", "app:app"]
