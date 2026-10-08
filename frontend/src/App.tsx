import { cloneElement, useCallback, useDeferredValue, useEffect, useId, useRef, useState } from 'react';
import type { FormEvent, ReactElement, ReactNode } from 'react';
import { bridge, emptySnapshot, normalizeSnapshot } from './api';
import { errorCode, errorText, phaseText } from './messages';
import { translate } from './i18n';
import brandLogo from '../brand/logo-v1.png';
import LegacyLibrary from './LegacyLibrary';
import type { CopyKey } from './i18n';
import type { ControllerEvent, Download, DriveWorkflow, HistoryEntry, Language, Metrics, Model, NodePackage, NodeVersion, Paged, Params, Snapshot } from './types';

const pages = ['session', 'models', 'nodes', 'workflows', 'accounts', 'activity', 'settings'] as const;
type Page = typeof pages[number];
const gpuChoices = ['T4', 'L4', 'A100', 'G4', 'CPU'];
const categories = ['checkpoints', 'diffusion_models', 'loras', 'vae', 'text_encoders', 'clip', 'clip_vision', 'controlnet', 'upscale_models', 'embeddings', 'unet', 'llm_gguf', 'onnx', 'audio_encoders', 'model_patches'];
const busyStates = ['authorizing', 'allocating', 'preparing', 'starting', 'reconnecting', 'stopping'];
type AuthState = { account_id: string; purpose: 'colab' | 'drive' | 'mount_drive'; session_id?: string; status: 'pending' | 'error'; authorization_url?: string; needs_code: boolean; needs_confirmation: boolean; error?: string; error_code?: string };
type WorkflowEvidence = { value: unknown; account_id: string | null; session_id: string | null; path: string };
function authMatches(auth: AuthState | null, value: Params) {
  return auth?.account_id === value.account_id && auth?.purpose === (value.purpose ?? 'colab') && (!value.session_id || value.session_id === auth.session_id);
}
function validAuthorizationUrl(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 16384) return false;
  try { const url = new URL(value); return url.protocol === 'https:' && ['accounts.google.com', 'sdk.cloud.google.com'].includes(url.hostname) && !url.username && !url.password && !url.port; } catch { return false; }
}

function Card({ title, children, className = '' }: { title: string; children: ReactNode; className?: string }) {
  return <section className={`card ${className}`}><h2>{title}</h2>{children}</section>;
}
function Empty({ title, hint }: { title: string; hint: string }) {
  return <div className="empty"><span className="empty-mark" aria-hidden="true">[ ]</span><h3>{title}</h3><p>{hint}</p></div>;
}
function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  const id = useId();
  return <div className="field"><label htmlFor={id}>{label}</label>{cloneElement(children as ReactElement<{ id: string; 'aria-describedby'?: string }>, { id, 'aria-describedby': hint ? `${id}-hint` : undefined })}{hint ? <small id={`${id}-hint`}>{hint}</small> : null}</div>;
}
function formatBytes(bytes: number | null | undefined, language: Language): string {
  if (bytes == null) return '—';
  const unit = bytes >= 1024 ** 3 ? 'GiB' : bytes >= 1024 ** 2 ? 'MiB' : 'KiB';
  const divisor = unit === 'GiB' ? 1024 ** 3 : unit === 'MiB' ? 1024 ** 2 : 1024;
  return `${(bytes / divisor).toLocaleString(language, { maximumFractionDigits: 1 })} ${unit}`;
}
function Pagination({ page, noun, disabled, t, onPage }: { page: Paged<unknown>; noun: string; disabled: boolean; t: (key: CopyKey) => string; onPage: (offset: number) => void }) {
  return <div className="section-toolbar pagination"><span>{page.total ? `${page.offset + 1}–${Math.min(page.offset + page.items.length, page.total)} / ${page.total}` : '0'} · {noun}</span><div className="actions"><button disabled={disabled || page.offset === 0} onClick={() => onPage(Math.max(0, page.offset - page.limit))}>{t('previousPage')}</button><button disabled={disabled || page.offset + page.limit >= page.total} onClick={() => onPage(page.offset + page.limit)}>{t('nextPage')}</button></div></div>;
}
function ResourceReadings({ readings, language, t, local = false }: { readings: Metrics; language: Language; t: (key: CopyKey) => string; local?: boolean }) {
  const percent = (value: number | null | undefined) => value == null ? '—' : `${value.toLocaleString(language, { maximumFractionDigits: 1 })}%`;
  const device = readings.gpu?.find(item => item.type === 'cuda');
  const rows = [[t('cpu'), percent(readings.cpu_percent)], [t('memory'), `${formatBytes(readings.ram_used, language)} / ${formatBytes(readings.ram_total, language)}`],
    ...(local ? [[t('controllerMemory'), formatBytes(readings.controller_rss, language)], [t('controllerCpu'), percent(readings.controller_cpu_percent)]] : [[t('freeDisk'), formatBytes(readings.disk_free, language)], [t('vram'), device?.vram_total != null && device?.vram_free != null ? `${formatBytes(device.vram_total - device.vram_free, language)} / ${formatBytes(device.vram_total, language)}` : '—']])];
  return <dl className="resource-readings">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}
function WorkflowResult({ result, t }: { result: unknown; t: (key: CopyKey) => string }) {
  const value = typeof result === 'object' && result != null ? result as Record<string, unknown> : {};
  const missingNodes = Array.isArray(value.missing_nodes) ? value.missing_nodes : [];
  const missingModels = Array.isArray(value.missing_models) ? value.missing_models : [];
  return <div className="workflow-result" role="status"><h3>{value.imported ? t('workflowImported') : value.valid === true ? t('dependenciesFound') : t('dependenciesAttention')}</h3>
    {missingNodes.length ? <><h4>{t('missingNodes')}</h4><ul>{missingNodes.map((item, index) => <li key={index}>{String(item)}</li>)}</ul></> : null}
    {missingModels.length ? <><h4>{t('missingModels')}</h4><ul>{missingModels.map((item, index) => <li key={index}>{String(item)}</li>)}</ul></> : null}
    {!value.imported ? <p>{t('staticWorkflowHint')}</p> : <p>{String(value.name ?? '')}</p>}
    <details><summary>{t('details')}</summary><pre>{typeof result === 'string' ? result : JSON.stringify(result, null, 2)}</pre></details>
  </div>;
}
function Dialog({ title, detail, confirm, cancel, onConfirm, onClose, secondary }: { title: string; detail: string; confirm: string; cancel: string; onConfirm: () => void; onClose: () => void; secondary?: { label: string; action: () => void } }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => { ref.current?.close(); previous?.focus(); };
  }, []);
  return <dialog ref={ref} aria-labelledby="confirmation-title" aria-describedby="confirmation-detail" onCancel={event => { event.preventDefault(); onClose(); }}>
    <span className="eyebrow">Easy Comfy Colab 2</span><h2 id="confirmation-title">{title}</h2><p id="confirmation-detail">{detail}</p>
    <div className="actions"><button autoFocus onClick={onClose}>{cancel}</button>{secondary ? <button onClick={secondary.action}>{secondary.label}</button> : null}<button className="danger" onClick={onConfirm}>{confirm}</button></div>
  </dialog>;
}

