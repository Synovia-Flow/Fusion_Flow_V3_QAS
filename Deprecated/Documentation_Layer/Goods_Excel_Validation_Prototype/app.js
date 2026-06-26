// ==========================
// VALIDATION CONFIGURATION
// ==========================
// This prototype is used to document the rules that a customer goods Excel file
// must pass before the rows can be processed as consignments.

const EXPECTED_COLUMNS = [
  {
    key: "goods_item_number",
    label: "Goods item number",
    required: "Pending",
    type: "Text / number",
    rule: "Expected to identify each goods row. Required status must be confirmed.",
    aliases: ["goods_item_number", "goods_item_no", "item_number", "item_no", "line_number", "line_no"],
  },
  {
    key: "transport_document_number",
    label: "Transport document number",
    required: "Yes",
    type: "Text",
    rule: "Required. Used as the default consignment grouping reference.",
    aliases: ["transport_document_number", "transport_doc_number", "tdn", "transport_document", "document_number"],
  },
  {
    key: "goods_description",
    label: "Goods description",
    required: "Yes",
    type: "Text",
    rule: "Required. Empty descriptions cannot be processed.",
    aliases: ["goods_description", "description", "item_description", "product_description", "commodity_description"],
  },
  {
    key: "commodity_code",
    label: "Commodity code",
    required: "Conditional",
    type: "Numeric text",
    rule: "Conditional by TSS API: min 8 digits, or 6 only when APC is 1SG. Mandatory for SD, FFD and IMMI.",
    aliases: ["commodity_code", "hs_code", "tariff_code", "cn_code", "goods_code"],
  },
  {
    key: "gross_mass_kg",
    label: "Gross mass kg",
    required: "Yes",
    type: "Number",
    rule: "Required. Must be greater than zero.",
    aliases: ["gross_mass_kg", "gross_mass", "gross_weight", "gross_weight_kg", "gross_kg"],
  },
  {
    key: "net_mass_kg",
    label: "Net mass kg",
    required: "Pending",
    type: "Number",
    rule: "If provided, it must be greater than or equal to zero and not greater than gross mass.",
    aliases: ["net_mass_kg", "net_mass", "net_weight", "net_weight_kg", "net_kg"],
  },
  {
    key: "number_of_packages",
    label: "Number of packages",
    required: "Yes",
    type: "Integer",
    rule: "Required. Must be a whole number greater than zero.",
    aliases: ["number_of_packages", "packages", "package_count", "no_of_packages", "number_packages"],
  },
  {
    key: "type_of_packages",
    label: "Type of packages",
    required: "Yes",
    type: "Code / text",
    rule: "Required. Code list and text-to-code mapping are pending confirmation.",
    aliases: ["type_of_packages", "package_type", "kind_of_packages", "package_kind"],
  },
  {
    key: "country_of_origin",
    label: "Country of origin",
    required: "Conditional",
    type: "ISO country code",
    rule: "Conditional by TSS API: required when preference is between 100 and 199.",
    aliases: ["country_of_origin", "origin_country", "coo", "origin"],
  },
  {
    key: "item_invoice_amount",
    label: "Invoice amount",
    required: "No",
    type: "Number",
    rule: "Not mandatory in the TSS Goods Item contract. Validate only if provided or if a later SDI/valuation rule requires it.",
    aliases: ["item_invoice_amount", "invoice_amount", "item_value", "value", "customs_value"],
  },
  {
    key: "item_invoice_currency",
    label: "Invoice currency",
    required: "No",
    type: "ISO currency code",
    rule: "Not mandatory in the TSS Goods Item contract. Validate only if provided or if a later SDI/valuation rule requires it.",
    aliases: ["item_invoice_currency", "invoice_currency", "currency", "value_currency"],
  },
  {
    key: "controlled_goods",
    label: "Controlled goods flag",
    required: "Yes",
    type: "Boolean / yes-no",
    rule: "Mandatory at TSS consignment level. The open decision is the source/default, not whether the field is required.",
    aliases: ["controlled_goods", "is_controlled_goods", "controlled"],
  },
  {
    key: "controlled_goods_type",
    label: "Controlled goods type",
    required: "Pending",
    type: "Text",
    rule: "Required when controlled goods are present. Rule is pending confirmation.",
    aliases: ["controlled_goods_type", "controlled_type", "dangerous_goods_type"],
  },
  {
    key: "package_marks",
    label: "Package marks",
    required: "Yes",
    type: "Text",
    rule: "Mandatory in the TSS Goods Item contract. Use ADDR if marks are unknown.",
    aliases: ["package_marks", "marks", "shipping_marks"],
  },
];


