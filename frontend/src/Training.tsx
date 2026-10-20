import { useEffect, useRef, useState } from 'react';
import { bridge } from './api';
import { errorCode, errorText } from './messages';
import type { Language, Params } from './types';

type Recipe = { name: string; trigger: string; steps: number; rank: number; resolution: number; lr: number; seed: number; save_every: number; profile: string };
type Project = { id: string; settings: Recipe; dataset_sha256: string; run: string };
type Status = { phase: string; busy: boolean; preserved: boolean; project_id?: string; error?: string; projects: Project[]; checkpoints: string[]; metrics?: { step: number; loss: number; step_time: number }; eta_seconds?: number; gpu?: { name: string; vram_bytes: number } };
const defaults: Recipe = { name: 'identity', trigger: 'hrn_person', steps: 500, rank: 16, resolution: 768, lr: 0.0001, seed: 42, save_every: 100, profile: 'auto' };
const copy = {
  project: ['Projeto reutilizável', 'Reusable project'], saved: ['Projetos no Drive', 'Drive projects'], fresh: ['Novo projeto', 'New project'], name: ['Nome (letras, números, _ e -)', 'Name (letters, numbers, _ and -)'], trigger: ['Palavra de ativação nas legendas', 'Trigger in captions'],
  dataset: ['Fotos e legendas', 'Images and captions'], datasetHint: ['Escolha um ZIP com uma pasta de imagens e legendas .txt individuais. O envio copia o arquivo para o Drive da conta conectada. Limite: 1 GiB. O trigger precisa aparecer em todas as legendas.', 'Choose a ZIP with one image folder and individual .txt captions. Upload copies it to the connected account’s Drive. Limit: 1 GiB. Every caption must include the trigger.'],
  choose: ['Escolher ZIP', 'Choose ZIP'], upload: ['Enviar dataset ao Drive', 'Upload dataset to Drive'], uploaded: ['Dataset verificado no Drive', 'Dataset verified on Drive'],
  prepare: ['Preparar projeto', 'Prepare project'], prepareHint: ['Instala um ambiente separado, baixa os pesos SFT INT8 e cria os caches. A GPU conectada define a memória disponível. Preparar não inicia o treino. A primeira preparação pode demorar e consome o tempo da sessão Colab.', 'Installs a separate environment, downloads SFT INT8 weights and builds caches. Available memory comes from the connected GPU. Preparing does not start training. First preparation may take time and consumes Colab session time.'],
  start: ['Iniciar treinamento', 'Start training'], resume: ['Retomar checkpoint', 'Resume checkpoint'], cancel: ['Solicitar parada', 'Request stop'], verify: ['Verificar resultados no Drive', 'Verify Drive results'], export: ['Adicionar LoRA à biblioteca ComfyUI', 'Add LoRA to ComfyUI library'],
  status: ['Andamento', 'Progress'], unknown: ['Aguardando consulta à sessão', 'Waiting for session status'], connect: ['Conecte uma sessão GPU pelo aplicativo para preparar e treinar. Esta prévia não aloca uma VM.', 'Connect a GPU session through the desktop app to prepare and train. This preview does not allocate a VM.'],
  preservation: ['Treino e checkpoints sempre ficam em EasyComfyColab2/output/training no Drive, inclusive com imagens no modo PC. Durante o treino, geração, reinício e encerramento ficam bloqueados. Uma interrupção perde as etapas posteriores ao último checkpoint completo.', 'Training and checkpoints always stay in EasyComfyColab2/output/training on Drive, including with PC image outputs. Generation, restart and shutdown are blocked during training. An interruption loses steps after the latest complete checkpoint.'],
  steps: ['Etapas', 'Steps'], rank: ['Rank', 'Rank'], resolution: ['Resolução (área alvo)', 'Resolution (target area)'], lr: ['Taxa de aprendizado', 'Learning rate'], seed: ['Seed', 'Seed'], save: ['Salvar a cada N etapas', 'Save every N steps'], profile: ['Perfil de memória', 'Memory profile'], auto: ['Automático (recomendado)', 'Automatic (recommended)'], balanced: ['Equilibrado', 'Balanced'], low: ['Pouca VRAM', 'Low VRAM'],
  refresh: ['Atualizar', 'Refresh'], loss: ['Loss', 'Loss'], eta: ['Estimativa restante', 'Estimated remaining'], checkpoint: ['Checkpoints completos', 'Complete checkpoints'], protected: ['Arquivos verificados no Drive', 'Files verified on Drive'], blocked: ['Preservação pendente; mantenha a VM ligada', 'Preservation pending; keep VM running'], working: ['Operação em andamento…', 'Operation in progress…'], library: ['LoRA verificado na biblioteca', 'Verified LoRA in library'],
} as const;
const phases: Record<string, [string, string]> = {
  idle: ['Aguardando projeto', 'Waiting for project'], launching: ['Iniciando preparação', 'Launching'], 'gpu-check': ['Verificando GPU', 'Checking GPU'], 'trainer-install': ['Baixando treinador', 'Downloading trainer'], 'trainer-verify': ['Verificando treinador', 'Verifying trainer'], environment: ['Criando ambiente', 'Creating environment'], 'torch-install': ['Instalando PyTorch isolado', 'Installing isolated PyTorch'], dependencies: ['Instalando dependências', 'Installing dependencies'], 'dependencies-check': ['Verificando dependências', 'Checking dependencies'], 'weights-diffusion_models': ['Baixando MOSS SFT INT8', 'Downloading MOSS SFT INT8'], 'weights-text_encoders': ['Baixando encoder de texto', 'Downloading text encoder'], 'weights-vae': ['Baixando VAE', 'Downloading VAE'], dataset: ['Extraindo dataset', 'Extracting dataset'], 'images-check': ['Verificando imagens', 'Checking images'], 'config-check': ['Verificando configuração', 'Checking configuration'], inspect: ['Inspecionando dataset', 'Inspecting dataset'], 'text-cache': ['Criando cache de texto', 'Building text cache'], 'latent-cache': ['Criando cache de imagens', 'Building image cache'], ready: ['Pronto para iniciar', 'Ready to start'], training: ['Treinando', 'Training'], complete: ['Concluído', 'Complete'], stopped: ['Parado', 'Stopped'], error: ['Falha; confira a mensagem', 'Failed; check message'],
};

