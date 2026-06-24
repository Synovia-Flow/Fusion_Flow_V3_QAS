/*
    Fusion_Flow_V3_QAS SQL migration.

    Purpose:
        Add the MVP Graph foreign keys and lookup indexes.

    Run order:
        Execute files in numeric filename order. Scripts are idempotent
        where practical so QAS can be refreshed safely.
*/
IF OBJECT_ID('ING.FK_ING_Graph_CFG_Graph', 'F') IS NULL
BEGIN
    ALTER TABLE ING.Graph WITH CHECK
    ADD CONSTRAINT FK_ING_Graph_CFG_Graph
        FOREIGN KEY (ConfigID)
        REFERENCES CFG.Graph (ConfigID);
END;
GO

IF OBJECT_ID('ING.FK_ING_Graph_EXC_Graph', 'F') IS NULL
BEGIN
    ALTER TABLE ING.Graph WITH CHECK
    ADD CONSTRAINT FK_ING_Graph_EXC_Graph
        FOREIGN KEY (ExecutionID)
        REFERENCES EXC.Graph (ExecutionID);
END;
GO

IF OBJECT_ID('STG.FK_STG_SalesOrder_ING_Graph', 'F') IS NULL
BEGIN
    ALTER TABLE STG.SalesOrder WITH CHECK
    ADD CONSTRAINT FK_STG_SalesOrder_ING_Graph
        FOREIGN KEY (GraphID)
        REFERENCES ING.Graph (GraphID);
END;
GO

IF OBJECT_ID('TSS.FK_TSS_Submission_STG_SalesOrder', 'F') IS NULL
BEGIN
    ALTER TABLE TSS.Submission WITH CHECK
    ADD CONSTRAINT FK_TSS_Submission_STG_SalesOrder
        FOREIGN KEY (SalesOrderID)
        REFERENCES STG.SalesOrder (SalesOrderID);
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_ING_Graph_ConfigID'
      AND object_id = OBJECT_ID('ING.Graph')
)
BEGIN
    CREATE INDEX IX_ING_Graph_ConfigID ON ING.Graph (ConfigID);
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_ING_Graph_ExecutionID'
      AND object_id = OBJECT_ID('ING.Graph')
)
BEGIN
    CREATE INDEX IX_ING_Graph_ExecutionID ON ING.Graph (ExecutionID);
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_STG_SalesOrder_GraphID'
      AND object_id = OBJECT_ID('STG.SalesOrder')
)
BEGIN
    CREATE INDEX IX_STG_SalesOrder_GraphID ON STG.SalesOrder (GraphID);
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_TSS_Submission_SalesOrderID'
      AND object_id = OBJECT_ID('TSS.Submission')
)
BEGIN
    CREATE INDEX IX_TSS_Submission_SalesOrderID ON TSS.Submission (SalesOrderID);
END;
GO
