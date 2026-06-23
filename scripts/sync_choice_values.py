"""
Choice-value table health check.

Ported and adapted from the older Birkdale repo.
This script does not fetch external data; it verifies that the expected
TSS.CV_* lookup tables exist and records current row counts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_standalone_connection
from app.job_logger import JobRun

CV_TABLES = [
    'CV_addition_deduction_code',
    'CV_additional_info_code',
    'CV_additional_procedure_code',
    'CV_ap_auth_role_code',
    'CV_ap_auth_role_type',
    'CV_auth_role_type',
    'CV_auth_type_code',
    'CV_cargo_or_consignment',
    'CV_commodity_code',
    'CV_controlled_goods_type',
    'CV_country',
    'CV_currency',
    'CV_declaration_category',
    'CV_document_code',
    'CV_document_status',
    'CV_ffd_declaration_choice',
    'CV_ffd_location_of_goods',
    'CV_final_destination_location_code',
    'CV_goods_domestic_status',
    'CV_guarantee_type',
    'CV_gvms_routes',
    'CV_incoterm',
    'CV_inland_mode_of_transport',
    'CV_item_add_ded_code',
    'CV_load_type',
    'CV_measurement_unit',
    'CV_method_of_payment',
    'CV_mode_of_transport',
    'CV_movement_type',
    'CV_national_additional_code',
    'CV_nature_of_transaction',
    'CV_ni_additional_information_code',
    'CV_no_sfd_reason',
    'CV_passive_transport_types',
    'CV_port',
    'CV_port_stg',
    'CV_preference',
    'CV_previous_document_class',
    'CV_previous_document_type',
    'CV_procedure_code',
    'CV_representation_type',
    'CV_route',
    'CV_sd_declaration_choice',
    'CV_sd_location_of_goods',
    'CV_sd_status',
    'CV_sfd_declaration_choice',
    'CV_sfd_header_movement_type',
    'CV_special_authorisation',
    'CV_standalone_sdi_authorisation_type',
    'CV_supervising_customs_office',
    'CV_tax_base_unit',
    'CV_tax_type',
    'CV_transport_charge',
    'CV_transport_document_type',
    'CV_type_of_package',
    'CV_un_locode',
    'CV_valuation_indicator',
    'CV_valuation_method',
]


def main():
    lines = []
    empty_tables = []
    missing_tables = []

    with JobRun('sync_choice_values', triggered_by='manual') as jr:
        conn = get_standalone_connection()
        cursor = conn.cursor()

        lines.append(f'Checking {len(CV_TABLES)} TSS choice-value tables.')
        print(lines[-1])

        for table in CV_TABLES:
            exists = cursor.execute(
                """
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = 'TSS' AND TABLE_NAME = ?
                """,
                table,
            ).fetchone()[0]

            if not exists:
                missing_tables.append(table)
                msg = f'MISSING: TSS.{table}'
                lines.append(msg)
                print(msg)
                continue

            count = cursor.execute(f"SELECT COUNT(*) FROM TSS.[{table}]").fetchone()[0]
            msg = f'OK: TSS.{table} -> {count} row(s)'
            lines.append(msg)
            print(msg)
            if count == 0:
                empty_tables.append(table)

        conn.close()

        summary = (
            f'Choice-value check complete. Missing={len(missing_tables)} '
            f'Empty={len(empty_tables)} Present={len(CV_TABLES) - len(missing_tables)}'
        )
        lines.append(summary)
        print(summary)

        if missing_tables:
            print('Missing tables:')
            for table in missing_tables:
                print(f'  - TSS.{table}')

        if empty_tables:
            print('Empty tables:')
            for table in empty_tables:
                print(f'  - TSS.{table}')

        jr.rows_processed = len(CV_TABLES) - len(missing_tables)
        jr.log_lines = lines


if __name__ == '__main__':
    main()
