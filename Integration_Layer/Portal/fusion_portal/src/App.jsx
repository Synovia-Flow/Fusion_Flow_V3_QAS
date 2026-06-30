import { useMemo, useState } from 'react';

const SESSION = {
  tenantCode: 'PLE',
  tenantName: 'Primeline Express',
  username: 'synovia',
  role: 'CentralAdmin',
};

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

function MaterialIcon({ children, className = '' }) {
  return <span className={`material-symbols-outlined ${className}`} aria-hidden="true">{children}</span>;
}

function DrawerRow({ icon, label, active = false, danger = false, indent = false, trailing, onClick }) {
  return (
    <button className={`drawer-row ${active ? 'active' : ''} ${danger ? 'danger' : ''} ${indent ? 'indent' : ''}`} type="button" onClick={onClick}>
      <MaterialIcon>{icon}</MaterialIcon>
      <span>{label}</span>
      {trailing && <MaterialIcon className="trailing">{trailing}</MaterialIcon>}
    </button>
  );
}

function Drawer({ open, view, isAuthenticated, onNavigate, onLogout }) {
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
        <DrawerRow icon="settings" label="Settings" trailing="expand_less" />
        <DrawerRow icon="dark_mode" label="Dark theme" indent />
        <DrawerRow icon="frame_reload" label="Reload application" danger indent onClick={() => window.location.reload()} />
        <DrawerRow icon="badge" label="Session" trailing="expand_less" />
        {isAuthenticated ? (
          <DrawerRow icon="logout" label="Logout" indent onClick={onLogout} />
        ) : (
          <DrawerRow icon="login" label="Login" active={view === 'login'} indent />
        )}
      </nav>
      <div className="drawer-bottom">
        <DrawerRow icon="code" label="App Info" trailing="expand_more" />
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

  function handleSubmit(event) {
    event.preventDefault();
    onLogin();
  }

  return (
    <section className="login-card" aria-label="Login form">
      <img className="flow-logo" src="/assets/SynoviaFlowLogo.png" alt="Synovia Flow" />
      <form className="login-form" onSubmit={handleSubmit}>
        <label className="input-shell">
          <input type="text" placeholder="Username*" autoComplete="username" />
        </label>
        <label className="input-shell password-shell">
          <input type={showPassword ? 'text' : 'password'} placeholder="Password*" autoComplete="current-password" />
          <button className="visibility-button" type="button" onClick={() => setShowPassword((value) => !value)} aria-label="Toggle password visibility">
            <MaterialIcon>{showPassword ? 'visibility' : 'visibility_off'}</MaterialIcon>
          </button>
        </label>
        <button className="submit-button" type="submit">Login</button>
        <button className="forgot-button" type="button">Forgot password?</button>
      </form>
    </section>
  );
}

