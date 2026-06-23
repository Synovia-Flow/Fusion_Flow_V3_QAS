# Azure Deployment Notes

Keep the first Azure deployment simple:

- Backend/API: Azure App Service or Azure Container App.
- Scheduled jobs: Azure WebJob, Azure Function Timer, or Container App Job.
- Database: Azure SQL.
- Secrets: Azure App Settings or Key Vault.
- Frontend: not required yet.

The initial deploy should prove the backend/job execution path before adding UI complexity.
