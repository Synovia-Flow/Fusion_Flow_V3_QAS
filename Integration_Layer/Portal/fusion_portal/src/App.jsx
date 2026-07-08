import React, { useEffect, useMemo, useRef, useState } from 'react';
import { getAdminSettings, getApiDocsUrl, getConsignmentDetail, getConsignments, getControlTower, getDashboard, getDeclarations, getSession, getTssConnections, getValidationDiagnostics, loginPortal, prepareTssConsignmentSubmit, previewConsignmentUpload, saveAdminSettings, validateConsignmentPreview, testTssConnection } from './api';

const DEFAULT_SESSION = {
  tenantCode: 'SYNOVIA',
  tenantName: 'Synovia',
  username: 'synovia',
  role: 'CentralAdmin',
  mode: 'DEMO_ADMIN',
};
const DEFAULT_OPERATIONAL_CLIENT_CODE = 'BKD';
const TSS_ENVIRONMENT_DEMO_OPTION = { value: 'DEMO', label: 'Demo' };
const MASTER_LIVE_URL = 'https://synovia-flow-3-live.onrender.com/';
const MASTER_LIVE_EMBED_URL = '/master-live/index.html';

function normalizeEnvironmentMode(value) {
  const normalized = String(value || '').trim().toUpperCase();
  if (['PRD', 'PROD', 'PRODUCTION', 'LIVE'].includes(normalized)) return 'PRODUCTION';
  if (['TEST', 'TST', 'QAS', 'QA', 'UAT'].includes(normalized)) return 'TEST';
  return 'DEMO';
}

function environmentModeFromSettings(settings) {
  const rows = (settings?.sections || []).flatMap((section) => section.rows || []);
  const envRow = rows.find((row) => String(row.key || '').toUpperCase() === 'ENVIRONMENT');
  return envRow?.value ? normalizeEnvironmentMode(envRow.value) : '';
}

function environmentModeToSettingsValue(value) {
  const mode = normalizeEnvironmentMode(value);
  if (mode === 'PRODUCTION') return 'PRD';
  if (mode === 'TEST') return 'TST';
  return 'DEMO';
}

function choicesWithDemoMode(row) {
  const choices = Array.isArray(row?.choices) ? row.choices : [];
  return choices.some((choice) => String(choice.value || '').toUpperCase() === 'DEMO')
    ? choices
    : [TSS_ENVIRONMENT_DEMO_OPTION, ...choices];
}

function isTssEnvironmentUpdate(update) {
  return String(update?.sectionId || update?.section || update?.category || '').toUpperCase() === 'TSS_API'
    && String(update?.key || '').toUpperCase() === 'ENVIRONMENT';
}

const PORTAL_CLIENTS = [
  { tenantCode: 'BKD', tenantName: 'Birkdale' },
  { tenantCode: 'PLE', tenantName: 'Primeline Express' },
  { tenantCode: 'CWD', tenantName: 'Countrywide' },
];
const DEMO_ENS_BY_CLIENT = {
  PLE: {
    declarationNumber: 'ENS900000000000001',
    movementKey: 'DEMO-PLE-ENS-001',
    arrivalPort: 'GBAUBELBELBEL',
    carrierEori: 'GB123456789000',
  },
  CWD: {
    declarationNumber: 'ENS900000000000002',
    movementKey: 'DEMO-CWD-ENS-001',
    arrivalPort: 'GBAUBELBELBEL',
    carrierEori: 'GB123456789000',
  },
  BKD: {
    declarationNumber: 'ENS900000000000003',
    movementKey: 'DEMO-BKD-ENS-001',
    arrivalPort: 'GBAUBELBELBEL',
    carrierEori: 'GB123456789000',
  },
};
const SETTINGS_NAV_SECTIONS = [
  { id: 'TSS_API', label: 'TSS Portal API', icon: 'sync_alt' },
  { id: 'GRAPH', label: 'Inbound Email / Microsoft Graph', icon: 'mail' },
  { id: 'INGEST_AUTO', label: 'Ingestion & Folders', icon: 'drive_folder_upload' },
  { id: 'SDI_AUTO', label: 'SDI / SupDec Automation', icon: 'bolt' },
  { id: 'VALIDATION', label: 'Validation Controls', icon: 'shield' },
  { id: 'NOTIFY', label: 'Email Automation Notifications', icon: 'notifications' },
];

function mergeSettingsSections(rawSections = []) {
  const incomingSections = Array.isArray(rawSections) ? rawSections : [];
  const incomingById = new Map(
    incomingSections
      .filter((section) => section?.id)
      .map((section) => [section.id, section]),
  );
  const knownIds = new Set(SETTINGS_NAV_SECTIONS.map((section) => section.id));
  const mergedSections = SETTINGS_NAV_SECTIONS.map((fallback) => {
    const incoming = incomingById.get(fallback.id);
    return {
      ...fallback,
      ...(incoming || {}),
      rows: incoming?.rows || [],
    };
  });
  const extraSections = incomingSections.filter((section) => section?.id && !knownIds.has(section.id));
  return [...mergedSections, ...extraSections];
}

function clientOptionFor(clientCode) {
  return PORTAL_CLIENTS.find((client) => client.tenantCode === clientCode) || PORTAL_CLIENTS[0];
}

function sessionFallback(clientCode = DEFAULT_SESSION.tenantCode) {
  if (clientCode === DEFAULT_SESSION.tenantCode) return { ...DEFAULT_SESSION };
  const client = clientOptionFor(clientCode);
  return {
    ...DEFAULT_SESSION,
    mode: 'CLIENT_SESSION',
    tenantCode: client.tenantCode,
    tenantName: client.tenantName,
  };
}

function isSynoviaSession(session) {
  return (session?.tenantCode || '').toUpperCase() === DEFAULT_SESSION.tenantCode;
}

const PORTAL_SESSION_STORAGE_KEY = 'fusion_portal_session_v1';
const PERSISTABLE_VIEWS = new Set(['dashboard', 'declarations', 'upload', 'consignments', 'controlTower', 'masterLive', 'settings']);

