/*
    Fusion_Flow_V3_QAS SQL migration.

    Purpose:
        Add tenant ingestion foreign keys and indexes.

    Run order:
        Execute files in numeric filename order. Scripts are idempotent
        where practical so QAS can be refreshed safely.
*/
IF OBJECT_ID('CFG.FK_CFG_IngestionRoute_CFG_Tenant', 'F') IS NULL
BEGIN
    ALTER TABLE CFG.IngestionRoute WITH CHECK
    ADD CONSTRAINT FK_CFG_IngestionRoute_CFG_Tenant
        FOREIGN KEY (TenantID)
        REFERENCES CFG.Tenant (TenantID);
END;
GO
IF OBJECT_ID('CFG.FK_CFG_TenantSetting_CFG_Tenant', 'F') IS NULL
BEGIN
    ALTER TABLE CFG.TenantSetting WITH CHECK
    ADD CONSTRAINT FK_CFG_TenantSetting_CFG_Tenant
        FOREIGN KEY (TenantID)
        REFERENCES CFG.Tenant (TenantID);
END;
GO

IF OBJECT_ID('CFG.FK_CFG_IngestionPackRule_CFG_IngestionRoute', 'F') IS NULL
BEGIN
    ALTER TABLE CFG.IngestionPackRule WITH CHECK
    ADD CONSTRAINT FK_CFG_IngestionPackRule_CFG_IngestionRoute
        FOREIGN KEY (RouteID)
        REFERENCES CFG.IngestionRoute (RouteID);
END;
GO

IF OBJECT_ID('EXC.FK_EXC_ExecutionLog_EXC_Graph', 'F') IS NULL
BEGIN
    ALTER TABLE EXC.ExecutionLog WITH CHECK
    ADD CONSTRAINT FK_EXC_ExecutionLog_EXC_Graph
        FOREIGN KEY (ExecutionID)
        REFERENCES EXC.Graph (ExecutionID);
END;
GO

IF OBJECT_ID('ING.FK_ING_ProcessFile_ING_Graph', 'F') IS NULL
BEGIN
    ALTER TABLE ING.ProcessFile WITH CHECK
    ADD CONSTRAINT FK_ING_ProcessFile_ING_Graph
        FOREIGN KEY (GraphID)
        REFERENCES ING.Graph (GraphID);
END;
GO

IF OBJECT_ID('ING.FK_ING_ProcessFile_EXC_Graph', 'F') IS NULL
BEGIN
    ALTER TABLE ING.ProcessFile WITH CHECK
    ADD CONSTRAINT FK_ING_ProcessFile_EXC_Graph
        FOREIGN KEY (ExecutionID)
        REFERENCES EXC.Graph (ExecutionID);
END;
GO

IF OBJECT_ID('ING.FK_ING_ProcessFile_CFG_Tenant', 'F') IS NULL
BEGIN
    ALTER TABLE ING.ProcessFile WITH CHECK
    ADD CONSTRAINT FK_ING_ProcessFile_CFG_Tenant
        FOREIGN KEY (TenantID)
        REFERENCES CFG.Tenant (TenantID);
END;
GO

IF OBJECT_ID('ING.FK_ING_ProcessFile_CFG_IngestionRoute', 'F') IS NULL
BEGIN
    ALTER TABLE ING.ProcessFile WITH CHECK
    ADD CONSTRAINT FK_ING_ProcessFile_CFG_IngestionRoute
        FOREIGN KEY (RouteID)
        REFERENCES CFG.IngestionRoute (RouteID);
END;
GO

IF OBJECT_ID('ING.FK_ING_LoadRow_ING_ProcessFile', 'F') IS NULL
BEGIN
    ALTER TABLE ING.LoadRow WITH CHECK
    ADD CONSTRAINT FK_ING_LoadRow_ING_ProcessFile
        FOREIGN KEY (ProcessFileID)
        REFERENCES ING.ProcessFile (ProcessFileID);
END;
GO

IF OBJECT_ID('CFG.FK_CFG_Graph_CFG_Tenant', 'F') IS NULL
BEGIN
    ALTER TABLE CFG.Graph WITH CHECK
    ADD CONSTRAINT FK_CFG_Graph_CFG_Tenant
        FOREIGN KEY (TenantID)
        REFERENCES CFG.Tenant (TenantID);
END;
GO

IF OBJECT_ID('CFG.FK_CFG_Graph_CFG_IngestionRoute', 'F') IS NULL
BEGIN
    ALTER TABLE CFG.Graph WITH CHECK
    ADD CONSTRAINT FK_CFG_Graph_CFG_IngestionRoute
        FOREIGN KEY (RouteID)
        REFERENCES CFG.IngestionRoute (RouteID);
END;
GO

IF OBJECT_ID('ING.FK_ING_Graph_CFG_IngestionRoute', 'F') IS NULL
BEGIN
    ALTER TABLE ING.Graph WITH CHECK
    ADD CONSTRAINT FK_ING_Graph_CFG_IngestionRoute
        FOREIGN KEY (RouteID)
        REFERENCES CFG.IngestionRoute (RouteID);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_Graph_TenantID' AND object_id = OBJECT_ID('CFG.Graph'))
BEGIN
    CREATE INDEX IX_CFG_Graph_TenantID ON CFG.Graph (TenantID);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_Graph_RouteID' AND object_id = OBJECT_ID('CFG.Graph'))
BEGIN
    CREATE INDEX IX_CFG_Graph_RouteID ON CFG.Graph (RouteID);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ING_Graph_RouteID' AND object_id = OBJECT_ID('ING.Graph'))
BEGIN
    CREATE INDEX IX_ING_Graph_RouteID ON ING.Graph (RouteID);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_IngestionRoute_TenantID' AND object_id = OBJECT_ID('CFG.IngestionRoute'))
BEGIN
    CREATE INDEX IX_CFG_IngestionRoute_TenantID ON CFG.IngestionRoute (TenantID);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_TenantSetting_Env_Tenant_Key' AND object_id = OBJECT_ID('CFG.TenantSetting'))
BEGIN
    CREATE INDEX IX_CFG_TenantSetting_Env_Tenant_Key ON CFG.TenantSetting (EnvCode, TenantCode, SettingKey);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_CFG_IngestionPackRule_RouteID' AND object_id = OBJECT_ID('CFG.IngestionPackRule'))
BEGIN
    CREATE INDEX IX_CFG_IngestionPackRule_RouteID ON CFG.IngestionPackRule (RouteID);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_EXC_ExecutionLog_ExecutionID' AND object_id = OBJECT_ID('EXC.ExecutionLog'))
BEGIN
    CREATE INDEX IX_EXC_ExecutionLog_ExecutionID ON EXC.ExecutionLog (ExecutionID);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ING_ProcessFile_GraphID' AND object_id = OBJECT_ID('ING.ProcessFile'))
BEGIN
    CREATE INDEX IX_ING_ProcessFile_GraphID ON ING.ProcessFile (GraphID);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ING_ProcessFile_Tenant_Status' AND object_id = OBJECT_ID('ING.ProcessFile'))
BEGIN
    CREATE INDEX IX_ING_ProcessFile_Tenant_Status ON ING.ProcessFile (EnvCode, TenantCode, Status);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ING_LoadRow_ProcessFileID' AND object_id = OBJECT_ID('ING.LoadRow'))
BEGIN
    CREATE INDEX IX_ING_LoadRow_ProcessFileID ON ING.LoadRow (ProcessFileID);
END;
GO