export default function Training({ active, ready, accountId, sessionId, language, locked }: { active: boolean; ready: boolean; accountId: string | null; sessionId: string | null; language: Language; locked: boolean }) {
  const index = language === 'en' ? 1 : 0;
  const t = (key: keyof typeof copy) => copy[key][index];
  const [recipe, setRecipe] = useState<Recipe>(defaults);
  const [path, setPath] = useState('');
  const [sha, setSha] = useState('');
  const [selected, setSelected] = useState('');
  const [status, setStatus] = useState<Status | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [exported, setExported] = useState('');
  const identity = `${accountId ?? ''}/${sessionId ?? ''}`;
  const current = useRef(identity); current.current = identity;
  const generation = useRef(0);
  const polling = useRef(false);
  const identityParams = { account_id: accountId, session_id: sessionId };

  useEffect(() => { generation.current++; setStatus(null); setSha(''); setPath(''); setSelected(''); setError(''); setExported(''); }, [identity, ready]);
  async function refresh() {
    if (!ready || polling.current) return;
    const owner = identity; const version = generation.current;
    polling.current = true;
    try {
      const next = await bridge().request<Status>('training.status', identityParams);
      if (owner === current.current && version === generation.current) setStatus(next);
    } catch (failure) { if (owner === current.current && version === generation.current) setError(errorText(language, errorCode(failure))); }
    finally { polling.current = false; }
  }
  useEffect(() => {
    if (!active || !ready || pending || locked) return;
    void refresh(); const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [active, ready, identity, pending, locked]);

  async function request(action: string, params: Params = {}) {
    const owner = identity; const version = generation.current;
    setPending(true); setError(''); setExported('');
    try {
      const result = await bridge().request<Params>(action, { ...params, ...identityParams });
      if (owner !== current.current || version !== generation.current) return;
      if (typeof result.dataset_sha256 === 'string') setSha(result.dataset_sha256);
      if (typeof result.project_id === 'string') setSelected(result.project_id);
      if (action === 'training.export' && result.verified === true) setExported(String(result.path));
      await refresh();
    } catch (failure) { if (owner === current.current && version === generation.current) setError(errorText(language, errorCode(failure))); }
    finally { setPending(false); }
  }
  async function choose() {
    const owner = identity;
    try {
      const next = await bridge().request<string | null>('shell.pickFile', { kind: 'training-dataset' });
      if (next && owner === current.current) { setPath(next); setSha(''); setSelected(''); }
    } catch (failure) { setError(errorText(language, errorCode(failure))); }
  }
  const busy = status?.busy !== false;
  const disabled = !ready || pending || locked || busy || status?.preserved !== true;
  const chosen = status?.projects.find(p => p.id === selected);
  const ownProject = selected && status?.project_id === selected;
  const metricProject = status?.projects.find(p => p.id === status.project_id);
  const step = status?.metrics?.step;
  function update<K extends keyof Recipe>(key: K, value: Recipe[K]) { setRecipe(previous => ({ ...previous, [key]: value })); setSelected(''); }

  return <>
    {!ready ? <p role="status">{t('connect')}</p> : null}
    <section className="card training-card"><h2>{t('project')}</h2><p>{t('preservation')}</p>
      <label className="field">{t('saved')}<select disabled={disabled} value={selected} onChange={event => {
        const id = event.target.value; setSelected(id); setPath(''); setExported('');
        const project = status?.projects.find(p => p.id === id);
        if (project) { setRecipe(project.settings); setSha(project.dataset_sha256); } else { setRecipe(defaults); setSha(''); }
      }}><option value="">{t('fresh')}</option>{status?.projects.map(p => <option key={p.id} value={p.id}>{p.settings.name} · {p.id.slice(0, 8)}</option>)}</select></label>
      <form onSubmit={event => { event.preventDefault(); void request('training.prepare', { ...recipe, dataset_sha256: sha }); }}>
        <fieldset disabled={disabled} className="training-fields"><div className="two-columns">
          <label className="field">{t('name')}<input required pattern="[A-Za-z0-9_-]{1,48}" value={recipe.name} onChange={e => update('name', e.target.value)} /></label>
          <label className="field">{t('trigger')}<input required pattern="[A-Za-z0-9_-]{1,64}" value={recipe.trigger} onChange={e => update('trigger', e.target.value)} /></label>
          {(['steps', 'rank', 'resolution', 'lr', 'seed', 'save_every'] as const).map(key => <label className="field" key={key}>{t(key === 'save_every' ? 'save' : key)}<input required type="number" min={key === 'lr' ? 0.0000001 : key === 'seed' ? 0 : key === 'resolution' ? 256 : key === 'rank' ? 4 : 10} max={key === 'lr' ? 0.001 : key === 'seed' ? 2147483647 : key === 'resolution' ? 1536 : key === 'rank' ? 128 : key === 'save_every' ? 2000 : 20000} step={key === 'lr' ? 'any' : key === 'resolution' ? 32 : 1} value={recipe[key]} onChange={e => update(key, Number(e.target.value))} /></label>)}
          <label className="field">{t('profile')}<select value={recipe.profile} onChange={e => update('profile', e.target.value)}>{(['auto', 'balanced', 'low'] as const).map(p => <option key={p} value={p}>{t(p)}</option>)}</select></label>
        </div></fieldset>
        <h3>{t('dataset')}</h3><p>{t('datasetHint')}</p><div className="actions"><button type="button" disabled={disabled} onClick={() => void choose()}>{t('choose')}</button><button type="button" disabled={disabled || !path} onClick={() => void request('training.import', { path })}>{t('upload')}</button></div>
        {path ? <p className="supporting">{path}</p> : null}{sha ? <p className="supporting">{t('uploaded')} · {sha.slice(0, 12)}</p> : null}
        <p>{t('prepareHint')}</p><button className="primary" disabled={disabled || !sha}>{t('prepare')}</button>
      </form>
    </section>
    <section className="card training-card"><div className="section-toolbar"><h2>{t('status')}</h2><button disabled={!ready || pending || locked} onClick={() => void refresh()}>{t('refresh')}</button></div>
      <p role="status" aria-live="polite">{pending ? t('working') : status ? (phases[status.phase]?.[index] ?? status.phase) : t('unknown')}</p>
      {status?.gpu ? <p>{status.gpu.name} · {(status.gpu.vram_bytes / 1024**3).toFixed(1)} GiB</p> : null}
      {typeof step === 'number' ? <><progress aria-label={t('steps')} max={metricProject?.settings.steps ?? recipe.steps} value={step} /><p>{step} / {metricProject?.settings.steps ?? recipe.steps} · {t('loss')}: {status?.metrics?.loss?.toFixed(4)}{typeof status?.eta_seconds === 'number' && status.busy ? ` · ${t('eta')}: ${Math.ceil(status.eta_seconds / 60)} min` : ''}</p></> : null}
      <p>{t('checkpoint')}: {status?.checkpoints.length ?? '—'}</p>{chosen ? <p className="supporting">{chosen.run}</p> : null}
      {status ? <p>{status.preserved ? t('protected') : t('blocked')}</p> : null}
      <div className="actions"><button className="primary" disabled={disabled || !ownProject || status?.phase !== 'ready'} onClick={() => void request('training.start', { project_id: selected })}>{t('start')}</button><button disabled={disabled || !ownProject || !status?.checkpoints.length} onClick={() => void request('training.resume', { project_id: selected })}>{t('resume')}</button><button disabled={!ready || pending || locked || !status?.busy} onClick={() => void request('training.cancel')}>{t('cancel')}</button><button disabled={!ready || pending || locked || (status?.busy && !status.error)} onClick={() => void request('training.preserve')}>{t('verify')}</button><button disabled={disabled || !selected} onClick={() => void request('training.export', { project_id: selected })}>{t('export')}</button></div>
      {error || status?.error ? <p role="alert">{error || status?.error}</p> : null}{exported ? <p role="status">{t('library')}: {exported}</p> : null}
    </section>
  </>;
}