function DashboardPage({ onNavigate }) {
  return (
    <section className="dashboard-page" aria-label="Dashboard">
      <div className="welcome-block">
        <div className="welcome-title">
          <span>Welcome to</span>
          <img src="/assets/SynoviaFlowLogo.png" alt="Synovia Flow" />
        </div>
        <p>Follow the steps below to prepare and send consignments to TSS.</p>
      </div>
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

function TemplateButton({ icon, children }) {
  return (
    <button className="template-button" type="button">
      <MaterialIcon>{icon}</MaterialIcon>
      <span>{children}</span>
    </button>
  );
}

function UploadConsignmentPage({ onBack }) {
  const [selectedFile, setSelectedFile] = useState(null);
  const [isDragging, setIsDragging] = useState(false);

  function handleFiles(files) {
    const nextFile = files?.[0];
    if (nextFile) setSelectedFile(nextFile);
  }

  function handleDrop(event) {
    event.preventDefault();
    setIsDragging(false);
    handleFiles(event.dataTransfer.files);
  }

  return (
    <section className="upload-page page-card" aria-label="Upload consignments">
      <div className="page-card-topline">
        <button className="back-button" type="button" onClick={onBack}>
          <MaterialIcon>arrow_back</MaterialIcon>
          <span>Back</span>
        </button>
        <button className="api-link" type="button">
          <MaterialIcon>help</MaterialIcon>
          <span>API</span>
        </button>
      </div>

      <header className="upload-heading">
        <h1>Create Consignment From Template</h1>
        <p>Use the templates below to create consignments in bulk by filling in the required information and uploading the file.</p>
        <p>After uploading, you can preview the parsed consignments and send them to TSS either as drafts or final submissions.</p>
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

      <div className="declaration-area">
        <label>Declaration type:</label>
        <div className="declaration-pill">Entry Summary Declaration</div>
      </div>

      <label className="field-shell stacked">
        <span>Header Declaration Number*</span>
        <input type="text" placeholder="ENS000000000000000" />
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
        <input type="file" accept=".xlsx,.xls,.csv" onChange={(event) => handleFiles(event.target.files)} />
        <MaterialIcon>cloud_upload</MaterialIcon>
        <strong>{selectedFile ? selectedFile.name : 'Drag and drop files here or click'}</strong>
        <span>Supported formats: .xlsx, .xls, .csv</span>
        <span>Max file size: 50 MB</span>
      </label>

      <div className="upload-actions">
        <button className="file-picker-button" type="button" onClick={() => document.querySelector('.drop-zone input')?.click()}>Open file picker</button>
        <button className="clear-button" type="button" disabled={!selectedFile} onClick={() => setSelectedFile(null)}>Clear</button>
      </div>

      <button className="preview-button" type="button" disabled={!selectedFile}>Upload & Preview</button>
    </section>
  );
}

function StatusBadge({ status }) {
  return <span className={`status-badge ${status.toLowerCase().replace('_', '-')}`}>{status.replace('_', ' ')}</span>;
}

function ViewConsignmentsPage({ onBack }) {
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('ALL');
  const [selectedId, setSelectedId] = useState(CONSIGNMENTS[0].id);

  const filtered = useMemo(() => {
    const value = query.trim().toLowerCase();
    return CONSIGNMENTS.filter((row) => {
      const matchesStatus = status === 'ALL' || row.status === status;
      const haystack = `${row.consignmentNumber} ${row.traderReference} ${row.transportDocumentNumber} ${row.goodsDescription} ${row.consigneeName}`.toLowerCase();
      return matchesStatus && (!value || haystack.includes(value));
    });
  }, [query, status]);

  const selected = filtered.find((row) => row.id === selectedId) || filtered[0] || CONSIGNMENTS[0];

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
        <div><span>ClientCode</span><strong>{SESSION.tenantCode}</strong></div>
        <div><span>PRS.Consignment</span><strong>{CONSIGNMENTS.length}</strong></div>
        <div><span>Goods Items</span><strong>{CONSIGNMENTS.reduce((total, row) => total + row.goodsItems, 0)}</strong></div>
        <div><span>Ready / Validated</span><strong>{CONSIGNMENTS.filter((row) => ['READY', 'VALIDATED'].includes(row.status)).length}</strong></div>
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
            <button className="outline-action" type="button"><MaterialIcon>send</MaterialIcon><span>Queue for TSS</span></button>
          </div>
        </aside>
      </div>
    </section>
  );
}

export default function App() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [view, setView] = useState('login');

  function navigate(nextView) {
    setView(nextView);
    setDrawerOpen(false);
  }

  function handleLogin() {
    setIsAuthenticated(true);
    navigate('dashboard');
  }

  function handleLogout() {
    setIsAuthenticated(false);
    navigate('login');
  }

  const mainClass = isAuthenticated ? `page-main app-main ${view}-main` : 'page-main login-main';

  return (
    <div className="app-shell">
      <AppBar session={SESSION} isAuthenticated={isAuthenticated} onToggleDrawer={() => setDrawerOpen((value) => !value)} onLogout={handleLogout} />
      <main className={mainClass}>
        {!isAuthenticated && <LoginCard onLogin={handleLogin} />}
        {isAuthenticated && view === 'dashboard' && <DashboardPage onNavigate={navigate} />}
        {isAuthenticated && view === 'upload' && <UploadConsignmentPage onBack={() => navigate('dashboard')} />}
        {isAuthenticated && view === 'consignments' && <ViewConsignmentsPage onBack={() => navigate('dashboard')} />}
      </main>
      {drawerOpen && <button className="scrim" type="button" aria-label="Close navigation" onClick={() => setDrawerOpen(false)} />}
      <Drawer open={drawerOpen} view={view} isAuthenticated={isAuthenticated} onNavigate={navigate} onLogout={handleLogout} />
    </div>
  );
}