/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 17 OF N
    =================================================
    Purpose : Widen CFG.Choice_Value_Cache so large TSS reference sets load without
              "String or binary data would be truncated". Some fields (e.g.
              additional_procedure_code, commodity_code) return long descriptions
              and longer codes than the original sizing assumed.

                ChoiceName : nvarchar(400) -> nvarchar(max)   (display text; not indexed)
                ChoiceValue: nvarchar(100) -> nvarchar(255)   (keyed; widen via UQ rebuild)

    Run after : 002 (table), 015 (sync columns). Safe to rerun (width guards).
*/

/* ChoiceName -> nvarchar(max). Not part of any index/constraint. */
IF COL_LENGTH('CFG.Choice_Value_Cache', 'ChoiceName') <> -1
    ALTER TABLE CFG.Choice_Value_Cache ALTER COLUMN ChoiceName nvarchar(max) NULL;
GO

/* ChoiceValue -> nvarchar(255). It is in UQ_CFG_Choice_Value_Cache, so drop the
   constraint, widen, then recreate. COL_LENGTH is bytes (2/char): 100->200, 255->510. */
IF COL_LENGTH('CFG.Choice_Value_Cache', 'ChoiceValue') < 510
BEGIN
    IF EXISTS (SELECT 1 FROM sys.key_constraints WHERE name = 'UQ_CFG_Choice_Value_Cache')
        ALTER TABLE CFG.Choice_Value_Cache DROP CONSTRAINT UQ_CFG_Choice_Value_Cache;

    ALTER TABLE CFG.Choice_Value_Cache ALTER COLUMN ChoiceValue nvarchar(255) NOT NULL;

    IF NOT EXISTS (SELECT 1 FROM sys.key_constraints WHERE name = 'UQ_CFG_Choice_Value_Cache')
        ALTER TABLE CFG.Choice_Value_Cache
            ADD CONSTRAINT UQ_CFG_Choice_Value_Cache UNIQUE (ChoiceField, ChoiceValue);
END;
GO
