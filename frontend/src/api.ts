import type { Bridge, Params, Snapshot } from './types';

export class ControllerError extends Error {
  constructor(public code: string, message: string) { super(message); this.name = 'ControllerError'; }
}
export const emptySnapshot = (): Snapshot => ({
  accounts: [], selected_account_id: null,
  session: { state: 'idle', account_id: null, session_id: null, phase: null, started_at: null, comfy_url: null, mcp_url: null, hardware: null, output_mode: null, pending_outputs: null, error: null },
  downloads: [], models: [], packages: [], history: [],
  preferences: { language: 'pt-BR', idle_minutes: 0, balance_alert: 20, auto_open: true },
  metrics: {}, local_metrics: {}, cache: { busy: null, status: null, items: [] }, capabilities: {},
});
export function normalizeSnapshot(raw: Partial<Snapshot>): Snapshot {
  const base = emptySnapshot();
  return { ...base, ...raw, session: { ...base.session, ...raw.session }, preferences: { ...base.preferences, ...raw.preferences },
    accounts: Array.isArray(raw.accounts) ? raw.accounts : [], downloads: Array.isArray(raw.downloads) ? raw.downloads.map(job => {
      const fault = typeof job.error === 'object' && job.error != null ? job.error as { code?: string; message?: string } : null;
      return fault ? { ...job, error: String(fault.message ?? ''), error_code: fault.code } : job;
    }) : [],
    models: Array.isArray(raw.models) ? raw.models : [], packages: Array.isArray(raw.packages) ? raw.packages : [],
    history: Array.isArray(raw.history) ? raw.history : [], metrics: raw.metrics ?? {}, local_metrics: raw.local_metrics ?? {},
    cache: { ...base.cache, ...raw.cache, items: Array.isArray(raw.cache?.items) ? raw.cache.items : [] }, capabilities: raw.capabilities ?? {} };
}
export const offlineBridge: Bridge = {
  async request<T>(action: string): Promise<T> {
    if (action === 'app.snapshot') return emptySnapshot() as T;
    throw new ControllerError('offline_preview', 'Abra o aplicativo desktop para conectar uma conta. / Open the desktop app to connect an account.');
  },
  subscribe: () => () => {},
};
export function bridge(): Bridge { return window.easyComfy ?? offlineBridge; }

export async function installTauriBridge(): Promise<void> {
  if (!window.__TAURI_INTERNALS__ || window.easyComfy) return;
  const [{ invoke }, { listen }] = await Promise.all([import('@tauri-apps/api/core'), import('@tauri-apps/api/event')]);
  const listeners = new Set<Parameters<Bridge['subscribe']>[0]>();
  const pending = new Map<string, { resolve: (value: unknown) => void; reject: (error: Error) => void; timeout: ReturnType<typeof setTimeout> }>();
  const unlisten = await listen<{ id?: string; ok?: boolean; result?: unknown; error?: { code: string; message: string }; event?: string; data?: Params }>('controller-message', ({ payload }) => {
    if (payload.id && pending.has(payload.id)) {
      const request = pending.get(payload.id)!; pending.delete(payload.id); clearTimeout(request.timeout);
      if (payload.ok) request.resolve(payload.result);
      else request.reject(new ControllerError(payload.error?.code ?? 'controller_error', payload.error?.message ?? 'Controller unavailable. Reopen the app.'));
    } else if (payload.event) {
      if (payload.event === 'controller.unavailable') {
        for (const request of pending.values()) { clearTimeout(request.timeout); request.reject(new ControllerError('controller_unavailable', 'O controlador parou. Reabra o aplicativo para reconectar com segurança. / The controller stopped. Reopen the app to reconnect safely.')); }
        pending.clear();
      }
      for (const listener of listeners) listener({ event: payload.event, data: payload.data ?? {} });
    }
  });
  window.easyComfy = {
    request<T>(action: string, params: Params = {}): Promise<T> {
      if (action === 'shell.pickFile') {
        if (!['workflow', 'training-dataset'].includes(String(params.kind))) return Promise.reject(new ControllerError('invalid_file_kind', 'Escolha um workflow JSON. / Select a workflow JSON file.'));
        return import('@tauri-apps/plugin-dialog').then(({ open }) => open({ multiple: false, directory: false, filters: params.kind === 'training-dataset' ? [{ name: 'Training dataset', extensions: ['zip'] }] : [{ name: 'ComfyUI workflow', extensions: ['json'] }] })) as Promise<T>;
      }
      const id = crypto.randomUUID();
      return new Promise<T>((resolve, reject) => {
        const timeout = setTimeout(() => { pending.delete(id); reject(new ControllerError('request_timeout', 'A operação não respondeu. Confira a atividade e reconecte. / Check Activity and reconnect.')); }, ['legacy.import', 'training.import'].includes(action) ? 1_260_000 : 600_000);
        pending.set(id, { resolve: value => resolve(value as T), reject, timeout });
        invoke('controller_request', { request: { id, action, params } }).catch(error => {
          clearTimeout(timeout); pending.delete(id); reject(new ControllerError('controller_unavailable', String(error)));
        });
      });
    },
    subscribe(listener) { listeners.add(listener); return () => { listeners.delete(listener); }; },
  };
  window.addEventListener('beforeunload', () => { unlisten(); for (const p of pending.values()) clearTimeout(p.timeout); pending.clear(); }, { once: true });
}