function getPortalSessionStorage() {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function normalizeStoredPortalView(view) {
  return PERSISTABLE_VIEWS.has(view) ? view : 'dashboard';
}

function normalizeStoredSettingsSection(sectionId) {
  return SETTINGS_NAV_SECTIONS.some((section) => section.id === sectionId)
    ? sectionId
    : SETTINGS_NAV_SECTIONS[0].id;
}
function normalizeRouteSettingsSection(sectionId) {
  const normalized = String(sectionId || '')
    .trim()
    .replace(/[-\s]+/g, '_')
    .toUpperCase();
  if (!normalized) return SETTINGS_NAV_SECTIONS[0].id;
  const match = SETTINGS_NAV_SECTIONS.find((section) => section.id.toUpperCase() === normalized);
  return match?.id || SETTINGS_NAV_SECTIONS[0].id;
}

function routePartsFromPath(pathname = '/') {
  return String(pathname || '/')
    .replace(/^#+/, '')
    .replace(/^\/+|\/+$/g, '')
    .split('/')
    .filter(Boolean)
    .map((part) => decodeURIComponent(part));
}

function portalRouteFromLocation(location = typeof window !== 'undefined' ? window.location : null) {
  if (!location) return { view: '' };
  const rawHash = String(location.hash || '').replace(/^#/, '');
  const hashPath = rawHash.split('?')[0] || '';
  const hashSearch = rawHash.includes('?') ? `?${rawHash.split('?').slice(1).join('?')}` : '';
  const routePath = hashPath || location.pathname || '/';
  const parts = routePartsFromPath(routePath);

  if (!parts.length) return { view: '' };

  const route = parts[0].toLowerCase();
  if (route === 'login') return { view: 'login' };
  if (route === 'dashboard') return { view: 'dashboard' };
  if (route === 'upload') return { view: 'upload' };
  if (route === 'declarations' || route === 'ens') return { view: 'declarations', declarationId: parts[1] || '' };
  if (route === 'consignments') return { view: 'consignments', consignmentId: parts[1] || '' };
  if (route === 'control-tower' || route === 'controltower') return { view: 'controlTower' };
  if (route === 'master-live' || route === 'masterlive') return { view: 'masterLive' };
  if (route === 'settings') {
    const params = new URLSearchParams(hashSearch || location.search || '');
    const querySection = params.get('section') || params.get('settings') || (params.has('tss-api') ? 'TSS_API' : '');
    return { view: 'settings', settingsSection: normalizeRouteSettingsSection(parts[1] || querySection) };
  }
  return { view: 'dashboard' };
}

function portalPathForRoute(view, options = {}) {
  if (view === 'login') return '/#/login';
  if (view === 'upload') return '/#/upload';
  if (view === 'declarations') {
    const token = String(options.declarationId || '').trim();
    return token ? `/#/declarations/${encodeURIComponent(token)}` : '/#/declarations';
  }
  if (view === 'consignments') {
    const token = String(options.consignmentId || '').trim();
    return token ? `/#/consignments/${encodeURIComponent(token)}` : '/#/consignments';
  }
  if (view === 'controlTower') return '/#/control-tower';
  if (view === 'masterLive') return '/#/master-live';
  if (view === 'settings') return `/#/settings/${encodeURIComponent(options.settingsSection || SETTINGS_NAV_SECTIONS[0].id)}`;
  return '/#/dashboard';
}

function updateBrowserRoute(view, options = {}, { replace = false } = {}) {
  if (typeof window === 'undefined' || !window.history?.pushState) return;
  const nextPath = portalPathForRoute(view, options);
  const currentPath = `${window.location.pathname}${window.location.search || ''}${window.location.hash || ''}`;
  if (currentPath === nextPath) return;
  const method = replace ? 'replaceState' : 'pushState';
  window.history[method]({ view, ...options }, '', nextPath);
}

function normalizeStoredSession(rawSession) {
  const tenantCode = String(rawSession?.tenantCode || DEFAULT_SESSION.tenantCode).toUpperCase();
  const fallback = sessionFallback(tenantCode);
  return {
    ...fallback,
    tenantName: rawSession?.tenantName || fallback.tenantName,
    username: rawSession?.username || DEFAULT_SESSION.username,
    role: rawSession?.role || DEFAULT_SESSION.role,
    mode: rawSession?.mode || fallback.mode,
  };
}

function readStoredPortalSession() {
  const storage = getPortalSessionStorage();
  if (!storage) return null;

  try {
    const raw = storage.getItem(PORTAL_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed?.authenticated || !parsed.session) return null;

    const session = normalizeStoredSession(parsed.session);
    const defaultClientCode = session.tenantCode === DEFAULT_SESSION.tenantCode
      ? DEFAULT_OPERATIONAL_CLIENT_CODE
      : session.tenantCode;

    return {
      session,
      activeClientCode: String(parsed.activeClientCode || defaultClientCode).toUpperCase(),
      view: normalizeStoredPortalView(parsed.view),
      settingsSection: normalizeStoredSettingsSection(parsed.settingsSection),
      environmentMode: normalizeEnvironmentMode(parsed.environmentMode || 'DEMO'),
    };
  } catch {
    storage.removeItem(PORTAL_SESSION_STORAGE_KEY);
    return null;
  }
}

function writeStoredPortalSession(payload) {
  const storage = getPortalSessionStorage();
  if (!storage) return;

  try {
    storage.setItem(PORTAL_SESSION_STORAGE_KEY, JSON.stringify({
      authenticated: true,
      session: payload.session,
      activeClientCode: String(payload.activeClientCode || DEFAULT_OPERATIONAL_CLIENT_CODE).toUpperCase(),
      view: normalizeStoredPortalView(payload.view),
      settingsSection: normalizeStoredSettingsSection(payload.settingsSection),
      environmentMode: normalizeEnvironmentMode(payload.environmentMode || 'DEMO'),
      savedAt: new Date().toISOString(),
    }));
  } catch {
    // A blocked storage write should not interrupt the portal session.
  }
}

function clearStoredPortalSession() {
  const storage = getPortalSessionStorage();
  if (!storage) return;
  storage.removeItem(PORTAL_SESSION_STORAGE_KEY);
}
function demoEnsForClient(clientCode = DEFAULT_OPERATIONAL_CLIENT_CODE) {
  return DEMO_ENS_BY_CLIENT[clientCode] || DEMO_ENS_BY_CLIENT[DEFAULT_OPERATIONAL_CLIENT_CODE];
}


const CONSIGNMENTS = [
  {
    id: 'PRS-C000184',
    ensHeaderRowId: 4812,
    consignmentRowId: 9134,
    movementKey: 'PLE-20260630-001',
    declarationNumber: 'ENS000000000184',
    consignmentNumber: 'CON-000184',
    traderReference: 'PLE/NI/184',
    transportDocumentNumber: 'TDR-774219',
    goodsDescription: 'Mixed ambient food products',
    consigneeName: 'Primeline Express Belfast',
    destinationCountry: 'GB',
    goodsItems: 18,
    grossMassKg: '4,820.40',
    status: 'VALIDATED',
    source: 'PRS.Consignment',
    updatedAt: '2026-06-30 09:42',
  },
  {
    id: 'PRS-C000185',
    ensHeaderRowId: 4812,
    consignmentRowId: 9135,
    movementKey: 'PLE-20260630-001',
    declarationNumber: 'ENS000000000184',
    consignmentNumber: 'CON-000185',
    traderReference: 'PLE/NI/185',
    transportDocumentNumber: 'TDR-774220',
    goodsDescription: 'Retail household goods',
    consigneeName: 'Prime Logistics NI',
    destinationCountry: 'GB',
    goodsItems: 9,
    grossMassKg: '1,204.00',
    status: 'READY',
    source: 'PRS.Consignment',
    updatedAt: '2026-06-30 09:44',
  },
  {
    id: 'PRS-C000186',
    ensHeaderRowId: 4813,
    consignmentRowId: 9136,
    movementKey: 'PLE-20260630-002',
    declarationNumber: null,
    consignmentNumber: 'CON-000186',
    traderReference: 'PLE/NI/186',
    transportDocumentNumber: 'TDR-774236',
    goodsDescription: 'Packaging materials and labels',
    consigneeName: 'Belfast Consolidation Hub',
    destinationCountry: 'GB',
    goodsItems: 4,
    grossMassKg: '612.75',
    status: 'NEEDS_REVIEW',
    source: 'PRS.Consignment',
    updatedAt: '2026-06-30 10:07',
  },
  {
    id: 'PRS-C000187',
    ensHeaderRowId: 4814,
    consignmentRowId: 9137,
    movementKey: 'PLE-20260630-003',
    declarationNumber: null,
    consignmentNumber: 'CON-000187',
    traderReference: 'PLE/NI/187',
    transportDocumentNumber: 'TDR-774244',
    goodsDescription: 'Frozen prepared meals',
    consigneeName: 'Cold Chain Belfast',
    destinationCountry: 'GB',
    goodsItems: 22,
    grossMassKg: '8,910.10',
    status: 'INGESTED',
    source: 'ING.Inbound_File',
    updatedAt: '2026-06-30 10:19',
  },
];

function formatNumber(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '0';
}

function connectionFileText(connection) {
  const ordinal = connection?.fileSelection?.requiredFileOrdinal;
  return ordinal ? `Attached file #${ordinal}` : 'Attachment rule pending';
}

function credentialText(connection) {
  const credential = connection?.credential;
  const client = connection?.tssCredentialClientCode || credential?.credentialClientCode;
  const env = connection?.preferredEnvCode || credential?.envCode;
  return client && env ? `${client} / ${env}` : 'Credential pending';
}

function routeText(connection) {
  const route = connection?.route || [];
  const updateIndex = route.findIndex((step) => `${step.operationCode || step.opType || ''}`.toUpperCase().includes('UPDATE'));
  const submitIndex = route.findIndex((step) => `${step.operationCode || step.opType || ''}`.toUpperCase().includes('SUBMIT'));
  return updateIndex > -1 && submitIndex > -1 && updateIndex < submitIndex ? 'ENS update before submit' : 'Route needs review';
}

const TSS_STATUS_OPTIONS = [
  'DRAFT',
  'CREATED',
  'SUBMITTED',
  'PROCESSING',
  'TRADER INPUT REQUIRED',
  'AUTHORISED FOR MOVEMENT',
  'ARRIVED',
  'CANCELLED',
  'UPDATED',
  'DELETED',
  'RECLASSIFIED',
  'ERROR',
];

function normalizeStatusText(value, fallback = 'PENDING') {
  const text = String(value || '').trim().toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return text || fallback;
}

function normalizeStatusDisplay(value, fallback = '') {
  const text = String(value ?? '').trim().replaceAll('_', ' ').replace(/\s+/g, ' ');
  return text ? text.toUpperCase() : fallback;
}

function statusClassName(status) {
  return normalizeStatusDisplay(status, 'PENDING').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'pending';
}

function statusNeedsAttention(status) {
  return [
    'ERROR',
    'FAILED',
    'REJECTED',
    'INVALID',
    'TRADER_INPUT_REQUIRED',
    'AMENDMENT_REQUIRED',
    'DO_NOT_LOAD',
    'SUBMIT_ERROR',
    'VALIDATION_ERROR',
  ].includes(normalizeStatusText(status, ''));
}

function deriveTssStatus(row) {
  const explicit = row?.TssStatus ?? row?.Tss_Status ?? row?.tssStatus ?? row?.tss_status;
  return explicit ? normalizeStatusDisplay(explicit) : '';
}

function readRecordValue(record, keys, fallback = '') {
  const keyList = Array.isArray(keys) ? keys : [keys];
  for (const key of keyList) {
    const value = record?.[key];
    if (value !== null && value !== undefined && value !== '') return value;
  }
  return fallback;
}

function buildConsignmentDraft(row, detail = null) {
  return {
    consignmentNumber: String(readRecordValue(detail, ['consignment_number', 'ConsignmentNumber'], row?.consignmentNumber || '')),
    declarationNumber: String(readRecordValue(detail, ['declaration_number', 'DeclarationNumber', 'HeaderDeclarationNumber'], row?.declarationNumber || '')),
    traderReference: String(readRecordValue(detail, ['trader_reference', 'TraderReference'], row?.traderReference || '')),
    transportDocumentNumber: String(readRecordValue(detail, ['transport_document_number', 'TransportDocumentNumber'], row?.transportDocumentNumber || '')),
    goodsDescription: String(readRecordValue(detail, ['goods_description', 'GoodsDescription'], row?.goodsDescription || '')),
    consigneeName: String(readRecordValue(detail, ['consignee_name', 'ConsigneeName'], row?.consigneeName || '')),
    destinationCountry: String(readRecordValue(detail, ['destination_country', 'DestinationCountry'], row?.destinationCountry || '')),
    tssStatus: deriveTssStatus(detail || row),
  };
}

function applyConsignmentDraft(row, draft) {
  return {
    ...row,
    consignmentNumber: draft.consignmentNumber.trim() || row.consignmentNumber,
    declarationNumber: draft.declarationNumber.trim() || null,
    traderReference: draft.traderReference.trim(),
    transportDocumentNumber: draft.transportDocumentNumber.trim(),
    goodsDescription: draft.goodsDescription.trim(),
    consigneeName: draft.consigneeName.trim(),
    destinationCountry: draft.destinationCountry.trim().toUpperCase(),
    tssStatus: draft.tssStatus,
  };
}
function normalizeConsignment(row) {
  const consignmentRowId = row.ConsignmentRowID ?? row.consignmentRowID ?? row.consignmentRowId;
  const fallbackNumber = consignmentRowId ? `PRS-${consignmentRowId}` : 'PRS-DRAFT';
  return {
    id: `PRS-C${String(consignmentRowId || '').padStart(6, '0')}`,
    clientCode: row.ClientCode ?? row.clientCode ?? '',
    ensHeaderRowId: row.EnsHeaderRowID ?? row.ensHeaderRowID ?? row.ensHeaderRowId,
    consignmentRowId,
    movementKey: row.MovementKey ?? row.movementKey ?? '',
    declarationNumber: row.DeclarationNumber ?? row.declarationNumber ?? row.HeaderDeclarationNumber ?? null,
    consignmentNumber: row.ConsignmentNumber ?? row.consignmentNumber ?? fallbackNumber,
    traderReference: row.TraderReference ?? row.traderReference ?? '',
    transportDocumentNumber: row.TransportDocumentNumber ?? row.transportDocumentNumber ?? '',
    goodsDescription: row.GoodsDescription ?? row.goodsDescription ?? '',
    consigneeName: row.ConsigneeName ?? row.consigneeName ?? '',
    destinationCountry: row.DestinationCountry ?? row.destinationCountry ?? '',
    goodsItems: Number(row.GoodsItems ?? row.goodsItems ?? 0),
    grossMassKg: formatNumber(row.GrossMassKg ?? row.grossMassKg),
    status: row.Status ?? row.status ?? 'DRAFT',
    tssStatus: deriveTssStatus(row),
    sfdReference: row.SfdReference ?? row.SFDReference ?? row.sfdReference ?? '',
    sfdMrn: row.SfdMrn ?? row.SFDMrn ?? row.sfdMrn ?? '',
    sdiReferences: row.SdiReferences ?? row.SDIReferences ?? row.sdiReferences ?? '',
    rejectReason: row.RejectReason ?? row.rejectReason ?? '',
    validationSummary: row.ValidationSummary ?? row.validationSummary ?? null,
    validationStatus: row.ValidationStatus ?? row.validationStatus ?? '',
    missingRequiredCount: Number(row.MissingRequiredCount ?? row.missingRequiredCount ?? 0),
    assumptionCount: Number(row.AssumptionCount ?? row.assumptionCount ?? 0),
    packageTypeMissingCount: Number(row.PackageTypeMissingCount ?? row.packageTypeMissingCount ?? 0),
    packageTypeNormalisedCount: Number(row.PackageTypeNormalisedCount ?? row.packageTypeNormalisedCount ?? 0),
    source: row.Source ?? row.source ?? 'PRS.Consignment',
    arrivalDateTime: row.ArrivalDateTime ?? row.arrivalDateTime ?? row.HeaderArrivalDateTime ?? '',
    updatedAt: row.UpdatedAt ?? row.updatedAt ?? '',
  };
}

function normalizeDeclaration(row) {
  const ensHeaderRowId = row.EnsHeaderRowID ?? row.ensHeaderRowID ?? row.ensHeaderRowId;
  const movementKey = row.MovementKey ?? row.movementKey ?? '';
  const declarationNumber = row.DeclarationNumber ?? row.declarationNumber ?? row.Declaration_Number ?? '';
  const fallbackId = movementKey || declarationNumber || ensHeaderRowId || 'DRAFT';
  return {
    id: `PRS-H${String(fallbackId).replace(/[^A-Za-z0-9_-]+/g, '-')}`,
    clientCode: row.ClientCode ?? row.clientCode ?? '',
    ensHeaderRowId,
    movementKey,
    declarationNumber,
    status: row.Status ?? row.status ?? row.Fusion_Status ?? 'DRAFT',
    tssStatus: deriveTssStatus(row),
    movementType: row.MovementType ?? row.movementType ?? row.movement_type ?? '',
    arrivalPort: row.ArrivalPort ?? row.arrivalPort ?? row.arrival_port ?? '',
    arrivalDateTime: row.ArrivalDateTime ?? row.arrivalDateTime ?? row.arrival_date_time ?? '',
    carrierName: row.CarrierName ?? row.carrierName ?? row.carrier_name ?? '',
    carrierEori: row.CarrierEori ?? row.carrierEori ?? row.carrier_eori ?? '',
    consignments: Number(row.Consignments ?? row.consignments ?? row.ConsignmentCount ?? 0),
    goodsItems: Number(row.GoodsItems ?? row.goodsItems ?? row.GoodsItemCount ?? 0),
    sourceChannel: row.SourceChannel ?? row.sourceChannel ?? '',
    sourceFile: row.SourceFile ?? row.sourceFile ?? '',
    source: row.SourceTable ?? row.sourceTable ?? row.Source ?? row.source ?? 'PRS.ENS_Header',
    lastExecutionId: row.LastExecutionID ?? row.lastExecutionID ?? row.ExecutionID ?? '',
    createdAt: row.CreatedAt ?? row.createdAt ?? '',
    updatedAt: row.UpdatedAt ?? row.updatedAt ?? '',
  };
}

function declarationDateValue(row) {
  return row?.arrivalDateTime || row?.updatedAt || row?.createdAt || '';
}

function declarationTimestamp(row) {
  const raw = declarationDateValue(row);
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function declarationPrimaryRef(row) {
  if (row?.declarationNumber) return row.declarationNumber;
  if (row?.ensHeaderRowId) return `ENS #${row.ensHeaderRowId}`;
  return row?.movementKey || '-';
}

function localDeclarationStatus(row) {
  return normalizeStatusText(row?.status ?? row?.Status, 'DRAFT');
}

function buildDeclarationsFromConsignments(rows = []) {
  const grouped = new Map();
  rows.forEach((item) => {
    const row = normalizeConsignment(item);
    const key = row.movementKey || row.declarationNumber || row.ensHeaderRowId || row.id;
    const current = grouped.get(key) || {
      MovementKey: row.movementKey,
      DeclarationNumber: row.declarationNumber,
      EnsHeaderRowID: row.ensHeaderRowId,
      ClientCode: row.clientCode,
      Status: row.status,
      TssStatus: row.tssStatus,
      ArrivalDateTime: row.arrivalDateTime,
      Consignments: 0,
      GoodsItems: 0,
      SourceTable: 'Derived from PRS.Consignment',
      UpdatedAt: row.updatedAt,
    };
    current.Consignments += 1;
    current.GoodsItems += Number(row.goodsItems || 0);
    current.DeclarationNumber = current.DeclarationNumber || row.declarationNumber;
    current.EnsHeaderRowID = current.EnsHeaderRowID || row.ensHeaderRowId;
    current.ArrivalDateTime = current.ArrivalDateTime || row.arrivalDateTime;
    current.UpdatedAt = row.updatedAt || current.UpdatedAt;
    if (['ERROR', 'REJECTED', 'NEEDS_REVIEW'].includes(localConsignmentStatus(row))) current.Status = row.status;
    else if (['VALIDATED', 'READY'].includes(localConsignmentStatus(row))) current.Status = row.status;
    if (row.tssStatus && !current.TssStatus) current.TssStatus = row.tssStatus;
    grouped.set(key, current);
  });
  return [...grouped.values()].map(normalizeDeclaration);
}

const DECLARATIONS = buildDeclarationsFromConsignments(CONSIGNMENTS);
const STATUS_VOCABULARY_FALLBACK = [
  { resultStatus: 'INGESTED', processName: 'INGESTION', meaning: 'Raw evidence has landed.', sortOrder: 10 },
  { resultStatus: 'NORMALISED', processName: 'PROCESSING', meaning: 'Source data has been normalised.', sortOrder: 20 },
  { resultStatus: 'ENRICHED', processName: 'PROCESSING', meaning: 'CFG/masterdata enrichment has run.', sortOrder: 30 },
  { resultStatus: 'CONSTRUCTED', processName: 'PROCESSING', meaning: 'Canonical PRS object has been built.', sortOrder: 40 },
  { resultStatus: 'VALIDATED', processName: 'VALIDATION', meaning: 'Validation checks have passed.', sortOrder: 50 },
  { resultStatus: 'REJECTED', processName: 'VALIDATION', meaning: 'Validation or submission rejected the record.', sortOrder: 60, isException: true },
  { resultStatus: 'STG_MATERIALISED', processName: 'PROMOTION', meaning: 'Record has been promoted to STG.', sortOrder: 70 },
  { resultStatus: 'READY', processName: 'SUBMISSION', meaning: 'Record is ready for submission.', sortOrder: 80 },
  { resultStatus: 'SUBMITTING', processName: 'SUBMISSION', meaning: 'Submission is in progress.', sortOrder: 90 },
  { resultStatus: 'SUBMITTED', processName: 'SUBMISSION', meaning: 'Submission was sent.', sortOrder: 100 },
  { resultStatus: 'ACKNOWLEDGED', processName: 'SUBMISSION', meaning: 'TSS acknowledged the record.', sortOrder: 110 },
  { resultStatus: 'IN_PROGRESS', processName: 'SYNC', meaning: 'External processing is still in progress.', sortOrder: 120 },
  { resultStatus: 'RECONCILED', processName: 'SYNC', meaning: 'Local and external state are reconciled.', sortOrder: 130 },
  { resultStatus: 'MISMATCH', processName: 'SYNC', meaning: 'Local and external state differ.', sortOrder: 140, isException: true },
  { resultStatus: 'ERROR', processName: 'ERROR', meaning: 'Processing failed.', sortOrder: 150, isException: true },
  { resultStatus: 'CANCELLED', processName: 'LIFECYCLE', meaning: 'Record is cancelled.', sortOrder: 160, isTerminal: true },
  { resultStatus: 'ON_HOLD', processName: 'LIFECYCLE', meaning: 'Record is on hold.', sortOrder: 170 },
];

function normalizeStatusVocabulary(rows = []) {
  const source = Array.isArray(rows) && rows.length ? rows : STATUS_VOCABULARY_FALLBACK;
  return source
    .map((row, index) => ({
      processName: String(row.ProcessName ?? row.processName ?? '').trim(),
      resultStatus: normalizeStatusText(row.ResultStatus ?? row.resultStatus ?? row.status, ''),
      meaning: String(row.Meaning ?? row.meaning ?? '').trim(),
      sortOrder: Number(row.SortOrder ?? row.sortOrder ?? index + 999),
      isTerminal: Boolean(row.IsTerminal ?? row.isTerminal),
      isException: Boolean(row.IsException ?? row.isException),
    }))
    .filter((row) => row.resultStatus)
    .sort((left, right) => left.sortOrder - right.sortOrder || left.resultStatus.localeCompare(right.resultStatus));
}

function statusVocabularyMap(vocabulary = STATUS_VOCABULARY_FALLBACK) {
  const map = new Map();
  normalizeStatusVocabulary(vocabulary).forEach((row) => {
    if (!map.has(row.resultStatus)) map.set(row.resultStatus, row);
  });
  return map;
}

function localConsignmentStatus(row) {
  return normalizeStatusText(row?.status ?? row?.Status, 'DRAFT');
}

const CONSIGNMENT_PIPELINE_STAGES = [
  { key: 'PRS', label: 'PRS', sub: 'Canonical record', icon: 'schema' },
  { key: 'VALIDATION', label: 'Validation', sub: 'Rules + enrichment', icon: 'rule' },
  { key: 'STG', label: 'STG', sub: 'Submission-ready', icon: 'inventory_2' },
  { key: 'ENS', label: 'ENS', sub: 'Header mirror', icon: 'fact_check' },
  { key: 'CONSIGNMENT', label: 'Consignment', sub: 'TSS consignment', icon: 'package_2' },
  { key: 'GOODS', label: 'Goods', sub: '99-row batches', icon: 'list_alt' },
  { key: 'SFD', label: 'SFD', sub: 'Lookup / link', icon: 'travel_explore' },
  { key: 'SDI', label: 'SDI', sub: 'Supplementary', icon: 'bolt' },
];

function consignmentPipelinePosition(row, goodsItems = []) {
  const tssStatus = normalizeStatusText(deriveTssStatus(row), '');
  const localStatus = normalizeStatusText(row?.status || row?.Status, 'DRAFT');
  if (row?.sdiReferences) return 7;
  if (row?.sfdReference || row?.sfdMrn) return 6;
  if (goodsItems.length || Number(row?.goodsItems || 0) > 0) return 5;
  if (['CREATED', 'SUBMITTED', 'ACCEPTED', 'PROCESSING', 'AUTHORISED_FOR_MOVEMENT', 'AUTHORIZED_FOR_MOVEMENT', 'ARRIVED'].includes(tssStatus)) return 4;
  if (row?.declarationNumber || row?.HeaderDeclarationNumber) return 3;
  if (['READY', 'VALIDATED'].includes(localStatus)) return 2;
  return rowValidationMissingCount(row) ? 1 : 2;
}

function ConsignmentPipelineRail({ row, goodsItems = [] }) {
  const currentIndex = consignmentPipelinePosition(row, goodsItems);
  return (
    <section className="consignment-pipeline-card" aria-label="ENS consignment pipeline">
      <div className="pipeline-card-heading">
        <div>
          <span>Modules path</span>
          <h3>PRS {'->'} STG {'->'} TSS {'->'} SFD/SDI</h3>
        </div>
        <strong>Route A</strong>
      </div>
      <div className="consignment-pipeline-rail">
        {CONSIGNMENT_PIPELINE_STAGES.map((stage, index) => {
          const state = index < currentIndex ? 'done' : index === currentIndex ? 'current' : 'pending';
          return (
            <div key={stage.key} className={`pipeline-stage ${state}`}>
              <div className="pipeline-node"><MaterialIcon>{state === 'done' ? 'check' : stage.icon}</MaterialIcon></div>
              <div className="pipeline-copy">
                <strong>{stage.label}</strong>
                <span>{stage.sub}</span>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function displayStatusLabel(value, vocabularyMap = null) {
  const normalized = normalizeStatusText(value, '');
  const known = vocabularyMap?.get?.(normalized);
  return String(known?.resultStatus || value || '').replaceAll('_', ' ');
}

function toDateInputValue(value) {
  if (!value) return '';
  const date = new Date(value);
  if (!Number.isNaN(date.getTime())) return date.toISOString().slice(0, 10);
  const match = String(value).match(/(\d{4})-(\d{2})-(\d{2})/);
  return match ? match[0] : '';
}

function consignmentDateValue(row) {
  return row?.arrivalDateTime || row?.updatedAt || '';
}

function consignmentTimestamp(row) {
  const raw = consignmentDateValue(row);
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function consignmentPrimaryRef(row) {
  return row?.consignmentNumber || (row?.consignmentRowId ? `Draft #${row.consignmentRowId}` : '-');
}

function consignmentEnsRef(row) {
  if (row?.declarationNumber) return row.declarationNumber;
  if (row?.ensHeaderRowId) return `ENS #${row.ensHeaderRowId}`;
  return '';
}

function rowValidationMissingCount(row) {
  if (Number.isFinite(Number(row?.missingRequiredCount))) return Number(row.missingRequiredCount || 0);
  return (row?.validationSummary?.missingRequired || []).reduce((total, item) => total + Number(item.count || 0), 0);
}

function ConsignmentValidationSignal({ row }) {
  const missingCount = rowValidationMissingCount(row);
  const assumptionCount = Number(row?.assumptionCount || row?.validationSummary?.assumptions?.length || 0);
  const packageMissing = Number(row?.packageTypeMissingCount || row?.validationSummary?.packageType?.missingCount || 0);
  const packageNormalised = Number(row?.packageTypeNormalisedCount || row?.validationSummary?.packageType?.normalisedCount || 0);
  const status = row?.validationStatus || row?.validationSummary?.status || (missingCount ? 'NEEDS_REVIEW' : 'READY');
  return (
    <div className={`consignment-validation-signal ${missingCount || assumptionCount ? 'needs-review' : 'ready'}`} title="Modules validation summary">
      <span>{displayStatusLabel(status)}</span>
      <small>{missingCount ? `${missingCount} missing` : 'Ready'}</small>
      {!!assumptionCount && <em>{assumptionCount} assumption</em>}
      {!!packageMissing && <em>{packageMissing} pkg missing</em>}
      {!!packageNormalised && <em>{packageNormalised} pkg mapped</em>}
    </div>
  );
}

function ConsignmentValidationMini({ row }) {
  const missingCount = rowValidationMissingCount(row);
  const assumptionCount = Number(row?.assumptionCount || row?.validationSummary?.assumptions?.length || 0);
  const status = row?.validationStatus || row?.validationSummary?.status || (missingCount ? 'NEEDS_REVIEW' : 'READY');
  return (
    <span className={`consignment-validation-mini ${missingCount || assumptionCount ? 'needs-review' : 'ready'}`} title="Modules validation summary">
      {displayStatusLabel(status)}{missingCount ? ` · ${missingCount} missing` : ''}{assumptionCount ? ` · ${assumptionCount} assumed` : ''}
    </span>
  );
}
function rowMatchesDateFilters(row, month, from, to) {
  const dateValue = toDateInputValue(consignmentDateValue(row));
  if (!dateValue) return !(month || from || to);
  if (month && !dateValue.startsWith(month)) return false;
  if (from && dateValue < from) return false;
  if (to && dateValue > to) return false;
  return true;
}

function MaterialIcon({ children, className = '' }) {
  return <span className={`material-symbols-outlined ${className}`} aria-hidden="true">{children}</span>;
}

function LoadingSpinner({ className = '' }) {
  return <span className={`loading-spinner ${className}`} aria-hidden="true" />;
}

function BulkModeToggle({ active, group, onToggle, label = 'Select mode' }) {
  return (
    <button
      className={`bulk-mode-toggle ${active ? 'active' : ''}`}
      type="button"
      data-bulk-select-toggle=""
      data-bulk-group={group}
      data-bulk-inactive-label={label}
      data-bulk-active-label={label}
      aria-pressed={active}
      onClick={onToggle}
    >
      <span className="bulk-mode-toggle-switch" aria-hidden="true" />
      <span data-bulk-toggle-label="">{label}</span>
    </button>
  );
}

function bulkRowIdForTarget(target) {
  if (!(target instanceof Element)) return '';
  const row = target.closest('tr[data-bulk-row-id]');
  return row?.dataset?.bulkRowId || '';
}

function isIgnoredBulkDragTarget(target) {
  if (!(target instanceof Element)) return true;
  if (target.closest('a, button, select, textarea, [data-no-row-link]')) return true;
  const input = target.closest('input');
  return Boolean(input && !input.matches('[data-bulk-item]'));
}

function useBulkRowDragSelection({ selectMode, selectedRows, setSelectedRows }) {
  const dragStateRef = useRef(null);
  const suppressClickRef = useRef(false);
  const suppressTimerRef = useRef(0);

  function setRowSelected(rowId, checked) {
    if (!rowId) return;
    setSelectedRows((current) => {
      const next = new Set(current);
      if (checked) next.add(rowId);
      else next.delete(rowId);
      return next;
    });
  }

  function applyDragSelection(rowId) {
    const dragState = dragStateRef.current;
    if (!dragState || !rowId || dragState.seen.has(rowId)) return;
    dragState.seen.add(rowId);
    setRowSelected(rowId, dragState.targetChecked);
  }

  function startBulkDrag(event) {
    if (!selectMode || (event.pointerType === 'mouse' && event.button !== 0)) return;
    const target = event.target instanceof Element ? event.target : null;
    if (!target || isIgnoredBulkDragTarget(target)) return;
    const rowId = bulkRowIdForTarget(target);
    if (!rowId) return;

    event.preventDefault();
    window.clearTimeout(suppressTimerRef.current);
    suppressClickRef.current = true;
    dragStateRef.current = {
      pointerId: event.pointerId,
      targetChecked: !selectedRows.has(rowId),
      seen: new Set(),
    };
    event.currentTarget?.classList?.add('is-bulk-dragging');
    event.currentTarget?.setPointerCapture?.(event.pointerId);
    applyDragSelection(rowId);
  }

  function continueBulkDrag(event) {
    const dragState = dragStateRef.current;
    if (!dragState || event.pointerId !== dragState.pointerId) return;
    event.preventDefault();
    const target = document.elementFromPoint(event.clientX, event.clientY);
    applyDragSelection(bulkRowIdForTarget(target));
  }

  function stopBulkDrag(event) {
    const dragState = dragStateRef.current;
    if (!dragState || (event && event.pointerId !== dragState.pointerId)) return;
    event?.preventDefault?.();
    event?.currentTarget?.classList?.remove('is-bulk-dragging');
    if (event?.currentTarget?.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    dragStateRef.current = null;
    suppressTimerRef.current = window.setTimeout(() => {
      suppressClickRef.current = false;
    }, 250);
  }

  function shouldSuppressClick() {
    if (!suppressClickRef.current) return false;
    suppressClickRef.current = false;
    window.clearTimeout(suppressTimerRef.current);
    return true;
  }

  return { startBulkDrag, continueBulkDrag, stopBulkDrag, shouldSuppressClick };
}
function DrawerRow({ icon, label, active = false, danger = false, indent = false, trailing, expanded, onClick }) {
  return (
    <button
      className={`drawer-row ${active ? 'active' : ''} ${danger ? 'danger' : ''} ${indent ? 'indent' : ''}`}
      type="button"
      aria-expanded={expanded}
      onClick={onClick}
    >
      <MaterialIcon>{icon}</MaterialIcon>
      <span>{label}</span>
      {trailing && <MaterialIcon className="trailing">{trailing}</MaterialIcon>}
    </button>
  );
}

function DrawerInfoRow({ icon, label, value }) {
  return (
    <div className="drawer-info-row">
      <MaterialIcon>{icon}</MaterialIcon>
      {label && <span>{label}</span>}
      <strong>{value || 'Not available'}</strong>
    </div>
  );
}

function detectBrowser(userAgent = '') {
  const browserRules = [
    ['Edg/', 'Edge'],
    ['Chrome/', 'Chrome'],
    ['Firefox/', 'Firefox'],
    ['Version/', 'Safari'],
  ];
  const match = browserRules.find(([token]) => userAgent.includes(token));
  if (!match) return 'Browser unknown';
  const [token, name] = match;
  const version = userAgent.split(token)[1]?.split(/[ .)]/).slice(0, 3).join('.') || '';
  return `${name}${version ? ` ${version}` : ''}`;
}

function detectOs(userAgent = '') {
  if (userAgent.includes('Windows NT 10.0')) return 'Windows 10/11';
  if (userAgent.includes('Windows')) return 'Windows';
  if (userAgent.includes('Mac OS X')) return 'macOS';
  if (userAgent.includes('Android')) return 'Android';
  if (userAgent.includes('iPhone') || userAgent.includes('iPad')) return 'iOS';
  if (userAgent.includes('Linux')) return 'Linux';
  return 'OS unknown';
}

function appInfoSnapshot(environmentMode = 'DEMO') {
  const nav = typeof navigator === 'undefined' ? {} : navigator;
  return {
    appName: 'SynoviaFlow',
    version: import.meta.env?.VITE_APP_VERSION || '1.1.7',
    runtime: `React ${React.version}`,
    environment: environmentMode,
    culture: nav.language || 'en-US',
    browser: detectBrowser(nav.userAgent || ''),
    os: detectOs(nav.userAgent || ''),
  };
}

function Drawer({ open, view, isAuthenticated, isDarkTheme, settingsSections = [], settingsSection, session, apiStatus, environmentMode, onNavigate, onSettingsSection, onLogout, onToggleTheme }) {
  const visibleSettings = mergeSettingsSections(settingsSections);
  const firstSettingsId = visibleSettings[0]?.id || SETTINGS_NAV_SECTIONS[0].id;
  const [openSections, setOpenSections] = useState({
    settings: true,
    session: true,
    appInfo: false,
    device: true,
  });
  const appInfo = useMemo(() => appInfoSnapshot(environmentMode), [environmentMode]);

  function toggleSection(sectionId) {
    setOpenSections((current) => ({ ...current, [sectionId]: !current[sectionId] }));
  }

  function openSettings() {
    if (isAuthenticated && view !== 'settings') {
      setOpenSections((current) => ({ ...current, settings: true }));
      onSettingsSection(settingsSection || firstSettingsId);
      return;
    }
    setOpenSections((current) => ({ ...current, settings: !current.settings }));
  }

  return (
    <aside className={`drawer ${open ? 'is-open' : ''}`} aria-label="Navigation">
      <div className="drawer-brand">SynoviaFlow</div>
      <nav className="drawer-nav">
        <DrawerRow icon="home" label="Home" active={view === 'dashboard'} onClick={() => onNavigate(isAuthenticated ? 'dashboard' : 'login')} />
        {isAuthenticated && (
          <>
            <DrawerRow icon="fact_check" label="ENS / Declarations" active={view === 'declarations'} onClick={() => onNavigate('declarations')} />
            <DrawerRow icon="upload_file" label="Upload Consignments" active={view === 'upload'} onClick={() => onNavigate('upload')} />
            <DrawerRow icon="list_alt" label="View Consignments" active={view === 'consignments'} onClick={() => onNavigate('consignments')} />
            <DrawerRow icon="hub" label="Control Tower" active={view === 'controlTower'} onClick={() => onNavigate('controlTower')} />
            <DrawerRow icon="table_view" label="Master Live" active={view === 'masterLive'} onClick={() => onNavigate('masterLive')} />
          </>
        )}
        <DrawerRow
          icon="settings"
          label="Settings"
          active={view === 'settings'}
          trailing={openSections.settings ? 'expand_less' : 'expand_more'}
          expanded={openSections.settings}
          onClick={openSettings}
        />
        {isAuthenticated && openSections.settings && visibleSettings.map((section) => (
          <DrawerRow key={section.id} icon={section.icon || 'tune'} label={section.label} active={view === 'settings' && settingsSection === section.id} indent onClick={() => onSettingsSection(section.id)} />
        ))}
        {openSections.settings && <DrawerRow icon="dark_mode" label="Dark theme" active={isDarkTheme} indent onClick={onToggleTheme} />}
        {openSections.settings && <DrawerRow icon="frame_reload" label="Reload application" danger indent onClick={() => window.location.reload()} />}
        <DrawerRow
          icon="badge"
          label="Session"
          trailing={openSections.session ? 'expand_less' : 'expand_more'}
          expanded={openSections.session}
          onClick={() => toggleSection('session')}
        />
        {openSections.session && isAuthenticated && (
          <div className="drawer-info-block">
            <DrawerInfoRow icon="person" label="User" value={session?.username} />
            <DrawerInfoRow icon="admin_panel_settings" label="Role" value={session?.role} />
            <DrawerInfoRow icon="business" label="Tenant" value={session?.tenantName} />
            <DrawerInfoRow icon="cloud_done" label="API" value={apiStatus || 'idle'} />
          </div>
        )}
        {openSections.session && (isAuthenticated ? (
          <DrawerRow icon="logout" label="Logout" indent onClick={onLogout} />
        ) : (
          <DrawerRow icon="login" label="Login" active={view === 'login'} indent />
        ))}
      </nav>
      <div className="drawer-bottom">
        <DrawerRow
          icon="code"
          label="App Info"
          trailing={openSections.appInfo ? 'expand_less' : 'expand_more'}
          expanded={openSections.appInfo}
          onClick={() => toggleSection('appInfo')}
        />
        {openSections.appInfo && (
          <div className="drawer-info-block app-info-block">
            <DrawerInfoRow icon="manage_accounts" label="" value={appInfo.appName} />
            <DrawerInfoRow icon="archive" label="Version" value={appInfo.version} />
            <DrawerInfoRow icon="memory" label="Runtime" value={appInfo.runtime} />
            <DrawerInfoRow icon="public" label="Env" value={appInfo.environment} />
            <DrawerRow
              icon="computer"
              label="Device & OS"
              active
              trailing={openSections.device ? 'expand_less' : 'expand_more'}
              expanded={openSections.device}
              onClick={() => toggleSection('device')}
            />
            {openSections.device && (
              <div className="drawer-info-nested">
                <DrawerInfoRow icon="translate" label="Culture" value={appInfo.culture} />
                <DrawerInfoRow icon="data_exploration" label="Browser" value={appInfo.browser} />
                <DrawerInfoRow icon="desktop_windows" label="OS" value={appInfo.os} />
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}

function AppBar({ session, isAuthenticated, environmentMode, onToggleDrawer, onLogout }) {
  return (
    <header className="appbar">
      <div className="appbar-left">
        <button className="hamburger-button" type="button" aria-label="Toggle navigation" onClick={onToggleDrawer}>
          <MaterialIcon>menu</MaterialIcon>
        </button>
        <img className="appbar-logo" src="/assets/SynoviaFlowLogo_white.png" alt="Synovia Flow" />
      </div>

      <div className={`environment-indicator ${environmentMode.toLowerCase()}`} aria-label="Current TSS environment from Settings">
        <span>TSS Env</span>
        <strong>{environmentMode}</strong>
      </div>

      {isAuthenticated ? (
        <div className="session-strip" aria-label="Current session">
          <span className="session-chip">{session.username}</span>
          <span className="session-role">{session.role}</span>
          <span className="session-tenant">{session.tenantName}</span>
          <button className="logout-button" type="button" aria-label="Logout" onClick={onLogout}>
            <MaterialIcon>logout</MaterialIcon>
          </button>
        </div>
      ) : (
        <button className="top-login" type="button">
          <span>Login</span>
          <MaterialIcon>login</MaterialIcon>
        </button>
      )}
    </header>
  );
}

function LoginCard({ onLogin }) {
  const [showPassword, setShowPassword] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loginState, setLoginState] = useState({ status: 'idle', error: '' });

  const isCheckingLogin = loginState.status === 'loading';

  async function handleSubmit(event) {
    event.preventDefault();
    if (isCheckingLogin) return;
    setLoginState({ status: 'loading', error: '' });
    try {
      await onLogin({ username, password });
      setLoginState({ status: 'idle', error: '' });
    } catch (error) {
      setLoginState({ status: 'error', error: error.message });
    }
  }

  return (
    <section className="login-card" aria-label="Login form">
      <img className="flow-logo" src="/assets/SynoviaFlowLogo.png" alt="Synovia Flow" />
      <form className="login-form" onSubmit={handleSubmit}>
        <label className="input-shell">
          <input type="text" placeholder="Username*" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} />
        </label>
        <label className="input-shell password-shell">
          <input type={showPassword ? 'text' : 'password'} placeholder="Password*" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
          <button className="visibility-button" type="button" onClick={() => setShowPassword((value) => !value)} aria-label="Toggle password visibility">
            <MaterialIcon>{showPassword ? 'visibility' : 'visibility_off'}</MaterialIcon>
          </button>
        </label>
        {loginState.status === 'error' && <div className="login-error">{loginState.error}</div>}
        <button className="submit-button" type="submit" disabled={isCheckingLogin} aria-busy={isCheckingLogin}>
          <span>{isCheckingLogin ? 'Checking' : 'Login'}</span>
          {isCheckingLogin && <LoadingSpinner className="button-spinner" />}
        </button>
        <button className="forgot-button" type="button">Forgot password?</button>
      </form>
    </section>
  );
}

function TssConnectionStrip({ connection }) {
  if (!connection) return null;
  const credential = connection.credential || {};
  return (
    <div className="connection-strip" aria-label="TSS connection">
      <div>
        <span>Portal</span>
        <strong>{connection.portalClientCode} - {connection.clientName}</strong>
      </div>
      <div>
        <span>File to map</span>
        <strong>{connectionFileText(connection)}</strong>
      </div>
      <div>
        <span>TSS credential</span>
        <strong>{credentialText(connection)}</strong>
      </div>
      <div>
        <span>Route</span>
        <strong>{routeText(connection)}</strong>
      </div>
      <div>
        <span>Status</span>
        <strong>{credential.lastStatus || (credential.hasPassword ? 'READY' : 'CHECK')}</strong>
      </div>
    </div>
  );
}
function OperationalContextPanel({ activeClientCode, onClientChange, isDemoAdmin }) {
  if (!isDemoAdmin) return null;
  const activeClient = clientOptionFor(activeClientCode);
  return (
    <div className="operational-context-panel" aria-label="Operational client context">
      <div>
        <span>Synovia demo context</span>
        <strong>{activeClient.tenantName}</strong>
        <small>Preview only. DB off / TSS off.</small>
      </div>
      <label>
        <span>Client</span>
        <select value={activeClient.tenantCode} onChange={(event) => onClientChange(event.target.value)}>
          {PORTAL_CLIENTS.map((client) => <option key={client.tenantCode} value={client.tenantCode}>{client.tenantName}</option>)}
        </select>
      </label>
    </div>
  );
}

function DashboardPage({ onNavigate, connection, activeClientCode, onClientChange, isDemoAdmin }) {
  return (
    <section className="dashboard-page" aria-label="Dashboard">
      <div className="welcome-block">
        <div className="welcome-title">
          <span>Welcome to</span>
          <img src="/assets/SynoviaFlowLogo.png" alt="Synovia Flow" />
        </div>
        <p>Follow the steps below to prepare and send consignments to TSS.</p>
      </div>
      <OperationalContextPanel activeClientCode={activeClientCode} onClientChange={onClientChange} isDemoAdmin={isDemoAdmin} />
      <TssConnectionStrip connection={connection} />
      <div className="action-panel" aria-label="Workflow actions">
        <div className="action-column">
          <MaterialIcon className="action-icon">fact_check</MaterialIcon>
          <h2>ENS / Declarations</h2>
          <button className="primary-action blue" type="button" onClick={() => onNavigate('declarations')}>
            <MaterialIcon>fact_check</MaterialIcon>
            <span>View Declarations</span>
          </button>
        </div>
        <div className="action-column">
          <MaterialIcon className="action-icon">upload_file</MaterialIcon>
          <h2>Create or Upload Consignments</h2>
          <button className="primary-action teal" type="button" onClick={() => onNavigate('upload')}>
            <MaterialIcon>upload_file</MaterialIcon>
            <span>Upload Consignment</span>
          </button>
        </div>
        <div className="action-column">
          <MaterialIcon className="action-icon">format_list_bulleted</MaterialIcon>
          <h2>Send to TSS</h2>
          <button className="primary-action blue" type="button" onClick={() => onNavigate('consignments')}>
            <MaterialIcon>format_list_bulleted</MaterialIcon>
            <span>View Consignments</span>
          </button>
        </div>
      </div>
    </section>
  );
}

function SettingsInput({ row, value, onChange }) {
  if (row.editable === false) {
    return <div className="settings-readonly">{value || row.placeholder || 'Not configured'}</div>;
  }

  if (row.inputType === 'boolean') {
    const checked = String(value).toLowerCase() === 'true' || value === '1';
    return (
      <button className={`settings-toggle ${checked ? 'is-on' : ''}`} type="button" onClick={() => onChange(checked ? 'false' : 'true')} aria-pressed={checked}>
        <span className="settings-toggle-track"><span /></span>
        <strong>{checked ? 'Enabled' : 'Disabled'}</strong>
      </button>
    );
  }

  if (row.inputType === 'select' && row.choices?.length) {
    return (
      <select className="settings-input" value={value || ''} onChange={(event) => onChange(event.target.value)}>
        <option value="">Select</option>
        {row.choices.map((choice) => <option key={choice.value} value={choice.value}>{choice.label || choice.value}</option>)}
      </select>
    );
  }

  return (
    <input
      className="settings-input"
      type={row.inputType === 'password' ? 'password' : row.inputType || 'text'}
      value={value || ''}
      placeholder={row.placeholder || ''}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

function SettingsValidationDiagnostics({ diagnostics, status = 'idle', error = '', onRefresh }) {
  const productMaster = diagnostics?.productMaster || {};
  const rowCount = productMaster.rowCount || 0;
  const packageTypeRowCount = productMaster.packageTypeRowCount || 0;
  const packageTypeMissingCount = productMaster.packageTypeMissingCount || 0;
  const packageValues = productMaster.packageTypeValues || [];
  const resolutionOrder = diagnostics?.packageTypeResolutionOrder || [];
  const notes = diagnostics?.notes || [];
  const hasPackageWarning = Boolean(productMaster.packageTypeWarning || productMaster.warning);
  const isRefreshing = status === 'loading';
  const hasDiagnostics = Boolean(diagnostics);
  const writeMode = diagnostics?.writeMode || 'read_only_diagnostics';
  return (
    <div className={`settings-diagnostics ${hasPackageWarning ? 'has-warning' : ''}`}>
      <div className="settings-diagnostics-head">
        <div>
          <strong>Validation & enrichment diagnostics</strong>
          <span>Read-only view from Modules.Processing and CFG masterdata. DB writes off / TSS writes off.</span>
        </div>
        <div className="settings-diagnostics-actions">
          {onRefresh && (
            <button className="settings-diagnostics-refresh" type="button" onClick={onRefresh} disabled={isRefreshing} aria-busy={isRefreshing} title="Refresh validation diagnostics">
              {!isRefreshing && <MaterialIcon>refresh</MaterialIcon>}
              <span>{isRefreshing ? 'Refreshing' : 'Refresh'}</span>
              {isRefreshing && <LoadingSpinner className="button-spinner" />}
            </button>
          )}
          <em>{writeMode}</em>
        </div>
      </div>
      {error && (
        <div className="settings-diagnostics-error">
          <MaterialIcon>error</MaterialIcon>
          <span>{error}</span>
        </div>
      )}
      {!hasDiagnostics && (
        <div className="settings-diagnostics-empty">
          {isRefreshing && <LoadingSpinner />}
          <span>{isRefreshing ? 'Loading validation diagnostics from the portal API...' : 'Validation diagnostics have not been loaded yet.'}</span>
        </div>
      )}
      <div className="settings-diagnostics-grid">
        <div>
          <span>CFG.Product_Master</span>
          <strong>{productMaster.available ? `${rowCount} rows` : 'Not available'}</strong>
          <small>{productMaster.available ? `${productMaster.lookupKeyCount || 0} lookup keys` : productMaster.reason || 'No masterdata loaded'}</small>
        </div>
        <div className={hasPackageWarning ? 'is-warning' : ''}>
          <span>PackageType coverage</span>
          <strong>{packageTypeRowCount}/{rowCount}</strong>
          <small>{packageTypeMissingCount} rows missing PackageType</small>
        </div>
        <div>
          <span>Portal mode</span>
          <strong>{diagnostics?.databaseWrite || diagnostics?.tssWrite ? 'Writes enabled' : 'Preview only'}</strong>
          <small>Validation explains output before commit/submit.</small>
        </div>
      </div>
      {(productMaster.packageTypeWarning || productMaster.warning) && (
        <div className="settings-diagnostics-warning">
          <MaterialIcon>info</MaterialIcon>
          <span>{productMaster.packageTypeWarning || productMaster.warning}</span>
        </div>
      )}
      {packageValues.length > 0 && (
        <div className="settings-diagnostics-list compact">
          <strong>Normalised package values</strong>
          <div>
            {packageValues.map((item) => (
              <span key={item.value}>{item.value}: {item.rowCount}</span>
            ))}
          </div>
        </div>
      )}
      {resolutionOrder.length > 0 && (
        <div className="settings-diagnostics-list">
          <strong>Package type resolution order</strong>
          <ol>
            {resolutionOrder.map((item) => <li key={item}>{item}</li>)}
          </ol>
        </div>
      )}
      {notes.length > 0 && (
        <div className="settings-diagnostics-notes">
          {notes.map((note) => <span key={note}>{note}</span>)}
        </div>
      )}
    </div>
  );
}

function SettingsPage({ settings, activeSection, environmentMode, validationDiagnostics, validationDiagnosticsStatus = 'idle', validationDiagnosticsError = '', onSectionChange, onBack, onSaveSettings, onTestTssApi, onRefreshValidationDiagnostics }) {
  const isSettingsLoading = !settings;
  const sections = useMemo(() => {
    const rawSections = mergeSettingsSections(settings?.sections || []);
    return rawSections.map((section) => ({
      ...section,
      rows: (section.rows || []).map((row) => {
        if (section.id !== 'TSS_API' || String(row.key || '').toUpperCase() !== 'ENVIRONMENT') return row;
        return {
          ...row,
          value: environmentModeToSettingsValue(environmentMode || row.value),
          choices: choicesWithDemoMode(row),
        };
      }),
    }));
  }, [settings, environmentMode]);
  const selectedSection = sections.find((section) => section.id === activeSection) || sections[0];
  const [draft, setDraft] = useState({});
  const [saveState, setSaveState] = useState('idle');
  const [saveError, setSaveError] = useState('');
  const isSavingSettings = saveState === 'saving';
  const [testState, setTestState] = useState({ status: 'idle', result: null, error: '' });
  const isTestingApi = testState.status === 'testing';
  const isTssApiSection = selectedSection?.id === 'TSS_API';
  const isValidationSection = selectedSection?.id === 'VALIDATION';

  useEffect(() => {
    const nextDraft = {};
    sections.forEach((section) => {
      (section.rows || []).forEach((row) => {
        nextDraft[`${section.id}.${row.key}`] = row.value || '';
      });
    });
    setDraft(nextDraft);
    setSaveState('idle');
    setSaveError('');
  }, [sections]);

  useEffect(() => {
    setTestState({ status: 'idle', result: null, error: '' });
  }, [activeSection]);

  function draftKey(row) {
    return `${selectedSection.id}.${row.key}`;
  }

  function draftValueFor(sectionId, key) {
    const row = sections.find((section) => section.id === sectionId)?.rows?.find((item) => item.key === key);
    return draft[`${sectionId}.${key}`] ?? row?.value ?? '';
  }

  function updateRow(row, value) {
    setDraft((current) => ({ ...current, [draftKey(row)]: value }));
    setSaveState('changed');
  }

  async function saveSettings() {
    if (!onSaveSettings || !settings) return;
    const updates = [];
    sections.forEach((section) => {
      (section.rows || []).forEach((row) => {
        if (row.editable === false) return;
        const key = `${section.id}.${row.key}`;
        const nextValue = draft[key] ?? '';
        const originalValue = row.value ?? '';
        if (row.isSecret && !nextValue) return;
        if (String(nextValue) === String(originalValue)) return;
        updates.push({ sectionId: section.id, key: row.key, value: nextValue });
      });
    });
    if (!updates.length) {
      setSaveState('saved');
      setSaveError('');
      return;
    }
    setSaveState('saving');
    setSaveError('');
    try {
      await onSaveSettings({ clientCode: settings.portalClientCode || settings.clientCode, updates });
      setSaveState('saved');
    } catch (error) {
      setSaveState('changed');
      setSaveError(error.message);
    }
  }

  async function testTssApi() {
    if (!onTestTssApi || !settings || isTestingApi || saveState === 'changed') return;
    const envCode = String(draftValueFor('TSS_API', 'ENVIRONMENT') || '').toUpperCase();
    if (normalizeEnvironmentMode(envCode) === 'DEMO') {
      setTestState({
        status: 'warning',
        result: {
          ok: false,
          severity: 'warning',
          envCode: 'DEMO',
          endpoint: 'not called',
          httpStatus: null,
          message: 'Demo mode is preview-only: DB off / TSS off. Select Production or Test/QAS and save before testing the real API.',
        },
        error: '',
      });
      return;
    }
    setTestState({ status: 'testing', result: null, error: '' });
    try {
      const result = await onTestTssApi({ clientCode: settings.portalClientCode || settings.clientCode, envCode });
      const nextStatus = result?.ok ? 'success' : result?.severity === 'warning' ? 'warning' : 'error';
      setTestState({ status: nextStatus, result, error: result?.message || 'TSS API test failed.' });
    } catch (error) {
      setTestState({ status: 'error', result: null, error: error.message });
    }
  }

  const apiTestClass = testState.status === 'success' ? 'is-success' : testState.status === 'warning' ? 'is-warning' : 'is-error';
  const apiTestTitle = testState.status === 'success' ? 'TSS API OK' : testState.status === 'warning' ? 'TSS API check warning' : 'TSS API test failed';
  const apiTestDetail = testState.result
    ? `GET ${testState.result.endpoint || '/choice_values/country'} - ${testState.result.envCode || draftValueFor('TSS_API', 'ENVIRONMENT')} - HTTP ${testState.result.httpStatus ?? 'no response'}${testState.result.message ? ` - ${testState.result.message}` : ''}`
    : testState.error;

  return (
    <section className="settings-page" aria-label="Configuration settings">
      <div className="settings-header">
        <div className="settings-heading-row">
          <button className="back-button" type="button" onClick={onBack}>
            <MaterialIcon>arrow_back</MaterialIcon>
            <span>Back</span>
          </button>
          <div>
            <h1>Configuration</h1>
            <p>{settings?.clientCode || 'Tenant'} values from {settings?.source || 'CFG'}.</p>
          </div>
        </div>
        <button className="settings-save" type="button" onClick={saveSettings} disabled={isSettingsLoading || saveState !== 'changed'} aria-busy={isSettingsLoading || isSavingSettings}>
          {isSettingsLoading || isSavingSettings ? <LoadingSpinner className="button-spinner" /> : <MaterialIcon>save</MaterialIcon>}
          <span>{isSettingsLoading ? 'Loading' : isSavingSettings ? 'Saving' : saveState === 'saved' ? 'Saved' : 'Save settings'}</span>
        </button>
      </div>

      <div className="settings-workspace">
        <nav className="settings-section-nav" aria-label="Settings sections">
          {sections.map((section) => (
            <button key={section.id} className={section.id === selectedSection.id ? 'active' : ''} type="button" onClick={() => onSectionChange(section.id)}>
              <MaterialIcon>{section.icon || 'tune'}</MaterialIcon>
              <span>{section.label}</span>
            </button>
          ))}
        </nav>

        <div className="settings-form-panel">
          <div className="settings-panel-title">
            <div>
              <h2>{selectedSection.label}</h2>
              <p>{selectedSection.description || 'Settings prepared for this tenant.'}</p>
            </div>
            <div className="settings-panel-actions">
              {isTssApiSection && (
                <button className="settings-test-api" type="button" onClick={testTssApi} disabled={isSettingsLoading || isSavingSettings || isTestingApi || saveState === 'changed'} aria-busy={isTestingApi} title={saveState === 'changed' ? 'Save settings before testing the API.' : 'Run a GET against the configured TSS API credentials.'}>
                  {!isTestingApi && <MaterialIcon>cloud_sync</MaterialIcon>}
                  <span>{isTestingApi ? 'Testing API' : 'Test API'}</span>
                  {isTestingApi && <LoadingSpinner className="button-spinner" />}
                </button>
              )}
              <span className="settings-mode-chip">{settings?.writeMode === 'db_write_existing_cfg' ? 'DB backed' : settings?.writeMode === 'draft_only' ? 'Draft only' : 'Ready'}</span>
            </div>
          </div>

          {saveError && <div className="settings-save-error">{saveError}</div>}
          {isTssApiSection && testState.status !== 'idle' && testState.status !== 'testing' && (
            <div className={`settings-api-test-result ${apiTestClass}`}>
              <strong>{apiTestTitle}</strong>
              <span>{apiTestDetail}</span>
            </div>
          )}
          {isValidationSection && <SettingsValidationDiagnostics diagnostics={validationDiagnostics} status={validationDiagnosticsStatus} error={validationDiagnosticsError} onRefresh={onRefreshValidationDiagnostics} />}
          <div className="settings-grid">
            {isSettingsLoading && <div className="settings-empty">Loading configuration from CFG...</div>}
            {!isSettingsLoading && (selectedSection.rows || []).map((row) => (
              <div className="settings-config-row" key={row.key}>
                <div className="settings-key-cell">
                  <strong>{row.label}</strong>
                  <span>{row.key}</span>
                </div>
                <div className="settings-value-cell">
                  <SettingsInput row={row} value={draft[draftKey(row)] ?? row.value ?? ''} onChange={(value) => updateRow(row, value)} />
                </div>
                <div className="settings-desc-cell">{row.description}</div>
                <div className="settings-updated-cell">
                  <span>{row.sourceTable}</span>
                  <strong>{row.updatedAt ? String(row.updatedAt).replace('T', ' ').slice(0, 19) : 'No timestamp'}</strong>
                </div>
              </div>
            ))}
            {!isSettingsLoading && !(selectedSection.rows || []).length && <div className="settings-empty">No settings loaded for this section.</div>}
          </div>
        </div>
      </div>
    </section>
  );
}
function TemplateButton({ icon, children }) {
  return (
    <button className="template-button" type="button">
      <MaterialIcon>{icon}</MaterialIcon>
      <span>{children}</span>
    </button>
  );
}


const PREVIEW_GOODS_COLUMNS = [
  { field: 'ordinal', label: '#' },
  { field: 'goods_description', label: 'Description' },
  { field: 'commodity_code', label: 'Commodity' },
  { field: 'type_of_packages', label: 'Pkg type' },
  { field: 'number_of_packages', label: 'Pkgs' },
  { field: 'gross_mass_kg', label: 'Gross kg' },
  { field: 'net_mass_kg', label: 'Net kg' },
  { field: 'status', label: 'Status' },
];

function previewDisplay(value) {
  if (value === null || value === undefined || value === '') return 'Missing';
  return String(value);
}

function previewIssueCount(items = []) {
  return items.reduce((count, item) => count + ((item.issues || []).length), 0);
}

function previewInputValue(value) {
  if (value === null || value === undefined) return '';
  return String(value);
}

function isPreviewMissing(value) {
  return value === null || value === undefined || String(value).trim() === '';
}

function editablePreviewField(field, value) {
  const nextValue = isPreviewMissing(value) ? null : value;
  const changed = nextValue !== field.value;
  const source = changed
    ? { source: 'manualEdit', label: 'EDITED', reason: 'Edited in preview. Run Validate & enrich to apply Modules/Processing validation.' }
    : field.source;
  return {
    ...field,
    value: nextValue,
    blank: isPreviewMissing(nextValue),
    source,
    validationPending: changed || field.validationPending,
  };
}

function previewDraftSummary(consignments = [], baseSummary = {}) {
  let goodsItemCount = 0;
  let splitConsignmentCount = 0;
  let validationPendingCount = 0;
  (consignments || []).forEach((item) => {
    if (item.validationPending) validationPendingCount += 1;
    if (item.split?.isSplit) splitConsignmentCount += 1;
    (item.goodsItems || []).forEach((goods) => {
      goodsItemCount += 1;
      if (goods.validationPending) validationPendingCount += 1;
    });
  });
  return {
    ...baseSummary,
    consignmentCount: consignments.length,
    goodsItemCount,
    splitConsignmentCount,
    validationPendingCount,
  };
}

function clonePreviewConsignments(consignments = []) {
  return JSON.parse(JSON.stringify(consignments || []));
}

function markGoodsNeedsValidation(goods) {
  return {
    ...goods,
    status: 'NEEDS_VALIDATION',
    validationPending: true,
  };
}

function markConsignmentNeedsValidation(item) {
  return {
    ...item,
    status: 'NEEDS_VALIDATION',
    validationPending: true,
    tssPayloadPreview: item.tssPayloadPreview
      ? { ...item.tssPayloadPreview, ready: false, stale: true, requiresBackendValidation: true }
      : item.tssPayloadPreview,
  };
}

function buildEditablePayloadPreview(selected, needsValidation = false) {
  if (!selected) return null;
  const payloadPreview = selected.tssPayloadPreview || null;
  if (!needsValidation || !payloadPreview) return payloadPreview;
  return {
    ...payloadPreview,
    ready: false,
    stale: true,
    requiresBackendValidation: true,
    goodsItemCount: selected.goodsItems?.length || payloadPreview.goodsItemCount || 0,
  };
}
function isAssumptionSource(source) {
  if (!source) return false;
  const rawSource = [source.source, source.label, source.rule, source.reason, source.kind]
    .filter(Boolean)
    .join(' ')
    .toUpperCase();
  return Boolean(
    source.assumption
    || source.assumed
    || source.defaulted
    || rawSource.includes('ASSUMPTION')
    || rawSource.includes('ASSUMED')
  );
}
function previewSourceLabel(source) {
  if (!source) return '';
  if (isAssumptionSource(source)) return source.reason || source.label || 'Assumed default';
  if (source.source === 'manualEdit') return source.reason || 'Edited in preview.';
  return source.reason || source.source || source.sourceColumn || source.apiField || 'mapped';
}

function previewSourceKind(source) {
  if (!source) return '';
  const rawSource = String(source.source || source.label || '').toUpperCase();
  if (isAssumptionSource(source) || rawSource.startsWith('ASSUMPTION')) return 'assumption';
  if (source.source === 'manualEdit') return 'edited';
  if (source.normalised || rawSource.includes('NORMALIS')) return 'normalised';
  if (rawSource.includes('CFG.PRODUCT_MASTER') || rawSource.includes('MASTERDATA')) return 'masterdata';
  return rawSource ? 'mapped' : '';
}

function previewFieldSourceClass(source) {
  const kind = previewSourceKind(source);
  if (!kind || kind === 'mapped') return '';
  return `has-${kind}`;
}

function previewSourceBadge(source) {
  const kind = previewSourceKind(source);
  if (kind === 'assumption') return 'ASSUMPTION';
  if (kind === 'masterdata') return 'MASTERDATA';
  if (kind === 'normalised') return 'NORMALISED';
  if (kind === 'edited') return 'EDITED';
  return 'MAPPED';
}

function sourceHasMasterdata(source) {
  const rawSource = String(source?.source || source?.label || '').toUpperCase();
  return rawSource.includes('CFG.PRODUCT_MASTER') || rawSource.includes('MASTERDATA');
}

function PreviewSourceNote({ source, compact = false }) {
  const kind = previewSourceKind(source);
  if (!kind || kind === 'mapped') return null;
  const label = previewSourceBadge(source);
  const detail = previewSourceLabel(source);
  return (
    <small className={`preview-source-assumption is-${kind}`} title={detail}>
      <em>{label}</em>
      {!compact && detail && <span>{detail}</span>}
    </small>
  );
}

function collectPreviewLineage(item) {
  const counts = { masterdata: 0, assumption: 0, normalised: 0, edited: 0 };
  const examples = [];
  const register = (field) => {
    const source = field?.source;
    if (!source) return;
    const kind = previewSourceKind(source);
    let counted = false;
    if (sourceHasMasterdata(source)) {
      counts.masterdata += 1;
      counted = true;
    }
    if (isAssumptionSource(source)) {
      counts.assumption += 1;
      counted = true;
    }
    if (source.normalised) {
      counts.normalised += 1;
      counted = true;
    }
    if (source.source === 'manualEdit') {
      counts.edited += 1;
      counted = true;
    }
    if (counted && examples.length < 6) {
      examples.push({
        field: field.label || field.field,
        badge: previewSourceBadge(source),
        detail: previewSourceLabel(source),
        kind,
      });
    }
  };
  (item?.fields || []).forEach(register);
  (item?.goodsItems || []).forEach((goods) => (goods.fields || []).forEach(register));
  return { counts, examples };
}

function normalisePreviewLineage(item) {
  const serverLineage = item?.lineageSummary;
  if (!serverLineage?.counts) return collectPreviewLineage(item);
  const counts = {
    masterdata: Number(serverLineage.counts.masterdata || 0),
    assumption: Number(serverLineage.counts.assumption || 0),
    normalised: Number(serverLineage.counts.normalised || 0),
    edited: Number(serverLineage.counts.edited || 0),
  };
  const examples = (serverLineage.examples || []).slice(0, 6).map((entry) => {
    const kinds = entry.kinds || [];
    const kind = kinds.includes('assumption') ? 'assumption' : (kinds.includes('masterdata') ? 'masterdata' : (kinds[0] || 'mapped'));
    return {
      field: entry.label || entry.field,
      badge: kind === 'masterdata' ? 'MASTERDATA' : kind.toUpperCase(),
      detail: entry.reason || entry.source || '',
      kind,
    };
  });
  return { counts, examples };
}

function PreviewLineageSummary({ item, title = '' }) {
  const { counts, examples } = normalisePreviewLineage(item);
  const total = counts.masterdata + counts.assumption + counts.normalised + counts.edited;
  if (!total) return null;
  const chips = [
    ['masterdata', 'CFG masterdata'],
    ['assumption', 'Assumptions'],
    ['normalised', 'Normalised'],
    ['edited', 'Edited'],
  ].filter(([key]) => counts[key] > 0);
  return (
    <div className="preview-lineage-panel">
      {title && <strong className="preview-lineage-title">{title}</strong>}
      <div className="preview-lineage-chips">
        {chips.map(([key, label]) => (
          <span className={`is-${key}`} key={key}>{label}: <strong>{counts[key]}</strong></span>
        ))}
      </div>
      {examples.length > 0 && (
        <div className="preview-lineage-examples">
          {examples.map((entry, index) => (
            <span className={`is-${entry.kind}`} key={`${entry.field}-${index}`} title={entry.detail}>
              <em>{entry.badge}</em>{entry.field}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function PreviewFieldGrid({ fields = [], onChange }) {
  return (
    <div className="preview-field-grid">
      {fields.map((field) => {
        const hasError = (field.issues || []).some((issue) => issue.severity === 'error');
        const hasWarning = (field.issues || []).some((issue) => issue.severity === 'warning');
        const hasInfo = (field.issues || []).some((issue) => issue.severity === 'info');
        const sourceClass = previewFieldSourceClass(field.source);
        const className = ['preview-field', field.missing ? 'is-missing' : '', hasError ? 'has-error' : '', hasWarning ? 'has-warning' : '', hasInfo ? 'has-info' : '', sourceClass].filter(Boolean).join(' ');
        return (
          <label className={className} key={field.field}>
            <span>{field.label}{field.required ? '*' : ''}</span>
            <input
              aria-label={field.label || field.field}
              className="preview-inline-input"
              type="text"
              value={previewInputValue(field.value)}
              placeholder="Missing"
              onChange={(event) => onChange?.(field.field, event.target.value)}
            />
            <PreviewSourceNote source={field.source} />
          </label>
        );
      })}
    </div>
  );
}

function PreviewPayloadPanel({ payloadPreview, needsValidation = false }) {
  if (!payloadPreview) return null;
  const operations = payloadPreview.operations || [];
  const goodsItems = payloadPreview.goodsItems || [];
  const goodsSample = goodsItems.slice(0, 5);
  const readinessLabel = needsValidation ? 'VALIDATE FIRST' : (payloadPreview.ready ? 'READY' : 'NEEDS REVIEW');
  const readinessClass = needsValidation ? 'needs-validation' : (payloadPreview.ready ? 'ready' : 'needs-review');
  return (
    <div className="preview-payload-panel">
      <div className="preview-payload-heading">
        <div>
          <span>Preview payload</span>
          <h3>TSS-ready shape</h3>
        </div>
        <strong className={`preview-payload-readiness ${readinessClass}`}>{readinessLabel} / DB off / TSS off</strong>
      </div>
      <div className="preview-payload-grid">
        {operations.map((operation) => (
          <div className="preview-payload-block" key={operation.operationCode}>
            <span>{operation.operationCode}</span>
            <pre>{JSON.stringify(operation.payload || {}, null, 2)}</pre>
          </div>
        ))}
        <div className="preview-payload-block goods">
          <span>PRS.Goods_Item payloads {goodsItems.length > goodsSample.length ? `(first ${goodsSample.length} of ${goodsItems.length})` : `(${goodsItems.length})`}</span>
          <pre>{JSON.stringify(goodsSample, null, 2)}</pre>
        </div>
      </div>
    </div>
  );
}

function PreviewValidationContext({ context }) {
  if (!context) return null;
  const productMaster = context.productMaster;
  const chips = [];
  if (context.mode || context.source) {
    chips.push({ key: 'mode', label: 'Validation', value: context.mode || context.source, tone: 'processing' });
  }
  if (productMaster) {
    const rowCount = productMaster.rowCount || 0;
    const packageTypeRowCount = productMaster.packageTypeRowCount || 0;
    chips.push({
      key: 'masterdata',
      label: 'CFG.Product_Master',
      value: productMaster.available ? `${rowCount} rows loaded` : productMaster.reason || 'Not used',
      tone: productMaster.available ? 'masterdata' : 'muted',
    });
    if (productMaster.available) {
      chips.push({
        key: 'packageType',
        label: 'PackageType',
        value: productMaster.packageTypeWarning || `${packageTypeRowCount}/${rowCount} populated`,
        tone: packageTypeRowCount ? 'masterdata' : 'assumption',
      });
    }
  }
  const packageTypeResolutionOrder = Array.isArray(context.packageTypeResolutionOrder) ? context.packageTypeResolutionOrder : [];
  if (packageTypeResolutionOrder.length) {
    chips.push({
      key: 'packageResolution',
      label: 'Package type',
      value: packageTypeResolutionOrder.map((item) => String(item).replace(/^source\s+/i, '')).join(' -> '),
      tone: 'processing',
    });
  }
  if (!chips.length) return null;
  return (
    <div className="preview-validation-context" aria-label="Validation context">
      {chips.map((chip) => (
        <span className={`is-${chip.tone}`} key={chip.key} title={chip.value}>
          <em>{chip.label}</em>{chip.value}
        </span>
      ))}
    </div>
  );
}

function PreviewIssueList({ title, issues = [], missingRequired = [] }) {
  if (!issues.length && !missingRequired.length) return null;
  return (
    <div className="preview-issues">
      <strong>{title}</strong>
      {missingRequired.length > 0 && (
        <div className="preview-missing-row">
          {missingRequired.map((field) => <span key={field}>{field}</span>)}
        </div>
      )}
      {issues.map((issue, index) => (
        <p className={`preview-issue ${issue.severity || 'error'}`} key={`${issue.field || 'issue'}-${index}`}>
          <span>{issue.label || issue.field}</span>
          {issue.message}
        </p>
      ))}
    </div>
  );
}

function PreviewDetailsModal({ payload, onClose, onValidated }) {
  const preview = payload?.processingPreview;
  const consignments = preview?.consignments || [];
  const [selectedId, setSelectedId] = useState(consignments[0]?.previewId || '');
  const [draftConsignments, setDraftConsignments] = useState(() => clonePreviewConsignments(consignments));
  const [validatedPreview, setValidatedPreview] = useState(null);
  const [serverValidation, setServerValidation] = useState({ status: 'idle', message: '', context: null });

  useEffect(() => {
    setSelectedId(consignments[0]?.previewId || '');
    setDraftConsignments(clonePreviewConsignments(consignments));
    setValidatedPreview(null);
    setServerValidation({ status: 'idle', message: '', context: null });
  }, [payload?.sha256]);

  if (!preview) return null;
  const effectivePreview = validatedPreview || preview;
  const editableConsignments = draftConsignments.length ? draftConsignments : consignments;
  const selected = editableConsignments.find((item) => item.previewId === selectedId) || editableConsignments[0];
  const selectedGoods = selected?.goodsItems || [];

  function markPreviewDirty() {
    if (serverValidation.status === 'loading') return;
    setServerValidation({
      status: 'dirty',
      message: 'Preview edited. Run Validate & enrich before using this payload.',
      context: null,
    });
  }

  function updateConsignmentField(fieldName, value) {
    markPreviewDirty();
    setDraftConsignments((current) => (current.length ? current : clonePreviewConsignments(consignments)).map((item) => {
      if (item.previewId !== selected?.previewId) return item;
      const fields = (item.fields || []).map((field) => (field.field === fieldName ? editablePreviewField(field, value) : field));
      return markConsignmentNeedsValidation({
        ...item,
        values: { ...(item.values || {}), [fieldName]: isPreviewMissing(value) ? null : value },
        fields,
      });
    }));
  }

  function updateGoodsField(goodsOrdinal, fieldName, value) {
    markPreviewDirty();
    setDraftConsignments((current) => (current.length ? current : clonePreviewConsignments(consignments)).map((item) => {
      if (item.previewId !== selected?.previewId) return item;
      const goodsItems = (item.goodsItems || []).map((goods) => {
        if (goods.ordinal !== goodsOrdinal) return goods;
        const fields = (goods.fields || []).map((field) => (field.field === fieldName ? editablePreviewField(field, value) : field));
        return markGoodsNeedsValidation({
          ...goods,
          values: { ...(goods.values || {}), [fieldName]: isPreviewMissing(value) ? null : value },
          fields,
        });
      });
      return markConsignmentNeedsValidation({ ...item, goodsItems });
    }));
  }

  async function handleServerValidation() {
    const processingPreview = { ...effectivePreview, consignments: editableConsignments };
    setServerValidation({ status: 'loading', message: 'Validating and enriching edited preview...', context: null });
    try {
      const response = await validateConsignmentPreview({
        clientCode: payload.clientCode,
        demoMode: payload.demoMode,
        processingPreview,
      });
      const nextPreview = response.processingPreview || processingPreview;
      const nextConsignments = clonePreviewConsignments(nextPreview.consignments || []);
      setValidatedPreview(nextPreview);
      setDraftConsignments(nextConsignments);
      setSelectedId((current) => (nextConsignments.some((item) => item.previewId === current) ? current : nextConsignments[0]?.previewId || ''));
      const missing = nextPreview.summary?.missingRequiredCount || 0;
      const issues = nextPreview.summary?.issueCount || 0;
      const enriched = nextPreview.summary?.enrichmentCount || 0;
      const nextPayload = {
        ...payload,
        ...response,
        processingPreview: nextPreview,
        validationContext: response.validationContext || payload.validationContext,
        databaseWrite: response.databaseWrite ?? payload.databaseWrite,
        tssWrite: response.tssWrite ?? payload.tssWrite,
        writeMode: response.writeMode || payload.writeMode,
        demoMode: response.demoMode ?? payload.demoMode,
      };
      onValidated?.(nextPayload);
      setServerValidation({
        status: 'success',
        message: missing || issues
          ? `${missing} missing / ${issues} issues / ${enriched} enriched or assumed fields after backend validation.`
          : `Backend validation and enrichment passed for the edited preview. ${enriched} fields enriched or assumed.`,
        context: response.validationContext || null,
      });
    } catch (error) {
      setServerValidation({ status: 'error', message: error.message || 'Backend validation failed.', context: null });
    }
  }
  const activeValidationContext = serverValidation.status === 'dirty' ? null : (serverValidation.context || payload.validationContext || null);
  const summary = serverValidation.status === 'dirty'
    ? previewDraftSummary(editableConsignments, effectivePreview.summary || {})
    : (effectivePreview.summary || {});
  const payloadNeedsValidation = serverValidation.status === 'dirty';
  const splitLabel = summary.splitConsignmentCount ? `${summary.splitConsignmentCount} split parts` : 'No split needed';
  const rowModeText = preview.rowMode === 'api_field_value'
    ? 'Field/value manifest mapped into PRS/TSS shape.'
    : preview.rowMode === 'multi_sheet'
      ? 'Workbook sheets combined into PRS/TSS shape.'
      : 'Workbook rows mapped into PRS/TSS shape.';
  const sourceSheetText = (effectivePreview.sourceSheets || [])
    .map((sheet) => `${sheet.sheetName || 'Sheet'}: ${sheet.rowMode === 'api_field_value' ? 'field/value' : 'rows'} (${sheet.mappedFieldCount || 0} mapped)`)
    .join(' | ');

  return (
    <div className="preview-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="preview-modal" role="dialog" aria-modal="true" aria-label="Mapped preview details">
        <header className="preview-modal-header">
          <div>
            <span className="preview-eyebrow">Preview only / DB off / TSS off</span>
            <h2>{payload.filename}</h2>
            <p>{rowModeText}</p>
            {sourceSheetText && <p className="preview-source-sheets">{sourceSheetText}</p>}
          </div>
          <div className="preview-modal-actions">
            <button className="preview-validate-button" type="button" disabled={serverValidation.status === 'loading'} onClick={handleServerValidation}>
              {serverValidation.status === 'loading' ? <LoadingSpinner className="button-spinner" /> : <MaterialIcon>fact_check</MaterialIcon>}
              <span>{serverValidation.status === 'loading' ? 'Validating' : 'Validate & enrich'}</span>
            </button>
            <button className="modal-close-button" type="button" onClick={onClose} aria-label="Close mapped preview">
              <MaterialIcon>close</MaterialIcon>
            </button>
          </div>
        </header>

        {serverValidation.status !== 'idle' && (
          <div className={`preview-validation-banner ${serverValidation.status}`}>
            <MaterialIcon>{serverValidation.status === 'success' ? 'task_alt' : serverValidation.status === 'error' ? 'error' : serverValidation.status === 'dirty' ? 'edit_note' : 'hourglass_top'}</MaterialIcon>
            <span>{serverValidation.message}</span>
          </div>
        )}

        <PreviewValidationContext context={activeValidationContext} />

        <div className="preview-summary-bar">
          <div><span>Consignments</span><strong>{summary.consignmentCount || 0}</strong></div>
          <div><span>Goods items</span><strong>{summary.goodsItemCount || 0}</strong></div>
          <div><span>Mapped fields</span><strong>{summary.mappedFieldCount || 0}</strong></div>
          <div className={(summary.enrichmentCount || 0) > 0 ? 'is-enriched' : ''}><span>Enriched / assumed</span><strong>{summary.enrichmentCount || 0}</strong></div>
          <div className={(summary.missingRequiredCount || 0) > 0 ? 'is-alert' : ''}><span>Missing required</span><strong>{summary.missingRequiredCount || 0}</strong></div>
          <div><span>99-row split</span><strong>{splitLabel}</strong></div>
        </div>

        <div className="preview-global-lineage">
          <PreviewLineageSummary item={{ lineageSummary: summary.lineageSummary }} title="Batch lineage" />
        </div>

        <div className="preview-modal-body">
          <aside className="preview-consignment-list" aria-label="Preview consignments">
            {editableConsignments.map((item) => (
              <button className={`preview-consignment-tab ${item.previewId === selected?.previewId ? 'is-selected' : ''}`} type="button" key={item.previewId} onClick={() => setSelectedId(item.previewId)}>
                <span>{item.values?.consignment_number || item.previewId}</span>
                <strong>{item.goodsItemCount} goods</strong>
                {item.split?.isSplit && <small>Part {item.split.part}/{item.split.partCount}</small>}
                {(item.missingRequired || []).length > 0 && <em>{item.missingRequired.length} missing</em>}
              </button>
            ))}
          </aside>

          {selected && (
            <div className="preview-detail-surface">
              <div className="preview-detail-title">
                <div>
                  <span>PRS.Consignment</span>
                  <h3>{selected.values?.consignment_number || selected.previewId}</h3>
                </div>
                <StatusBadge status={selected.status || 'NEEDS_REVIEW'} />
              </div>

              {selected.split?.isSplit && (
                <div className="preview-split-note">
                  <MaterialIcon>call_split</MaterialIcon>
                  <span>Original {selected.split.originalConsignmentNumber} split into {selected.split.partCount} consignments with max {selected.split.maxGoodsPerConsignment} goods each. Description and shared fields are preserved.</span>
                </div>
              )}

              <PreviewLineageSummary item={selected} />
              <PreviewIssueList title="Consignment fields needing attention" issues={selected.issues || []} missingRequired={selected.missingRequired || []} />
              <PreviewFieldGrid fields={selected.fields || []} onChange={updateConsignmentField} />

              <div className="preview-goods-header">
                <div>
                  <span>PRS.Goods_Item</span>
                  <h3>{selectedGoods.length} goods rows</h3>
                </div>
                <strong>{previewIssueCount(selectedGoods)} goods issues</strong>
              </div>

              <div className="preview-goods-table-wrap">
                <table className="preview-goods-table">
                  <thead>
                    <tr>
                      {PREVIEW_GOODS_COLUMNS.map((column) => <th key={column.field}>{column.label}</th>)}
                      <th>Missing / Issues</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedGoods.map((goods) => {
                      const fieldLookup = Object.fromEntries((goods.fields || []).map((field) => [field.field, field]));
                      return (
                        <tr className={(goods.issues || []).some((issue) => issue.severity === 'error') || (goods.missingRequired || []).length ? 'has-error' : ''} key={`${selected.previewId}-${goods.ordinal}`}>
                          {PREVIEW_GOODS_COLUMNS.map((column) => {
                            const field = fieldLookup[column.field];
                            const value = column.field === 'ordinal' ? goods.ordinal : (column.field === 'status' ? goods.status : field?.value);
                            if (column.field === 'ordinal' || column.field === 'status') {
                              return <td className={field?.missing ? 'is-missing' : ''} key={column.field}>{previewDisplay(value)}</td>;
                            }
                            const cellClass = [field?.missing ? 'is-missing' : '', previewFieldSourceClass(field?.source)].filter(Boolean).join(' ');
                            return (
                              <td className={cellClass} key={column.field}>
                                <input
                                  aria-label={`${column.label} row ${goods.ordinal}`}
                                  className="preview-table-input"
                                  type="text"
                                  value={previewInputValue(value)}
                                  placeholder="Missing"
                                  onChange={(event) => updateGoodsField(goods.ordinal, column.field, event.target.value)}
                                />
                                <PreviewSourceNote source={field?.source} compact />
                              </td>
                            );
                          })}
                          <td>
                            {(goods.missingRequired || []).length > 0 && <span className="goods-missing-list">{goods.missingRequired.join(', ')}</span>}
                            {(goods.issues || []).map((issue, index) => <small className={`goods-issue ${issue.severity || 'error'}`} key={index}>{issue.message}</small>)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <PreviewPayloadPanel payloadPreview={buildEditablePayloadPreview(selected, payloadNeedsValidation)} needsValidation={payloadNeedsValidation} />
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
function UploadConsignmentPage({ onBack, onPreviewUpload, connection, activeClientCode, environmentMode, forceDemoMode = false }) {
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [isDragging, setIsDragging] = useState(false);
  const [demoMode, setDemoMode] = useState(Boolean(forceDemoMode));
  const effectiveClientCode = activeClientCode || connection?.portalClientCode || DEFAULT_OPERATIONAL_CLIENT_CODE;
  const demoEns = demoEnsForClient(effectiveClientCode);
  const [headerDeclarationNumber, setHeaderDeclarationNumber] = useState('');
  const [previewState, setPreviewState] = useState({ status: 'idle', payload: null, error: '' });
  const [previewDetailsOpen, setPreviewDetailsOpen] = useState(false);

  useEffect(() => {
    if (forceDemoMode) {
      setDemoMode(true);
    }
  }, [forceDemoMode]);

  useEffect(() => {
    if (demoMode) {
      setHeaderDeclarationNumber(demoEns.declarationNumber);
    }
  }, [demoMode, demoEns.declarationNumber]);

  function handleFiles(files) {
    const nextFiles = Array.from(files || []);
    if (nextFiles.length) {
      setSelectedFiles(nextFiles);
      setPreviewState({ status: 'idle', payload: null, error: '' });
      setPreviewDetailsOpen(false);
    }
  }

  function clearFile() {
    setSelectedFiles([]);
    setPreviewState({ status: 'idle', payload: null, error: '' });
    setPreviewDetailsOpen(false);
  }

  async function handlePreview() {
    if (!selectedFiles.length) return;
    setPreviewState({ status: 'loading', payload: null, error: '' });
    try {
      const payload = await onPreviewUpload(selectedFiles, {
        demoMode,
        demoEnsReference: demoMode ? demoEns.declarationNumber : headerDeclarationNumber,
        environmentMode,
      });
      setPreviewState({ status: 'ready', payload, error: '' });
      setPreviewDetailsOpen(Boolean(payload.processingPreview));
    } catch (error) {
      setPreviewDetailsOpen(false);
      setPreviewState({ status: 'error', payload: null, error: error.message });
    }
  }

  function handleDrop(event) {
    event.preventDefault();
    setIsDragging(false);
    handleFiles(event.dataTransfer.files);
  }

  function handleOpenApiDocs(event) {
    event.preventDefault();
    window.open(getApiDocsUrl(), '_blank', 'noopener,noreferrer');
  }

  const processingPreview = previewState.payload?.processingPreview;
  const processingSummary = processingPreview?.summary || {};
  const productMasterContext = previewState.payload?.validationContext?.productMaster;
  const isFieldValuePreview = processingPreview?.rowMode === 'api_field_value';
  const hasProcessingPreview = Boolean(processingPreview);
  const previewMissingRequiredCount = processingSummary.missingRequiredCount || 0;
  const sourceSheetsText = (processingPreview?.sourceSheets || [])
    .map((sheet) => `${sheet.sheetName || 'Sheet'} ${sheet.rowMode === 'api_field_value' ? 'field/value' : 'rows'}: ${sheet.mappedFieldCount || 0} mapped`)
    .join(' | ');

  return (
    <section className="upload-page page-card" aria-label="Upload consignments">
      <div className="page-card-topline">
        <button className="back-button" type="button" onClick={onBack}>
          <MaterialIcon>arrow_back</MaterialIcon>
          <span>Back</span>
        </button>
        <a className="api-link" href={getApiDocsUrl()} target="_blank" rel="noreferrer" title="Open Swagger API documentation" onClick={handleOpenApiDocs}>
          <MaterialIcon>help</MaterialIcon>
          <span>API</span>
        </a>
      </div>

      <header className="upload-heading">
        <h1>Create Consignment From Template</h1>
        <p>Use the templates below to create consignments in bulk by filling in the required information and uploading the file.</p>
        <p>After uploading, you can preview parsed consignments, file-selection rules, and validation readiness before any live processing.</p>
      </header>

      <div className="template-grid" aria-label="Templates">
        <TemplateButton icon="file_present">Template (Excel)</TemplateButton>
        <TemplateButton icon="csv">Template (CSV)</TemplateButton>
        <TemplateButton icon="file_present">SD Template (Excel)</TemplateButton>
        <TemplateButton icon="csv">SD Template (CSV)</TemplateButton>
      </div>

      <div className="info-banner">
        <MaterialIcon>info</MaterialIcon>
        <span>Ensure all mandatory fields in the template are filled correctly before uploading. Mismatched or additional column headers may lead to rejection of the whole upload. Use the provided template as is and avoid modifying column headers or formats.</span>
      </div>

      {connection && (
        <div className="connection-note">
          <MaterialIcon>rule</MaterialIcon>
          <span>{connection.portalClientCode} maps {connectionFileText(connection)} and uses TSS credential {credentialText(connection)}. {routeText(connection)}.</span>
        </div>
      )}

      <div className={`demo-mode-panel ${demoMode ? 'is-active' : ''}`}>
        <label className="demo-toggle">
          <input type="checkbox" checked={demoMode} disabled={forceDemoMode} onChange={(event) => { if (forceDemoMode) return; setDemoMode(event.target.checked); setPreviewState({ status: 'idle', payload: null, error: '' }); setPreviewDetailsOpen(false); }} />
          <span className="demo-switch" aria-hidden="true" />
          <span>{forceDemoMode ? 'Demo mode forced by environment' : 'Demo mode'}</span>
        </label>
        <div className="demo-ens-summary">
          <span>{demoMode ? 'Demo ENS selected' : 'Manual ENS'}</span>
          <strong>{demoMode ? demoEns.declarationNumber : (headerDeclarationNumber || 'Not selected')}</strong>
          <small>{demoMode ? `${demoEns.movementKey} / ${demoEns.arrivalPort}` : 'Preview will use the declaration number above when provided.'}</small>
        </div>
        <div className="demo-safety-chip">DB off / TSS off</div>
      </div>

      <div className="declaration-area">
        <label>Declaration type:</label>
        <div className="declaration-pill">Entry Summary Declaration</div>
      </div>

      <label className="field-shell stacked">
        <span>Header Declaration Number*</span>
        <input type="text" value={demoMode ? demoEns.declarationNumber : headerDeclarationNumber} readOnly={demoMode} placeholder="ENS000000000000000" onChange={(event) => setHeaderDeclarationNumber(event.target.value)} />
      </label>

      <label className="select-shell stacked">
        <span>No SFD Reason | ENS Only reason</span>
        <select defaultValue="none">
          <option value="none">None (I want to use SFD)</option>
          <option value="ens-only">ENS only</option>
        </select>
      </label>

      <label
        className={`drop-zone ${isDragging ? 'is-dragging' : ''}`}
        onDragOver={(event) => { event.preventDefault(); setIsDragging(true); }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
      >
        <input type="file" accept=".xlsx,.xls,.csv,.pdf" multiple onChange={(event) => handleFiles(event.target.files)} />
        <MaterialIcon>cloud_upload</MaterialIcon>
        <strong>{selectedFiles.length ? `${selectedFiles.length} file${selectedFiles.length === 1 ? '' : 's'} selected` : 'Drag and drop files here or click'}</strong>
        <span>{selectedFiles.length ? selectedFiles.map((file) => file.name).join(' | ') : 'Supported formats: .xlsx, .xls, .csv, .pdf'}</span>
        <span>Max file size: 50 MB</span>
      </label>

      {previewState.status !== 'idle' && (
        <div className={`upload-preview-card ${previewState.status}`}>
          {previewState.status === 'loading' && <span className="loading-line"><LoadingSpinner /><span>Preparing API preview...</span></span>}
          {previewState.status === 'error' && <span>{previewState.error}</span>}
          {previewState.status === 'ready' && (
            <>
              <strong>{previewState.payload.filename}</strong>
              <span>{previewState.payload.demoMode ? `Demo ENS: ${previewState.payload.demoEns?.declarationNumber}` : previewState.payload.selectionRule}</span>
              <span>Mode: {previewState.payload.writeMode} / DB: {previewState.payload.databaseWrite ? 'on' : 'off'} / TSS: {previewState.payload.tssWrite ? 'on' : 'off'}</span>
              <span>{previewState.payload.selectionRule}</span>
              <span>Selected ordinal: {previewState.payload.selectedFileOrdinal} / received: {(previewState.payload.receivedFiles || []).length}</span>
              <span>Ignored: {(previewState.payload.ignoredFiles || []).map((item) => item.filename).join(', ') || 'none'}</span>
              <span>Target: {previewState.payload.wouldLand.fileTable} / {previewState.payload.wouldLand.rowTable}</span>
              {(previewState.payload.validationContext?.demoSatisfiedTargets || []).length > 0 && (
                <span>Demo supplied: {(previewState.payload.validationContext.demoSatisfiedTargets || []).map((item) => item.targetColumn).join(', ')}</span>
              )}
              {hasProcessingPreview ? (
                <>
                  {sourceSheetsText && <span>Workbook sheets: {sourceSheetsText}</span>}
                  <span>{isFieldValuePreview ? 'Field/value rows' : 'Preview rows'}: {processingSummary.sourceRows || 0} source / {processingSummary.mappedFieldCount || 0} matched / {processingSummary.unmatchedFieldCount || 0} unmatched into PRS/TSS preview</span>
                  <span>Enrichment: {processingSummary.enrichmentCount || 0} fields enriched or assumed before review</span>
                  {productMasterContext && (
                    <span>Masterdata: {productMasterContext.available ? `${productMasterContext.rowCount || 0} CFG.Product_Master rows loaded / PackageType ${productMasterContext.packageTypeRowCount || 0}/${productMasterContext.rowCount || 0}` : productMasterContext.reason || 'CFG.Product_Master not used'}</span>
                  )}
                  {productMasterContext?.warning && <span>{productMasterContext.warning}</span>}
                  <span>Preview required: {previewMissingRequiredCount ? `${previewMissingRequiredCount} missing across PRS/TSS details` : 'ready - no required fields missing'}</span>
                </>
              ) : (
                <>
                  <span>Mapping: {previewState.payload.mappingSummary?.status || 'UNKNOWN'} - {previewState.payload.mappingSummary?.mappedColumns || 0}/{previewState.payload.mappingSummary?.detectedColumns || 0} columns mapped</span>
                  <span>Suggested: {previewState.payload.mappingSuggestions?.suggestedCount || 0} matched / {previewState.payload.mappingSuggestions?.unmatchedCount || 0} unmatched</span>
                  {(previewState.payload.mappingSuggestions?.missingRequiredTargets || []).length > 0 && (
                    <span>Missing required: {(previewState.payload.mappingSuggestions.missingRequiredTargets || []).slice(0, 5).map((item) => item.targetColumn).join(', ')}{(previewState.payload.mappingSuggestions.missingRequiredTargets || []).length > 5 ? '...' : ''}</span>
                  )}
                </>
              )}
              {previewState.payload.detectedStructure?.warning && <span>{previewState.payload.detectedStructure.warning}</span>}
              {(previewState.payload.detectedStructure?.pdfDetectedReferences || []).length > 0 && (
                <span>PDF refs: {(previewState.payload.detectedStructure.pdfDetectedReferences || []).map((item) => `${item.type} ${item.value}`).join(', ')}</span>
              )}
              {previewState.payload.detectedStructure?.pdfRequiresOcr && <span>OCR required before automatic consignment/goods mapping.</span>}
              {(previewState.payload.detectedStructure?.columns || []).length > 0 && (
                <span>Columns: {(previewState.payload.detectedStructure.columns || []).slice(0, 8).map((column) => column.name).join(', ')}{(previewState.payload.detectedStructure.columns || []).length > 8 ? '...' : ''}</span>
              )}
              <span>SHA256: {previewState.payload.sha256.slice(0, 16)}...</span>
              {previewState.payload.processingPreview && (
                <button className="preview-details-button" type="button" onClick={() => setPreviewDetailsOpen(true)}>
                  <MaterialIcon>visibility</MaterialIcon>
                  <span>View mapped details</span>
                </button>
              )}
            </>
          )}
        </div>
      )}

      <div className="upload-actions">
        <button className="file-picker-button" type="button" onClick={() => document.querySelector('.drop-zone input')?.click()}>Open file picker</button>
        <button className="clear-button" type="button" disabled={!selectedFiles.length} onClick={clearFile}>Clear</button>
      </div>

      <button className="preview-button" type="button" disabled={!selectedFiles.length || previewState.status === 'loading'} onClick={handlePreview} aria-busy={previewState.status === 'loading'}>
        {previewState.status === 'loading' && <LoadingSpinner className="button-spinner" />}
        <span>{previewState.status === 'loading' ? 'Preparing Preview' : (demoMode ? 'Run Demo Preview' : 'Upload & Preview')}</span>
      </button>

      {previewDetailsOpen && previewState.payload?.processingPreview && (
        <PreviewDetailsModal
          payload={previewState.payload}
          onClose={() => setPreviewDetailsOpen(false)}
          onValidated={(nextPayload) => setPreviewState((current) => (
            current.payload?.sha256 === nextPayload.sha256 ? { ...current, payload: nextPayload } : current
          ))}
        />
      )}
    </section>
  );
}
function StatusBadge({ status, fallback = '-' }) {
  const cleanStatus = normalizeStatusDisplay(status, '');
  if (!cleanStatus) return <span className="status-placeholder">{fallback}</span>;
  return <span className={`status-badge ${statusClassName(cleanStatus)}`}>{cleanStatus}</span>;
}

const CONTROL_TOWER_TABS = [
  { id: 'overview', label: 'Overview', icon: 'dashboard' },
  { id: 'trace', label: 'Traceability', icon: 'account_tree' },
  { id: 'jobs', label: 'Jobs', icon: 'conversion_path' },
  { id: 'api', label: 'API / TSS Logs', icon: 'sync_alt' },
];

function controlDisplay(value, fallback = '-') {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

function controlBool(value) {
  return Number(value) || value === true ? 'Yes' : 'No';
}

function compactText(value, maxLength = 120) {
  const text = controlDisplay(value, '');
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
}

function formatDateTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function controlRowKey(row, index) {
  return row.QueueID ?? row.ExecutionID ?? row.CallID ?? row.LogID ?? row.JobCode ?? row.MovementKey ?? row.ParameterKey ?? index;
}

const MOVEMENT_COLUMNS = [
  { key: 'MovementKey', label: 'Movement' },
  { key: 'Declaration_Number', label: 'Declaration' },
  { key: 'Fusion_Status', label: 'Fusion', render: (row) => <StatusBadge status={row.Fusion_Status || row.Status} /> },
  { key: 'Tss_Status', label: 'TSS', render: (row) => (row.Tss_Status ? <StatusBadge status={row.Tss_Status} /> : '-') },
  { key: 'SourceTable', label: 'Source' },
  { key: 'LastExecutionID', label: 'Execution' },
  { key: 'UpdatedAt', label: 'Updated', render: (row) => formatDateTime(row.UpdatedAt || row.CreatedAt) },
];

const EXECUTION_COLUMNS = [
  { key: 'ExecutionID', label: 'Execution' },
  { key: 'ModuleName', label: 'Module' },
  { key: 'ProcessName', label: 'Process' },
  { key: 'RunMode', label: 'Mode' },
  { key: 'Status', label: 'Status', render: (row) => <StatusBadge status={row.Status} /> },
  { key: 'ItemsProcessed', label: 'Items', render: (row) => `${row.ItemsProcessed ?? 0}/${row.ItemsFound ?? 0}` },
  { key: 'ItemsFailed', label: 'Failed' },
  { key: 'StartedAt', label: 'Started', render: (row) => formatDateTime(row.StartedAt) },
  { key: 'ErrorMessage', label: 'Error', render: (row) => compactText(row.ErrorMessage) },
];

const JOB_COLUMNS = [
  { key: 'JobCode', label: 'Job' },
  { key: 'JobName', label: 'Name' },
  { key: 'ModuleName', label: 'Module' },
  { key: 'JobType', label: 'Type' },
  { key: 'StepNo', label: 'Step' },
  { key: 'EntryPoint', label: 'Entry point' },
  { key: 'Schedule', label: 'Schedule' },
  { key: 'IsActive', label: 'Active', render: (row) => controlBool(row.IsActive) },
];

const QUEUE_COLUMNS = [
  { key: 'QueueID', label: 'Queue' },
  { key: 'Verb', label: 'Verb' },
  { key: 'MovementKey', label: 'Movement' },
  { key: 'Status', label: 'Status', render: (row) => <StatusBadge status={row.Status} /> },
  { key: 'Attempts', label: 'Attempts' },
  { key: 'RequestedAt', label: 'Requested', render: (row) => formatDateTime(row.RequestedAt) },
  { key: 'ResultMessage', label: 'Result', render: (row) => compactText(row.ResultMessage) },
];

const ACTIVITY_COLUMNS = [
  { key: 'CreatedAt', label: 'Created', render: (row) => formatDateTime(row.CreatedAt) },
  { key: 'ModuleName', label: 'Module' },
  { key: 'StepName', label: 'Step' },
  { key: 'LogLevel', label: 'Level', render: (row) => <StatusBadge status={row.LogLevel || 'INFO'} /> },
  { key: 'Message', label: 'Message', render: (row) => compactText(row.Message, 180) },
];

const API_CALL_COLUMNS = [
  { key: 'CreatedAt', label: 'Created', render: (row) => formatDateTime(row.CreatedAt) },
  { key: 'MovementKey', label: 'Movement' },
  { key: 'ResourceName', label: 'Resource' },
  { key: 'OpType', label: 'Operation' },
  { key: 'EnvCode', label: 'Env' },
  { key: 'StatusCode', label: 'HTTP' },
  { key: 'Success', label: 'Success', render: (row) => controlBool(row.Success) },
  { key: 'IsDryRun', label: 'Dry run', render: (row) => controlBool(row.IsDryRun) },
  { key: 'DurationMs', label: 'ms' },
  { key: 'ErrorMessage', label: 'Error', render: (row) => compactText(row.ErrorMessage) },
];

const PARAM_COLUMNS = [
  { key: 'ParameterKey', label: 'Parameter' },
  { key: 'ParameterValue', label: 'Value' },
  { key: 'ValueType', label: 'Type' },
  { key: 'IsActive', label: 'Active', render: (row) => controlBool(row.IsActive) },
  { key: 'UpdatedAt', label: 'Updated', render: (row) => formatDateTime(row.UpdatedAt) },
];

function ControlMetric({ item }) {
  return (
    <div className={`control-metric tone-${item.tone || 'muted'}`}>
      <span>{item.label}</span>
      <strong>{formatNumber(item.value)}</strong>
      <small>{item.source}</small>
    </div>
  );
}

function ControlPanel({ title, icon, children, className = '' }) {
  return (
    <section className={`control-panel ${className}`}>
      <div className="control-panel-header">
        <MaterialIcon>{icon}</MaterialIcon>
        <h2>{title}</h2>
      </div>
      {children}
    </section>
  );
}

function ControlLoadingState({ label = 'Loading control tower data...', detail = 'Reading CFG, ING, PRS, STG, EXC, LOG and API records.' }) {
  return (
    <div className="control-loading-state" role="status" aria-live="polite">
      <LoadingSpinner />
      <div>
        <strong>{label}</strong>
        <span>{detail}</span>
      </div>
    </div>
  );
}

function ControlTowerTable({ columns, rows, emptyText, loading = false, loadingText = 'Loading records...' }) {
  if (loading) return <ControlLoadingState label={loadingText} detail="Waiting for the Control Tower API response." />;
  if (!rows?.length) return <div className="control-empty">{emptyText || 'No records'}</div>;
  return (
    <div className="control-table-wrap">
      <table className="control-table">
        <thead>
          <tr>{columns.map((column) => <th key={column.key}>{column.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={controlRowKey(row, index)}>
              {columns.map((column) => {
                const rendered = column.render ? column.render(row) : row[column.key];
                return <td key={column.key}>{rendered === null || rendered === undefined || rendered === '' ? '-' : rendered}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ControlTowerPage({ onBack, clientCode, connection }) {
  const [activeTab, setActiveTab] = useState('overview');
  const [refreshToken, setRefreshToken] = useState(0);
  const [state, setState] = useState({ status: 'loading', payload: null, error: '' });

  useEffect(() => {
    let cancelled = false;
    async function loadControlTower() {
      setState((current) => ({ status: 'loading', payload: current.payload, error: '' }));
      try {
        const payload = await getControlTower(clientCode || DEFAULT_OPERATIONAL_CLIENT_CODE);
        if (!cancelled) setState({ status: 'ready', payload, error: '' });
      } catch (error) {
        if (!cancelled) setState((current) => ({ status: 'error', payload: current.payload, error: error.message }));
      }
    }
    loadControlTower();
    return () => { cancelled = true; };
  }, [clientCode, refreshToken]);

  const payload = state.payload || {};
  const isControlLoading = state.status === 'loading';
  const isInitialControlLoading = isControlLoading && !state.payload;
  const counts = payload.counts || {};
  const summary = payload.summary?.length ? payload.summary : [
    { label: 'Inbound files', value: counts.inboundFiles || 0, source: 'ING.Inbound_File', tone: 'flow' },
    { label: 'PRS records', value: (counts.prsHeaders || 0) + (counts.prsConsignments || 0) + (counts.prsGoodsItems || 0), source: 'PRS', tone: 'good' },
    { label: 'Active jobs', value: counts.activeJobs || 0, source: 'CFG.Job', tone: 'fusion' },
    { label: 'API errors', value: counts.apiErrors || 0, source: 'API.Call', tone: 'bad' },
  ];
  const availabilityEntries = Object.entries(payload.availability || {});
  const availableCount = availabilityEntries.filter(([, exists]) => exists).length;
  const generatedAt = payload.generatedAt ? formatDateTime(payload.generatedAt) : (state.status === 'loading' ? 'loading' : '-');

  return (
    <section className="control-tower-page" aria-label="Control tower">
      <header className="control-tower-header">
        <button className="back-button" type="button" onClick={onBack}>
          <MaterialIcon>arrow_back</MaterialIcon>
          <span>Back</span>
        </button>
        <div className="control-heading-copy">
          <span className="control-eyebrow">CFG / ING / PRS / STG / EXC / LOG / API</span>
          <h1>Control Tower</h1>
          <p>{payload.clientCode || clientCode} - {generatedAt} - read-only</p>
        </div>
        <button className="control-refresh-button" type="button" disabled={state.status === 'loading'} onClick={() => setRefreshToken((value) => value + 1)} aria-busy={state.status === 'loading'}>
          {state.status === 'loading' ? <LoadingSpinner className="button-spinner" /> : <MaterialIcon>refresh</MaterialIcon>}
          <span>{state.status === 'loading' ? 'Loading' : 'Refresh'}</span>
        </button>
      </header>

      {state.status === 'error' && (
        <div className="action-feedback is-error control-error">
          <strong>Control Tower API unavailable</strong>
          <span>{state.error}</span>
        </div>
      )}

      <div className="control-meta-strip" aria-label="Control tower context">
        <div><span>Client</span><strong>{payload.clientCode || clientCode}</strong></div>
        <div><span>TSS</span><strong>{credentialText(connection)}</strong></div>
        <div><span>Route</span><strong>{routeText(connection)}</strong></div>
        <div><span>Objects</span><strong>{availabilityEntries.length ? `${availableCount}/${availabilityEntries.length}` : '-'}</strong></div>
      </div>

      {isInitialControlLoading ? (
        <ControlLoadingState
          label="Loading Control Tower..."
          detail="Reading pipeline counts, object coverage, jobs, queue, executions, process logs and TSS API calls."
        />
      ) : (
        <div className="summary-rail control-summary" aria-label="Control tower summary">
          {summary.map((item) => <ControlMetric key={item.label} item={item} />)}
        </div>
      )}

      {isControlLoading && state.payload && (
        <div className="control-refreshing-note" role="status" aria-live="polite">
          <LoadingSpinner />
          <span>Refreshing Control Tower data...</span>
        </div>
      )}

      <div className="control-tabs" role="tablist" aria-label="Control tower sections">
        {CONTROL_TOWER_TABS.map((tab) => (
          <button key={tab.id} className={activeTab === tab.id ? 'active' : ''} type="button" role="tab" aria-selected={activeTab === tab.id} onClick={() => setActiveTab(tab.id)}>
            <MaterialIcon>{tab.icon}</MaterialIcon>
            <span>{tab.label}</span>
          </button>
        ))}
      </div>

      {activeTab === 'overview' && (
        <div className="control-sections">
          <ControlPanel title="Pipeline" icon="schema">
            <div className="pipeline-grid">
              {(payload.pipeline || []).map((stage) => (
                <div className={`pipeline-card tone-${stage.tone || 'muted'}`} key={stage.stage}>
                  <span>{stage.stage}</span>
                  <strong>{formatNumber(stage.count)}</strong>
                  <small>{stage.label}</small>
                  <em>{stage.table}</em>
                </div>
              ))}
              {isInitialControlLoading && <ControlLoadingState label="Loading pipeline counts..." detail="Checking source, raw, PRS, STG and TSS activity." />}
              {!isInitialControlLoading && !(payload.pipeline || []).length && <div className="control-empty">No pipeline counts</div>}
            </div>
          </ControlPanel>
          <div className="control-grid">
            <ControlPanel title="Object coverage" icon="inventory_2">
              {availabilityEntries.length ? (
                <div className="availability-grid">
                  {availabilityEntries.map(([name, exists]) => (
                    <span className={exists ? 'is-present' : 'is-missing'} key={name}>
                      <MaterialIcon>{exists ? 'check_circle' : 'error'}</MaterialIcon>
                      {name}
                    </span>
                  ))}
                </div>
              ) : isInitialControlLoading ? (
                <ControlLoadingState label="Loading object coverage..." detail="Checking deployed CFG, ING, PRS, STG and API objects." />
              ) : <div className="control-empty">No object metadata</div>}
            </ControlPanel>
            <ControlPanel title="Runtime parameters" icon="tune">
              <ControlTowerTable columns={PARAM_COLUMNS} rows={payload.params || []} emptyText="No automation parameters" loading={isInitialControlLoading} loadingText="Loading runtime parameters..." />
            </ControlPanel>
          </div>
          <ControlPanel title="Recent activity" icon="article">
            <ControlTowerTable columns={ACTIVITY_COLUMNS} rows={payload.activity || []} emptyText="No process log rows" loading={isInitialControlLoading} loadingText="Loading process activity..." />
          </ControlPanel>
        </div>
      )}

      {activeTab === 'trace' && (
        <div className="control-sections">
          <ControlPanel title="Movements" icon="account_tree">
            <ControlTowerTable columns={MOVEMENT_COLUMNS} rows={payload.movements || []} emptyText="No traceable movements" loading={isInitialControlLoading} loadingText="Loading movement trace..." />
          </ControlPanel>
          <ControlPanel title="Executions" icon="task_alt">
            <ControlTowerTable columns={EXECUTION_COLUMNS} rows={payload.executions || []} emptyText="No execution rows" loading={isInitialControlLoading} loadingText="Loading execution history..." />
          </ControlPanel>
        </div>
      )}

      {activeTab === 'jobs' && (
        <div className="control-grid">
          <ControlPanel title="CFG.Job" icon="settings_suggest">
            <ControlTowerTable columns={JOB_COLUMNS} rows={payload.jobs || []} emptyText="No configured jobs" loading={isInitialControlLoading} loadingText="Loading configured jobs..." />
          </ControlPanel>
          <ControlPanel title="EXC.Job_Queue" icon="pending_actions">
            <ControlTowerTable columns={QUEUE_COLUMNS} rows={payload.queue || []} emptyText="No queued automation work" loading={isInitialControlLoading} loadingText="Loading queued work..." />
          </ControlPanel>
        </div>
      )}

      {activeTab === 'api' && (
        <div className="control-sections">
          <ControlPanel title="API.Call" icon="sync_alt">
            <ControlTowerTable columns={API_CALL_COLUMNS} rows={payload.apiCalls || []} emptyText="No API call rows" loading={isInitialControlLoading} loadingText="Loading API/TSS calls..." />
          </ControlPanel>
        </div>
      )}
    </section>
  );
}
function MasterLivePage({ onBack }) {
  return (
    <section className="master-live-page" aria-label="Master live prototype">
      <header className="master-live-header">
        <button className="back-button" type="button" onClick={onBack}>
          <MaterialIcon>arrow_back</MaterialIcon>
          <span>Back</span>
        </button>
        <div className="master-live-heading">
          <span className="control-eyebrow">Master branch prototype</span>
          <h1>Goods Excel Validator</h1>
          <p>Embedded copy from Master - Render live available</p>
        </div>
        <div className="master-live-actions">
          <a className="master-live-link" href={MASTER_LIVE_URL} target="_blank" rel="noreferrer">
            <MaterialIcon>open_in_new</MaterialIcon>
            <span>Open Render</span>
          </a>
          <a className="master-live-link secondary" href={MASTER_LIVE_EMBED_URL} target="_blank" rel="noreferrer">
            <MaterialIcon>open_in_browser</MaterialIcon>
            <span>Open Local</span>
          </a>
        </div>
      </header>

      <div className="master-live-frame-shell">
        <iframe
          className="master-live-frame"
          src={MASTER_LIVE_EMBED_URL}
          title="Goods Excel Validation Prototype"
        />
      </div>
    </section>
  );
}
function ConsignmentValidationSummary({ summary }) {
  if (!summary) return null;
  const missingRequired = summary.missingRequired || [];
  const assumptions = summary.assumptions || [];
  const enhancements = summary.enhancements || [];
  const packageType = summary.packageType || {};
  const packageValues = packageType.values || [];
  const decimalNormalisation = summary.decimalNormalisation || [];
  const notes = summary.notes || [];
  const missingCount = missingRequired.reduce((total, item) => total + Number(item.count || 0), 0);
  const status = summary.status || (missingCount ? 'NEEDS_REVIEW' : 'READY');

  return (
    <section className={`consignment-validation-panel ${missingCount ? 'needs-review' : 'ready'}`} aria-label="Validation and enrichment summary">
      <div className="consignment-validation-head">
        <div>
          <span>Modules validation</span>
          <h3>Validation & enrichment</h3>
          <p>{summary.source || 'PRS'} / {summary.engine || 'Modules.Processing'}</p>
        </div>
        <StatusBadge status={status} />
      </div>
      <div className="consignment-validation-mode">
        <span className="is-readonly">Read-only diagnostic</span>
        <span className={summary.databaseWrite ? 'is-warning' : 'is-safe'}>{summary.databaseWrite ? 'DB write enabled' : 'DB write off'}</span>
        <span className={summary.tssWrite ? 'is-warning' : 'is-safe'}>{summary.tssWrite ? 'TSS write enabled' : 'TSS write off'}</span>
      </div>
      <div className="consignment-validation-grid">
        <div><span>Goods rows</span><strong>{summary.goodsItemCount || 0}</strong><small>PRS.Goods_Item</small></div>
        <div className={missingCount ? 'is-warning' : ''}><span>Missing required</span><strong>{missingCount}</strong><small>{missingRequired.length ? `${missingRequired.length} field groups` : 'Ready'}</small></div>
        <div className={packageType.missingCount ? 'is-warning' : ''}><span>Package type</span><strong>{packageType.missingCount || 0}</strong><small>{packageType.normalisedCount || 0} normalised</small></div>
        <div className={assumptions.length ? 'is-assumption' : ''}><span>Assumptions</span><strong>{assumptions.length}</strong><small>Visible before TSS</small></div>
      </div>
      {!!packageValues.length && (
        <div className="consignment-validation-chips">
          <strong>Package values</strong>
          <div>{packageValues.slice(0, 8).map((item) => <span key={item.value}>{item.value} <em>{item.rowCount}</em></span>)}</div>
        </div>
      )}
      {!!missingRequired.length && (
        <div className="consignment-validation-list">
          <strong>Needs attention</strong>
          <ul>{missingRequired.slice(0, 8).map((item) => <li key={`${item.scope}-${item.field}`}>{item.label}: {item.count}</li>)}</ul>
        </div>
      )}
      {!!assumptions.length && (
        <div className="consignment-validation-list assumption-list">
          <strong>Assumptions</strong>
          <ul>{assumptions.map((item) => <li key={item.rule}>{item.rule}: {item.value} ({item.count})</li>)}</ul>
        </div>
      )}
      {!!enhancements.length && (
        <div className="consignment-validation-list">
          <strong>Module enrichment</strong>
          <ul>{enhancements.map((item) => <li key={`${item.field}-${item.source}`}>{item.field}: {item.count}</li>)}</ul>
        </div>
      )}
      {!!decimalNormalisation.length && (
        <div className="consignment-validation-list">
          <strong>TSS numeric format</strong>
          <ul>{decimalNormalisation.map((item) => <li key={item.field}>{item.field}: {item.count} values need 2-decimal formatting</li>)}</ul>
        </div>
      )}
      {!!notes.length && (
        <div className="consignment-validation-list muted-list">
          <strong>Notes</strong>
          <ul>{notes.map((note) => <li key={note}>{note}</li>)}</ul>
        </div>
      )}
    </section>
  );
}
function ConsignmentDetailModal({ row, onClose, onSave, onQueueForTss }) {
  const [detailState, setDetailState] = useState({ status: 'loading', payload: null, error: '' });
  const [draft, setDraft] = useState(() => buildConsignmentDraft(row));
  const [actionState, setActionState] = useState({ status: 'idle', label: '', error: '' });

  useEffect(() => {
    let cancelled = false;
    setDraft(buildConsignmentDraft(row));
    setActionState({ status: 'idle', label: '', error: '' });
    if (!row?.consignmentRowId) {
      setDetailState({ status: 'ready', payload: null, error: '' });
      return () => { cancelled = true; };
    }

    setDetailState({ status: 'loading', payload: null, error: '' });
    getConsignmentDetail(row.consignmentRowId)
      .then((payload) => {
        if (cancelled) return;
        setDetailState({ status: 'ready', payload, error: '' });
        setDraft(buildConsignmentDraft(row, payload?.consignment));
      })
      .catch((error) => {
        if (cancelled) return;
        setDetailState({ status: 'error', payload: null, error: error.message });
      });

    return () => { cancelled = true; };
  }, [row]);

  function updateDraft(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  function saveDraft() {
    onSave(applyConsignmentDraft(row, draft));
    onClose();
  }

  function showGatedAction(label, message) {
    setActionState({ status: 'ready', label, error: '', message });
  }

  async function runRouteCheck(label) {
    if (!onQueueForTss || !row?.consignmentRowId) return;
    setActionState({ status: 'loading', label, error: '' });
    try {
      await onQueueForTss(row);
      setActionState({ status: 'ready', label, error: '', message: 'Route check completed. Review blockers before live submission.' });
    } catch (error) {
      setActionState({ status: 'error', label, error: error.message });
    }
  }

  const detailRow = detailState.payload?.consignment || {};
  const goodsItems = detailState.payload?.goodsItems || [];
  const currentTssStatus = draft.tssStatus || deriveTssStatus(detailRow || row);
  const pipelineRow = { ...row, ...detailRow, tssStatus: currentTssStatus, declarationNumber: draft.declarationNumber || row.declarationNumber };
  const ensReference = draft.declarationNumber || row.declarationNumber || detailRow.HeaderDeclarationNumber || detailRow.declaration_number || '-';
  const sfdValue = row.sfdReference || row.sfdMrn || 'Pending';
  const sdiValue = row.sdiReferences || 'Pending';
  const consignorName = readRecordValue(detailRow, ['consignor_name', 'ConsignorName'], '-');
  const consignorEori = readRecordValue(detailRow, ['consignor_eori', 'ConsignorEori'], '');
  const consigneeEori = readRecordValue(detailRow, ['consignee_eori', 'ConsigneeEori'], '-');
  const importerEori = readRecordValue(detailRow, ['importer_eori', 'ImporterEori'], '-');
  const importerName = readRecordValue(detailRow, ['importer_name', 'ImporterName'], '');
  const conveyanceRef = readRecordValue(detailRow, ['conveyance_ref', 'ConveyanceRef'], '-');
  const sourceLabel = readRecordValue(detailRow, ['Source', 'source'], row.source || 'PRS.Consignment');
  const updatedLabel = formatDateTime(readRecordValue(detailRow, ['UpdatedAt', 'updated_at'], row.updatedAt || ''));
  const arrivalLabel = formatDateTime(readRecordValue(detailRow, ['HeaderArrivalDateTime', 'arrival_date_time', 'ArrivalDateTime'], row.arrivalDateTime || ''));
  const actionBusy = actionState.status === 'loading';
  const hasRouteCheck = Boolean(onQueueForTss && row?.consignmentRowId);

  return (
    <div className="preview-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="consignment-detail-modal" role="dialog" aria-modal="true" aria-label="ENS consignment detail">
        <header className="preview-modal-header consignment-detail-modal-header">
          <div>
            <span className="preview-eyebrow">ENS / PRS Consignment</span>
            <h2>{draft.consignmentNumber || row.consignmentNumber}</h2>
            <p>{row.movementKey || 'Movement pending'} / {row.transportDocumentNumber || 'Document pending'}</p>
          </div>
          <div className="consignment-modal-status">
            <span>TSS Status</span>
            <StatusBadge status={currentTssStatus} />
          </div>
          <button className="modal-close-button" type="button" onClick={onClose} aria-label="Close consignment detail">
            <MaterialIcon>close</MaterialIcon>
          </button>
        </header>

        <div className="consignment-detail-body">
          <div className="v2-consignment-status-strip" aria-label="V2 BKD consignment references">
            <span className="v2-ref-chip local"><strong>Local</strong><StatusBadge status={row.status || 'DRAFT'} /></span>
            <span className="v2-ref-chip tss"><strong>TSS</strong><StatusBadge status={currentTssStatus} /></span>
            <span className="v2-ref-chip"><strong>ENS</strong><em>{ensReference}</em></span>
            <span className="v2-ref-chip"><strong>SFD</strong><em>{sfdValue}</em></span>
            <span className="v2-ref-chip"><strong>SDI</strong><em>{sdiValue}</em></span>
          </div>

          <ConsignmentPipelineRail row={pipelineRow} goodsItems={goodsItems} />

          <div className="consignment-action-grid" aria-label="Consignment pipeline actions">
            <button className="pipeline-action primary" type="button" disabled={!hasRouteCheck || actionBusy} onClick={() => runRouteCheck('Submit consignment')}>
              {actionBusy ? <LoadingSpinner className="button-spinner" /> : <MaterialIcon>send</MaterialIcon>}
              <strong>Submit Consignment</strong>
              <span>Build TSS route check</span>
            </button>
            <button className="pipeline-action" type="button" onClick={() => showGatedAction('Sync TSS Now', 'TSS sync follows the Modules/Submission mirror flow and must use the existing API.Call audit path.')}>
              <MaterialIcon>sync</MaterialIcon>
              <strong>Sync TSS Now</strong>
              <span>{currentTssStatus || 'Not synced'}</span>
            </button>
            <button className="pipeline-action" type="button" onClick={() => showGatedAction('SFD Lookup', 'SFD lookup follows the V2 BKD downstream flow after the consignment has a valid TSS declaration/consignment reference.')}>
              <MaterialIcon>travel_explore</MaterialIcon>
              <strong>SFD Lookup</strong>
              <span>{sfdValue}</span>
            </button>
            <button className="pipeline-action" type="button" onClick={() => showGatedAction('SDI', 'SDI / SupDec remains a downstream phase after SFD/SUP context is resolved; no live write is triggered from this preview action.')}>
              <MaterialIcon>bolt</MaterialIcon>
              <strong>SDI</strong>
              <span>{sdiValue}</span>
            </button>
            <button className="pipeline-action" type="button" onClick={() => showGatedAction('TSS Response', 'Official TSS responses are read from API.Call / submission logs and should be mirrored back through the existing sync path.')}>
              <MaterialIcon>data_object</MaterialIcon>
              <strong>TSS Response</strong>
              <span>API.Call audit</span>
            </button>
          </div>

          {actionState.status !== 'idle' && (
            <div className={`action-feedback consignment-detail-action-feedback ${actionState.status === 'error' ? 'is-error' : ''}`}>
              {actionState.status === 'loading' ? (
                <strong className="feedback-loading"><LoadingSpinner /><span>{actionState.label}...</span></strong>
              ) : (
                <>
                  <strong>{actionState.status === 'ready' ? `${actionState.label} prepared` : `${actionState.label} failed`}</strong>
                  <span>{actionState.status === 'ready' ? actionState.message : actionState.error}</span>
                </>
              )}
            </div>
          )}

          <section className="v2-detail-panel" aria-label="Consignment details">
            <div className="consignment-section-heading">
              <span>Consignment details</span>
              <strong>ENS, SFD and SDI context</strong>
            </div>
            <div className="v2-detail-grid">
              <div><span>Sales Order / Document No</span><strong>{draft.transportDocumentNumber || row.transportDocumentNumber || '-'}</strong></div>
              <div><span>ENS Ref</span><strong>{ensReference}</strong></div>
              <div><span>Goods Description</span><strong>{draft.goodsDescription || '-'}</strong></div>
              <div><span>SFD DEC</span><strong>{row.sfdReference || '-'}</strong></div>
              <div><span>SFD MRN / EIDR</span><strong>{row.sfdMrn || '-'}</strong></div>
              <div><span>SFD Status</span><strong>{row.sfdReference ? currentTssStatus : '-'}</strong></div>
              <div><span>SDI / SupDec</span><strong>{row.sdiReferences || (row.sfdReference ? 'Pending autosync' : '-')}</strong></div>
              <div><span>Consignor</span><strong>{consignorName}{consignorEori ? ` (${consignorEori})` : ''}</strong></div>
              <div><span>Consignee</span><strong>{draft.consigneeName || row.consigneeName || '-'}</strong></div>
              <div><span>Consignee EORI</span><strong>{consigneeEori}</strong></div>
              <div><span>Importer EORI</span><strong>{importerEori}{importerName ? ` ${importerName}` : ''}</strong></div>
              <div><span>Conveyance</span><strong>{conveyanceRef}</strong></div>
              <div><span>Arrival</span><strong>{arrivalLabel || '-'}</strong></div>
              <div><span>Source</span><strong>{sourceLabel}</strong></div>
              <div><span>Updated</span><strong>{updatedLabel || '-'}</strong></div>
            </div>
          </section>

          {detailState.status === 'loading' && (
            <div className="consignment-detail-loading"><LoadingSpinner /><span>Loading detail...</span></div>
          )}
          {detailState.status === 'error' && (
            <div className="action-feedback is-error consignment-detail-error">
              <strong>Detail API unavailable</strong>
              <span>{detailState.error}</span>
            </div>
          )}

          <ConsignmentValidationSummary summary={detailState.payload?.validationSummary} />

          <section className="consignment-edit-panel" aria-label="Editable consignment fields">
            <div className="consignment-section-heading">
              <span>Editable PRS fields</span>
              <strong>Changes stay local until a commit/promotion action is wired.</strong>
            </div>
            <div className="consignment-edit-grid">
              <label><span>Consignment</span><input value={draft.consignmentNumber} onChange={(event) => updateDraft('consignmentNumber', event.target.value)} /></label>
              <label><span>Declaration</span><input value={draft.declarationNumber} onChange={(event) => updateDraft('declarationNumber', event.target.value)} /></label>
              <label><span>Trader Ref</span><input value={draft.traderReference} onChange={(event) => updateDraft('traderReference', event.target.value)} /></label>
              <label><span>Transport Document</span><input value={draft.transportDocumentNumber} onChange={(event) => updateDraft('transportDocumentNumber', event.target.value)} /></label>
              <label className="wide"><span>Goods Description</span><input value={draft.goodsDescription} onChange={(event) => updateDraft('goodsDescription', event.target.value)} /></label>
              <label><span>Consignee</span><input value={draft.consigneeName} onChange={(event) => updateDraft('consigneeName', event.target.value)} /></label>
              <label><span>Destination</span><input value={draft.destinationCountry} onChange={(event) => updateDraft('destinationCountry', event.target.value)} /></label>
              <label><span>TSS Status</span><select value={currentTssStatus} onChange={(event) => updateDraft('tssStatus', event.target.value)}><option value="">Not synced</option>{TSS_STATUS_OPTIONS.map((item) => <option key={item} value={item}>{normalizeStatusDisplay(item)}</option>)}</select></label>
            </div>
          </section>

          <section className="consignment-goods-panel" aria-label="Goods items">
            <div className="consignment-goods-heading">
              <div>
                <span>PRS.Goods_Item</span>
                <h3>{goodsItems.length || row.goodsItems || 0} rows</h3>
              </div>
              <div className="goods-flow-chip"><span>Batch cap</span><strong>99 goods</strong></div>
            </div>
            <div className="control-table-wrap">
              <table className="control-table consignment-goods-table v2-goods-table">
                <thead>
                  <tr>
                    <th>Seq</th>
                    <th>Description</th>
                    <th>Commodity</th>
                    <th>SKU / TSS ID</th>
                    <th>Gross kg</th>
                    <th>Net kg</th>
                    <th>Pkgs</th>
                    <th>Local Status</th>
                    <th>TSS Status</th>
                    <th>Error</th>
                  </tr>
                </thead>
                <tbody>
                  {goodsItems.length ? goodsItems.map((goods, index) => (
                    <tr key={goods.GoodsItemRowID || index}>
                      <td>{goods.GoodsItemOrdinal || index + 1}</td>
                      <td className="description-cell"><span>{controlDisplay(goods.goods_description)}</span></td>
                      <td className="font-mono muted-cell">{controlDisplay(goods.commodity_code)}</td>
                      <td className="font-mono muted-cell">{controlDisplay(goods.goods_id || goods.label || goods.GoodsItemRowID)}</td>
                      <td>{controlDisplay(goods.gross_mass_kg)}</td>
                      <td>{controlDisplay(goods.net_mass_kg)}</td>
                      <td>{controlDisplay(goods.number_of_packages)} {controlDisplay(goods.type_of_packages)}</td>
                      <td><StatusBadge status={goods.Status || 'PENDING'} /></td>
                      <td><span className="muted-cell">Pending sync</span></td>
                      <td className="goods-error-cell">{goods.RejectReason || '-'}</td>
                    </tr>
                  )) : (
                    <tr><td colSpan="10" className="control-empty-cell">No goods detail loaded</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>

        <footer className="consignment-detail-footer">
          <button className="outline-action" type="button" onClick={onClose}>Cancel</button>
          <button className="primary-action blue" type="button" onClick={saveDraft}><MaterialIcon>save</MaterialIcon><span>Save Detail</span></button>
        </footer>
      </section>
    </div>
  );
}

function DeclarationDetailModal({ row, onClose, onOpenConsignments }) {
  const primaryRef = declarationPrimaryRef(row);
  const localStatus = localDeclarationStatus(row);
  const tssStatus = deriveTssStatus(row);
  const sourceLabel = row.source || 'PRS.ENS_Header';
  const canOpenConsignments = Boolean(onOpenConsignments);

  return (
    <div className="preview-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="consignment-detail-modal declaration-detail-modal" role="dialog" aria-modal="true" aria-label="ENS declaration detail">
        <header className="preview-modal-header consignment-detail-modal-header">
          <div>
            <span className="preview-eyebrow">PRS.ENS_Header</span>
            <h2>{primaryRef}</h2>
            <p>{row.movementKey || 'Movement key pending'} / {sourceLabel}</p>
          </div>
          <div className="consignment-modal-status">
            <span>Local / TSS</span>
            <StatusBadge status={localStatus} />
            <StatusBadge status={tssStatus} />
          </div>
          <button className="modal-close-button" type="button" onClick={onClose} aria-label="Close declaration detail">
            <MaterialIcon>close</MaterialIcon>
          </button>
        </header>

        <div className="consignment-detail-body">
          <div className="v2-consignment-status-strip" aria-label="Declaration references">
            <span className="v2-ref-chip local"><strong>Local</strong><StatusBadge status={localStatus} /></span>
            <span className="v2-ref-chip tss"><strong>TSS</strong><StatusBadge status={tssStatus} /></span>
            <span className="v2-ref-chip"><strong>Movement</strong><em>{row.movementType || '-'}</em></span>
            <span className="v2-ref-chip"><strong>Cons</strong><em>{row.consignments || 0}</em></span>
            <span className="v2-ref-chip"><strong>Goods</strong><em>{row.goodsItems || 0}</em></span>
          </div>

          <section className="v2-detail-panel" aria-label="ENS declaration details">
            <div className="consignment-section-heading">
              <span>ENS / Declarations</span>
              <strong>PRS canonical header with STG/API/TSS status context.</strong>
            </div>
            <div className="v2-detail-grid">
              <div><span>ENS Header Row</span><strong>{row.ensHeaderRowId || '-'}</strong></div>
              <div><span>Declaration Number</span><strong>{row.declarationNumber || '-'}</strong></div>
              <div><span>Movement Key</span><strong>{row.movementKey || '-'}</strong></div>
              <div><span>Client Code</span><strong>{row.clientCode || '-'}</strong></div>
              <div><span>Movement Type</span><strong>{row.movementType || '-'}</strong></div>
              <div><span>Arrival Port</span><strong>{row.arrivalPort || '-'}</strong></div>
              <div><span>Arrival</span><strong>{formatDateTime(row.arrivalDateTime)}</strong></div>
              <div><span>Carrier</span><strong>{row.carrierName || '-'}</strong></div>
              <div><span>Carrier EORI</span><strong>{row.carrierEori || '-'}</strong></div>
              <div><span>Consignments</span><strong>{row.consignments || 0}</strong></div>
              <div><span>Goods Items</span><strong>{row.goodsItems || 0}</strong></div>
              <div><span>Source Channel</span><strong>{row.sourceChannel || '-'}</strong></div>
              <div><span>Source File</span><strong>{row.sourceFile || '-'}</strong></div>
              <div><span>Execution</span><strong>{row.lastExecutionId || '-'}</strong></div>
              <div><span>Updated</span><strong>{formatDateTime(row.updatedAt || row.createdAt)}</strong></div>
              <div><span>Source</span><strong>{sourceLabel}</strong></div>
            </div>
          </section>
        </div>

        <footer className="consignment-detail-footer">
          <button className="outline-action" type="button" onClick={onClose}>Close</button>
          <button className="primary-action blue" type="button" disabled={!canOpenConsignments} onClick={() => onOpenConsignments?.(row)}>
            <MaterialIcon>list_alt</MaterialIcon><span>View Consignments</span>
          </button>
        </footer>
      </section>
    </div>
  );
}

function DeclarationsPage({ onBack, rows, clientCode, statusVocabulary = STATUS_VOCABULARY_FALLBACK, routeDeclarationId = '', onDetailRoute, onClearDetailRoute, onOpenConsignments, onRefresh }) {
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('ALL');
  const [dateMonth, setDateMonth] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [sort, setSort] = useState('arrival_desc');
  const [pageSize, setPageSize] = useState('20');
  const [page, setPage] = useState(1);
  const [selectMode, setSelectMode] = useState(false);
  const [selectedRows, setSelectedRows] = useState(() => new Set());
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const sourceRows = rows.length ? rows : DECLARATIONS;
  const [selectedId, setSelectedId] = useState(sourceRows[0]?.id || '');
  const [detailOpen, setDetailOpen] = useState(false);
  const bulkSelection = useBulkRowDragSelection({ selectMode, selectedRows, setSelectedRows });

  useEffect(() => {
    if (sourceRows.length && !sourceRows.some((row) => row.id === selectedId)) {
      setSelectedId(sourceRows[0].id);
    }
  }, [sourceRows, selectedId]);

  useEffect(() => {
    const routeToken = String(routeDeclarationId || '').trim();
    if (!routeToken) {
      setDetailOpen(false);
      return;
    }
    const routeRow = sourceRows.find((row) => [row.id, row.ensHeaderRowId, row.declarationNumber, row.movementKey, declarationPrimaryRef(row)]
      .some((value) => String(value || '').trim() === routeToken));
    if (routeRow) {
      setSelectedId(routeRow.id);
      setDetailOpen(true);
    }
  }, [routeDeclarationId, sourceRows]);

  useEffect(() => {
    setPage(1);
  }, [query, status, dateMonth, dateFrom, dateTo, pageSize, sort]);

  useEffect(() => {
    const validIds = new Set(sourceRows.map((row) => row.id));
    setSelectedRows((current) => {
      const next = new Set([...current].filter((id) => validIds.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [sourceRows]);

  const statusMetadata = useMemo(() => statusVocabularyMap(statusVocabulary), [statusVocabulary]);
  const statusCounts = useMemo(() => {
    const counts = { ALL: sourceRows.length };
    sourceRows.forEach((row) => {
      const key = localDeclarationStatus(row);
      counts[key] = (counts[key] || 0) + 1;
    });
    return counts;
  }, [sourceRows]);

  const statusTabs = useMemo(() => {
    const configured = normalizeStatusVocabulary(statusVocabulary).map((row) => row.resultStatus);
    const dynamic = sourceRows.map((row) => localDeclarationStatus(row)).filter(Boolean);
    return ['ALL', ...new Set([...configured, ...dynamic])]
      .filter((item) => item === 'ALL' || statusCounts[item] > 0);
  }, [sourceRows, statusCounts, statusVocabulary]);

  const filtered = useMemo(() => {
    const value = query.trim().toLowerCase();
    return sourceRows.filter((row) => {
      const tssStatus = deriveTssStatus(row);
      const localStatus = localDeclarationStatus(row);
      const matchesStatus = status === 'ALL' || localStatus === status;
      const haystack = [
        row.ensHeaderRowId,
        declarationPrimaryRef(row),
        row.declarationNumber,
        row.movementKey,
        row.movementType,
        row.arrivalPort,
        row.carrierName,
        row.carrierEori,
        row.sourceChannel,
        row.sourceFile,
        row.source,
        row.lastExecutionId,
        localStatus,
        tssStatus,
      ].join(' ').toLowerCase();
      return matchesStatus && rowMatchesDateFilters(row, dateMonth, dateFrom, dateTo) && (!value || haystack.includes(value));
    });
  }, [query, status, dateMonth, dateFrom, dateTo, sourceRows]);

  const sorted = useMemo(() => {
    const direction = sort === 'arrival_asc' ? 1 : -1;
    return [...filtered].sort((left, right) => {
      const diff = declarationTimestamp(left) - declarationTimestamp(right);
      if (diff !== 0) return diff * direction;
      return Number(right.ensHeaderRowId || 0) - Number(left.ensHeaderRowId || 0);
    });
  }, [filtered, sort]);

  const pageSizeNumber = pageSize === 'all' ? sorted.length || 1 : Number(pageSize);
  const totalPages = pageSize === 'all' ? 1 : Math.max(1, Math.ceil(sorted.length / pageSizeNumber));
  const safePage = Math.min(page, totalPages);
  const pageStart = pageSize === 'all' ? 0 : (safePage - 1) * pageSizeNumber;
  const pagedRows = pageSize === 'all' ? sorted : sorted.slice(pageStart, pageStart + pageSizeNumber);
  const selected = sorted.find((row) => row.id === selectedId) || pagedRows[0] || sourceRows[0] || DECLARATIONS[0];
  const filtersActive = Boolean(query || dateMonth || dateFrom || dateTo || status !== 'ALL');
  const allPageSelected = pagedRows.length > 0 && pagedRows.every((row) => selectedRows.has(row.id));
  const showingStart = sorted.length ? pageStart + 1 : 0;
  const showingEnd = pageSize === 'all' ? sorted.length : Math.min(pageStart + pageSizeNumber, sorted.length);

  async function refreshRows() {
    if (!onRefresh || isRefreshing) return;
    setIsRefreshing(true);
    setRefreshError('');
    try {
      await onRefresh();
    } catch (error) {
      setRefreshError(error.message);
    } finally {
      setIsRefreshing(false);
    }
  }

  function clearFilters() {
    setQuery('');
    setStatus('ALL');
    setDateMonth('');
    setDateFrom('');
    setDateTo('');
  }

  function toggleRowSelection(row, event) {
    event?.stopPropagation();
    setSelectedRows((current) => {
      const next = new Set(current);
      if (next.has(row.id)) next.delete(row.id);
      else next.add(row.id);
      return next;
    });
  }

  function togglePageSelection(event) {
    event.stopPropagation();
    setSelectedRows((current) => {
      const next = new Set(current);
      if (allPageSelected) pagedRows.forEach((row) => next.delete(row.id));
      else pagedRows.forEach((row) => next.add(row.id));
      return next;
    });
  }

  function toggleSelectMode() {
    setSelectMode((current) => {
      const next = !current;
      if (!next) setSelectedRows(new Set());
      return next;
    });
  }

  function openDetail(row = selected, { syncRoute = true } = {}) {
    setSelectedId(row.id);
    setDetailOpen(true);
    if (syncRoute) onDetailRoute?.(row.ensHeaderRowId || row.declarationNumber || row.movementKey || row.id);
  }

  function closeDetail() {
    setDetailOpen(false);
    onClearDetailRoute?.();
  }

  function handleRowClick(row, event) {
    if (selectMode) {
      if (bulkSelection.shouldSuppressClick()) {
        event?.preventDefault?.();
        event?.stopPropagation?.();
        return;
      }
      toggleRowSelection(row, event);
      return;
    }
    openDetail(row);
  }

  function handleRowKeyDown(row, event) {
    if (selectMode && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      toggleRowSelection(row, event);
      return;
    }
    if (!selectMode && event.key === 'Enter') openDetail(row);
  }

  return (
    <section className="consignments-page declarations-page" aria-label="ENS declarations">
      <div className="consignments-header consignment-list-header">
        <button className="back-button" type="button" onClick={onBack}>
          <MaterialIcon>arrow_back</MaterialIcon>
          <span>Back</span>
        </button>
        <div>
          <h1>ENS / Declarations</h1>
          <p>PRS ENS headers with movement, arrival, carrier, consignment counts and TSS status context.</p>
        </div>
        <div className="page-actions consignment-page-actions">
          <button className="outline-action compact-action" type="button" onClick={refreshRows} disabled={isRefreshing} aria-busy={isRefreshing}>
            {isRefreshing ? <LoadingSpinner className="button-spinner" /> : <MaterialIcon>refresh</MaterialIcon>}
            <span>{isRefreshing ? 'Refreshing' : 'Refresh'}</span>
          </button>
        </div>
      </div>

      <div className="consignment-status-tabs" role="tablist" aria-label="Declaration status filters">
        {statusTabs.map((item) => {
          const count = item === 'ALL' ? sourceRows.length : statusCounts[item] || 0;
          return (
            <button key={item} className={status === item ? 'active' : ''} type="button" role="tab" aria-selected={status === item} onClick={() => setStatus(item)}>
              <span title={statusMetadata.get(item)?.meaning || item}>{displayStatusLabel(item, statusMetadata)}</span>
              <strong>{count}</strong>
            </button>
          );
        })}
      </div>

      <div className={`consignments-list-card declarations-list-card ${selectMode ? 'is-bulk-selecting' : ''}`} data-bulk-selection="ens-headers" onPointerDown={bulkSelection.startBulkDrag} onPointerMove={bulkSelection.continueBulkDrag} onPointerUp={bulkSelection.stopBulkDrag} onPointerCancel={bulkSelection.stopBulkDrag}>
        <div className="consignment-filter-bar">
          <label className="search-box consignment-search-box">
            <MaterialIcon>search</MaterialIcon>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search ENS ref, carrier, port, movement, source..." />
          </label>
          <label className="filter-field month-field">
            <MaterialIcon>calendar_month</MaterialIcon>
            <input type="month" value={dateMonth} aria-label="Filter by arrival month" onChange={(event) => { setDateMonth(event.target.value); setDateFrom(''); setDateTo(''); }} />
          </label>
          <label className="filter-field">
            <MaterialIcon>calendar_today</MaterialIcon>
            <input type="date" value={dateFrom} aria-label="Arrival from" onChange={(event) => { setDateFrom(event.target.value); setDateMonth(''); }} />
          </label>
          <label className="filter-field">
            <MaterialIcon>event_available</MaterialIcon>
            <input type="date" value={dateTo} aria-label="Arrival to" onChange={(event) => { setDateTo(event.target.value); setDateMonth(''); }} />
          </label>
          <label className="compact-select rows-select">
            <span>Rows</span>
            <select value={pageSize} onChange={(event) => setPageSize(event.target.value)}>
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="50">50</option>
              <option value="all">All</option>
            </select>
          </label>
          {filtersActive && <button className="clear-filters-button" type="button" onClick={clearFilters}>Clear</button>}
        </div>

        <div className="consignment-card-header">
          <div className="consignment-list-summary">
            <strong>ENS headers are the movement-level view.</strong>
            <span>{showingStart}-{showingEnd} of {sorted.length} declarations shown from {sourceRows.length} PRS rows for {clientCode}. Consignments stay in their own view.</span>
          </div>
          <div className="consignment-toolbar-actions">
            <select value={sort} onChange={(event) => setSort(event.target.value)} aria-label="Sort declarations">
              <option value="arrival_desc">Newest arrival first</option>
              <option value="arrival_asc">Oldest arrival first</option>
            </select>
            <button className="outline-action compact-action" type="button" disabled title="Excel export endpoint is not enabled in V3 yet.">
              <MaterialIcon>download</MaterialIcon><span>Excel</span>
            </button>
            <button className="outline-action compact-action danger-action" type="button" disabled title="Local delete endpoint is not enabled in V3 yet. TSS records are not cancelled from this view.">
              <MaterialIcon>delete</MaterialIcon><span>Delete</span>
            </button>
            <BulkModeToggle active={selectMode} group="ens-headers" onToggle={toggleSelectMode} />

          </div>
        </div>

        {refreshError && (
          <div className="action-feedback is-error consignment-refresh-error" role="alert">
            <strong>Refresh failed</strong>
            <span>{refreshError}</span>
          </div>
        )}

        {selectMode && (
          <div className="selection-strip">
            <span>{selectedRows.size} selected</span>
            <button type="button" onClick={() => setSelectedRows(new Set())}>Clear selection</button>
            <span>TSS records are not cancelled from this view.</span>
          </div>
        )}

        <div className="table-wrap consignment-table-wrap declaration-table-wrap">
          <table className="consignments-table v2-like-consignments-table declarations-table">
            <thead>
              <tr>
                <th className="select-column"><input type="checkbox" checked={allPageSelected} onChange={togglePageSelection} disabled={!selectMode} data-bulk-select-all="" data-bulk-group="ens-headers" aria-label="Select all visible declarations" /></th>
                <th>ID</th>
                <th>ENS Ref</th>
                <th>Local Status</th>
                <th>TSS Status</th>
                <th>Movement</th>
                <th>Port</th>
                <th>Carrier</th>
                <th>Cons / Goods</th>
                <th>Source</th>
                <th>Arrival</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {pagedRows.map((row) => {
                const tssStatus = deriveTssStatus(row);
                const localStatus = localDeclarationStatus(row);
                const primaryRef = declarationPrimaryRef(row);
                const isSelected = row.id === selected?.id;
                const rowChecked = selectedRows.has(row.id);
                return (
                  <tr
                    key={row.id}
                    data-bulk-row-id={row.id}
                    className={`${isSelected ? 'selected' : ''} ${rowChecked ? 'selection-checked bulk-selected-row' : ''} ${selectMode ? 'select-mode-row' : ''} ${statusNeedsAttention(tssStatus) || statusNeedsAttention(localStatus) ? 'row-needs-attention' : ''}`}
                    onClick={(event) => handleRowClick(row, event)}
                    role={selectMode ? 'checkbox' : 'link'}
                    aria-checked={selectMode ? rowChecked : undefined}
                    tabIndex={0}
                    onKeyDown={(event) => handleRowKeyDown(row, event)}
                  >
                    <td className="select-column"><input type="checkbox" checked={rowChecked} onChange={(event) => toggleRowSelection(row, event)} disabled={!selectMode} data-bulk-item="" data-bulk-group="ens-headers" aria-label={`Select declaration ${primaryRef}`} /></td>
                    <td className="font-mono">{row.ensHeaderRowId || '-'}</td>
                    <td className="ref-cell"><button type="button" onClick={(event) => { event.stopPropagation(); openDetail(row); }}>{primaryRef}</button><span>{row.movementKey || 'Movement pending'}</span></td>
                    <td><StatusBadge status={localStatus} /></td>
                    <td><StatusBadge status={tssStatus} /></td>
                    <td className="font-mono muted-cell">{row.movementType || '-'}</td>
                    <td className="font-mono muted-cell">{row.arrivalPort || '-'}</td>
                    <td className="description-cell"><span>{row.carrierName || row.carrierEori || '-'}</span></td>
                    <td className="numeric-cell"><strong>{row.consignments || 0}</strong> / {row.goodsItems || 0}</td>
                    <td className="font-mono muted-cell">{row.sourceChannel || row.source || '-'}</td>
                    <td className="muted-cell">{formatDateTime(row.arrivalDateTime)}</td>
                    <td className="muted-cell">{formatDateTime(row.updatedAt || row.createdAt)}</td>
                  </tr>
                );
              })}
              {!pagedRows.length && (
                <tr>
                  <td colSpan="12" className="control-empty-cell">No declarations found{query ? ` matching "${query}"` : ''}.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="consignment-pagination">
          <span>Page {safePage} of {totalPages}</span>
          <div>
            <button type="button" disabled={safePage <= 1 || pageSize === 'all'} onClick={() => setPage((value) => Math.max(1, value - 1))}>Previous</button>
            <button type="button" disabled={safePage >= totalPages || pageSize === 'all'} onClick={() => setPage((value) => Math.min(totalPages, value + 1))}>Next</button>
          </div>
        </div>
      </div>

      {detailOpen && selected && (
        <DeclarationDetailModal row={selected} onClose={closeDetail} onOpenConsignments={onOpenConsignments} />
      )}
    </section>
  );
}
function ViewConsignmentsPage({ onBack, rows, clientCode, connection, statusVocabulary = STATUS_VOCABULARY_FALLBACK, routeConsignmentId = '', onDetailRoute, onClearDetailRoute, onQueueForTss, onUpdateConsignment, onRefresh }) {
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('ALL');
  const [dateMonth, setDateMonth] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [sort, setSort] = useState('arrival_desc');
  const [pageSize, setPageSize] = useState('20');
  const [page, setPage] = useState(1);
  const [selectMode, setSelectMode] = useState(false);
  const [selectedRows, setSelectedRows] = useState(() => new Set());
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const sourceRows = rows.length ? rows : CONSIGNMENTS;
  const [selectedId, setSelectedId] = useState(sourceRows[0]?.id || '');
  const [detailOpen, setDetailOpen] = useState(false);
  const bulkSelection = useBulkRowDragSelection({ selectMode, selectedRows, setSelectedRows });

  useEffect(() => {
    if (sourceRows.length && !sourceRows.some((row) => row.id === selectedId)) {
      setSelectedId(sourceRows[0].id);
    }
  }, [sourceRows, selectedId]);

  useEffect(() => {
    const routeToken = String(routeConsignmentId || '').trim();
    if (!routeToken) {
      setDetailOpen(false);
      return;
    }
    const routeRow = sourceRows.find((row) => [row.id, row.consignmentRowId, consignmentPrimaryRef(row)]
      .some((value) => String(value || '').trim() === routeToken));
    if (routeRow) {
      setSelectedId(routeRow.id);
      setDetailOpen(true);
    }
  }, [routeConsignmentId, sourceRows]);

  useEffect(() => {
    setPage(1);
  }, [query, status, dateMonth, dateFrom, dateTo, pageSize, sort]);

  useEffect(() => {
    const validIds = new Set(sourceRows.map((row) => row.id));
    setSelectedRows((current) => {
      const next = new Set([...current].filter((id) => validIds.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [sourceRows]);

  const statusMetadata = useMemo(() => statusVocabularyMap(statusVocabulary), [statusVocabulary]);

  const statusCounts = useMemo(() => {
    const counts = { ALL: sourceRows.length };
    sourceRows.forEach((row) => {
      const key = localConsignmentStatus(row);
      counts[key] = (counts[key] || 0) + 1;
    });
    return counts;
  }, [sourceRows]);

  const statusTabs = useMemo(() => {
    const configured = normalizeStatusVocabulary(statusVocabulary).map((row) => row.resultStatus);
    const dynamic = sourceRows.map((row) => localConsignmentStatus(row)).filter(Boolean);
    return ['ALL', ...new Set([...configured, ...dynamic])]
      .filter((item) => item === 'ALL' || statusCounts[item] > 0);
  }, [sourceRows, statusCounts, statusVocabulary]);

  const filtered = useMemo(() => {
    const value = query.trim().toLowerCase();
    return sourceRows.filter((row) => {
      const tssStatus = deriveTssStatus(row);
      const localStatus = localConsignmentStatus(row);
      const matchesStatus = status === 'ALL' || localStatus === status;
      const haystack = [
        row.consignmentRowId,
        consignmentPrimaryRef(row),
        row.traderReference,
        row.transportDocumentNumber,
        row.goodsDescription,
        row.consigneeName,
        row.destinationCountry,
        row.movementKey,
        row.declarationNumber,
        row.sfdReference,
        row.sfdMrn,
        row.sdiReferences,
        row.source,
        row.status,
        localStatus,
        tssStatus,
      ].join(' ').toLowerCase();
      return matchesStatus && rowMatchesDateFilters(row, dateMonth, dateFrom, dateTo) && (!value || haystack.includes(value));
    });
  }, [query, status, dateMonth, dateFrom, dateTo, sourceRows]);

  const sorted = useMemo(() => {
    const direction = sort === 'arrival_asc' ? 1 : -1;
    return [...filtered].sort((left, right) => {
      const diff = consignmentTimestamp(left) - consignmentTimestamp(right);
      if (diff !== 0) return diff * direction;
      return Number(right.consignmentRowId || 0) - Number(left.consignmentRowId || 0);
    });
  }, [filtered, sort]);

  const pageSizeNumber = pageSize === 'all' ? sorted.length || 1 : Number(pageSize);
  const totalPages = pageSize === 'all' ? 1 : Math.max(1, Math.ceil(sorted.length / pageSizeNumber));
  const safePage = Math.min(page, totalPages);
  const pageStart = pageSize === 'all' ? 0 : (safePage - 1) * pageSizeNumber;
  const pagedRows = pageSize === 'all' ? sorted : sorted.slice(pageStart, pageStart + pageSizeNumber);
  const selected = sorted.find((row) => row.id === selectedId) || pagedRows[0] || sourceRows[0] || CONSIGNMENTS[0];
  const filtersActive = Boolean(query || dateMonth || dateFrom || dateTo || status !== 'ALL');
  const allPageSelected = pagedRows.length > 0 && pagedRows.every((row) => selectedRows.has(row.id));
  const showingStart = sorted.length ? pageStart + 1 : 0;
  const showingEnd = pageSize === 'all' ? sorted.length : Math.min(pageStart + pageSizeNumber, sorted.length);

  function applyLocalUpdate(updatedRow) {
    if (status !== 'ALL' && localConsignmentStatus(updatedRow) !== status) setStatus('ALL');
    setSelectedId(updatedRow.id);
    onUpdateConsignment?.(updatedRow);
  }

  async function refreshRows() {
    if (!onRefresh || isRefreshing) return;
    setIsRefreshing(true);
    setRefreshError('');
    try {
      await onRefresh();
    } catch (error) {
      setRefreshError(error.message);
    } finally {
      setIsRefreshing(false);
    }
  }


  function clearFilters() {
    setQuery('');
    setStatus('ALL');
    setDateMonth('');
    setDateFrom('');
    setDateTo('');
  }

  function toggleRowSelection(row, event) {
    event?.stopPropagation();
    setSelectedRows((current) => {
      const next = new Set(current);
      if (next.has(row.id)) next.delete(row.id);
      else next.add(row.id);
      return next;
    });
  }

  function togglePageSelection(event) {
    event.stopPropagation();
    setSelectedRows((current) => {
      const next = new Set(current);
      if (allPageSelected) pagedRows.forEach((row) => next.delete(row.id));
      else pagedRows.forEach((row) => next.add(row.id));
      return next;
    });
  }

  function toggleSelectMode() {
    setSelectMode((current) => {
      const next = !current;
      if (!next) setSelectedRows(new Set());
      return next;
    });
  }

  function handleRowClick(row, event) {
    if (selectMode) {
      if (bulkSelection.shouldSuppressClick()) {
        event?.preventDefault?.();
        event?.stopPropagation?.();
        return;
      }
      toggleRowSelection(row, event);
      return;
    }
    openDetail(row);
  }

  function handleRowKeyDown(row, event) {
    if (selectMode && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      toggleRowSelection(row, event);
      return;
    }
    if (!selectMode && event.key === 'Enter') openDetail(row);
  }

  function openDetail(row = selected, { syncRoute = true } = {}) {
    setSelectedId(row.id);
    setDetailOpen(true);
    if (syncRoute) onDetailRoute?.(row.consignmentRowId || row.id);
  }

  function closeDetail() {
    setDetailOpen(false);
    onClearDetailRoute?.();
  }

  return (
    <section className="consignments-page" aria-label="View consignments">
      <div className="consignments-header consignment-list-header">
        <button className="back-button" type="button" onClick={onBack}>
          <MaterialIcon>arrow_back</MaterialIcon>
          <span>Back</span>
        </button>
        <div>
          <h1>View Consignments</h1>
          <p>PRS consignments with linked ENS context, SFD/SDI references, and TSS status mirror.</p>
        </div>
        <div className="page-actions consignment-page-actions">
          <button className="outline-action compact-action" type="button" onClick={refreshRows} disabled={isRefreshing} aria-busy={isRefreshing}>
            {isRefreshing ? <LoadingSpinner className="button-spinner" /> : <MaterialIcon>refresh</MaterialIcon>}
            <span>{isRefreshing ? 'Refreshing' : 'Refresh'}</span>
          </button>
        </div>
      </div>

      <div className="consignment-status-tabs" role="tablist" aria-label="Consignment status filters">
        {statusTabs.map((item) => {
          const count = item === 'ALL' ? sourceRows.length : statusCounts[item] || 0;
          return (
            <button key={item} className={status === item ? 'active' : ''} type="button" role="tab" aria-selected={status === item} onClick={() => setStatus(item)}>
              <span title={statusMetadata.get(item)?.meaning || item}>{displayStatusLabel(item, statusMetadata)}</span>
              <strong>{count}</strong>
            </button>
          );
        })}
      </div>

      <div className={`consignments-list-card ${selectMode ? 'is-bulk-selecting' : ''}`} data-bulk-selection="consignments" onPointerDown={bulkSelection.startBulkDrag} onPointerMove={bulkSelection.continueBulkDrag} onPointerUp={bulkSelection.stopBulkDrag} onPointerCancel={bulkSelection.stopBulkDrag}>
        <div className="consignment-filter-bar">
          <label className="search-box consignment-search-box">
            <MaterialIcon>search</MaterialIcon>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search DEC, goods description, conveyance, ENS, SDI..." />
          </label>
          <label className="filter-field month-field">
            <MaterialIcon>calendar_month</MaterialIcon>
            <input type="month" value={dateMonth} aria-label="Filter by arrival month" onChange={(event) => { setDateMonth(event.target.value); setDateFrom(''); setDateTo(''); }} />
          </label>
          <label className="filter-field">
            <MaterialIcon>calendar_today</MaterialIcon>
            <input type="date" value={dateFrom} aria-label="Arrival from" onChange={(event) => { setDateFrom(event.target.value); setDateMonth(''); }} />
          </label>
          <label className="filter-field">
            <MaterialIcon>event_available</MaterialIcon>
            <input type="date" value={dateTo} aria-label="Arrival to" onChange={(event) => { setDateTo(event.target.value); setDateMonth(''); }} />
          </label>
          <label className="compact-select rows-select">
            <span>Rows</span>
            <select value={pageSize} onChange={(event) => setPageSize(event.target.value)}>
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="50">50</option>
              <option value="all">All</option>
            </select>
          </label>
          {filtersActive && <button className="clear-filters-button" type="button" onClick={clearFilters}>Clear</button>}
        </div>

        <div className="consignment-card-header">
          <div className="consignment-list-summary">
            <strong>Use Select mode for local cleanup or Excel export.</strong>
            <span>{showingStart}-{showingEnd} of {sorted.length} consignments shown from {sourceRows.length} PRS rows. TSS records are not cancelled.</span>
          </div>
          <div className="consignment-toolbar-actions">
            <select value={sort} onChange={(event) => setSort(event.target.value)} aria-label="Sort consignments">
              <option value="arrival_desc">Newest arrival first</option>
              <option value="arrival_asc">Oldest arrival first</option>
            </select>
            <button className="outline-action compact-action" type="button" disabled title="Excel export endpoint is not enabled in V3 yet.">
              <MaterialIcon>download</MaterialIcon><span>Excel</span>
            </button>
            <button className="outline-action compact-action danger-action" type="button" disabled title="Local delete endpoint is not enabled in V3 yet. TSS records are not cancelled from this view.">
              <MaterialIcon>delete</MaterialIcon><span>Delete</span>
            </button>
            <BulkModeToggle active={selectMode} group="consignments" onToggle={toggleSelectMode} />

          </div>
        </div>

        {refreshError && (
          <div className="action-feedback is-error consignment-refresh-error" role="alert">
            <strong>Refresh failed</strong>
            <span>{refreshError}</span>
          </div>
        )}

        {selectMode && (
          <div className="selection-strip">
            <span>{selectedRows.size} selected</span>
            <button type="button" onClick={() => setSelectedRows(new Set())}>Clear selection</button>
            <span>TSS records are not cancelled from this view.</span>
          </div>
        )}

        <div className="table-wrap consignment-table-wrap">
          <table className="consignments-table v2-like-consignments-table">
            <thead>
              <tr>
                <th className="select-column"><input type="checkbox" checked={allPageSelected} onChange={togglePageSelection} disabled={!selectMode} data-bulk-select-all="" data-bulk-group="consignments" aria-label="Select all visible consignments" /></th>
                <th>ID</th>
                <th>DEC Ref</th>
                <th>Local Status</th>
                <th>TSS Status</th>
                <th>SFD</th>
                <th>SDI</th>
                <th>Goods</th>
                <th>Goods Description</th>
                <th>Doc / Ref</th>
                <th>ENS Ref</th>
                <th>Arrival</th>
              </tr>
            </thead>
            <tbody>
              {pagedRows.map((row) => {
                const tssStatus = deriveTssStatus(row);
                const localStatus = localConsignmentStatus(row);
                const primaryRef = consignmentPrimaryRef(row);
                const ensRef = consignmentEnsRef(row);
                const isSelected = row.id === selected?.id;
                const rowChecked = selectedRows.has(row.id);
                return (
                  <tr
                    key={row.id}
                    data-bulk-row-id={row.id}
                    className={`${isSelected ? 'selected' : ''} ${rowChecked ? 'selection-checked bulk-selected-row' : ''} ${selectMode ? 'select-mode-row' : ''} ${statusNeedsAttention(tssStatus) || statusNeedsAttention(localStatus) ? 'row-needs-attention' : ''}`}
                    onClick={(event) => handleRowClick(row, event)}
                    role={selectMode ? 'checkbox' : 'link'}
                    aria-checked={selectMode ? rowChecked : undefined}
                    tabIndex={0}
                    onKeyDown={(event) => handleRowKeyDown(row, event)}
                  >
                    <td className="select-column"><input type="checkbox" checked={rowChecked} onChange={(event) => toggleRowSelection(row, event)} disabled={!selectMode} data-bulk-item="" data-bulk-group="consignments" aria-label={`Select consignment ${primaryRef}`} /></td>
                    <td className="font-mono">{row.consignmentRowId || '-'}</td>
                    <td className="ref-cell"><button type="button" onClick={(event) => { event.stopPropagation(); openDetail(row); }}>{primaryRef}</button><span>{row.transportDocumentNumber || 'Draft'}</span></td>
                    <td><StatusBadge status={localStatus} /></td>
                    <td><StatusBadge status={tssStatus} /></td>
                    <td className="font-mono muted-cell">{row.sfdReference || '-'}</td>
                    <td className="font-mono muted-cell">{row.sdiReferences || '-'}</td>
                    <td className="numeric-cell">{row.goodsItems || 0}</td>
                    <td className="description-cell"><span>{row.goodsDescription || '-'}</span></td>
                    <td className="font-mono muted-cell">{row.traderReference || row.transportDocumentNumber || '-'}</td>
                    <td className="font-mono ens-ref-cell">{ensRef || '-'}</td>
                    <td className="muted-cell">{formatDateTime(consignmentDateValue(row))}</td>
                  </tr>
                );
              })}
              {!pagedRows.length && (
                <tr>
                  <td colSpan="12" className="control-empty-cell">No consignments found{query ? ` matching "${query}"` : ''}.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="consignment-pagination">
          <span>Page {safePage} of {totalPages}</span>
          <div>
            <button type="button" disabled={safePage <= 1 || pageSize === 'all'} onClick={() => setPage((value) => Math.max(1, value - 1))}>Previous</button>
            <button type="button" disabled={safePage >= totalPages || pageSize === 'all'} onClick={() => setPage((value) => Math.min(totalPages, value + 1))}>Next</button>
          </div>
        </div>

      </div>

      {detailOpen && selected && (
        <ConsignmentDetailModal row={selected} onClose={closeDetail} onSave={applyLocalUpdate} onQueueForTss={onQueueForTss} />
      )}
    </section>
  );
}
export default function App() {
  const initialRoute = useMemo(() => portalRouteFromLocation(), []);
  const storedPortalSession = useMemo(() => readStoredPortalSession(), []);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState(() => Boolean(storedPortalSession));
  const [view, setView] = useState(() => initialRoute.view || storedPortalSession?.view || 'login');
  const [isDarkTheme, setIsDarkTheme] = useState(false);
  const [session, setSession] = useState(() => storedPortalSession?.session || sessionFallback(DEFAULT_SESSION.tenantCode));
  const [activeClientCode, setActiveClientCode] = useState(() => storedPortalSession?.activeClientCode || DEFAULT_OPERATIONAL_CLIENT_CODE);
  const [connection, setConnection] = useState(null);
  const [consignmentRows, setConsignmentRows] = useState(CONSIGNMENTS);
  const [declarationRows, setDeclarationRows] = useState(DECLARATIONS);
  const [statusVocabulary, setStatusVocabulary] = useState(() => normalizeStatusVocabulary());
  const [settingsPayload, setSettingsPayload] = useState(null);
  const [validationDiagnostics, setValidationDiagnostics] = useState(null);
  const [validationDiagnosticsStatus, setValidationDiagnosticsStatus] = useState('idle');
  const [validationDiagnosticsError, setValidationDiagnosticsError] = useState('');
  const [settingsSection, setSettingsSection] = useState(() => initialRoute.settingsSection || storedPortalSession?.settingsSection || SETTINGS_NAV_SECTIONS[0].id);
  const [routeConsignmentId, setRouteConsignmentId] = useState(() => initialRoute.consignmentId || '');
  const [routeDeclarationId, setRouteDeclarationId] = useState(() => initialRoute.declarationId || '');
  const [apiStatus, setApiStatus] = useState('idle');
  const [apiError, setApiError] = useState('');
  const [environmentMode, setEnvironmentMode] = useState(() => normalizeEnvironmentMode(storedPortalSession?.environmentMode || import.meta.env?.VITE_PORTAL_MODE || 'DEMO'));
  useEffect(() => {
    const settingsMode = environmentModeFromSettings(settingsPayload);
    if (settingsMode && !isSynoviaSession(session)) {
      setEnvironmentMode(settingsMode);
    }
  }, [settingsPayload, session]);

  useEffect(() => {
    updateBrowserRoute(view, { settingsSection, consignmentId: routeConsignmentId, declarationId: routeDeclarationId }, { replace: true });
  }, []);

  useEffect(() => {
    function handlePopState() {
      const nextRoute = portalRouteFromLocation();
      setView(nextRoute.view || (isAuthenticated ? 'dashboard' : 'login'));
      setSettingsSection(nextRoute.settingsSection || SETTINGS_NAV_SECTIONS[0].id);
      setRouteConsignmentId(nextRoute.consignmentId || '');
      setRouteDeclarationId(nextRoute.declarationId || '');
      setDrawerOpen(false);
    }
    window.addEventListener('popstate', handlePopState);
    window.addEventListener('hashchange', handlePopState);
    return () => {
      window.removeEventListener('popstate', handlePopState);
      window.removeEventListener('hashchange', handlePopState);
    };
  }, [isAuthenticated]);

  useEffect(() => {
    if (!isAuthenticated) return;
    writeStoredPortalSession({
      session,
      activeClientCode,
      view,
      settingsSection,
      environmentMode,
    });
  }, [isAuthenticated, session, activeClientCode, view, settingsSection, environmentMode]);

  useEffect(() => {
    if (!isAuthenticated) return undefined;
    let cancelled = false;
    const clientCode = activeClientCode || DEFAULT_OPERATIONAL_CLIENT_CODE;

    async function loadPortalData() {
      setApiStatus('loading');
      setApiError('');
      setValidationDiagnosticsStatus('loading');
      setValidationDiagnosticsError('');
      try {
        const [sessionPayload, dashboardPayload, declarationPayload, consignmentPayload, connectionPayload, settingsPayload, validationDiagnosticsPayload] = await Promise.all([
          getSession(clientCode),
          getDashboard(clientCode),
          getDeclarations({ clientCode }),
          getConsignments({ clientCode }),
          getTssConnections(clientCode),
          getAdminSettings(clientCode),
          getValidationDiagnostics(clientCode),
        ]);
        if (cancelled) return;
        const activeConnection = (connectionPayload.connections || [])[0] || null;
        const fallback = sessionFallback(clientCode);
        if (!isSynoviaSession(session)) {
          setSession({
            tenantCode: activeConnection?.portalClientCode || fallback.tenantCode,
            tenantName: activeConnection?.clientName || sessionPayload.tenantName || fallback.tenantName,
            username: sessionPayload.username || DEFAULT_SESSION.username,
            role: sessionPayload.role || DEFAULT_SESSION.role,
            mode: 'CLIENT_SESSION',
          });
        }
        const nextConsignmentRows = (consignmentPayload.consignments || []).map(normalizeConsignment);
        const nextDeclarationRows = (declarationPayload.declarations || []).map(normalizeDeclaration);
        setConnection(activeConnection);
        setConsignmentRows(nextConsignmentRows);
        setDeclarationRows(nextDeclarationRows.length ? nextDeclarationRows : buildDeclarationsFromConsignments(nextConsignmentRows));
        setStatusVocabulary(normalizeStatusVocabulary(declarationPayload.statusVocabulary || consignmentPayload.statusVocabulary));
        setSettingsPayload(settingsPayload);
        setValidationDiagnostics(validationDiagnosticsPayload);
        setValidationDiagnosticsStatus('ready');
        setValidationDiagnosticsError('');
        setSettingsSection((currentSection) => {
          const mergedSections = mergeSettingsSections(settingsPayload.sections || []);
          const defaultSection = mergedSections[0]?.id || SETTINGS_NAV_SECTIONS[0].id;
          return mergedSections.some((section) => section.id === currentSection) ? currentSection : defaultSection;
        });
        setApiStatus('online');
        setApiError(dashboardPayload?.counts ? '' : 'Dashboard counts unavailable');
      } catch (error) {
        if (cancelled) return;
        if (!isSynoviaSession(session)) {
          setSession(sessionFallback(clientCode));
        }
        setConnection(null);
        setConsignmentRows(CONSIGNMENTS);
        setDeclarationRows(DECLARATIONS);
        setStatusVocabulary(normalizeStatusVocabulary());
        setSettingsPayload(null);
        setValidationDiagnostics(null);
        setValidationDiagnosticsStatus('error');
        setValidationDiagnosticsError(error.message);
        setApiStatus('offline');
        setApiError(error.message);
      }
    }

    loadPortalData();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, activeClientCode, session.tenantCode]);

  function navigate(nextView, options = {}) {
    const nextSettingsSection = options.settingsSection || settingsSection;
    const nextConsignmentId = nextView === 'consignments' ? (options.consignmentId || '') : '';
    const nextDeclarationId = nextView === 'declarations' ? (options.declarationId || '') : '';
    if (nextView === 'settings') setSettingsSection(nextSettingsSection);
    setRouteConsignmentId(nextConsignmentId);
    setRouteDeclarationId(nextDeclarationId);
    setView(nextView);
    setDrawerOpen(false);
    updateBrowserRoute(nextView, { settingsSection: nextSettingsSection, consignmentId: nextConsignmentId, declarationId: nextDeclarationId }, { replace: Boolean(options.replace) });
  }

  function navigateSettings(sectionId = SETTINGS_NAV_SECTIONS[0].id) {
    navigate('settings', { settingsSection: sectionId });
  }

  async function handleLogin(credentials) {
    setApiStatus('loading');
    setApiError('');
    let payload;
    try {
      payload = await loginPortal(credentials);
    } catch (error) {
      setApiStatus('offline');
      setApiError(error.message);
      throw error;
    }
    const activeSession = payload.session || sessionFallback(DEFAULT_SESSION.tenantCode);
    const nextClientCode = payload.defaultClientCode || payload.connection?.portalClientCode || (activeSession.tenantCode === DEFAULT_SESSION.tenantCode ? DEFAULT_OPERATIONAL_CLIENT_CODE : activeSession.tenantCode) || DEFAULT_OPERATIONAL_CLIENT_CODE;
    const nextSession = {
      tenantCode: activeSession.tenantCode || DEFAULT_SESSION.tenantCode,
      tenantName: activeSession.tenantName || DEFAULT_SESSION.tenantName,
      username: activeSession.username || credentials?.username?.trim() || DEFAULT_SESSION.username,
      role: activeSession.role || DEFAULT_SESSION.role,
      mode: activeSession.mode || (activeSession.tenantCode === DEFAULT_SESSION.tenantCode ? 'DEMO_ADMIN' : 'CLIENT_SESSION'),
    };
    const nextEnvironmentMode = (payload.demoMode || isSynoviaSession(nextSession)) ? 'DEMO' : environmentMode;
    setSession(nextSession);
    setActiveClientCode(nextClientCode);
    setConnection(payload.connection || null);
    setIsAuthenticated(true);
    setApiStatus('online');
    setEnvironmentMode(nextEnvironmentMode);
    const postLoginRoute = portalRouteFromLocation();
    const nextView = postLoginRoute.view && postLoginRoute.view !== 'login' ? postLoginRoute.view : 'dashboard';
    const nextSettingsSection = postLoginRoute.settingsSection || settingsSection;
    const nextConsignmentId = postLoginRoute.consignmentId || '';
    const nextDeclarationId = postLoginRoute.declarationId || '';
    setSettingsSection(nextSettingsSection);
    writeStoredPortalSession({
      session: nextSession,
      activeClientCode: nextClientCode,
      view: nextView,
      settingsSection: nextSettingsSection,
      environmentMode: nextEnvironmentMode,
    });
    navigate(nextView, { settingsSection: nextSettingsSection, consignmentId: nextConsignmentId, declarationId: nextDeclarationId, replace: true });
  }
  function handleLogout() {
    clearStoredPortalSession();
    setIsAuthenticated(false);
    setSession(sessionFallback(DEFAULT_SESSION.tenantCode));
    setActiveClientCode(DEFAULT_OPERATIONAL_CLIENT_CODE);
    setConnection(null);
    setConsignmentRows(CONSIGNMENTS);
    setDeclarationRows(DECLARATIONS);
    setStatusVocabulary(normalizeStatusVocabulary());
    setSettingsPayload(null);
    setValidationDiagnostics(null);
    setValidationDiagnosticsStatus('idle');
    setValidationDiagnosticsError('');
    setSettingsSection(SETTINGS_NAV_SECTIONS[0].id);
    setApiStatus('idle');
    setApiError('');
    setEnvironmentMode('DEMO');
    navigate('login');
  }

  function handlePreviewUpload(files, options = {}) {
    return previewConsignmentUpload({ clientCode: activeClientCode, files, ...options });
  }

  async function refreshValidationDiagnostics(clientCode = activeClientCode) {
    const code = clientCode || activeClientCode || DEFAULT_OPERATIONAL_CLIENT_CODE;
    setValidationDiagnosticsStatus('loading');
    setValidationDiagnosticsError('');
    try {
      const diagnostics = await getValidationDiagnostics(code);
      setValidationDiagnostics(diagnostics);
      setValidationDiagnosticsStatus('ready');
      return diagnostics;
    } catch (error) {
      setValidationDiagnosticsStatus('error');
      setValidationDiagnosticsError(error.message || 'Validation diagnostics unavailable.');
      return null;
    }
  }

  async function handleSaveSettings(payload) {
    const updates = Array.isArray(payload?.updates) ? payload.updates : [];
    const environmentUpdate = updates.find(isTssEnvironmentUpdate);
    const selectedEnvironmentMode = environmentUpdate ? normalizeEnvironmentMode(environmentUpdate.value) : '';

    if (selectedEnvironmentMode === 'DEMO') {
      const dbUpdates = updates.filter((update) => !isTssEnvironmentUpdate(update));
      const nextSettings = dbUpdates.length ? await saveAdminSettings({ ...payload, updates: dbUpdates }) : settingsPayload;
      setEnvironmentMode('DEMO');
      setConnection((current) => current ? { ...current, preferredEnvCode: 'DEMO', credential: null } : current);
      if (nextSettings) setSettingsPayload(nextSettings);
      try { await refreshValidationDiagnostics(payload?.clientCode || activeClientCode); } catch { /* Keep saved settings even if diagnostics refresh is unavailable. */ }
      return nextSettings;
    }

    const nextSettings = await saveAdminSettings(payload);
    const nextMode = selectedEnvironmentMode || environmentModeFromSettings(nextSettings);
    if (nextMode) setEnvironmentMode(nextMode);
    setSettingsPayload(nextSettings);
    try { await refreshValidationDiagnostics(payload?.clientCode || activeClientCode); } catch { /* Keep saved settings even if diagnostics refresh is unavailable. */ }
    return nextSettings;
  }
  async function handleTestTssApi({ clientCode, envCode }) {
    const testClientCode = clientCode || activeClientCode;
    const result = await testTssConnection({ clientCode: testClientCode, envCode });
    const [connectionPayload, nextSettings] = await Promise.all([
      getTssConnections(testClientCode, envCode),
      getAdminSettings(testClientCode),
    ]);
    setConnection((connectionPayload.connections || [])[0] || null);
    setSettingsPayload(nextSettings);
    try { await refreshValidationDiagnostics(testClientCode); } catch { /* TSS test result remains valid even if diagnostics refresh fails. */ }
    return result;
  }

  function handleQueueForTss(row) {
    return prepareTssConsignmentSubmit({ clientCode: activeClientCode, consignmentRowId: row.consignmentRowId });
  }

  async function refreshDeclarations(clientCode = activeClientCode) {
    const payload = await getDeclarations({ clientCode });
    const rows = (payload.declarations || []).map(normalizeDeclaration);
    const nextRows = rows.length ? rows : buildDeclarationsFromConsignments(consignmentRows);
    setDeclarationRows(nextRows);
    setStatusVocabulary(normalizeStatusVocabulary(payload.statusVocabulary));
    return nextRows;
  }

  async function refreshConsignments(clientCode = activeClientCode) {
    const payload = await getConsignments({ clientCode });
    const rows = (payload.consignments || []).map(normalizeConsignment);
    setConsignmentRows(rows);
    if (!declarationRows.length) setDeclarationRows(buildDeclarationsFromConsignments(rows));
    setStatusVocabulary(normalizeStatusVocabulary(payload.statusVocabulary));
    return rows;
  }

  function handleConsignmentUpdate(updatedRow) {
    setConsignmentRows((currentRows) => currentRows.map((row) => (row.id === updatedRow.id ? { ...row, ...updatedRow } : row)));
  }
  const mainClass = isAuthenticated ? `page-main app-main ${view}-main` : 'page-main login-main';

  return (
    <div className="app-shell" data-theme={isDarkTheme ? 'dark' : 'light'} data-environment-mode={environmentMode.toLowerCase()} data-api-status={apiStatus} data-api-error={apiError}>
      <AppBar session={session} isAuthenticated={isAuthenticated} environmentMode={environmentMode} onToggleDrawer={() => setDrawerOpen((value) => !value)} onLogout={handleLogout} />
      <main className={mainClass}>
        {!isAuthenticated && <LoginCard onLogin={handleLogin} />}
        {isAuthenticated && view === 'dashboard' && <DashboardPage onNavigate={navigate} connection={connection} activeClientCode={activeClientCode} onClientChange={setActiveClientCode} isDemoAdmin={isSynoviaSession(session)} />}
        {isAuthenticated && view === 'declarations' && <DeclarationsPage onBack={() => navigate('dashboard')} rows={declarationRows} clientCode={activeClientCode} statusVocabulary={statusVocabulary} routeDeclarationId={routeDeclarationId} onDetailRoute={(declarationId) => navigate('declarations', { declarationId })} onClearDetailRoute={() => navigate('declarations', { replace: true })} onOpenConsignments={() => navigate('consignments')} onRefresh={() => refreshDeclarations(activeClientCode)} />}
        {isAuthenticated && view === 'upload' && <UploadConsignmentPage onBack={() => navigate('dashboard')} onPreviewUpload={handlePreviewUpload} connection={connection} activeClientCode={activeClientCode} environmentMode={environmentMode} forceDemoMode={environmentMode === 'DEMO'} />}
        {isAuthenticated && view === 'consignments' && <ViewConsignmentsPage onBack={() => navigate('dashboard')} rows={consignmentRows} clientCode={activeClientCode} connection={connection} statusVocabulary={statusVocabulary} routeConsignmentId={routeConsignmentId} onDetailRoute={(consignmentId) => navigate('consignments', { consignmentId })} onClearDetailRoute={() => navigate('consignments', { replace: true })} onQueueForTss={handleQueueForTss} onUpdateConsignment={handleConsignmentUpdate} onRefresh={() => refreshConsignments(activeClientCode)} />}
        {isAuthenticated && view === 'controlTower' && <ControlTowerPage onBack={() => navigate('dashboard')} clientCode={activeClientCode} connection={connection} />}
        {isAuthenticated && view === 'settings' && <SettingsPage settings={settingsPayload} activeSection={settingsSection} environmentMode={environmentMode} validationDiagnostics={validationDiagnostics} validationDiagnosticsStatus={validationDiagnosticsStatus} validationDiagnosticsError={validationDiagnosticsError} onSectionChange={navigateSettings} onBack={() => navigate('dashboard')} onSaveSettings={handleSaveSettings} onTestTssApi={handleTestTssApi} onRefreshValidationDiagnostics={() => refreshValidationDiagnostics(activeClientCode)} />}
      </main>
      {drawerOpen && <button className="scrim" type="button" aria-label="Close navigation" onClick={() => setDrawerOpen(false)} />}
      <Drawer open={drawerOpen} view={view} isAuthenticated={isAuthenticated} isDarkTheme={isDarkTheme} settingsSections={settingsPayload?.sections || []} settingsSection={settingsSection} session={session} apiStatus={apiStatus} environmentMode={environmentMode} onNavigate={navigate} onSettingsSection={navigateSettings} onLogout={handleLogout} onToggleTheme={() => setIsDarkTheme((value) => !value)} />
    </div>
  );
}
