/*
    FUSION FLOW V3 QAS - DATABASE SETUP - FILE 16 OF N
    =================================================
    Purpose : Align CFG.Choice_Field_Registry / CFG.Choice_Field_Map to the
              authoritative TSS Choice Fields reference (35 fields).

              Corrects file 014, which had wrongly added transport_charges /
              controlled_goods_type / package_type (NONE are /choice_values
              endpoints - they return HTTP 400), and mapped header movement_type
              to mode_of_transport. Per the TSS reference, header movement_type is
              served by GET /choice_values/movement_type; mode_of_transport is for
              FFD/IMMI only. transport_charges is a fixed value (BKD QAS = Y).

    Run after : 014 (field map). Safe to rerun.
*/

/* ------------------------------------------------------------------ */
/* Registry: add a UsedBy column; remove the invalid fields.           */
/* ------------------------------------------------------------------ */
IF COL_LENGTH('CFG.Choice_Field_Registry', 'UsedBy') IS NULL
    ALTER TABLE CFG.Choice_Field_Registry ADD UsedBy nvarchar(200) NULL;
GO

DELETE FROM CFG.Choice_Value_Cache
 WHERE ChoiceField IN ('transport_charges', 'controlled_goods_type', 'package_type');
DELETE FROM CFG.Choice_Field_Map
 WHERE ChoiceField IN ('transport_charges', 'controlled_goods_type', 'package_type');
DELETE FROM CFG.Choice_Field_Registry
 WHERE ChoiceField IN ('transport_charges', 'controlled_goods_type', 'package_type');
GO

/* ------------------------------------------------------------------ */
/* Map: header movement_type resolves against CV movement_type.        */
/* ------------------------------------------------------------------ */
UPDATE CFG.Choice_Field_Map
   SET ChoiceField = 'movement_type', UpdatedAt = SYSUTCDATETIME()
 WHERE ColumnName = 'movement_type' AND ChoiceField = 'mode_of_transport';
GO

/* ------------------------------------------------------------------ */
/* Registry: the authoritative 35 fields - descriptions + UsedBy.      */
/* ------------------------------------------------------------------ */
MERGE CFG.Choice_Field_Registry AS t
USING (VALUES
    ('country',                         'Alpha-2 country code',                          'All entities with country fields'),
    ('movement_type',                   'ENS/SFD movement type codes',                   '/tss_api/headers, /tss_api/sfd_headers'),
    ('port',                            'Port codes (ens_allowed, ffd_allowed flags)',   '/tss_api/headers'),
    ('procedure_code',                  'Customs procedure codes',                       'Goods Item'),
    ('additional_procedure_code',       'Additional procedure codes',                    'Goods Item'),
    ('commodity_code',                  'Commodity codes (with effective dates)',        'Goods Item'),
    ('document_code',                   'Document type codes',                           'DocumentReference'),
    ('document_status',                 'Document status codes',                         'DocumentReference'),
    ('auth_type_code',                  'Authorisation type codes',                      'HolderOfAuthorisation'),
    ('previous_document_type',          'Previous document type codes',                  'PreviousDocument'),
    ('additional_info_code',            'Additional information codes',                  'AdditionalInformation'),
    ('currency',                        'ISO currency codes',                            'Multiple entities'),
    ('sd_declaration_choice',           'SD declaration choices',                        'Consignment, SFDConsignment'),
    ('ffd_declaration_choice',          'FFD declaration choices (H1-H4)',               'FFD'),
    ('sfd_declaration_choice',          'SFD declaration choices',                       'Consignment'),
    ('declaration_category',            'Declaration category codes',                    'FFD, IMMI'),
    ('goods_domestic_status',           'Domestic status codes',                         'Consignment, FFD, SupDec'),
    ('incoterm',                        'Incoterm codes',                                'FFD'),
    ('mode_of_transport',               'Mode of transport codes',                       'FFD, IMMI'),
    ('method_of_payment',               'Method of payment codes',                       'FFD'),
    ('valuation_method',                'Valuation method codes',                        'Goods Item'),
    ('valuation_indicator',             'Valuation indicator codes',                     'Goods Item'),
    ('nature_of_transaction',           'Nature of transaction codes',                   'Goods Item'),
    ('no_sfd_reason',                   'Reason codes for no SFD',                       'Consignment'),
    ('gvms_routes',                     'GVMS route codes',                              'GVMS GMR'),
    ('transport_document_type',         'Transport document type codes',                 'IMMI'),
    ('passive_transport_types',         'Passive transport type codes',                  'Declaration Header'),
    ('load_type',                       'Load type codes',                               'Maritime ICR'),
    ('cargo_or_consignment',            'Cargo/consignment codes',                       'Maritime ICR'),
    ('final_destination_location_code', 'Final destination codes',                       'Maritime ICR'),
    ('guarantee_type',                  'Guarantee type codes',                          'GuaranteeType'),
    ('tax_base_unit',                   'Tax base unit codes',                           'TaxBase, Goods Item'),
    ('tax_type',                        'Tax type codes',                                'TaxBase, Goods Item'),
    ('preference',                      'Tariff preference codes',                       'Goods Item'),
    ('ni_additional_information_code',  'NI additional info codes',                      'Goods Item')
) AS s (ChoiceField, Description, UsedBy)
ON t.ChoiceField = s.ChoiceField
WHEN MATCHED THEN UPDATE SET Description = s.Description, UsedBy = s.UsedBy,
    ApiPath = CONCAT('/choice_values/', s.ChoiceField), IsActive = 1, UpdatedAt = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (ChoiceField, Description, UsedBy, ApiPath)
    VALUES (s.ChoiceField, s.Description, s.UsedBy, CONCAT('/choice_values/', s.ChoiceField));
GO
