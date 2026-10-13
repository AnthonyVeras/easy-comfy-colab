export type Language = 'pt-BR' | 'en';
export type Params = Record<string, unknown>;
export interface ControllerEvent { event: string; data: Params }
export interface Bridge {
  request<T = unknown>(action: string, params?: Params): Promise<T>;
  subscribe(listener: (event: ControllerEvent) => void): () => void;
}
declare global { interface Window { easyComfy?: Bridge; __TAURI_INTERNALS__?: unknown } }
export interface Account { id: string; label: string; email: string | null; connected: boolean; gpu: string; output_mode: string }
export interface Session { state: string; account_id: string | null; session_id: string | null; phase: string | null; started_at: number | null; comfy_url: string | null; mcp_url: string | null; hardware: string | null; output_mode: string | null; pending_outputs: number | null; error: string | null; error_code?: string | null }
export interface Model { id?: string; name: string; category: string; bytes?: number; size?: number; path?: string }
export interface Download { id: string; name: string; status: string; bytes?: number; total?: number | null; speed?: number | null; error?: string | null; error_code?: string | null }
export interface Paged<T> { items: T[]; total: number; offset: number; limit: number }
export interface Compatibility { state: 'compatible' | 'unknown' | 'incompatible'; reason?: string }
export interface NodePackage { id: string; name?: string; description?: string; status?: string; version?: string; installed?: boolean; protected?: boolean; latest_version?: string; compatibility?: Compatibility }
export interface NodeVersion { version: string; compatibility?: Compatibility }
export interface DriveWorkflow { name: string; relative_path: string; remote_path: string; size: number; modified?: number }
export interface LegacyFile { id: string; kind: 'model' | 'workflow'; category?: string; source_category?: string; name: string; relative_path: string; size: number; modified?: number }
export interface LegacyReceipt { imported: boolean; kind: 'model' | 'workflow'; category?: string; name: string; newrelative_path: string; remote_path: string; size: number; sha256: string; unchanged: boolean; source_preserved: boolean }
export interface CacheState { busy: boolean | null; status: string | null; error?: string | null; error_code?: string | null; items: { path: string; bytes?: number; warm_bytes?: number; status?: string }[]; requires_restart?: boolean }
export interface DeviceMetrics { name?: string; type?: string; vram_total?: number; vram_free?: number }
export interface Metrics { cpu_percent?: number | null; ram_used?: number | null; ram_total?: number | null; disk_free?: number | null; gpu?: DeviceMetrics[] | null; balance?: number | null; rate?: number | null; controller_rss?: number | null; controller_cpu_percent?: number | null; sampled_at?: number | null }
export interface AccountUsage { account_id: string | null; balance: number | null; rate: number | null; status: 'idle' | 'loading' | 'ready' | 'error'; checked_at: number | null; error_code: string | null }
export interface HistoryEntry { id?: string; time?: number; action?: string; operation?: string; account_id?: string; session_id?: string; result?: string; message?: string; duration_seconds?: number; elapsed_seconds?: number; error_code?: string; details?: Record<string, unknown> }
export interface OutputDelivery { status: 'complete' | 'error'; error_code?: string; delivered?: number; last_delivery_at?: number }
export interface Snapshot {
  accounts: Account[]; selected_account_id: string | null; session: Session; downloads: Download[];
  models: Model[]; packages: NodePackage[]; history: HistoryEntry[];
  preferences: { language: Language; idle_minutes: number; balance_alert: number; auto_open: boolean };
  metrics: Metrics; local_metrics: Metrics; cache: CacheState; account_usage?: AccountUsage; output_delivery?: OutputDelivery | null; downloads_total?: number; packages_total?: number; capabilities: Record<string, unknown>;
}