export default function App() {
  const offline = !window.easyComfy;
  const [data, setData] = useState<Snapshot>(emptySnapshot);
  const [language, setLanguage] = useState<Language>('pt-BR');
  const [page, setPage] = useState<Page>('session');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [pending, setPending] = useState<string[]>([]);
  const [message, setMessage] = useState<{ text: string; code?: string; error: boolean; page: Page } | null>(null);
  const [events, setEvents] = useState<{ id: number; event: string; message: string; phase?: string; time: number }[]>([]);
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [confirmation, setConfirmation] = useState<{ title: string; detail: string; label: string; action: () => void; secondary?: { label: string; action: () => void } } | null>(null);
  const [name, setName] = useState('');
  const [urls, setUrls] = useState('');
  const [category, setCategory] = useState('loras');
  const [parallel, setParallel] = useState(3);
  const [query, setQuery] = useState('');
  const [libraryPage, setLibraryPage] = useState<Paged<Model> | null>(null);
  const [downloadPage, setDownloadPage] = useState<Paged<Download> | null>(null);
  const [nodePage, setNodePage] = useState<Paged<NodePackage> | null>(null);
  const [installedNodePage, setInstalledNodePage] = useState<Paged<NodePackage> | null>(null);
  const [nodeQuery, setNodeQuery] = useState('');
  const [nodeVersions, setNodeVersions] = useState<(Paged<NodeVersion> & { id: string }) | null>(null);
  const [historyPage, setHistoryPage] = useState<Paged<HistoryEntry> | null>(null);
  const [historyAccount, setHistoryAccount] = useState('');
  const [driveWorkflows, setDriveWorkflows] = useState<Paged<DriveWorkflow> | null>(null);
  const [workflowSource, setWorkflowSource] = useState<'local' | 'drive'>('local');
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [warmRam, setWarmRam] = useState(false);
  const [ramLimit, setRamLimit] = useState(32);
  const [nodeId, setNodeId] = useState('');
  const [nodeVersion, setNodeVersion] = useState('');
  const nodeInput = useRef<HTMLInputElement>(null);
  const [workflowPath, setWorkflowPath] = useState('');
  const [workflowResult, setWorkflowResult] = useState<WorkflowEvidence | null>(null);
  const workflowIdentity = useRef({ account_id: null as string | null, session_id: null as string | null, path: '' });
  const pageState = useRef({ page: 'session' as Page, downloadOffset: null as number | null });
  const [destination, setDestination] = useState('');
  const [outputChoice, setOutputChoice] = useState('drive');
  const [folder, setFolder] = useState('');
  const [idleMinutes, setIdleMinutes] = useState(0);
  const [balanceAlert, setBalanceAlert] = useState(20);
  const [autoOpen, setAutoOpen] = useState(true);
  const mounted = useRef(true);
  const refreshActive = useRef(false);
  const inFlight = useRef(new Set<string>());
  const preferencesLoaded = useRef(false);
  const historyRequested = useRef(false);
  const autoOpened = useRef(new Set<string>());
  const currentLanguage = useRef(language);
  currentLanguage.current = language;
  const eventCounter = useRef(0);
  const closeRequest = useRef<() => void>(() => {});
  const t = (key: CopyKey) => translate(language, key);
  const account = data.accounts.find(item => item.id === data.selected_account_id);
  const session = data.session;
  pageState.current = { page, downloadOffset: downloadPage?.offset ?? null };
  workflowIdentity.current = { account_id: data.selected_account_id, session_id: session.session_id, path: workflowPath };
  const hasSession = session.state !== 'idle' || session.session_id != null;
  const sessionBusy = busyStates.includes(session.state);
  const ready = session.state === 'ready';
  const comfyReady = ready && session.comfy_url != null;
  const locked = pending.length > 0;
  const deferredQuery = useDeferredValue(query.trim().toLocaleLowerCase(language));
  const libraryModels = libraryPage?.items ?? data.models;
  const downloads = downloadPage?.items ?? data.downloads;
  const history = historyPage?.items ?? data.history;
  const nodePackages = nodePage?.items ?? data.packages;
  const basePackages = data.packages.filter(item => ['core', 'frontend', 'manager', 'mcp'].includes(item.id));
  const pendingBase = Array.isArray(data.capabilities.pending_base_packages) ? data.capabilities.pending_base_packages.filter((value): value is string => typeof value === 'string') : [];
  const initialInstalled = data.packages.filter(item => !['core', 'frontend', 'manager', 'mcp'].includes(item.id)).slice(0, 50);
  const installedNodes = [...basePackages, ...(installedNodePage?.items ?? initialInstalled)];
  const models = libraryModels.filter(item => `${item.name} ${item.category}`.toLocaleLowerCase(language).includes(deferredQuery));

  const handleAuth = useCallback((value: Params) => {
    if (value.completed === true) { setAuth(old => authMatches(old, value) ? null : old); return; }
    const purpose = value.purpose ?? 'colab';
    if (typeof value.account_id !== 'string' || !['colab', 'drive', 'mount_drive'].includes(String(purpose))) {
      setMessage({ text: 'Invalid authorization state.', code: 'invalid_state', error: true, page: 'session' });
      return;
    }
    const identity = { account_id: value.account_id, purpose: purpose as AuthState['purpose'], session_id: typeof value.session_id === 'string' ? value.session_id : undefined };
    if (typeof value.error === 'string' || typeof value.error_code === 'string') {
      setAuth({ ...identity, status: 'error', needs_code: false, needs_confirmation: false, error: typeof value.error === 'string' ? value.error : '', error_code: typeof value.error_code === 'string' ? value.error_code : 'oauth_callback_invalid' });
    } else if (validAuthorizationUrl(value.authorization_url) && (purpose !== 'mount_drive' || identity.session_id)) {
      setAuth({ ...identity, status: 'pending', authorization_url: value.authorization_url, needs_code: value.needs_code === true, needs_confirmation: value.needs_confirmation === true });
    } else {
      setAuth({ ...identity, status: 'error', needs_code: false, needs_confirmation: false, error_code: 'invalid_state' });
    }
  }, []);

  const refresh = useCallback(async (manual = false) => {
    if (refreshActive.current) return;
    refreshActive.current = true;
    if (manual) setRefreshing(true);
    try {
      const snapshot = normalizeSnapshot(await bridge().request<Snapshot>('app.snapshot'));
      if (!mounted.current) return;
      setData(snapshot);
      const paging = pageState.current;
      if (paging.page === 'models' && paging.downloadOffset != null && snapshot.session.state === 'ready' && inFlight.current.size === 0) {
        try {
          const result = await bridge().request<Paged<Download>>('download.list', { offset: paging.downloadOffset, limit: 50 });
          if (mounted.current && pageState.current.downloadOffset === result.offset && workflowIdentity.current.session_id === snapshot.session.session_id && workflowIdentity.current.account_id === snapshot.selected_account_id) setDownloadPage({ ...result, items: normalizeSnapshot({ downloads: result.items }).downloads });
        } catch (error) {
          if (mounted.current && errorCode(error) !== 'operation_busy') setMessage({ text: error instanceof Error ? error.message : String(error), code: errorCode(error), error: true, page: 'models' });
        }
      }
      if (!preferencesLoaded.current) {
        setLanguage(snapshot.preferences.language);
        setIdleMinutes(snapshot.preferences.idle_minutes);
        setBalanceAlert(snapshot.preferences.balance_alert);
        setAutoOpen(snapshot.preferences.auto_open);
        preferencesLoaded.current = true;
      }
    } catch (error) {
      if (mounted.current) setMessage({ text: error instanceof Error ? error.message : String(error), code: errorCode(error), error: true, page: 'session' });
    } finally {
      refreshActive.current = false;
      if (mounted.current) { setLoading(false); setRefreshing(false); }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const unsubscribe = bridge().subscribe((event: ControllerEvent) => {
      if (!mounted.current) return;
      if (event.event === 'app.close-request') { closeRequest.current(); return; }
      if (event.event === 'controller.unavailable') setMessage({ text: translate(currentLanguage.current, 'controllerStopped'), error: true, code: 'controller_unavailable', page: 'session' });
      if (event.event === 'state' && event.data.session) setData(old => ({ ...old, session: { ...old.session, ...event.data.session as Snapshot['session'] }, ...('output_delivery' in event.data ? { output_delivery: event.data.output_delivery as Snapshot['output_delivery'] } : {}) }));
      if (event.event === 'progress') setData(old => ({ ...old, session: { ...old.session, phase: String(event.data.phase ?? '') } }));
      if (event.event === 'auth') handleAuth(event.data);
      if (event.event === 'state' && event.data.connected === true) setAuth(old => authMatches(old, event.data) ? null : old);
      const description = String(event.data.message ?? event.data.phase ?? '');
      if (description) setEvents(old => [...old.slice(-199), { id: ++eventCounter.current, event: event.event, message: description, phase: typeof event.data.phase === 'string' ? event.data.phase : undefined, time: Date.now() }]);
      if (event.event !== 'progress') void refresh();
    });
    const timer = window.setInterval(() => { if (document.visibilityState !== 'hidden') void refresh(); }, 15_000);
    return () => { mounted.current = false; unsubscribe(); window.clearInterval(timer); };
  }, [refresh, handleAuth]);
  useEffect(() => { document.documentElement.lang = language; }, [language]);
  useEffect(() => { setOutputChoice(hasSession ? session.output_mode ?? account?.output_mode ?? 'drive' : account?.output_mode ?? 'drive'); }, [hasSession, session.output_mode, account?.id, account?.output_mode]);
  useEffect(() => { setLibraryPage(null); setDownloadPage(null); setNodePage(null); setInstalledNodePage(null); setNodeVersions(null); setDriveWorkflows(null); setSelectedModels([]); setWorkflowResult(null); setWorkflowPath(''); }, [data.selected_account_id, session.session_id]);
  useEffect(() => {
    if (!loading && !offline) {
      const frame = requestAnimationFrame(() => void bridge().request('shell.ready').catch(() => {}));
      return () => cancelAnimationFrame(frame);
    }
  }, [loading, offline]);
  useEffect(() => {
    const id = session.session_id;
    if (loading || offline || locked || !comfyReady || !id || !data.preferences.auto_open || autoOpened.current.has(id)) return;
    autoOpened.current.add(id);
    // The shell validates the URL returned by the owned controller; the renderer never supplies it.
    void bridge().request('session.open').catch(error => {
      if (mounted.current) setMessage({ text: error instanceof Error ? error.message : String(error), code: errorCode(error), error: true, page: 'session' });
    });
  }, [loading, offline, locked, comfyReady, session.session_id, data.preferences.auto_open]);
  useEffect(() => {
    if (page !== 'activity' || loading || offline || locked || historyRequested.current) return;
    historyRequested.current = true;
    void bridge().request<Paged<HistoryEntry>>('history.list', { offset: 0, limit: 50 }).then(result => {
      if (mounted.current && Array.isArray(result.items)) setHistoryPage(result);
    }).catch(error => { if (mounted.current) setMessage({ text: error instanceof Error ? error.message : String(error), code: errorCode(error), error: true, page: 'activity' }); });
  }, [page, loading, offline, locked]);

  async function run<T = unknown>(action: string, params: Params = {}): Promise<T | undefined> {
    if (inFlight.current.has(action)) return;
    inFlight.current.add(action); setPending([...inFlight.current]);
    setMessage(null);
    try {
      const result = await bridge().request<T>(action, params);
      if (!mounted.current) return result;
      if (action === 'account.connect' && result && typeof result === 'object' && 'authorization_url' in result) handleAuth({ ...params, ...result });
      if (action === 'account.submitCode') setAuth(old => authMatches(old, params) ? null : old);
      if (['node.install', 'node.update', 'node.remove'].includes(action)) { setNodePage(null); setInstalledNodePage(null); setNodeVersions(null); setNodeVersion(''); }
      if (action === 'legacy.import') { setLibraryPage(null); setDriveWorkflows(null); }
      if ((action === 'shell.pickFile' && result == null) || (['app.diagnostics', 'history.export'].includes(action) && (result as { saved?: boolean })?.saved === false)) return result;
      if (['legacy.list', 'legacy.import'].includes(action)) { await refresh(); return result; }
      setMessage({ text: t('done'), error: false, page });
      setEvents(old => [...old.slice(-199), { id: ++eventCounter.current, event: action, message: t('done'), time: Date.now() }]);
      await refresh();
      return result;
    } catch (error) {
      if (mounted.current) setMessage({ text: error instanceof Error ? error.message : String(error), code: errorCode(error), error: true, page });
    } finally { inFlight.current.delete(action); if (mounted.current) setPending([...inFlight.current]); }
  }
  closeRequest.current = () => {
    if (!hasSession && !locked) { void bridge().request('shell.close'); return; }
    setConfirmation({ title: t('closeTitle'), detail: t('closeHint'), label: t('stopAndClose'), action: async () => {
      setConfirmation(null);
      if (locked) { setMessage({ text: t('closeWait'), code: 'operation_busy', error: true, page }); return; }
      const result = await run('session.stop');
      if (result !== undefined) await bridge().request('shell.close');
    }, secondary: { label: t('leaveRunning'), action: () => { setConfirmation(null); void bridge().request('shell.close'); } } });
  };
  function confirm(action: string, params: Params, title: CopyKey, detail: CopyKey, label: CopyKey) {
    setConfirmation({ title: t(title), detail: t(detail), label: t(label), action: () => { setConfirmation(null); void run(action, params); } });
  }
  function updateAccount(changes: Params) { if (account) void run('account.update', { account_id: account.id, ...changes }); }
  function addAccount(event: FormEvent) { event.preventDefault(); if (name.trim()) void run('account.connect', { label: name.trim() }); }
  async function pickWorkflow() {
    setWorkflowResult(null);
    const path = await run<string | null>('shell.pickFile', { kind: 'workflow' });
    if (path) { setWorkflowPath(path); setWorkflowSource('local'); setWorkflowResult(null); }
  }
  async function inspectWorkflow(action: 'workflow.check' | 'workflow.import') {
    setWorkflowResult(null);
    const identity = { ...workflowIdentity.current };
    const result = await run(action, workflowSource === 'drive' ? { relative_path: identity.path } : { path: identity.path });
    const current = workflowIdentity.current;
    const receipt = typeof result === 'object' && result != null && 'identity' in result ? result.identity as { account_id?: string; session_id?: string } : undefined;
    if (receipt && (receipt.account_id !== identity.account_id || receipt.session_id !== identity.session_id)) {
      setMessage({ text: 'Workflow response belongs to another account or session.', code: 'identity_mismatch', error: true, page: 'workflows' });
      return;
    }
    if (result !== undefined && identity.account_id === current.account_id && identity.session_id === current.session_id && identity.path === current.path) setWorkflowResult({ value: result, ...identity });
  }
  async function listModels(offset = 0) {
    const result = await run<Paged<Model>>('model.list', { offset, limit: 50 });
    if (result && Array.isArray(result.items)) { setLibraryPage(result); setQuery(''); }
  }
  async function listDownloads(offset = 0) { const result = await run<Paged<Download>>('download.list', { offset, limit: 50 }); if (result && Array.isArray(result.items)) setDownloadPage({ ...result, items: normalizeSnapshot({ downloads: result.items }).downloads }); }
  async function listNodes(offset = 0) { const result = await run<Paged<NodePackage>>('node.catalog', { query: nodeQuery.trim(), offset, limit: 25 }); if (result && Array.isArray(result.items)) setNodePage(result); }
  async function listInstalledNodes(offset = 0) { const result = await run<Paged<NodePackage> & { packages: NodePackage[] }>('node.list', { offset, limit: 50 }); if (result && Array.isArray(result.items)) setInstalledNodePage({ ...result, items: result.packages.filter(item => !['core', 'frontend', 'manager', 'mcp'].includes(item.id)) }); }
  async function listVersions(id: string, offset = 0) {
    setNodeId(id); setNodeVersion(''); setNodeVersions(null);
    const result = await run<Paged<NodeVersion> & { id: string }>('node.versions', { id, offset, limit: 25 });
    if (result && Array.isArray(result.items)) { setNodeVersions(result); nodeInput.current?.focus(); }
  }
  async function listHistory(offset = 0, accountId = historyAccount) { const result = await run<Paged<HistoryEntry>>('history.list', { offset, limit: 50, ...(accountId ? { account_id: accountId } : {}) }); if (result && Array.isArray(result.items)) setHistoryPage(result); }
  async function listDriveWorkflows(offset = 0) { const result = await run<Paged<DriveWorkflow>>('workflow.list', { offset, limit: 25 }); if (result && Array.isArray(result.items)) setDriveWorkflows(result); }
  function operationLabel(operation?: string) {
    const labels: Record<string, CopyKey> = { 'session.start': 'start', 'session.stop': 'stop', 'session.restart': 'restart', 'session.reconnect': 'reconnect', 'session.recovery': 'reconnect', 'session.runtimeSave': 'preserveRuntime', 'session.freeMemory': 'freeMemory', 'account.connect': 'connect', 'account.submitCode': 'authTitle', 'account.importLegacy': 'importLegacy', 'account.remove': 'removeAccount', 'account.credentials': 'credentials', 'download.add': 'addModels', 'download.cancel': 'cancel', 'download.resume': 'resume', 'cache.prepare': 'prepareCache', 'cache.cancel': 'cancelCache', 'cache.clear': 'clearCache', 'node.install': 'install', 'node.update': 'update', 'node.remove': 'removePackage', 'workflow.check': 'checkWorkflow', 'workflow.import': 'importWorkflow', 'legacy.import': 'copyLegacyFile', 'drive.copy': 'copy', 'drive.share': 'share', 'app.preferences': 'preferences' };
    return t(labels[operation ?? ''] ?? 'activityUpdate');
  }
  const intro: Record<Page, CopyKey> = { session: 'sessionIntro', models: 'modelsIntro', nodes: 'nodesIntro', workflows: 'workflowsIntro', accounts: 'accountsIntro', activity: 'activityIntro', settings: 'settingsIntro' };
  const stateLabel = pages.includes(session.state as Page) ? t('unknown') : (['idle', 'authorizing', 'allocating', 'preparing', 'starting', 'ready', 'reconnecting', 'stopping', 'error'].includes(session.state) ? (ready && !comfyReady ? t('downloadsReady') : t(session.state as CopyKey)) : t('unknown'));
  const authAccount = data.accounts.find(item => item.id === auth?.account_id);
  const metric = (key: 'balance' | 'rate', suffix = '') => data.metrics[key] == null ? '—' : `${data.metrics[key]}${suffix}`;
  const hardware = typeof session.hardware === 'string' ? session.hardware : '—';
  const selectedNode = installedNodes.find(item => item.id === nodeId.trim()) ?? nodePackages.find(item => item.id === nodeId.trim());
  const selectedVersion = nodeVersions?.id === nodeId ? nodeVersions.items.find(item => item.version === nodeVersion) : undefined;
  const isProtectedNode = (item: Snapshot['packages'][number]) => item.protected || ['core', 'frontend', 'manager', 'mcp', 'easy_runtime_guard'].includes(item.id);
  const cacheLabel = data.cache.status === 'waiting' ? t('cacheWaiting') : data.cache.status && ['queued', 'complete', 'cancelled', 'copying', 'cached'].includes(data.cache.status) ? t(data.cache.status as CopyKey) : data.cache.status === 'error' ? t('failed') : data.cache.status === 'idle' ? t('idle') : t('cacheUnknown');

  return <div className="app-shell">
    <a className="skip" href="#main-content">{t('skip')}</a>
    <aside className="sidebar" aria-label={t('workspace')}>
      <div className="brand"><img className="brand-symbol" src={brandLogo} alt="" width={38} height={38} /><span>Easy Comfy<br />Colab <span className="muted">2</span></span></div>
      <span className="eyebrow sidebar-caption">{t('workspace')}</span>
      <nav>{pages.map((item, index) => <button key={item} className={page === item ? 'nav-item active' : 'nav-item'} aria-current={page === item ? 'page' : undefined} onClick={() => setPage(item)}><span className="nav-number" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>{t(item)}<span className="nav-arrow" aria-hidden="true">↗</span></button>)}</nav>
      <div className="sidebar-footer"><span className="status-square" aria-hidden="true" /><span>{offline ? t('preview') : t('native')}</span><span className="muted">v0.1</span></div>
    </aside>
    <div className="workspace">
      <header className="topbar"><div className="breadcrumb">Easy Comfy Colab <span aria-hidden="true">/</span> <strong>{t(page)}</strong></div><div className="top-actions"><label className="account-switch"><span className="sr-only">{t('chooseAccount')}</span><select value={account?.id ?? ''} disabled={locked || hasSession} onChange={event => void run('account.select', { account_id: event.target.value })}><option value="">{t('noAccount')}</option>{data.accounts.map(item => <option key={item.id} value={item.id}>{item.label} · {item.email ?? t('disconnected')}</option>)}</select></label><button className="compact" disabled={refreshing || locked} onClick={() => void refresh(true)} aria-label={t('refresh')}>{refreshing ? t('refreshing') : '↻'}<span className="refresh-word">{t('refresh')}</span></button></div></header>
      <main id="main-content" tabIndex={-1}>
        {offline ? <div className="preview-banner" role="note"><strong>{t('preview')}</strong><span>{t('previewHint')}</span></div> : null}
        <div className="page-heading"><div><span className="eyebrow">{t('workspace')} / {String(pages.indexOf(page) + 1).padStart(2, '0')}</span><h1>{t(page)}</h1><p>{t(intro[page])}</p></div><span className="page-tag">{offline ? t('preview') : stateLabel}</span></div>
        {loading ? <p role="status" className="loading">{t('loading')}</p> : null}
        <div className="operation-status" role="status" aria-live="polite">{locked ? t('busy') : ''}</div>
        {message && message.page === page ? <div className={`notice ${message.error ? 'notice-error' : ''}`} role={message.error ? 'alert' : 'status'}><div><strong>{message.error ? errorText(language, message.code) : message.text}</strong>{message.error ? <p>{t('recovery')}</p> : null}{message.error ? <details><summary>{t('details')}</summary>{message.code ? <code>{message.code}</code> : null}<p>{message.text}</p></details> : null}</div><button className="icon-button" aria-label={t('dismiss')} onClick={() => setMessage(null)}>×</button></div> : null}
        {auth ? <section className="auth-panel" aria-labelledby="auth-title"><div><span className="eyebrow">Google</span><h2 id="auth-title">{t(auth.status === 'error' ? 'authErrorTitle' : auth.purpose === 'mount_drive' ? 'authMountTitle' : auth.purpose === 'drive' ? 'authDriveTitle' : 'authTitle')}</h2><p><strong>{authAccount?.label ?? auth.account_id}</strong> · {authAccount?.email ?? t('disconnected')}</p>{auth.status === 'error' ? <div role="alert"><p>{errorText(language, auth.error_code)}</p>{auth.error ? <details><summary>{t('details')}</summary><p>{auth.error}</p></details> : null}</div> : <p>{t(auth.purpose === 'mount_drive' ? 'authMountHint' : 'authHint')}</p>}</div><div className="actions">{auth.authorization_url && auth.status === 'pending' ? <button disabled={pending.includes('shell.openExternal')} onClick={() => void run('shell.openExternal', { url: auth.authorization_url })}>{t('openAuth')} ↗</button> : null}{auth.needs_code ? <button className="primary" disabled={locked} onClick={() => void run('account.submitCode', { account_id: auth.account_id, purpose: auth.purpose })}>{t('submitCode')}</button> : null}{auth.needs_confirmation && auth.session_id ? <button className="primary" disabled={pending.includes('account.submitCode')} onClick={() => void run('account.submitCode', { account_id: auth.account_id, session_id: auth.session_id, purpose: 'mount_drive', confirmed: true })}>{t('confirmMount')}</button> : null}{auth.status === 'error' ? <button className="primary" disabled={locked} onClick={() => void run(auth.purpose === 'mount_drive' ? 'session.reconnect' : 'account.connect', { account_id: auth.account_id, purpose: auth.purpose === 'mount_drive' ? undefined : auth.purpose })}>{t(auth.purpose === 'mount_drive' ? 'reconnect' : 'retry')}</button> : null}</div></section> : null}

        <div hidden={page !== 'session'} className="page-content">
          <section className="session-hero" aria-label={t('session')}><div><div className="session-status"><span className={ready ? 'status-square solid' : 'status-square'} aria-hidden="true" />{account?.email ?? account?.label ?? 'Google Colab'}</div><h2>{account ? stateLabel : t('noAccount')}</h2><p role="status">{session.phase ? phaseText(language, session.phase) : (account?.connected ? t('gpuHint') : t('connectFirst'))}</p>{session.error ? <div role="alert" className="inline-error"><p>{errorText(language, session.error_code ?? (session.phase === 'recovery' ? 'recovery_unverified' : 'remote_operation_failed'))}</p><details><summary>{t('details')}</summary><p>{session.error}</p></details></div> : null}</div><div className="hero-actions">{!account ? <button className="primary" onClick={() => setPage('accounts')}>{t('addAccount')} →</button> : !account.connected ? <button className="primary" disabled={!account || locked} onClick={() => void run('account.connect', { account_id: account?.id, label: account?.label })}>{t('connect')}</button> : comfyReady ? <button className="primary" disabled={locked} onClick={() => void run('session.open')}>{t('openComfy')} ↗</button> : ready ? <span className="supporting">{t('cpuReadyHint')}</span> : <button className="primary" disabled={locked || sessionBusy} onClick={() => void run(hasSession ? 'session.reconnect' : 'session.start', { account_id: account.id, gpu: account.gpu, output_mode: account.output_mode })}>{hasSession ? t('reconnect') : t('start')} →</button>}{(sessionBusy && session.state !== 'stopping') || pending.includes('session.start') ? <button onClick={() => void run('session.cancel')}>{t('cancelStart')}</button> : hasSession ? <button className="danger-quiet" disabled={locked || session.state === 'stopping'} onClick={() => confirm('session.stop', {}, 'stopTitle', 'stopDetail', 'stop')}>{t('stop')}</button> : null}</div></section>
          {sessionBusy ? <p className="supporting">{t('cancelSafe')}</p> : null}
          {hasSession && session.output_mode === 'pc' && data.output_delivery?.status === 'error' ? <section className="notice notice-error" role="alert"><div><strong>{t('deliveryError')}</strong><p>{errorText(language, data.output_delivery.error_code ?? 'output_delivery_failed')}</p><p>{t('deliveryRetryHint')}</p><p>{t('pending')}: {session.pending_outputs == null ? '—' : session.pending_outputs}</p></div><div className="actions"><button disabled={locked || refreshing} onClick={() => void refresh(true)}>{t('refresh')}</button>{!ready ? <button disabled={locked || sessionBusy} onClick={() => void run('session.reconnect')}>{t('reconnect')}</button> : null}</div></section> : null}
          {hasSession && session.output_mode === 'pc' && data.output_delivery?.status === 'complete' ? <p className="supporting" role="status">{t('deliveryVerified')}{data.output_delivery.last_delivery_at ? ` · ${new Date(data.output_delivery.last_delivery_at * 1000).toLocaleString(language)}` : ''} · {t('pending')}: {session.pending_outputs == null ? '—' : session.pending_outputs}</p> : null}
          {account ? <><div className="metric-grid">{[['balance', t('balance'), metric('balance', ' CU')], ['rate', t('rate'), metric('rate', ' CU/h')], ['hardware', t('gpu'), hardware], ['outputs', t('pending'), session.pending_outputs == null ? '—' : String(session.pending_outputs)]].map(([key, label, value]) => <div className="metric" key={key}><span>{label}</span><strong>{value}</strong>{value === '—' ? <small>{t('noMetric')}</small> : null}</div>)}</div>
          <div className="two-columns"><Card title={t('hardware')}><Field label={t('hardware')}><select value={account?.gpu ?? 'T4'} disabled={!account || locked || hasSession} onChange={event => updateAccount({ gpu: event.target.value })}>{gpuChoices.map(gpu => <option key={gpu}>{gpu}</option>)}</select></Field><p className="supporting">{t('gpuHint')}</p></Card><Card title={t('output')}><Field label={t('output')}><select value={outputChoice} disabled={!account || locked || (hasSession && !comfyReady)} onChange={event => { const value = event.target.value; setOutputChoice(value); if (!hasSession) updateAccount({ output_mode: value }); }}><option value="drive">{t('drive')}</option><option value="pc">{t('pc')}</option></select></Field><p className="supporting">{t(outputChoice === 'pc' ? 'pcHint' : 'driveHint')}</p>{hasSession ? <p>{t('activeDestination')}: <strong>{session.output_mode ? t(session.output_mode === 'pc' ? 'pc' : 'drive') : t('unknown')}</strong><br /><small>{t(comfyReady ? 'nextDestinationLive' : 'nextDestination')}</small></p> : null}{comfyReady && outputChoice !== session.output_mode ? <button className="primary" disabled={locked} onClick={() => confirm('session.restart', { output_mode: outputChoice }, 'changeOutputTitle', 'changeOutputHint', 'applyOutput')}>{t('applyOutput')}</button> : null}</Card></div></> : null}
          {hasSession ? <details className="advanced"><summary>{t('maintenance')}</summary><p>{t('maintenanceHint')}</p><div className="actions"><button disabled={!comfyReady || locked} onClick={() => void run('session.restart')}>{t('restart')}</button><button disabled={!comfyReady || locked} onClick={() => void run('session.freeMemory')}>{t('freeMemory')}</button><button disabled={!ready || locked} onClick={() => void run('session.runtimeSave')}>{t('preserveRuntime')}</button></div></details> : null}
        </div>

        <div hidden={page !== 'models'} className="page-content">
          <Card title={t('addModels')}><form onSubmit={event => { event.preventDefault(); if (urls.trim()) void run('download.add', { urls: urls.split('\n').filter(line => line.trim()), category, parallel }); }}><Field label={t('urls')} hint={t('urlsHint')}><textarea required rows={3} value={urls} onChange={event => setUrls(event.target.value)} placeholder="https://huggingface.co/…/resolve/main/model.safetensors" /></Field><div className="form-row"><Field label={t('category')}><select value={category} onChange={event => setCategory(event.target.value)}>{categories.map(item => <option key={item}>{item}</option>)}</select></Field><Field label={t('parallels')}><input type="number" min={1} max={6} value={parallel} onChange={event => setParallel(Number(event.target.value))} /></Field><button className="primary" disabled={!ready || locked}>{t('enqueue')} →</button></div>{!ready ? <p className="supporting">{t('accountRequired')}</p> : null}</form></Card>
          <Card title={t('downloads')}>
            <div className="section-toolbar"><p>{t('queueHint')}</p><button disabled={!ready || locked} onClick={() => void listDownloads(downloadPage?.offset ?? 0)}>{t('refresh')}</button></div>
            {downloads.length ? <div className="table-scroll"><table><thead><tr><th>{t('name')}</th><th>{t('status')}</th><th>{t('progress')}</th><th>{t('actions')}</th></tr></thead><tbody>{downloads.map(job => <tr key={job.id}>
              <td><span className="file-name">{job.name}</span>{job.error ? <div className="inline-error"><p>{errorText(language, job.error_code ?? 'download_failed')}</p><details><summary>{t('details')}</summary><p>{job.error}</p></details></div> : null}</td>
              <td>{(['queued', 'running', 'complete', 'cancelled', 'cancelling', 'interrupted', 'paused'].includes(job.status) ? t(job.status as CopyKey) : job.status === 'error' ? t('failed') : t('unknown'))}</td>
              <td className="numeric">{formatBytes(job.bytes, language)} / {formatBytes(job.total, language)}</td>
              <td>{job.status === 'complete' ? <span>{t('complete')}</span> : <button disabled={!ready || locked || !['running', 'queued', 'cancelled', 'interrupted', 'paused', 'error'].includes(job.status)} aria-label={`${t(['running', 'queued'].includes(job.status) ? 'cancel' : 'resume')} ${job.name}`} onClick={() => void run(['running', 'queued'].includes(job.status) ? 'download.cancel' : 'download.resume', { job_id: job.id })}>{t(['running', 'queued'].includes(job.status) ? 'cancel' : 'resume')}</button>}</td>
            </tr>)}</tbody></table></div> : <Empty title={t('noDownloads')} hint={t('noDownloadsHint')} />}
            <Pagination page={downloadPage ?? { items: data.downloads, total: data.downloads_total ?? data.downloads.length, offset: 0, limit: 50 }} noun={t('downloadCount')} disabled={!ready || locked} t={t} onPage={offset => void listDownloads(offset)} />
          </Card>
          <Card title={t('library')}><div className="search-row"><Field label={t('search')}><input type="search" value={query} onChange={event => setQuery(event.target.value)} /></Field>{query ? <button onClick={() => setQuery('')}>{t('clearSearch')}</button> : null}<button disabled={!ready || locked} onClick={() => void listModels(libraryPage?.offset ?? 0)}>{t('refresh')}</button></div>{models.length ? <div className="table-scroll"><table><thead><tr><th><span className="sr-only">{t('selectModel')}</span></th><th>{t('name')}</th><th>{t('category')}</th><th>{t('size')}</th></tr></thead><tbody>{models.map(item => { const id = item.id ?? `${item.category}/${item.name}`; return <tr key={id}><td><input type="checkbox" aria-label={`${t('selectModel')} ${item.name}`} checked={selectedModels.includes(id)} onChange={event => setSelectedModels(old => event.target.checked ? [...old, id] : old.filter(value => value !== id))} /></td><td className="file-name">{item.name}</td><td>{item.category}</td><td className="numeric">{formatBytes(item.bytes ?? item.size, language)}</td></tr>; })}</tbody></table></div> : <Empty title={t(query ? 'noResults' : 'noModels')} hint={t(query ? 'clearSearch' : 'noModelsHint')} />}
            {libraryPage ? <div className="section-toolbar pagination"><span>{libraryPage.total ? `${libraryPage.offset + 1}–${Math.min(libraryPage.offset + libraryPage.limit, libraryPage.total)} / ${libraryPage.total}` : '0'} · {t('modelCount')}</span><div className="actions"><button disabled={locked || !ready || libraryPage.offset === 0} onClick={() => void listModels(Math.max(0, libraryPage.offset - libraryPage.limit))}>{t('previousPage')}</button><button disabled={locked || !ready || libraryPage.offset + libraryPage.limit >= libraryPage.total} onClick={() => void listModels(libraryPage.offset + libraryPage.limit)}>{t('nextPage')}</button></div></div> : <p className="supporting">{t('libraryPageHint')}</p>}
            <details className="advanced"><summary>{t('cache')}</summary><p>{t('cacheHint')}</p><div className="form-row"><label className="check-field"><input type="checkbox" checked={warmRam} onChange={event => setWarmRam(event.target.checked)} />{t('warmRam')}</label><Field label={t('ramLimit')}><input type="number" min={1} max={128} value={ramLimit} onChange={event => setRamLimit(Number(event.target.value))} /></Field></div><div className="actions"><button disabled={!ready || locked || selectedModels.length === 0} onClick={() => void run('cache.prepare', { models: selectedModels, warm_ram: warmRam, ram_gib: ramLimit })}>{t('prepareCache')}</button><button disabled={!ready || locked} onClick={() => void run('cache.cancel')}>{t('cancelCache')}</button><button disabled={!ready || locked} onClick={() => void run('cache.clear')}>{t('clearCache')}</button></div></details>
          </Card>
          <Card title={t('cacheProgress')}><p role="status">{cacheLabel}</p>{data.cache.requires_restart ? <p>{t('cacheRestart')}</p> : null}{data.cache.error ? <div className="inline-error" role="alert"><p>{errorText(language, data.cache.error_code ?? 'cache_operation_failed')}</p><details><summary>{t('details')}</summary><p>{data.cache.error}</p></details></div> : null}
            {data.cache.items.length ? <div className="table-scroll"><table><thead><tr><th>{t('name')}</th><th>{t('status')}</th><th>{t('progress')}</th><th>{t('warmBytes')}</th></tr></thead><tbody>{data.cache.items.map(item => <tr key={item.path}><td className="file-name">{item.path}</td><td>{item.status && ['queued', 'copying', 'cached', 'complete', 'cancelled'].includes(item.status) ? t(item.status as CopyKey) : t('unknown')}</td><td>{formatBytes(item.bytes, language)}</td><td>{formatBytes(item.warm_bytes, language)}</td></tr>)}</tbody></table></div> : null}
          </Card>
        </div>

        <div hidden={page !== 'nodes'} className="page-content">
          {pendingBase.length ? <Card title={t('pendingBasePackages')}><p>{t('pendingBaseHint')}</p><p>{pendingBase.join(', ')}</p></Card> : null}
          <Card title={t('registry')}><form className="search-row" onSubmit={event => { event.preventDefault(); void listNodes(); }}><Field label={t('registrySearch')}><input type="search" value={nodeQuery} onChange={event => setNodeQuery(event.target.value)} /></Field><button className="primary" disabled={!comfyReady || locked}>{t('searchRegistry')}</button></form><p>{t('registryHint')}</p>
            {nodePage ? <><div className="package-list">{nodePage.items.map(item => <article className="package" key={item.id}><div><span className="eyebrow">{item.installed ? t('installed') : t('registry')}</span><h3>{item.name ?? item.id}</h3><small>{item.id}</small>{item.description ? <p>{item.description}</p> : null}<p>{t(item.compatibility?.state === 'compatible' ? 'compatible' : item.compatibility?.state === 'incompatible' ? 'incompatible' : 'compatibilityUnknown')}</p></div><button disabled={!comfyReady || locked} onClick={() => void listVersions(item.id)}>{t('chooseVersion')}</button></article>)}</div>{!nodePage.items.length ? <p>{t('noPackages')}</p> : null}<Pagination page={nodePage} noun={t('packageCount')} disabled={!comfyReady || locked} t={t} onPage={offset => void listNodes(offset)} /></> : null}
          </Card>
          <Card title={t('addPackage')}><form onSubmit={event => { event.preventDefault(); if (nodeId.trim() && nodeVersion.trim()) void run(selectedNode?.installed ? 'node.update' : 'node.install', { package_id: nodeId.trim(), version: nodeVersion.trim(), expected_version: selectedNode?.version }); }}><p>{t('nodeVersionHint')}</p><div className="two-columns">
            <Field label={t('nodeId')}><input ref={nodeInput} disabled={locked} required pattern="[a-zA-Z0-9_-]{1,128}" value={nodeId} onChange={event => { setNodeId(event.target.value); setNodeVersions(null); setNodeVersion(''); }} placeholder="comfyui-example" /></Field>
            <Field label={t('targetVersion')} hint={nodeVersions ? t('versionPageHint') : undefined}>{nodeVersions ? <select disabled={locked} required value={nodeVersion} onChange={event => setNodeVersion(event.target.value)}><option value="">{t('chooseVersion')}</option>{nodeVersions.items.map(item => <option key={item.version} value={item.version} disabled={item.compatibility?.state === 'incompatible'}>{item.version} · {t(item.compatibility?.state === 'compatible' ? 'compatible' : item.compatibility?.state === 'incompatible' ? 'incompatible' : 'compatibilityUnknown')}</option>)}</select> : <input required pattern="[0-9]+[.][0-9]+[.][0-9]+([+][a-zA-Z0-9.-]+)?" value={nodeVersion} onChange={event => setNodeVersion(event.target.value)} placeholder="1.0.0" />}</Field>
          </div>{selectedVersion ? <div><p>{t(selectedVersion.compatibility?.state === 'compatible' ? 'compatible' : selectedVersion.compatibility?.state === 'incompatible' ? 'incompatible' : 'compatibilityUnknown')}</p>{selectedVersion.compatibility?.reason ? <details><summary>{t('details')}</summary>{selectedVersion.compatibility.reason}</details> : null}</div> : null}
          <div className="actions"><button type="button" disabled={!comfyReady || locked || !nodeId.trim()} onClick={() => void listVersions(nodeId.trim())}>{t('chooseVersion')}</button><button className="primary" disabled={!comfyReady || locked || selectedVersion?.compatibility?.state === 'incompatible' || !!(selectedNode && isProtectedNode(selectedNode))}>{t(selectedNode?.installed ? 'update' : 'install')}</button></div>
          {nodeVersions ? <><Pagination page={nodeVersions} noun={t('version')} disabled={!comfyReady || locked} t={t} onPage={offset => void listVersions(nodeId, offset)} />{!nodeVersions.items.length ? <p>{t('noVersions')}</p> : null}</> : null}</form></Card>
          <Card title={t('installedPackages')}><div className="section-toolbar"><p>{t('noPackagesHint')}</p><button disabled={!comfyReady || locked} onClick={() => void listInstalledNodes(installedNodePage?.offset ?? 0)}>{t('refresh')}</button></div>{installedNodes.length ? <div className="package-list">{installedNodes.map(item => <article className="package" key={item.id}><div><span className="eyebrow">{isProtectedNode(item) ? t('basePackage') : t('installed')}</span><h3>{item.name ?? item.id}</h3><small>{t('version')}: {item.version ?? '—'}</small></div>{!isProtectedNode(item) ? <div className="actions"><button disabled={!comfyReady || locked} onClick={() => void listVersions(item.id)}>{t('update')}</button><button disabled={!comfyReady || locked} onClick={() => confirm('node.remove', { package_id: item.id, expected_version: item.version }, 'removePackage', 'removePackageHint', 'remove')}>{t('remove')}</button></div> : null}</article>)}</div> : <Empty title={t('noPackages')} hint={t('noPackagesHint')} />}<Pagination page={installedNodePage ?? { items: initialInstalled, total: Math.max(0, (data.packages_total ?? data.packages.length) - basePackages.length), offset: 0, limit: 50 }} noun={t('packageCount')} disabled={!comfyReady || locked} t={t} onPage={offset => void listInstalledNodes(offset)} /></Card>
        </div>

        <div hidden={page !== 'workflows'} className="page-content">
          <Card title={t('driveWorkflows')}><p>{t('driveWorkflowHint')}</p><button disabled={!ready || locked} onClick={() => void listDriveWorkflows(driveWorkflows?.offset ?? 0)}>{t('browseWorkflows')}</button>{driveWorkflows ? <><div className="package-list">{driveWorkflows.items.map(item => <article className="package" key={item.relative_path}><div><h3>{item.name}</h3><small>{formatBytes(item.size, language)}</small></div><button disabled={locked} aria-label={`${t('selectWorkflow')} ${item.name}`} onClick={() => { setWorkflowResult(null); setWorkflowPath(item.relative_path); setWorkflowSource('drive'); }}>{t('selectWorkflow')}</button></article>)}</div><Pagination page={driveWorkflows} noun={t('workflowCount')} disabled={!ready || locked} t={t} onPage={offset => void listDriveWorkflows(offset)} /></> : null}</Card>
          <Card title={t('workflows')}><p>{t('workflowHint')}</p><button className="primary" disabled={locked} onClick={() => void pickWorkflow()}>{t('chooseWorkflow')} →</button>{workflowPath ? <><p>{t(workflowSource === 'drive' ? 'driveWorkflows' : 'localWorkflow')}</p><Field label={t('workflowPath')}><input readOnly value={workflowPath} /></Field><div className="actions"><button disabled={!comfyReady || locked} onClick={() => void inspectWorkflow('workflow.check')}>{t('checkWorkflow')}</button>{workflowSource === 'local' ? <button disabled={!ready || locked} onClick={() => void inspectWorkflow('workflow.import')}>{t('importWorkflow')}</button> : null}</div></> : <Empty title={t('noWorkflow')} hint={t('noWorkflowHint')} />}{workflowResult != null && workflowResult.account_id === data.selected_account_id && workflowResult.session_id === session.session_id && workflowResult.path === workflowPath ? <WorkflowResult result={workflowResult.value} t={t} /> : null}</Card>
        </div>

        <div hidden={page !== 'accounts'} className="page-content"><Card title={t('accounts')}><p>{t('accountsHint')}</p>{data.accounts.length ? <div className="account-list">{data.accounts.map(item => <article className="account-card" key={item.id}><div><h3>{item.label}</h3><p>{item.email ?? t('disconnected')}</p><small>{item.connected ? t('connected') : t('disconnected')}{item.id === account?.id ? ` · ${t('currentAccount')}` : ''}</small></div><div className="actions"><button disabled={locked} onClick={() => void run('account.connect', { account_id: item.id, label: item.label, purpose: 'colab' })}>{t('connect')}</button><button disabled={locked || !item.connected} onClick={() => void run('account.connect', { account_id: item.id, purpose: 'drive' })}>{t('connectDrive')}</button><button disabled={locked || hasSession || item.id === account?.id || !item.connected} onClick={() => void run('account.select', { account_id: item.id })}>{t('useAccount')}</button><button disabled={locked || (hasSession && session.account_id === item.id)} onClick={() => confirm('account.remove', { account_id: item.id }, 'removeAccount', 'removeAccountHint', 'remove')}>{t('remove')}</button></div></article>)}</div> : <Empty title={t('noAccount')} hint={t('noAccountsHint')} />}<form className="add-account" onSubmit={addAccount}><Field label={t('accountName')}><input required value={name} onChange={event => setName(event.target.value)} placeholder={t('accountNameExample')} maxLength={80} /></Field><button className="primary" disabled={locked}>{t('addAccount')} +</button></form></Card>
          <Card title={t('credentials')}><p>{t('credentialsHint')}</p><div className="actions">{['Hugging Face', 'Civitai'].map(provider => <button key={provider} disabled={!account || locked} onClick={() => void run('account.credentials', { account_id: account?.id, provider })}>{provider} · {t('configureCredential')}</button>)}</div></Card>
          <details className="advanced"><summary>{t('transfer')}</summary><p>{t('copyHint')}</p><div className="two-columns"><Field label={t('source')}><input readOnly value={account?.email ?? account?.label ?? t('noAccount')} /></Field><Field label={t('destination')}><select value={destination} onChange={event => setDestination(event.target.value)}><option value="">{t('chooseAccount')}</option>{data.accounts.filter(item => item.id !== account?.id).map(item => <option key={item.id} value={item.id}>{item.label} · {item.email ?? t('disconnected')}</option>)}</select></Field></div><Field label={t('folder')} hint={t('folderHint')}><input value={folder} onChange={event => setFolder(event.target.value)} /></Field><div className="actions"><button disabled={!account || !destination || locked} onClick={() => confirm('drive.copy', { source_account_id: account?.id, destination_account_id: destination, folder_id: folder.trim() || undefined }, 'copyTitle', 'copyHint', 'copy')}>{t('copy')}</button><button disabled={!account || !destination || locked} onClick={() => confirm('drive.share', { source_account_id: account?.id, destination_account_id: destination, folder_id: folder.trim() || undefined }, 'shareTitle', 'copyHint', 'share')}>{t('share')}</button></div></details>
          <details className="advanced"><summary>{t('legacy')}</summary><p>{t('legacyHint')}</p><button disabled={locked || hasSession} onClick={() => confirm('account.importLegacy', { all_accounts: true, confirmed: true }, 'legacyTitle', 'legacyHint', 'importLegacy')}>{t('importLegacy')}</button></details>
          <LegacyLibrary accountId={data.selected_account_id} sessionId={session.session_id} ready={ready && session.account_id === data.selected_account_id} locked={locked} language={language} categories={categories} request={run} t={t} />
        </div>

        <div hidden={page !== 'activity'} className="page-content">
          <Card title={t('monitor')}><p>{t('monitorHint')}</p><div className="two-columns"><section><h3>{t('monitorLocal')}</h3><ResourceReadings readings={data.local_metrics} language={language} t={t} local /></section><section><h3>{t('monitorRemote')}</h3><ResourceReadings readings={data.metrics} language={language} t={t} /></section></div></Card>
          <Card title={t('activity')}><div className="section-toolbar"><p>{t('noActivityHint')}</p><button disabled={locked} onClick={() => void run('app.diagnostics')}>{t('diagnostics')}</button></div>{events.length ? <ol className="activity-list">{events.map(event => <li key={event.id}><time>{new Date(event.time).toLocaleTimeString(language)}</time><span>{event.phase ? phaseText(language, event.phase) : event.event.includes('.') && event.event !== 'controller.unavailable' ? t('done') : event.event === 'controller.unavailable' ? t('controllerStopped') : t('activityUpdate')}<details><summary>{t('details')}</summary><p>{event.message}</p></details></span></li>)}</ol> : <Empty title={t('noActivity')} hint={t('noActivityHint')} />}</Card>
          <Card title={t('history')}><form className="search-row" onSubmit={event => { event.preventDefault(); void listHistory(); }}><Field label={t('historyAccount')}><select disabled={locked} value={historyAccount} onChange={event => { const value = event.target.value; setHistoryAccount(value); void listHistory(0, value); }}><option value="">{t('allAccounts')}</option>{data.accounts.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></Field><button disabled={locked}>{t('refresh')}</button><button type="button" disabled={locked || offline} onClick={() => void run('history.export', historyAccount ? { account_id: historyAccount } : {})}>{t('exportHistory')}</button></form><p>{t('historyHint')}</p><p className="supporting">{t('exportHistoryHint')}</p>
            {history.length ? <div className="table-scroll"><table><thead><tr><th>{t('date')}</th><th>{t('account')}</th><th>{t('operation')}</th><th>{t('duration')}</th><th>{t('result')}</th></tr></thead><tbody>{history.map((item, index) => { const duration = item.duration_seconds ?? item.elapsed_seconds ?? item.details?.duration_seconds ?? item.details?.elapsed_seconds; return <tr key={item.id ?? `${item.time}-${index}`}><td className="numeric">{item.time ? new Date(item.time < 1e12 ? item.time * 1000 : item.time).toLocaleString(language) : '—'}</td><td>{data.accounts.find(value => value.id === item.account_id)?.label ?? item.account_id ?? '—'}</td><td>{operationLabel(item.action ?? item.operation)}</td><td className="numeric">{typeof duration === 'number' ? `${duration.toLocaleString(language, { maximumFractionDigits: 1 })} s` : '—'}</td><td>{t(item.result === 'complete' || item.result === 'success' ? 'complete' : item.result === 'error' || item.result === 'failed' ? 'failed' : 'activityUpdate')}{item.error_code || typeof item.details?.error_code === 'string' ? <p>{errorText(language, item.error_code ?? String(item.details?.error_code))}</p> : null}<details><summary>{t('recordDetails')}</summary><pre>{JSON.stringify({ operation: item.action ?? item.operation, result: item.result, session_id: item.session_id, ...item.details }, null, 2)}</pre></details></td></tr>; })}</tbody></table></div> : <p className="supporting">{t('noActivity')}</p>}
            <Pagination page={historyPage ?? { items: data.history, total: data.history.length, offset: 0, limit: 50 }} noun={t('historyCount')} disabled={locked} t={t} onPage={offset => void listHistory(offset)} />
          </Card>
        </div>

        <div hidden={page !== 'settings'} className="page-content"><Card title={t('language')}><Field label={t('language')}><select value={language} onChange={event => { const value = event.target.value as Language; setLanguage(value); if (!offline) void run('app.preferences', { language: value }); }}><option value="pt-BR">Português (Brasil)</option><option value="en">English</option></select></Field></Card><Card title={t('preferences')}><form onSubmit={event => { event.preventDefault(); void run('app.preferences', { idle_minutes: idleMinutes, balance_alert: balanceAlert, auto_open: autoOpen }); }}><div className="two-columns"><Field label={t('idleMinutes')} hint={t('idleHint')}><input type="number" required min={0} max={1440} value={idleMinutes} onChange={event => setIdleMinutes(Number(event.target.value))} /></Field><Field label={t('balanceAlert')}><input type="number" required min={0} max={100000} step="any" value={balanceAlert} onChange={event => setBalanceAlert(Number(event.target.value))} /></Field></div><label className="check-field"><input type="checkbox" checked={autoOpen} onChange={event => setAutoOpen(event.target.checked)} />{t('autoOpen')}</label><button disabled={locked} className="primary">{t('savePreferences')}</button></form></Card><Card title={t('security')}><p>{t('securityHint')}</p></Card></div>
        <footer className="content-footer"><span>Easy Comfy Colab 2</span><span>{offline ? t('preview') : `${t('session')} · ${account?.label ?? '—'}`}</span></footer>
      </main>
    </div>
    {confirmation ? <Dialog {...confirmation} confirm={confirmation.label} cancel={t('cancel')} onConfirm={confirmation.action} onClose={() => setConfirmation(null)} /> : null}
  </div>;
}