const PROD_API_COLUMNS = [
  {
    key: "trader_reference",
    label: "Trader reference",
    required: "No",
    type: "Text",
    rule: "Not mandatory in the TSS Consignment contract, but useful for source traceability and natural keys.",
    aliases: ["trader_reference", "trader_ref", "customer_reference", "manifest_reference", "document_no"],
  },
  {
    key: "destination_country",
    label: "Destination country",
    required: "No",
    type: "ISO country code",
    rule: "Not mandatory for TSS Consignment/SFD Consignment. Required for Full Frontier Declaration paths.",
    aliases: ["destination_country", "dest_country", "country_of_destination"],
  },
  {
    key: "goods_domestic_status",
    label: "Goods domestic status",
    required: "Pending",
    type: "Choice value",
    rule: "Required for SDI flow. Can normally come from trader default if stable.",
    aliases: ["goods_domestic_status", "domestic_status"],
  },
  {
    key: "container_indicator",
    label: "Container indicator",
    required: "Pending",
    type: "0 / 1",
    rule: "Required when the movement is containerised. If 1, equipment number becomes required.",
    aliases: ["container_indicator", "containerised", "containerized", "container"],
  },
  {
    key: "importer_eori",
    label: "Importer EORI",
    required: "Yes",
    type: "EORI",
    rule: "Mandatory in the TSS Consignment contract. The open decision is the data source.",
    aliases: ["importer_eori", "importer_eori_number"],
  },
  {
    key: "consignor_eori",
    label: "Consignor EORI",
    required: "Yes",
    type: "EORI",
    rule: "Mandatory in the TSS Consignment contract. GB EORI is not accepted for this field.",
    aliases: ["consignor_eori", "shipper_eori", "sender_eori"],
  },
  {
    key: "consignee_eori",
    label: "Consignee EORI",
    required: "Yes",
    type: "EORI",
    rule: "Mandatory in the TSS Consignment contract. The open decision is the data source.",
    aliases: ["consignee_eori", "receiver_eori"],
  },
  {
    key: "exporter_eori",
    label: "Exporter EORI",
    required: "Yes",
    type: "EORI",
    rule: "Mandatory in the TSS Consignment contract. The open decision is the data source.",
    aliases: ["exporter_eori"],
  },
  {
    key: "procedure_code",
    label: "Procedure code",
    required: "Conditional",
    type: "Choice value",
    rule: "Required for controlled goods and SDI goods. Can be source/product/trader/default.",
    aliases: ["procedure_code", "procedure"],
  },
  {
    key: "additional_procedure_code",
    label: "Additional procedure code",
    required: "Conditional",
    type: "Choice value",
    rule: "Required for controlled goods and SDI goods. Can be source/product/trader/default.",
    aliases: ["additional_procedure_code", "additional_procedure", "additional_procedure_codes"],
  },
  {
    key: "taric_code",
    label: "TARIC code",
    required: "Conditional",
    type: "Compact numeric text",
    rule: "Required only when the declaration path needs TARIC. v2.9.5 expects compact text without separators.",
    aliases: ["taric_code", "taric"],
  },
  {
    key: "valuation_method",
    label: "Valuation method",
    required: "Conditional",
    type: "Choice value",
    rule: "Required for SDI goods. Usually suitable for trader/product default.",
    aliases: ["valuation_method"],
  },
  {
    key: "nature_of_transaction",
    label: "Nature of transaction",
    required: "Conditional",
    type: "Choice value",
    rule: "Required for SDI goods. Usually suitable for trader/product default.",
    aliases: ["nature_of_transaction", "transaction_nature"],
  },
  {
    key: "preference",
    label: "Preference",
    required: "Conditional",
    type: "Choice value",
    rule: "Required for SDI goods. Usually suitable for product/trader default.",
    aliases: ["preference", "preference_code"],
  },
  {
    key: "ni_additional_information_codes",
    label: "NI additional information codes",
    required: "Conditional",
    type: "Choice value",
    rule: "Required for SDI goods where the NI route applies. Usually suitable for product/trader default.",
    aliases: ["ni_additional_information_codes", "national_additional_codes", "ni_addl_info_code"],
  },
  {
    key: "invoice_number",
    label: "Invoice number",
    required: "Conditional",
    type: "Text",
    rule: "Useful for SDI document references. Can fall back to transport document number if approved.",
    aliases: ["invoice_number", "invoice_no", "commercial_invoice"],
  },
];

