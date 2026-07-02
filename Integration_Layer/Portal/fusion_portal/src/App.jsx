import React, { useEffect, useMemo, useState } from 'react';
import { getAdminSettings, getApiDocsUrl, getConsignments, getDashboard, getSession, getTssConnections, loginPortal, prepareTssConsignmentSubmit, previewConsignmentUpload, saveAdminSettings } from './api';

const DEFAULT_SESSION = {
  tenantCode: 'SYNOVIA',
  tenantName: 'Synovia',
  username: 'synovia',
  role: 'CentralAdmin',
  mode: 'DEMO_ADMIN',
};
const DEFAULT_OPERATIONAL_CLIENT_CODE = 'PLE';

const PORTAL_CLIENTS = [
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

function normalizeConsignment(row) {
  const consignmentRowId = row.ConsignmentRowID ?? row.consignmentRowID ?? row.consignmentRowId;
  return {
    id: `PRS-C${String(consignmentRowId || '').padStart(6, '0')}`,
    ensHeaderRowId: row.EnsHeaderRowID ?? row.ensHeaderRowID ?? row.ensHeaderRowId,
    consignmentRowId,
    movementKey: row.MovementKey ?? row.movementKey ?? '',
    declarationNumber: row.DeclarationNumber ?? row.declarationNumber ?? null,
    consignmentNumber: row.ConsignmentNumber ?? row.consignmentNumber ?? `PRS-${consignmentRowId}`,
    traderReference: row.TraderReference ?? row.traderReference ?? '',
    transportDocumentNumber: row.TransportDocumentNumber ?? row.transportDocumentNumber ?? '',
    goodsDescription: row.GoodsDescription ?? row.goodsDescription ?? '',
    consigneeName: row.ConsigneeName ?? row.consigneeName ?? '',
    destinationCountry: row.DestinationCountry ?? row.destinationCountry ?? '',
    goodsItems: Number(row.GoodsItems ?? row.goodsItems ?? 0),
    grossMassKg: formatNumber(row.GrossMassKg ?? row.grossMassKg),
    status: row.Status ?? row.status ?? 'DRAFT',
    source: 'PRS.Consignment',
    updatedAt: row.UpdatedAt ?? row.updatedAt ?? '',
  };
}
function MaterialIcon({ children, className = '' }) {
  return <span className={`material-symbols-outlined ${className}`} aria-hidden="true">{children}</span>;
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

function appInfoSnapshot() {
  const nav = typeof navigator === 'undefined' ? {} : navigator;
  const mode = import.meta.env?.MODE || 'production';
  return {
    appName: 'SynoviaFlow',
    version: import.meta.env?.VITE_APP_VERSION || '1.1.7',
    runtime: `React ${React.version}`,
    environment: mode.charAt(0).toUpperCase() + mode.slice(1),
    culture: nav.language || 'en-US',
    browser: detectBrowser(nav.userAgent || ''),
    os: detectOs(nav.userAgent || ''),
  };
}

function Drawer({ open, view, isAuthenticated, isDarkTheme, settingsSections = [], settingsSection, session, apiStatus, onNavigate, onSettingsSection, onLogout, onToggleTheme }) {
  const visibleSettings = settingsSections.length ? settingsSections : SETTINGS_NAV_SECTIONS;
  const firstSettingsId = visibleSettings[0]?.id || SETTINGS_NAV_SECTIONS[0].id;
  const [openSections, setOpenSections] = useState({
    settings: true,
    session: true,
    appInfo: false,
    device: true,
  });
  const appInfo = useMemo(() => appInfoSnapshot(), []);

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
            <DrawerRow icon="upload_file" label="Upload Consignments" active={view === 'upload'} onClick={() => onNavigate('upload')} />
            <DrawerRow icon="list_alt" label="View Consignments" active={view === 'consignments'} onClick={() => onNavigate('consignments')} />
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

function AppBar({ session, isAuthenticated, onToggleDrawer, onLogout }) {
  return (
    <header className="appbar">
      <div className="appbar-left">
        <button className="hamburger-button" type="button" aria-label="Toggle navigation" onClick={onToggleDrawer}>
          <MaterialIcon>menu</MaterialIcon>
        </button>
        <img className="appbar-logo" src="/assets/SynoviaFlowLogo_white.png" alt="Synovia Flow" />
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

  async function handleSubmit(event) {
    event.preventDefault();
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
        <button className="submit-button" type="submit" disabled={loginState.status === 'loading'}>{loginState.status === 'loading' ? 'Checking' : 'Login'}</button>
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

function SettingsPage({ settings, activeSection, onSectionChange, onBack, onSaveSettings }) {
  const isSettingsLoading = !settings;
  const sections = useMemo(() => (
    settings?.sections?.length ? settings.sections : SETTINGS_NAV_SECTIONS.map((section) => ({ ...section, rows: [] }))
  ), [settings]);
  const selectedSection = sections.find((section) => section.id === activeSection) || sections[0];
  const [draft, setDraft] = useState({});
  const [saveState, setSaveState] = useState('idle');
  const [saveError, setSaveError] = useState('');

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
  }, [settings]);

  function draftKey(row) {
    return `${selectedSection.id}.${row.key}`;
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
        <button className="settings-save" type="button" onClick={saveSettings} disabled={isSettingsLoading || saveState !== 'changed'}>
          <MaterialIcon>save</MaterialIcon>
          <span>{saveState === 'saving' ? 'Saving' : saveState === 'saved' ? 'Saved' : 'Save settings'}</span>
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
            <span className="settings-mode-chip">{settings?.writeMode === 'db_write_existing_cfg' ? 'DB backed' : settings?.writeMode === 'draft_only' ? 'Draft only' : 'Ready'}</span>
          </div>

          {saveError && <div className="settings-save-error">{saveError}</div>}
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

const PREVIEW_NUMERIC_FIELDS = new Set(['gross_mass_kg', 'net_mass_kg', 'item_invoice_amount', 'number_of_packages']);
const PREVIEW_TWO_DECIMAL_FIELDS = new Set(['gross_mass_kg', 'net_mass_kg']);

function previewInputValue(value) {
  if (value === null || value === undefined) return '';
  return String(value);
}

function isPreviewMissing(value) {
  return value === null || value === undefined || String(value).trim() === '';
}

function numericPreviewIssue(fieldName, value) {
  if (!PREVIEW_NUMERIC_FIELDS.has(fieldName) || isPreviewMissing(value)) return null;
  const number = Number(String(value).replace(/,/g, ''));
  if (!Number.isFinite(number)) return { severity: 'error', message: `${fieldName.replaceAll('_', ' ')} must be numeric.` };
  if (fieldName === 'gross_mass_kg' && number <= 0) return { severity: 'error', message: 'Gross mass must be greater than zero.' };
  if (fieldName === 'net_mass_kg' && number < 0) return { severity: 'error', message: 'Net mass cannot be negative.' };
  if (fieldName === 'number_of_packages' && number <= 0) return { severity: 'error', message: 'Number of packages must be greater than zero.' };
  return null;
}

function previewIssuesForField(field, value) {
  const structuralIssues = (field.issues || []).filter((issue) => {
    const message = String(issue.message || '').toLowerCase();
    return !(message.includes('required') || message.includes('blank') || message.includes('numeric') || message.includes('greater than zero') || message.includes('cannot be negative'));
  });
  const issues = [...structuralIssues];
  if (field.required && isPreviewMissing(value)) {
    issues.push({ severity: 'error', message: `${field.label || field.field} is required before TSS processing.` });
  }
  const numericIssue = numericPreviewIssue(field.field, value);
  if (numericIssue) issues.push(numericIssue);
  return issues;
}

function editablePreviewField(field, value) {
  const nextValue = isPreviewMissing(value) ? null : value;
  const issues = previewIssuesForField(field, nextValue);
  const source = field.source?.source === 'assumption' && nextValue !== field.value
    ? { source: 'manualEdit', label: 'EDITED', reason: 'Edited in preview.' }
    : field.source;
  return {
    ...field,
    value: nextValue,
    missing: Boolean(field.required) && isPreviewMissing(nextValue),
    blank: isPreviewMissing(nextValue),
    source,
    issues,
  };
}

function issuesFromPreviewFields(fields = []) {
  return fields.flatMap((field) => (field.issues || []).map((issue) => ({ field: field.field, label: field.label, ...issue })));
}

function missingFromPreviewFields(fields = []) {
  return fields.filter((field) => field.required && field.missing).map((field) => field.field);
}

function previewStatusFromFields(fields = []) {
  const missing = missingFromPreviewFields(fields);
  const hasErrors = fields.some((field) => (field.issues || []).some((issue) => issue.severity === 'error'));
  return !missing.length && !hasErrors ? 'READY' : 'NEEDS_REVIEW';
}

function previewPayloadValue(fieldName, value) {
  if (isPreviewMissing(value)) return null;
  if (PREVIEW_TWO_DECIMAL_FIELDS.has(fieldName)) {
    const number = Number(String(value).replace(/,/g, ''));
    if (Number.isFinite(number)) return number.toFixed(2);
  }
  return value;
}

function payloadFromPreviewFields(fields = []) {
  return fields.reduce((payload, field) => {
    const value = previewPayloadValue(field.field, field.value);
    if (!isPreviewMissing(value)) payload[field.field] = value;
    return payload;
  }, {});
}

function clonePreviewConsignments(consignments = []) {
  return JSON.parse(JSON.stringify(consignments || []));
}

function refreshPreviewConsignment(item) {
  const goodsItems = (item.goodsItems || []).map((goods) => {
    const missingRequired = missingFromPreviewFields(goods.fields || []);
    const issues = issuesFromPreviewFields(goods.fields || []);
    return {
      ...goods,
      missingRequired,
      issues,
      status: previewStatusFromFields(goods.fields || []),
    };
  });
  const missingRequired = missingFromPreviewFields(item.fields || []);
  const issues = issuesFromPreviewFields(item.fields || []);
  const goodsHasBlockers = goodsItems.some((goods) => goods.status !== 'READY');
  return {
    ...item,
    missingRequired,
    issues,
    goodsItems,
    status: !missingRequired.length && !issues.some((issue) => issue.severity === 'error') && goodsItems.length && !goodsHasBlockers ? 'READY' : 'NEEDS_REVIEW',
  };
}

function buildEditablePayloadPreview(selected) {
  if (!selected) return null;
  const updatePayload = { op_type: 'update', ...payloadFromPreviewFields(selected.fields || []) };
  const declarationNumber = updatePayload.declaration_number || selected.values?.declaration_number;
  const consignmentNumber = updatePayload.consignment_number || selected.values?.consignment_number;
  const submitPayload = {
    op_type: 'submit',
    ...(declarationNumber ? { declaration_number: declarationNumber } : {}),
    ...(consignmentNumber ? { consignment_number: consignmentNumber } : {}),
  };
  const goodsItems = (selected.goodsItems || []).map((goods) => ({
    ordinal: goods.ordinal,
    status: goods.status,
    ...payloadFromPreviewFields(goods.fields || []),
  }));
  return {
    ...(selected.tssPayloadPreview || {}),
    ready: selected.status === 'READY' && goodsItems.length && goodsItems.every((goods) => goods.status === 'READY'),
    operations: [
      { operationCode: 'UPDATE_CONSIGNMENT_WITH_ENS', payload: updatePayload },
      { operationCode: 'SUBMIT_CONSIGNMENT', payload: submitPayload },
    ],
    goodsItems,
    goodsItemCount: goodsItems.length,
  };
}

function previewSourceLabel(source) {
  if (!source) return '';
  if (source.source === 'assumption') return source.reason || source.label || 'Assumed default';
  if (source.source === 'manualEdit') return source.reason || 'Edited in preview.';
  return source.source || source.sourceColumn || source.apiField || 'mapped';
}

function PreviewFieldGrid({ fields = [], onChange }) {
  return (
    <div className="preview-field-grid">
      {fields.map((field) => {
        const hasError = (field.issues || []).some((issue) => issue.severity === 'error');
        const hasWarning = (field.issues || []).some((issue) => issue.severity === 'warning');
        const isAssumption = field.source?.source === 'assumption';
        const className = ['preview-field', field.missing ? 'is-missing' : '', hasError ? 'has-error' : '', hasWarning ? 'has-warning' : '', isAssumption ? 'has-assumption' : ''].filter(Boolean).join(' ');
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
            {field.source && (
              <small className={isAssumption ? 'preview-source-assumption' : ''}>
                {isAssumption && <em>ASSUMPTION</em>}
                <span>{previewSourceLabel(field.source)}</span>
              </small>
            )}
          </label>
        );
      })}
    </div>
  );
}

function PreviewPayloadPanel({ payloadPreview }) {
  if (!payloadPreview) return null;
  const operations = payloadPreview.operations || [];
  const goodsItems = payloadPreview.goodsItems || [];
  const goodsSample = goodsItems.slice(0, 5);
  return (
    <div className="preview-payload-panel">
      <div className="preview-payload-heading">
        <div>
          <span>Preview payload</span>
          <h3>TSS-ready shape</h3>
        </div>
        <strong>{payloadPreview.ready ? 'READY' : 'NEEDS REVIEW'} / DB off / TSS off</strong>
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

function PreviewDetailsModal({ payload, onClose }) {
  const preview = payload?.processingPreview;
  const consignments = preview?.consignments || [];
  const [selectedId, setSelectedId] = useState(consignments[0]?.previewId || '');
  const [draftConsignments, setDraftConsignments] = useState(() => clonePreviewConsignments(consignments));

  useEffect(() => {
    setSelectedId(consignments[0]?.previewId || '');
    setDraftConsignments(clonePreviewConsignments(consignments));
  }, [payload?.sha256]);

  if (!preview) return null;
  const editableConsignments = draftConsignments.length ? draftConsignments : consignments;
  const selected = editableConsignments.find((item) => item.previewId === selectedId) || editableConsignments[0];
  const selectedGoods = selected?.goodsItems || [];

  function updateConsignmentField(fieldName, value) {
    setDraftConsignments((current) => (current.length ? current : clonePreviewConsignments(consignments)).map((item) => {
      if (item.previewId !== selected?.previewId) return item;
      const fields = (item.fields || []).map((field) => (field.field === fieldName ? editablePreviewField(field, value) : field));
      return refreshPreviewConsignment({
        ...item,
        values: { ...(item.values || {}), [fieldName]: isPreviewMissing(value) ? null : value },
        fields,
      });
    }));
  }

  function updateGoodsField(goodsOrdinal, fieldName, value) {
    setDraftConsignments((current) => (current.length ? current : clonePreviewConsignments(consignments)).map((item) => {
      if (item.previewId !== selected?.previewId) return item;
      const goodsItems = (item.goodsItems || []).map((goods) => {
        if (goods.ordinal !== goodsOrdinal) return goods;
        const fields = (goods.fields || []).map((field) => (field.field === fieldName ? editablePreviewField(field, value) : field));
        return {
          ...goods,
          values: { ...(goods.values || {}), [fieldName]: isPreviewMissing(value) ? null : value },
          fields,
        };
      });
      return refreshPreviewConsignment({ ...item, goodsItems });
    }));
  }
  const summary = preview.summary || {};
  const splitLabel = summary.splitConsignmentCount ? `${summary.splitConsignmentCount} split parts` : 'No split needed';
  const rowModeText = preview.rowMode === 'api_field_value'
    ? 'Field/value manifest mapped into PRS/TSS shape.'
    : preview.rowMode === 'multi_sheet'
      ? 'Workbook sheets combined into PRS/TSS shape.'
      : 'Workbook rows mapped into PRS/TSS shape.';
  const sourceSheetText = (preview.sourceSheets || [])
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
          <button className="modal-close-button" type="button" onClick={onClose} aria-label="Close mapped preview">
            <MaterialIcon>close</MaterialIcon>
          </button>
        </header>

        <div className="preview-summary-bar">
          <div><span>Consignments</span><strong>{summary.consignmentCount || 0}</strong></div>
          <div><span>Goods items</span><strong>{summary.goodsItemCount || 0}</strong></div>
          <div><span>Mapped fields</span><strong>{summary.mappedFieldCount || 0}</strong></div>
          <div className={(summary.missingRequiredCount || 0) > 0 ? 'is-alert' : ''}><span>Missing required</span><strong>{summary.missingRequiredCount || 0}</strong></div>
          <div><span>99-row split</span><strong>{splitLabel}</strong></div>
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
                            return (
                              <td className={field?.missing ? 'is-missing' : ''} key={column.field}>
                                <input
                                  aria-label={`${column.label} row ${goods.ordinal}`}
                                  className="preview-table-input"
                                  type="text"
                                  value={previewInputValue(value)}
                                  placeholder="Missing"
                                  onChange={(event) => updateGoodsField(goods.ordinal, column.field, event.target.value)}
                                />
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

              <PreviewPayloadPanel payloadPreview={buildEditablePayloadPreview(selected)} />
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
function UploadConsignmentPage({ onBack, onPreviewUpload, connection, activeClientCode, forceDemoMode = false }) {
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
          <span>{forceDemoMode ? 'Demo mode (Synovia admin)' : 'Demo mode'}</span>
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
        <input type="file" accept=".xlsx,.xls,.csv" multiple onChange={(event) => handleFiles(event.target.files)} />
        <MaterialIcon>cloud_upload</MaterialIcon>
        <strong>{selectedFiles.length ? `${selectedFiles.length} file${selectedFiles.length === 1 ? '' : 's'} selected` : 'Drag and drop files here or click'}</strong>
        <span>{selectedFiles.length ? selectedFiles.map((file) => file.name).join(' | ') : 'Supported formats: .xlsx, .xls, .csv'}</span>
        <span>Max file size: 50 MB</span>
      </label>

      {previewState.status !== 'idle' && (
        <div className={`upload-preview-card ${previewState.status}`}>
          {previewState.status === 'loading' && <span>Preparing API preview...</span>}
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

      <button className="preview-button" type="button" disabled={!selectedFiles.length || previewState.status === 'loading'} onClick={handlePreview}>
        {previewState.status === 'loading' ? 'Preparing Preview' : (demoMode ? 'Run Demo Preview' : 'Upload & Preview')}
      </button>

      {previewDetailsOpen && previewState.payload?.processingPreview && (
        <PreviewDetailsModal payload={previewState.payload} onClose={() => setPreviewDetailsOpen(false)} />
      )}
    </section>
  );
}
function StatusBadge({ status }) {
  return <span className={`status-badge ${status.toLowerCase().replace('_', '-')}`}>{status.replace('_', ' ')}</span>;
}

function ViewConsignmentsPage({ onBack, rows, clientCode, connection, onQueueForTss }) {
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('ALL');
  const sourceRows = rows.length ? rows : CONSIGNMENTS;
  const [selectedId, setSelectedId] = useState(sourceRows[0]?.id || '');
  const [tssState, setTssState] = useState({ status: 'idle', rowId: '', payload: null, error: '' });

  useEffect(() => {
    if (sourceRows.length && !sourceRows.some((row) => row.id === selectedId)) {
      setSelectedId(sourceRows[0].id);
    }
  }, [sourceRows, selectedId]);

  const filtered = useMemo(() => {
    const value = query.trim().toLowerCase();
    return sourceRows.filter((row) => {
      const matchesStatus = status === 'ALL' || row.status === status;
      const haystack = `${row.consignmentNumber} ${row.traderReference} ${row.transportDocumentNumber} ${row.goodsDescription} ${row.consigneeName}`.toLowerCase();
      return matchesStatus && (!value || haystack.includes(value));
    });
  }, [query, status, sourceRows]);

  const selected = filtered.find((row) => row.id === selectedId) || filtered[0] || sourceRows[0] || CONSIGNMENTS[0];

  async function handleQueueForTss() {
    if (!selected?.consignmentRowId || !onQueueForTss) return;
    setTssState({ status: 'loading', rowId: selected.id, payload: null, error: '' });
    try {
      const payload = await onQueueForTss(selected);
      setTssState({ status: 'ready', rowId: selected.id, payload, error: '' });
    } catch (error) {
      setTssState({ status: 'error', rowId: selected.id, payload: null, error: error.message });
    }
  }

  return (
    <section className="consignments-page" aria-label="View consignments">
      <div className="consignments-header">
        <button className="back-button" type="button" onClick={onBack}>
          <MaterialIcon>arrow_back</MaterialIcon>
          <span>Back</span>
        </button>
        <div>
          <h1>View Consignments</h1>
          <p>Review PRS consignments, goods-item counts, validation state, and TSS readiness.</p>
        </div>
      </div>

      <div className="summary-rail" aria-label="Consignment summary">
        <div><span>ClientCode</span><strong>{clientCode}</strong></div>
        <div><span>File Rule</span><strong>{connectionFileText(connection)}</strong></div>
        <div><span>TSS</span><strong>{credentialText(connection)}</strong></div>
        <div><span>Route</span><strong>{routeText(connection)}</strong></div>
        <div><span>PRS.Consignment</span><strong>{sourceRows.length}</strong></div>
        <div><span>Goods Items</span><strong>{sourceRows.reduce((total, row) => total + row.goodsItems, 0)}</strong></div>
        <div><span>Ready / Validated</span><strong>{sourceRows.filter((row) => ['READY', 'VALIDATED'].includes(row.status)).length}</strong></div>
      </div>

      <div className="consignment-workspace">
        <div className="list-panel">
          <div className="table-toolbar">
            <label className="search-box">
              <MaterialIcon>search</MaterialIcon>
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search consignment, trader ref, document..." />
            </label>
            <label className="compact-select">
              <span>Status</span>
              <select value={status} onChange={(event) => setStatus(event.target.value)}>
                <option value="ALL">All</option>
                <option value="VALIDATED">Validated</option>
                <option value="READY">Ready</option>
                <option value="NEEDS_REVIEW">Needs review</option>
                <option value="INGESTED">Ingested</option>
              </select>
            </label>
          </div>

          <div className="table-wrap">
            <table className="consignments-table">
              <thead>
                <tr>
                  <th>Consignment</th>
                  <th>Movement</th>
                  <th>Trader Ref</th>
                  <th>Goods</th>
                  <th>Gross Mass</th>
                  <th>Status</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row) => (
                  <tr key={row.id} className={row.id === selected.id ? 'selected' : ''} onClick={() => setSelectedId(row.id)}>
                    <td><strong>{row.consignmentNumber}</strong><span>{row.transportDocumentNumber}</span></td>
                    <td>{row.movementKey}</td>
                    <td>{row.traderReference}</td>
                    <td>{row.goodsItems}</td>
                    <td>{row.grossMassKg}</td>
                    <td><StatusBadge status={row.status} /></td>
                    <td>{row.updatedAt}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <aside className="detail-panel" aria-label="Selected consignment detail">
          <div className="detail-title-row">
            <div>
              <span>PRS.Consignment</span>
              <h2>{selected.consignmentNumber}</h2>
            </div>
            <StatusBadge status={selected.status} />
          </div>
          <dl className="detail-grid">
            <div><dt>EnsHeaderRowID</dt><dd>{selected.ensHeaderRowId}</dd></div>
            <div><dt>ConsignmentRowID</dt><dd>{selected.consignmentRowId}</dd></div>
            <div><dt>Declaration</dt><dd>{selected.declarationNumber || 'Pending'}</dd></div>
            <div><dt>Consignee</dt><dd>{selected.consigneeName}</dd></div>
            <div><dt>Destination</dt><dd>{selected.destinationCountry}</dd></div>
            <div><dt>Source</dt><dd>{selected.source}</dd></div>
          </dl>
          <div className="hierarchy-box">
            <div><MaterialIcon>account_tree</MaterialIcon><span>PRS.ENS_Header</span></div>
            <div><MaterialIcon>subdirectory_arrow_right</MaterialIcon><span>PRS.Consignment</span></div>
            <div><MaterialIcon>subdirectory_arrow_right</MaterialIcon><span>{selected.goodsItems} PRS.Goods_Item rows</span></div>
          </div>
          <div className="detail-actions">
            <button className="primary-action blue" type="button"><MaterialIcon>visibility</MaterialIcon><span>Open Detail</span></button>
            <button className="outline-action" type="button" onClick={handleQueueForTss} disabled={tssState.status === 'loading'}><MaterialIcon>send</MaterialIcon><span>{tssState.status === 'loading' ? 'Checking TSS Route' : 'Queue for TSS'}</span></button>
          </div>
          {tssState.status !== 'idle' && tssState.rowId === selected.id && (
            <div className={`action-feedback ${tssState.status === 'error' ? 'is-error' : ''}`}>
              <strong>{tssState.status === 'ready' ? (tssState.payload?.plan?.ready ? 'Ready for TSS dry-run' : 'TSS blockers found') : 'TSS route check failed'}</strong>
              <span>{tssState.status === 'ready' ? `ENS step first: ${tssState.payload?.plan?.routeIsEnsFirst ? 'yes' : 'no'} - Missing: ${(tssState.payload?.plan?.missing || []).join(', ') || 'none'}` : tssState.error}</span>
            </div>
          )}
        </aside>
      </div>
    </section>
  );
}

export default function App() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [view, setView] = useState('login');
  const [isDarkTheme, setIsDarkTheme] = useState(false);
  const [session, setSession] = useState(sessionFallback(DEFAULT_SESSION.tenantCode));
  const [activeClientCode, setActiveClientCode] = useState(DEFAULT_OPERATIONAL_CLIENT_CODE);
  const [connection, setConnection] = useState(null);
  const [consignmentRows, setConsignmentRows] = useState(CONSIGNMENTS);
  const [settingsPayload, setSettingsPayload] = useState(null);
  const [settingsSection, setSettingsSection] = useState(SETTINGS_NAV_SECTIONS[0].id);
  const [apiStatus, setApiStatus] = useState('idle');
  const [apiError, setApiError] = useState('');

  useEffect(() => {
    if (!isAuthenticated) return undefined;
    let cancelled = false;
    const clientCode = activeClientCode || DEFAULT_OPERATIONAL_CLIENT_CODE;

    async function loadPortalData() {
      setApiStatus('loading');
      setApiError('');
      try {
        const [sessionPayload, dashboardPayload, consignmentPayload, connectionPayload, settingsPayload] = await Promise.all([
          getSession(clientCode),
          getDashboard(clientCode),
          getConsignments({ clientCode }),
          getTssConnections(clientCode),
          getAdminSettings(clientCode),
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
        setConnection(activeConnection);
        setConsignmentRows((consignmentPayload.consignments || []).map(normalizeConsignment));
        setSettingsPayload(settingsPayload);
        setSettingsSection(settingsPayload.sections?.[0]?.id || SETTINGS_NAV_SECTIONS[0].id);
        setApiStatus('online');
        setApiError(dashboardPayload?.counts ? '' : 'Dashboard counts unavailable');
      } catch (error) {
        if (cancelled) return;
        if (!isSynoviaSession(session)) {
          setSession(sessionFallback(clientCode));
        }
        setConnection(null);
        setConsignmentRows(CONSIGNMENTS);
        setSettingsPayload(null);
        setApiStatus('offline');
        setApiError(error.message);
      }
    }

    loadPortalData();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, activeClientCode, session.tenantCode]);

  function navigate(nextView) {
    setView(nextView);
    setDrawerOpen(false);
  }

  function navigateSettings(sectionId = SETTINGS_NAV_SECTIONS[0].id) {
    setSettingsSection(sectionId);
    navigate('settings');
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
    setSession({
      tenantCode: activeSession.tenantCode || DEFAULT_SESSION.tenantCode,
      tenantName: activeSession.tenantName || DEFAULT_SESSION.tenantName,
      username: activeSession.username || credentials?.username?.trim() || DEFAULT_SESSION.username,
      role: activeSession.role || DEFAULT_SESSION.role,
      mode: activeSession.mode || (activeSession.tenantCode === DEFAULT_SESSION.tenantCode ? 'DEMO_ADMIN' : 'CLIENT_SESSION'),
    });
    setActiveClientCode(nextClientCode);
    setConnection(payload.connection || null);
    setIsAuthenticated(true);
    setApiStatus('online');
    navigate('dashboard');
  }

  function handleLogout() {
    setIsAuthenticated(false);
    setSession(sessionFallback(DEFAULT_SESSION.tenantCode));
    setActiveClientCode(DEFAULT_OPERATIONAL_CLIENT_CODE);
    setConnection(null);
    setConsignmentRows(CONSIGNMENTS);
    setSettingsPayload(null);
    setSettingsSection(SETTINGS_NAV_SECTIONS[0].id);
    setApiStatus('idle');
    setApiError('');
    navigate('login');
  }

  function handlePreviewUpload(files, options = {}) {
    return previewConsignmentUpload({ clientCode: activeClientCode, files, ...options });
  }

  async function handleSaveSettings(payload) {
    const nextSettings = await saveAdminSettings(payload);
    setSettingsPayload(nextSettings);
    return nextSettings;
  }

  function handleQueueForTss(row) {
    return prepareTssConsignmentSubmit({ clientCode: activeClientCode, consignmentRowId: row.consignmentRowId });
  }

  const mainClass = isAuthenticated ? `page-main app-main ${view}-main` : 'page-main login-main';

  return (
    <div className="app-shell" data-theme={isDarkTheme ? 'dark' : 'light'} data-api-status={apiStatus} data-api-error={apiError}>
      <AppBar session={session} isAuthenticated={isAuthenticated} onToggleDrawer={() => setDrawerOpen((value) => !value)} onLogout={handleLogout} />
      <main className={mainClass}>
        {!isAuthenticated && <LoginCard onLogin={handleLogin} />}
        {isAuthenticated && view === 'dashboard' && <DashboardPage onNavigate={navigate} connection={connection} activeClientCode={activeClientCode} onClientChange={setActiveClientCode} isDemoAdmin={isSynoviaSession(session)} />}
        {isAuthenticated && view === 'upload' && <UploadConsignmentPage onBack={() => navigate('dashboard')} onPreviewUpload={handlePreviewUpload} connection={connection} activeClientCode={activeClientCode} forceDemoMode={isSynoviaSession(session)} />}
        {isAuthenticated && view === 'consignments' && <ViewConsignmentsPage onBack={() => navigate('dashboard')} rows={consignmentRows} clientCode={activeClientCode} connection={connection} onQueueForTss={handleQueueForTss} />}
        {isAuthenticated && view === 'settings' && <SettingsPage settings={settingsPayload} activeSection={settingsSection} onSectionChange={setSettingsSection} onBack={() => navigate('dashboard')} onSaveSettings={handleSaveSettings} />}
      </main>
      {drawerOpen && <button className="scrim" type="button" aria-label="Close navigation" onClick={() => setDrawerOpen(false)} />}
      <Drawer open={drawerOpen} view={view} isAuthenticated={isAuthenticated} isDarkTheme={isDarkTheme} settingsSections={settingsPayload?.sections || SETTINGS_NAV_SECTIONS} settingsSection={settingsSection} session={session} apiStatus={apiStatus} onNavigate={navigate} onSettingsSection={navigateSettings} onLogout={handleLogout} onToggleTheme={() => setIsDarkTheme((value) => !value)} />
    </div>
  );
}
