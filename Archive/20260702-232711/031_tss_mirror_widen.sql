/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 31 OF N
    =================================================
    Purpose : Widen TSS.BKD_ENS_Header to hold every field the TSS header GET can
              return (per the API Reference read-back), so the live mirror is
              complete. Adds the place-of-acceptance / place-of-delivery fields and
              the carrier address lines that 027 didn't include.

    Run after : 027 (TSS mirror). Safe to rerun (COL_LENGTH guards).
*/

IF COL_LENGTH('TSS.BKD_ENS_Header', 'place_of_acceptance_same_as_loading') IS NULL
    ALTER TABLE TSS.BKD_ENS_Header ADD place_of_acceptance_same_as_loading varchar(3) NULL;
GO
IF COL_LENGTH('TSS.BKD_ENS_Header', 'place_of_acceptance') IS NULL
    ALTER TABLE TSS.BKD_ENS_Header ADD place_of_acceptance nvarchar(33) NULL;
GO
IF COL_LENGTH('TSS.BKD_ENS_Header', 'place_of_delivery_same_as_unloading') IS NULL
    ALTER TABLE TSS.BKD_ENS_Header ADD place_of_delivery_same_as_unloading varchar(3) NULL;
GO
IF COL_LENGTH('TSS.BKD_ENS_Header', 'place_of_delivery') IS NULL
    ALTER TABLE TSS.BKD_ENS_Header ADD place_of_delivery nvarchar(33) NULL;
GO
IF COL_LENGTH('TSS.BKD_ENS_Header', 'carrier_street_number') IS NULL
    ALTER TABLE TSS.BKD_ENS_Header ADD carrier_street_number nvarchar(35) NULL;
GO
IF COL_LENGTH('TSS.BKD_ENS_Header', 'carrier_city') IS NULL
    ALTER TABLE TSS.BKD_ENS_Header ADD carrier_city nvarchar(35) NULL;
GO
IF COL_LENGTH('TSS.BKD_ENS_Header', 'carrier_postcode') IS NULL
    ALTER TABLE TSS.BKD_ENS_Header ADD carrier_postcode nvarchar(9) NULL;
GO