EXPECTED_COLUMNS.push(...PROD_API_COLUMNS);
const PARTY_AND_ADDRESS_COLUMNS = [
  { key: "consignment_description", label: "Consignment description", required: "Pending", type: "Text", rule: "Useful consignment-level description. Confirm if it maps to consignment goods_description or internal description.", aliases: ["consignment_description", "consignment_desc"] },
  { key: "consignor_name", label: "Consignor name", required: "Pending", type: "Text", rule: "Party detail used when EORI is missing or for audit/master data.", aliases: ["consignor_name", "shipper_name", "sender_name"] },
  { key: "consignor_street_number", label: "Consignor street/number", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for audit/master data.", aliases: ["consignor_street_number", "consignor_street_and_number", "consignor_address"] },
  { key: "consignor_city", label: "Consignor city", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for audit/master data.", aliases: ["consignor_city"] },
  { key: "consignor_postcode", label: "Consignor postcode", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for audit/master data.", aliases: ["consignor_postcode", "consignor_post_code"] },
  { key: "consignor_country", label: "Consignor country", required: "Pending", type: "ISO country code", rule: "Party address country. Expected as ISO2 when used for API/master data.", aliases: ["consignor_country"] },

  { key: "consignee_name", label: "Consignee name", required: "Pending", type: "Text", rule: "Party detail used when EORI is missing or for audit/master data.", aliases: ["consignee_name", "receiver_name"] },
  { key: "consignee_street_number", label: "Consignee street/number", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for audit/master data.", aliases: ["consignee_street_number", "consignee_street_and_number", "consignee_address"] },
  { key: "consignee_city", label: "Consignee city", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for audit/master data.", aliases: ["consignee_city"] },
  { key: "consignee_postcode", label: "Consignee postcode", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for audit/master data.", aliases: ["consignee_postcode", "consignee_post_code"] },
  { key: "consignee_country", label: "Consignee country", required: "Pending", type: "ISO country code", rule: "Party address country. Expected as ISO2 when used for API/master data.", aliases: ["consignee_country"] },

  { key: "importer_name", label: "Importer name", required: "Pending", type: "Text", rule: "Party detail used when EORI is missing or for SDI/master data.", aliases: ["importer_name"] },
  { key: "importer_street_number", label: "Importer street/number", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for SDI/master data.", aliases: ["importer_street_number", "importer_street_and_number", "importer_address"] },
  { key: "importer_city", label: "Importer city", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for SDI/master data.", aliases: ["importer_city"] },
  { key: "importer_postcode", label: "Importer postcode", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for SDI/master data.", aliases: ["importer_postcode", "importer_post_code"] },
  { key: "importer_country", label: "Importer country", required: "Pending", type: "ISO country code", rule: "Party address country. Expected as ISO2 when used for API/master data.", aliases: ["importer_country"] },

  { key: "exporter_name", label: "Exporter name", required: "Pending", type: "Text", rule: "Party detail used when EORI is missing or for SDI/master data.", aliases: ["exporter_name"] },
  { key: "exporter_street_number", label: "Exporter street/number", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for SDI/master data.", aliases: ["exporter_street_number", "exporter_street_and_number", "exporter_address"] },
  { key: "exporter_city", label: "Exporter city", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for SDI/master data.", aliases: ["exporter_city"] },
  { key: "exporter_postcode", label: "Exporter postcode", required: "Pending", type: "Text", rule: "Party address detail used when EORI is missing or for SDI/master data.", aliases: ["exporter_postcode", "exporter_post_code"] },
  { key: "exporter_country", label: "Exporter country", required: "Pending", type: "ISO country code", rule: "Party address country. Expected as ISO2 when used for API/master data.", aliases: ["exporter_country"] },

  { key: "buyer_same_as_importer", label: "Buyer same as importer", required: "Pending", type: "Yes / no", rule: "Can reduce buyer data requirements when approved.", aliases: ["buyer_same_as_importer", "buyer_importer_same"] },
  { key: "buyer_eori", label: "Buyer EORI", required: "Pending", type: "EORI", rule: "Buyer identifier when buyer is not the importer.", aliases: ["buyer_eori"] },
  { key: "buyer_name", label: "Buyer name", required: "Pending", type: "Text", rule: "Buyer party detail when buyer is not the importer.", aliases: ["buyer_name"] },
  { key: "buyer_street_number", label: "Buyer street/number", required: "Pending", type: "Text", rule: "Buyer party address when buyer is not the importer.", aliases: ["buyer_street_number", "buyer_street_and_number", "buyer_address"] },
  { key: "buyer_city", label: "Buyer city", required: "Pending", type: "Text", rule: "Buyer party address when buyer is not the importer.", aliases: ["buyer_city"] },
  { key: "buyer_postcode", label: "Buyer postcode", required: "Pending", type: "Text", rule: "Buyer party address when buyer is not the importer.", aliases: ["buyer_postcode", "buyer_post_code"] },
  { key: "buyer_country", label: "Buyer country", required: "Pending", type: "ISO country code", rule: "Buyer country when buyer is not the importer.", aliases: ["buyer_country"] },

  { key: "seller_same_as_exporter", label: "Seller same as exporter", required: "Pending", type: "Yes / no", rule: "Can reduce seller data requirements when approved.", aliases: ["seller_same_as_exporter", "seller_exporter_same"] },
  { key: "seller_eori", label: "Seller EORI", required: "Pending", type: "EORI", rule: "Seller identifier when seller is not the exporter.", aliases: ["seller_eori"] },
  { key: "seller_name", label: "Seller name", required: "Pending", type: "Text", rule: "Seller party detail when seller is not the exporter.", aliases: ["seller_name"] },
  { key: "seller_street_number", label: "Seller street/number", required: "Pending", type: "Text", rule: "Seller party address when seller is not the exporter.", aliases: ["seller_street_number", "seller_street_and_number", "seller_address"] },
  { key: "seller_city", label: "Seller city", required: "Pending", type: "Text", rule: "Seller party address when seller is not the exporter.", aliases: ["seller_city"] },
  { key: "seller_postcode", label: "Seller postcode", required: "Pending", type: "Text", rule: "Seller party address when seller is not the exporter.", aliases: ["seller_postcode", "seller_post_code"] },
  { key: "seller_country", label: "Seller country", required: "Pending", type: "ISO country code", rule: "Seller country when seller is not the exporter.", aliases: ["seller_country"] },
];

EXPECTED_COLUMNS.push(...PARTY_AND_ADDRESS_COLUMNS);

const FIELD_OVERRIDES = {
  commodity_code: {
    required: "Conditional",
    rule: "Conditional by TSS API: min 8 digits, or 6 only when APC is 1SG. Mandatory for SD, FFD and IMMI.",
  },
  controlled_goods: {
    required: "Yes",
    rule: "Mandatory at TSS consignment level. Can only default to no with customer approval.",
  },
  item_invoice_currency: {
    required: "No",
    rule: "Not mandatory in the TSS Goods Item contract. Validate only if provided or if a later SDI/valuation rule requires it.",
  },
  package_marks: {
    required: "Yes",
    rule: "Required. Production uses ADDR as a safe fallback when marks are not known.",
  },
  net_mass_kg: {
    rule: "Optional for ENS/SFD unless conditional, required for controlled/SDI quality. Can default from gross only with approval.",
  },
};

EXPECTED_COLUMNS.forEach((column) => {
  const override = FIELD_OVERRIDES[column.key];
  if (override) {
    Object.assign(column, override);
  }
});

const PACKAGE_TYPE_MAP = {
  box: "PK",
  boxes: "PK",
  carton: "PK",
  cartons: "PK",
  package: "PK",
  packages: "PK",
  pallet: "pallets",
  pallets: "pallets",
  plt: "pallets",
  plts: "pallets",
  bx: "BX",
  pk: "PK",
  ct: "CT",
};

const DECISION_ITEMS = [
  {
    field: "package_marks",
    need: "Required for goods payload readiness.",
    source: "Excel or default ADDR.",
    ease: "Easy",
    decision: "Approve ADDR fallback when the customer file has no marks.",
    status: "pending",
  },
  {
    field: "controlled_goods",
    need: "Mandatory at TSS consignment level.",
    source: "Excel, product master data, or controlled_goods_type inference.",
    ease: "Medium",
    decision: "Confirm source/default for the yes/no value; do not ask whether the API requires it.",
    status: "pending",
  },
  {
    field: "commodity_code",
    need: "Conditional by TSS API; mandatory for SD, FFD and IMMI.",
    source: "Excel or product master data.",
    ease: "Hard if only 6 digits are supplied",
    decision: "Confirm declaration path and enrichment source for short commodity codes.",
    status: "warning",
  },
  {
    field: "type_of_packages",
    need: "Required choice value.",
    source: "Excel text/code or package type default.",
    ease: "Easy",
    decision: "Approve mapping such as Boxes -> PK/BX per tenant.",
    status: "pending",
  },
  {
    field: "item_invoice_currency",
    need: "Not mandatory in the TSS Goods Item contract.",
    source: "Excel, product/trader default, or tenant default.",
    ease: "Easy if SDI/valuation requires it later",
    decision: "Confirm only if SDI/valuation processing needs it in the MVP.",
    status: "pending",
  },
  {
    field: "net_mass_kg",
    need: "Conditional for ENS/SFD, important/required for SDI and controlled goods.",
    source: "Excel, product weights, or gross mass fallback.",
    ease: "Medium",
    decision: "Confirm if blank net mass can default to gross mass.",
    status: "pending",
  },
  {
    field: "procedure_code / additional_procedure_code",
    need: "Required for controlled goods and SDI goods.",
    source: "Excel, product master data, trader default.",
    ease: "Medium",
    decision: "Confirm customer-level defaults such as 4000 / 000 where valid.",
    status: "pending",
  },
  {
    field: "valuation_method / nature_of_transaction / preference / NI codes",
    need: "Required for SDI goods readiness.",
    source: "Product/trader defaults.",
    ease: "Medium",
    decision: "Confirm if defaults from existing production template can be reused.",
    status: "pending",
  },
  {
    field: "party EORIs and addresses",
    need: "Needed for consignment/SDI party validation.",
    source: "Excel or master data by customer/trader.",
    ease: "Medium",
    decision: "Confirm which parties are fixed per tenant and which must come from the file.",
    status: "pending",
  },
  {
    field: "99 goods split",
    need: "TSS goods limit per consignment.",
    source: "Calculated from row count after grouping.",
    ease: "Easy",
    decision: "Keep automatic split every 99 goods rows per consignment group.",
    status: "valid",
  },
];
const DEFAULT_GROUP_FIELDS = [
  "transport_document_number",
  "commodity_code",
  "goods_description",
  "country_of_origin",
  "type_of_packages",
];

const state = {
  headers: [],
  rows: [],
  mapping: [],
  issues: [],
  rowIssueLevel: new Map(),
  consolidatedRows: [],
};

// ==========================
// PAGE EVENTS
// ==========================

document.addEventListener("DOMContentLoaded", () => {
  renderEmptyState();

  document.getElementById("processButton").addEventListener("click", processFile);
  document.getElementById("resetButton").addEventListener("click", resetPrototype);

  document.querySelectorAll(".group-field").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      if (state.rows.length > 0) {
        state.consolidatedRows = buildConsolidation();
        renderConsolidation();
      }
    });
  });
});

// ==========================
// MAIN SCRIPT FLOW
// ==========================

async function processFile() {
  resetResultsOnly();

  const referenceInput = document.getElementById("consignmentReference");
  const fileInput = document.getElementById("excelFile");
  const consignmentReference = referenceInput.value;
  const file = fileInput.files[0];

  validateConsignmentReference(consignmentReference);

  if (!file) {
    addIssue(0, "Excel file", "", "File required", "error", "Select a customer Excel file.", "Upload a .xlsx or .xls file.");
    renderAll();
    return;
  }

  validateExcelFile(file);

  try {
    const workbook = await readWorkbook(file);
    loadFirstSheet(workbook);
    state.mapping = buildColumnMapping(state.headers);
    validateHeaderRules();
    validateRows();
    validateCrossRows();
    state.consolidatedRows = buildConsolidation();
  } catch (error) {
    addIssue(0, "Excel file", file.name, "Read workbook", "error", error.message, "Confirm the file is a valid Excel workbook.");
  }

  renderAll();
}

// ==========================
// EXCEL READING
// ==========================

function readWorkbook(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();

    reader.onload = (event) => {
      try {
        const data = new Uint8Array(event.target.result);
        const workbook = XLSX.read(data, { type: "array" });
        resolve(workbook);
      } catch (error) {
        reject(new Error("The file could not be read as an Excel workbook."));
      }
    };

    reader.onerror = () => reject(new Error("The browser could not read the selected file."));
    reader.readAsArrayBuffer(file);
  });
}

function loadFirstSheet(workbook) {
  if (!workbook.SheetNames || workbook.SheetNames.length === 0) {
    throw new Error("The workbook does not contain any sheets.");
  }

  const firstSheetName = workbook.SheetNames[0];
  const sheet = workbook.Sheets[firstSheetName];
  const matrix = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: "", raw: false });
  const headerRowIndex = findHeaderRow(matrix);

  if (headerRowIndex < 0) {
    addIssue(0, firstSheetName, "", "Header row", "error", "The first sheet does not contain a recognisable header row.", "Use a goods template with named columns.");
    state.rows = [];
    state.headers = [];
    return;
  }

  state.headers = matrix[headerRowIndex]
    .map((header) => cleanText(header))
    .filter((header) => header !== "");

  state.rows = matrix
    .slice(headerRowIndex + 1)
    .map((values) => rowArrayToObject(state.headers, values))
    .filter((row) => !isEmptyRow(row));

  if (state.rows.length === 0) {
    addIssue(0, firstSheetName, "", "Sheet data", "error", "The first sheet does not contain goods rows after the header.", "Provide at least one goods row.");
  }
}

function findHeaderRow(matrix) {
  const knownAliases = new Set(EXPECTED_COLUMNS.flatMap((column) => column.aliases));
  let bestIndex = -1;
  let bestScore = 0;

  matrix.slice(0, 12).forEach((row, index) => {
    const score = row.reduce((total, cell) => {
      const normalized = normalizeHeader(cell);
      return total + (knownAliases.has(normalized) ? 1 : 0);
    }, 0);

    if (score > bestScore) {
      bestScore = score;
      bestIndex = index;
    }
  });

  if (bestScore >= 2) {
    return bestIndex;
  }

  return matrix.findIndex((row) => row.filter((cell) => cleanText(cell)).length > 1);
}

function rowArrayToObject(headers, values) {
  return headers.reduce((record, header, index) => {
    record[header] = values[index] === undefined ? "" : values[index];
    return record;
  }, {});
}

// ==========================
// VALIDATION RULES
// ==========================

function validateConsignmentReference(value) {
  const trimmed = value.trim();

  if (!trimmed) {
    addIssue(0, "Consignment reference", value, "Reference required", "error", "The consignment / ENS reference is required.", "Enter the ENS reference before processing the Excel file.");
    return;
  }

  if (value !== trimmed) {
    addIssue(0, "Consignment reference", value, "No outer spaces", "warning", "The reference has leading or trailing spaces.", "Trim the reference before sending it to TSS.");
  }

  if (!/^ENS00[A-Z0-9-]+$/i.test(trimmed)) {
    addIssue(0, "Consignment reference", value, "Expected format", "warning", "The reference does not match the expected ENS00... pattern.", "Confirm the final reference format with TSS.");
  }

  if (!/^[A-Z0-9-]+$/i.test(trimmed)) {
    addIssue(0, "Consignment reference", value, "Allowed characters", "error", "The reference contains unexpected characters.", "Use only letters, numbers and hyphens.");
  }

  addIssue(0, "Consignment reference", trimmed, "Reference length", "pending", "Minimum and maximum length are pending confirmation.", "Confirm the official TSS length rule.");
}

function validateExcelFile(file) {
  const lowerName = file.name.toLowerCase();

  if (!lowerName.endsWith(".xlsx") && !lowerName.endsWith(".xls")) {
    addIssue(0, "Excel file", file.name, "File extension", "error", "Only Excel files are expected.", "Upload a .xlsx or .xls file.");
  }

  addIssue(0, "Excel file", formatBytes(file.size), "File size", "pending", "Maximum file size is pending confirmation.", "Define the operational file size limit.");
}

function validateHeaderRules() {
  if (state.headers.length === 0) {
    addIssue(0, "Headers", "", "Header row", "error", "No header row was found in the first sheet.", "Use a template with named columns.");
    return;
  }

  state.mapping.forEach((item) => {
    if (item.required === "Yes" && !item.foundHeader) {
      addIssue(0, item.label, "", "Required column", "error", `Missing required column: ${item.label}.`, "Add this column to the Excel template.");
    }

    if (item.required === "Conditional" && !item.foundHeader) {
      addIssue(0, item.label, "", "Conditional column", "pending", `${item.label} is not present in the file.`, "Only require it when the documented TSS API condition applies.");
    }

    if (item.required === "Pending" && !item.foundHeader) {
      addIssue(0, item.label, "", "Pending column", "pending", `${item.label} is not present in the file.`, "Confirm the business source/default, not the API mandatory status.");
    }
  });

  state.headers.forEach((header) => {
    const normalized = normalizeHeader(header);
    const known = EXPECTED_COLUMNS.some((column) => column.aliases.includes(normalized));

    if (!known) {
      addIssue(0, header, "", "Unmapped source column", "pending", "The Excel contains a column that is not recognised by the current prototype mapping.", "Decide whether to ignore it or add it to the tenant mapping.");
    }
  });
}

function validateRows() {
  const itemNumbers = new Map();

  state.rows.forEach((row, index) => {
    const rowNumber = index + 2;

    validateRequiredValue(row, rowNumber, "transport_document_number");
    validateRequiredValue(row, rowNumber, "goods_description");
    validateRequiredValue(row, rowNumber, "gross_mass_kg");
    validateRequiredValue(row, rowNumber, "number_of_packages");
    validateRequiredValue(row, rowNumber, "type_of_packages");
    validateRequiredValue(row, rowNumber, "package_marks");
    validateRequiredValue(row, rowNumber, "controlled_goods");
    validateRequiredValue(row, rowNumber, "consignor_eori");
    validateRequiredValue(row, rowNumber, "consignee_eori");
    validateRequiredValue(row, rowNumber, "importer_eori");
    validateRequiredValue(row, rowNumber, "exporter_eori");

    validateGoodsItemNumber(row, rowNumber, itemNumbers);
    validateCommodityCode(row, rowNumber);
    validateMass(row, rowNumber);
    validatePackages(row, rowNumber);
    validatePackageType(row, rowNumber);
    validateCountry(row, rowNumber);
    validateCurrency(row, rowNumber);
    validateInvoiceAmount(row, rowNumber);
    validateControlledGoods(row, rowNumber);
  });
}

function validateRequiredValue(row, rowNumber, key) {
  const column = getColumnConfig(key);
  const value = getValue(row, key);

  if (isBlank(value)) {
    addIssue(rowNumber, column.label, value, "Required value", "error", `${column.label} is required.`, "Populate the field before processing.");
  }
}

function validateGoodsItemNumber(row, rowNumber, itemNumbers) {
  const value = getValue(row, "goods_item_number");

  if (isBlank(value)) {
    addIssue(rowNumber, "Goods item number", value, "Item identifier", "pending", "Goods item number is missing or not mapped.", "Confirm if the system should generate it or require it from the customer.");
    return;
  }

  const normalized = String(value).trim();

  if (itemNumbers.has(normalized)) {
    addIssue(rowNumber, "Goods item number", value, "Unique item number", "error", "Duplicate goods item number found.", `Review duplicate with row ${itemNumbers.get(normalized)}.`);
  } else {
    itemNumbers.set(normalized, rowNumber);
  }
}

function validateCommodityCode(row, rowNumber) {
  const value = cleanText(getValue(row, "commodity_code"));

  if (!value) {
    return;
  }

  if (!/^\d+$/.test(value)) {
    addIssue(rowNumber, "Commodity code", value, "Numeric commodity code", "error", "Commodity code must contain numbers only.", "Remove letters, spaces or symbols.");
    return;
  }

  if (value.length < 6 || value.length === 7) {
    addIssue(rowNumber, "Commodity code", value, "Commodity code length", "error", "TSS expects at least 8 digits, except 6 digits only when APC is 1SG.", "Confirm product master data or enrichment source for this item.");
    return;
  }

  if (value.length === 6) {
    addIssue(rowNumber, "Commodity code", value, "Conditional commodity code", "warning", "A 6-digit commodity code is only valid when APC is 1SG.", "Confirm the additional procedure code before API submission.");
    return;
  }

  if (value.length > 10) {
    addIssue(rowNumber, "Commodity code", value, "Commodity code length", "error", "Commodity code cannot exceed 10 digits in the TSS contract.", "Use the accepted commodity code format.");
    return;
  }

  if (value.length === 9) {
    addIssue(rowNumber, "Commodity code", value, "Commodity code length", "warning", "Commodity code length is unusual for API submission.", "Confirm the expected length for this declaration path.");
  }
}

function validateMass(row, rowNumber) {
  const gross = toNumber(getValue(row, "gross_mass_kg"));
  const net = toNumber(getValue(row, "net_mass_kg"));

  if (gross.present && !gross.valid) {
    addIssue(rowNumber, "Gross mass kg", gross.raw, "Numeric gross mass", "error", "Gross mass must be numeric.", "Use a valid number.");
  }

  if (gross.valid && gross.value <= 0) {
    addIssue(rowNumber, "Gross mass kg", gross.raw, "Gross mass greater than zero", "error", "Gross mass must be greater than zero.", "Correct the gross mass.");
  }

  if (net.present && !net.valid) {
    addIssue(rowNumber, "Net mass kg", net.raw, "Numeric net mass", "error", "Net mass must be numeric.", "Use a valid number.");
  }

  if (net.valid && net.value < 0) {
    addIssue(rowNumber, "Net mass kg", net.raw, "Net mass non-negative", "error", "Net mass cannot be negative.", "Correct the net mass.");
  }

  if (gross.valid && net.valid && net.value > gross.value) {
    addIssue(rowNumber, "Net mass kg", net.raw, "Net mass <= gross mass", "error", "Net mass cannot be greater than gross mass.", "Review gross and net mass values.");
  }
}

function validatePackages(row, rowNumber) {
  const packages = toNumber(getValue(row, "number_of_packages"));

  if (packages.present && !packages.valid) {
    addIssue(rowNumber, "Number of packages", packages.raw, "Numeric packages", "error", "Number of packages must be numeric.", "Use a whole number.");
  }

  if (packages.valid && (!Number.isInteger(packages.value) || packages.value <= 0)) {
    addIssue(rowNumber, "Number of packages", packages.raw, "Packages greater than zero", "error", "Number of packages must be a whole number greater than zero.", "Correct the package count.");
  }
}

function validatePackageType(row, rowNumber) {
  const value = cleanText(getValue(row, "type_of_packages"));

  if (!value) {
    return;
  }

  const normalized = value.toLowerCase();
  const mapped = PACKAGE_TYPE_MAP[normalized];

  if (mapped && mapped !== value) {
    addIssue(rowNumber, "Type of packages", value, "Package type mapping", "pending", `Package type can be mapped to ${mapped}.`, "Confirm the tenant package mapping before API submission.");
    return;
  }

  if (!mapped && value.length > 2) {
    addIssue(rowNumber, "Type of packages", value, "Package type choice", "warning", "Package type text is not mapped to a known choice value.", "Add this value to the tenant package mapping or correct the file.");
  }
}

function validateCountry(row, rowNumber) {
  const value = cleanText(getValue(row, "country_of_origin")).toUpperCase();

  if (!value) {
    return;
  }

  if (!/^[A-Z]{2}$/.test(value)) {
    addIssue(rowNumber, "Country of origin", value, "ISO country code", "error", "Country of origin should be a 2-letter code.", "Use ISO format, for example GB, IE or CN.");
  }
}

function validateCurrency(row, rowNumber) {
  const value = cleanText(getValue(row, "item_invoice_currency")).toUpperCase();

  if (!value) {
    return;
  }

  if (!/^[A-Z]{3}$/.test(value)) {
    addIssue(rowNumber, "Invoice currency", value, "ISO currency code", "error", "Currency should be a 3-letter code.", "Use ISO format, for example GBP, EUR or USD.");
  }
}

function validateInvoiceAmount(row, rowNumber) {
  const amount = toNumber(getValue(row, "item_invoice_amount"));

  if (!amount.present) {
    return;
  }

  if (!amount.valid) {
    addIssue(rowNumber, "Invoice amount", amount.raw, "Numeric invoice amount", "error", "Invoice amount must be numeric.", "Use a valid number.");
  }

  if (amount.valid && amount.value < 0) {
    addIssue(rowNumber, "Invoice amount", amount.raw, "Invoice amount non-negative", "error", "Invoice amount cannot be negative.", "Correct the invoice amount.");
  }

  if (amount.valid && hasMoreThanTwoDecimals(amount.raw)) {
    addIssue(rowNumber, "Invoice amount", amount.raw, "Invoice amount precision", "warning", "Production validation expects no more than 2 decimal places.", "Round the invoice amount to 2 decimals.");
  }
}

function validateControlledGoods(row, rowNumber) {
  const controlledFlag = cleanText(getValue(row, "controlled_goods")).toLowerCase();
  const controlledType = cleanText(getValue(row, "controlled_goods_type"));

  if (!controlledFlag && controlledType) {
    addIssue(rowNumber, "Controlled goods", controlledType, "Controlled goods flag", "pending", "Controlled goods type exists but the controlled goods flag is missing.", "Confirm if the flag should be inferred as yes or required from the customer.");
    return;
  }

  if (controlledFlag && !/^(yes|no)$/.test(controlledFlag)) {
    addIssue(rowNumber, "Controlled goods", controlledFlag, "Controlled goods values", "warning", "Production validation expects controlled_goods as yes or no.", "Map the source value to yes/no before API submission.");
    return;
  }

  if (controlledFlag === "yes") {
    if (isBlank(getValue(row, "net_mass_kg"))) {
      addIssue(rowNumber, "Net mass kg", "", "Controlled goods requirement", "error", "Net mass is required when controlled_goods=yes.", "Provide net mass or approve a controlled-goods fallback rule.");
    }
    if (!controlledType) {
      addIssue(rowNumber, "Controlled goods type", "", "Controlled goods requirement", "error", "Controlled goods type is required when controlled_goods=yes.", "Provide the controlled goods type from the source or product master data.");
    }
    if (isBlank(getValue(row, "procedure_code"))) {
      addIssue(rowNumber, "Procedure code", "", "Controlled goods requirement", "error", "Procedure code is required when controlled_goods=yes.", "Use source, product or trader default.");
    }
    if (isBlank(getValue(row, "additional_procedure_code"))) {
      addIssue(rowNumber, "Additional procedure code", "", "Controlled goods requirement", "error", "Additional procedure is required when controlled_goods=yes.", "Use source, product or trader default.");
    }
    if (isBlank(getValue(row, "item_invoice_amount"))) {
      addIssue(rowNumber, "Invoice amount", "", "Controlled goods requirement", "error", "Invoice amount is required when controlled_goods=yes.", "Provide invoice amount from the source file or commercial invoice.");
    }
  }
}

function validateTaricCode(row, rowNumber) {
  const value = cleanText(getValue(row, "taric_code"));

  if (!value) {
    return;
  }

  const compact = value.replace(/[\s,;:/\\|_-]+/g, "");

  if (!/^\d+$/.test(compact)) {
    addIssue(rowNumber, "TARIC code", value, "TARIC format", "error", "TARIC code must be numeric after removing separators.", "Correct the TARIC value before API submission.");
    return;
  }

  if (compact !== value) {
    addIssue(rowNumber, "TARIC code", value, "TARIC compact format", "pending", `TARIC can be normalised to ${compact}.`, "Confirm automatic separator removal for v2.9.5.");
  }

  if (![8, 10].includes(compact.length) && compact.length % 4 !== 0) {
    addIssue(rowNumber, "TARIC code", value, "TARIC length", "warning", "TARIC length does not look like compact 4-character segments or a 10-digit code.", "Confirm the final TARIC rule for this declaration path.");
  }
}

function validateCrossRows() {
  // Duplicate consolidation keys are expected when multiple goods rows should be grouped.
  // The consolidation table shows the grouped line count instead of adding row warnings.
}

// ==========================
// CONSOLIDATION RULES
// ==========================

function buildConsolidation() {
  const groupFields = getSelectedGroupFields();
  const groups = new Map();

  state.rows.forEach((row, index) => {
    const rowNumber = index + 2;
    const keyValues = groupFields.map((field) => cleanText(getValue(row, field)) || "(blank)");
    const groupKey = keyValues.join(" | ");

    if (!groups.has(groupKey)) {
      groups.set(groupKey, {
        key: groupKey,
        rowNumbers: [],
        goodsItemNumbers: [],
        grossMassKg: 0,
        netMassKg: 0,
        packages: 0,
        invoiceAmount: 0,
        status: "valid",
        observations: [],
      });
    }

    const group = groups.get(groupKey);
    group.rowNumbers.push(rowNumber);
    group.goodsItemNumbers.push(cleanText(getValue(row, "goods_item_number")) || `row ${rowNumber}`);
    group.grossMassKg += numericValue(row, "gross_mass_kg");
    group.netMassKg += numericValue(row, "net_mass_kg");
    group.packages += numericValue(row, "number_of_packages");
    group.invoiceAmount += numericValue(row, "item_invoice_amount");

    const rowLevel = state.rowIssueLevel.get(rowNumber);
    if (rowLevel === "error") {
      group.status = "error";
    } else if (rowLevel === "warning" && group.status !== "error") {
      group.status = "warning";
    } else if (rowLevel === "pending" && group.status === "valid") {
      group.status = "pending";
    }
  });

  return Array.from(groups.values()).map((group) => {
    if (group.rowNumbers.length > 99) {
      group.status = "error";
      group.observations.push("More than 99 goods rows in one group. Split into additional consignments.");
    }

    if (group.rowNumbers.length > 1) {
      group.observations.push("Rows are grouped by the selected consolidation key.");
    }

    return group;
  });
}

function getSelectedGroupFields() {
  const selected = Array.from(document.querySelectorAll(".group-field:checked")).map((checkbox) => checkbox.value);
  return selected.length > 0 ? selected : DEFAULT_GROUP_FIELDS;
}

// ==========================
// COLUMN MAPPING
// ==========================

function buildColumnMapping(headers) {
  const normalizedHeaders = headers.map((header) => ({
    original: header,
    normalized: normalizeHeader(header),
  }));

  return EXPECTED_COLUMNS.map((column) => {
    const match = normalizedHeaders.find((header) => column.aliases.includes(header.normalized));
    let status = "valid";

    if (!match && column.required === "Yes") {
      status = "error";
    } else if (!match && (column.required === "Pending" || column.required === "Conditional")) {
      status = "pending";
    }

    return {
      ...column,
      foundHeader: match ? match.original : "",
      status,
    };
  });
}

function getColumnConfig(key) {
  return EXPECTED_COLUMNS.find((column) => column.key === key);
}

function getMappedHeader(key) {
  const mapping = state.mapping.find((item) => item.key === key);
  return mapping ? mapping.foundHeader : "";
}

function getValue(row, key) {
  if (key === "consignment_reference") {
    return document.getElementById("consignmentReference").value.trim();
  }

  const header = getMappedHeader(key);
  return header ? row[header] : "";
}

// ==========================
// RENDERING
// ==========================

function renderAll() {
  renderSummary();
  renderMapping();
  renderDecisionMatrix();
  renderIssues();
  renderConsolidation();
  renderDataPreview();
  renderPendingDecisions();
}

function renderSummary() {
  const errorRows = new Set(state.issues.filter((issue) => issue.severity === "error" && issue.row > 0).map((issue) => issue.row));
  const warningRows = new Set(state.issues.filter((issue) => issue.severity === "warning" && issue.row > 0).map((issue) => issue.row));
  const warningCount = state.issues.filter((issue) => issue.severity === "warning").length;
  const validGoods = state.rows.filter((_, index) => !errorRows.has(index + 2)).length;

  setText("totalRows", state.rows.length);
  setText("validGoods", validGoods);
  setText("validRows", validGoods);
  setText("errorRows", errorRows.size);
  setText("warningRows", warningRows.size);
  setText("warningCount", warningCount);
  setText("grossTotal", formatNumber(sumField("gross_mass_kg")));
  setText("packageTotal", formatNumber(sumField("number_of_packages")));
}

function renderMapping() {
  const body = document.getElementById("mappingBody");
  body.innerHTML = "";

  state.mapping.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(item.foundHeader || "Not found")}</td>
      <td>${escapeHtml(item.label)}</td>
      <td>${escapeHtml(item.required)}</td>
      <td>${escapeHtml(item.type)}</td>
      <td>${escapeHtml(item.rule)}</td>
      <td>${statusBadge(item.status)}</td>
    `;
    body.appendChild(row);
  });

  if (state.mapping.length === 0) {
    body.innerHTML = `<tr><td colspan="6" class="empty-state">Process an Excel file to see the mapping.</td></tr>`;
  }
}

function renderDecisionMatrix() {
  const body = document.getElementById("decisionBody");
  if (!body) {
    return;
  }

  body.innerHTML = "";

  DECISION_ITEMS.forEach((item) => {
    const row = document.createElement("tr");
    row.className = item.status === "warning" ? "row-warning" : "";
    row.innerHTML = `
      <td>${escapeHtml(item.field)}</td>
      <td>${escapeHtml(item.need)}</td>
      <td>${escapeHtml(item.source)}</td>
      <td>${escapeHtml(item.ease)}</td>
      <td>${escapeHtml(item.decision)}</td>
      <td>${statusBadge(item.status)}</td>
    `;
    body.appendChild(row);
  });
}
function renderIssues() {
  const body = document.getElementById("issuesBody");
  body.innerHTML = "";

  state.issues.forEach((issue) => {
    const row = document.createElement("tr");
    row.className = issue.severity === "error" ? "row-error" : issue.severity === "warning" ? "row-warning" : "";
    row.innerHTML = `
      <td>${issue.row || "File"}</td>
      <td>${escapeHtml(issue.column)}</td>
      <td>${escapeHtml(issue.value)}</td>
      <td>${escapeHtml(issue.rule)}</td>
      <td>${statusBadge(issue.severity)}</td>
      <td>${escapeHtml(issue.message)}</td>
      <td>${escapeHtml(issue.action)}</td>
    `;
    body.appendChild(row);
  });

  if (state.issues.length === 0) {
    body.innerHTML = `<tr><td colspan="7" class="empty-state">No issues detected yet.</td></tr>`;
  }
}

function renderConsolidation() {
  const body = document.getElementById("consolidationBody");
  body.innerHTML = "";

  state.consolidatedRows.forEach((group) => {
    const row = document.createElement("tr");
    row.className = group.status === "error" ? "row-error" : group.status === "warning" ? "row-warning" : "";
    row.innerHTML = `
      <td>${escapeHtml(group.key)}</td>
      <td>${formatNumber(group.grossMassKg)}</td>
      <td>${formatNumber(group.netMassKg)}</td>
      <td>${formatNumber(group.packages)}</td>
      <td>${formatNumber(group.invoiceAmount)}</td>
      <td>${group.rowNumbers.length}</td>
      <td>${escapeHtml(group.goodsItemNumbers.join(", "))}</td>
      <td>${statusBadge(group.status)}</td>
      <td>${escapeHtml(group.observations.join(" "))}</td>
    `;
    body.appendChild(row);
  });

  if (state.consolidatedRows.length === 0) {
    body.innerHTML = `<tr><td colspan="9" class="empty-state">Process an Excel file to see consolidated goods.</td></tr>`;
  }
}

function renderDataPreview() {
  const head = document.getElementById("dataHead");
  const body = document.getElementById("dataBody");
  head.innerHTML = "";
  body.innerHTML = "";

  if (state.rows.length === 0) {
    body.innerHTML = `<tr><td class="empty-state">Process an Excel file to preview the first rows.</td></tr>`;
    return;
  }

  const previewHeaders = state.headers.slice(0, 16);
  const headerRow = document.createElement("tr");
  previewHeaders.forEach((header) => {
    const th = document.createElement("th");
    th.textContent = header;
    headerRow.appendChild(th);
  });
  head.appendChild(headerRow);

  state.rows.slice(0, 25).forEach((excelRow, index) => {
    const rowNumber = index + 2;
    const row = document.createElement("tr");
    const level = state.rowIssueLevel.get(rowNumber);
    row.className = level === "error" ? "row-error" : level === "warning" ? "row-warning" : "";

    previewHeaders.forEach((header) => {
      const td = document.createElement("td");
      td.textContent = excelRow[header];
      row.appendChild(td);
    });

    body.appendChild(row);
  });
}

function renderPendingDecisions() {
  const pending = [
    "Confirm tenant defaults for package_marks, invoice currency and controlled_goods when the Excel is blank.",
    "Confirm product master data source for commodity enrichment, origin, weights, procedure codes and TARIC.",
    "Confirm which party fields are fixed per tenant and which must be supplied by PLE/CW files.",
    "Confirm whether net_mass_kg can default to gross_mass_kg when not supplied.",
    "Confirm the exact package type mapping for customer text values such as Boxes.",
    "Confirm SDI path rules: when generate_SD, procedure, valuation and NI fields become mandatory.",
    "Confirm automatic split into additional consignments when a group has more than 99 goods rows.",
  ];

  const list = document.getElementById("pendingList");
  list.innerHTML = pending.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
}

function renderEmptyState() {
  renderSummary();
  renderMapping();
  renderDecisionMatrix();
  renderIssues();
  renderConsolidation();
  renderDataPreview();
  renderPendingDecisions();
}

// ==========================
// STATE AND ISSUE HELPERS
// ==========================

function resetPrototype() {
  document.getElementById("consignmentReference").value = "";
  document.getElementById("excelFile").value = "";
  resetResultsOnly();
  renderEmptyState();
}

function resetResultsOnly() {
  state.headers = [];
  state.rows = [];
  state.mapping = [];
  state.issues = [];
  state.rowIssueLevel = new Map();
  state.consolidatedRows = [];
}

function addIssue(row, column, value, rule, severity, message, action) {
  state.issues.push({
    row,
    column,
    value: value === undefined || value === null ? "" : String(value),
    rule,
    severity,
    message,
    action,
  });

  if (row > 0) {
    const current = state.rowIssueLevel.get(row);
    state.rowIssueLevel.set(row, strongestSeverity(current, severity));
  }
}

function strongestSeverity(current, next) {
  const order = { valid: 0, pending: 1, warning: 2, error: 3 };
  if (!current) {
    return next;
  }
  return order[next] > order[current] ? next : current;
}

// ==========================
// DATA HELPERS
// ==========================

function normalizeHeader(header) {
  return String(header)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function cleanText(value) {
  return String(value === undefined || value === null ? "" : value).trim();
}

function isBlank(value) {
  return cleanText(value) === "";
}

function isEmptyRow(row) {
  return Object.values(row).every((value) => isBlank(value));
}

function toNumber(value) {
  const raw = cleanText(value).replace(/,/g, "");

  if (!raw) {
    return { raw: "", present: false, valid: false, value: 0 };
  }

  const parsed = Number(raw);
  return {
    raw,
    present: true,
    valid: Number.isFinite(parsed),
    value: Number.isFinite(parsed) ? parsed : 0,
  };
}

function numericValue(row, key) {
  const number = toNumber(getValue(row, key));
  return number.valid ? number.value : 0;
}

function sumField(key) {
  return state.rows.reduce((total, row) => total + numericValue(row, key), 0);
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("en-GB", {
    maximumFractionDigits: 3,
  });
}

function hasMoreThanTwoDecimals(rawValue) {
  const text = cleanText(rawValue);
  if (!text.includes(".")) {
    return false;
  }
  return text.split(".")[1].length > 2;
}
function formatBytes(bytes) {
  if (!bytes) {
    return "0 B";
  }

  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / Math.pow(1024, index);
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function statusBadge(status) {
  const safeStatus = status || "valid";
  return `<span class="status ${safeStatus}">${escapeHtml(safeStatus.toUpperCase())}</span>`;
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) {
    element.textContent = value;
  }
}

function escapeHtml(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
