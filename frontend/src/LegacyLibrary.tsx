import { useEffect, useId, useRef, useState } from 'react';
import type { CopyKey } from './i18n';
import { errorText } from './messages';
import type { Language, LegacyFile, LegacyReceipt, Paged, Params } from './types';

export default function LegacyLibrary({ accountId, sessionId, ready, locked, language, categories, request, t }: {
  accountId: string | null; sessionId: string | null; ready: boolean; locked: boolean; language: Language; categories: string[];
  request<T = unknown>(action: string, params?: Params): Promise<T | undefined>; t: (key: CopyKey) => string;
}) {
  const id = useId();
  const [kind, setKind] = useState<'model' | 'workflow'>('model');
  const [category, setCategory] = useState('checkpoints');
  const [page, setPage] = useState<Paged<LegacyFile> | null>(null);
  const [selected, setSelected] = useState<LegacyFile | null>(null);
  const [destinationName, setDestinationName] = useState('');
  const [receipt, setReceipt] = useState<LegacyReceipt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const key = `${accountId}|${sessionId}|${kind}|${category}`;
  const current = useRef({ key, path: '' });
  current.current = { key, path: selected?.relative_path ?? '' };
  useEffect(() => { setPage(null); setSelected(null); setDestinationName(''); setReceipt(null); setError(null); }, [key]);
  const unavailable = locked || !ready || !accountId || !sessionId;
  const sourceFolder = kind === 'model' ? `models/${category}` : 'user/default/workflows';
  const selectedSourceFolder = kind === 'model' ? `models/${selected?.source_category ?? category}` : sourceFolder;
  const extension = selected?.name.includes('.') ? selected.name.slice(selected.name.lastIndexOf('.')) : '';
  const validName = destinationName.trim().length > 0 && new TextEncoder().encode(destinationName).length <= 255 && !/[\\/:\x00-\x1f]/.test(destinationName) && !['.', '..'].includes(destinationName) && !!extension && destinationName.toLowerCase().endsWith(extension.toLowerCase());
  async function list(offset = 0) {
    const expected = key; setError(null); setReceipt(null); setSelected(null); setDestinationName('');
    const result = await request<Paged<LegacyFile>>('legacy.list', { kind, ...(kind === 'model' ? { category } : {}), offset, limit: 25 });
    if (result && current.current.key === expected) {
      if (Array.isArray(result.items)) setPage(result); else setError('remote_response_invalid');
    }
  }
  async function copy() {
    if (!selected || !validName || unavailable) return;
    const expected = { key, path: selected.relative_path }; setError(null); setReceipt(null);
    const result = await request<LegacyReceipt>('legacy.import', { kind, ...(kind === 'model' ? { category, ...(selected.source_category ? { source_category: selected.source_category } : {}) } : {}), relative_path: selected.relative_path, destination_name: destinationName.trim() });
    if (result && current.current.key === expected.key && current.current.path === expected.path) {
      if (result.imported === true && result.source_preserved === true && /^[a-f0-9]{64}$/i.test(result.sha256)) setReceipt(result);
      else setError('remote_response_invalid');
    }
  }
  return <details className="advanced"><summary>{t('legacyLibrary')}</summary><p>{t('legacyLibraryHint')}</p>
    {!ready ? <p className="supporting">{t('accountRequired')}</p> : null}
    <div className="two-columns"><div className="field"><label htmlFor={`${id}-kind`}>{t('legacyKind')}</label><select id={`${id}-kind`} value={kind} disabled={locked} onChange={event => setKind(event.target.value as 'model' | 'workflow')}><option value="model">{t('models')}</option><option value="workflow">{t('workflows')}</option></select></div>
      {kind === 'model' ? <div className="field"><label htmlFor={`${id}-category`}>{t('legacyCategory')}</label><select id={`${id}-category`} value={category} disabled={locked} onChange={event => setCategory(event.target.value)}>{categories.map(value => <option key={value}>{value}</option>)}</select></div> : null}</div>
    <button disabled={!!unavailable} onClick={() => void list()}>{t('browseLegacy')}</button>
    {page ? <><h3>{t('legacyFiles')}</h3>{page.items.length ? <div className="table-scroll"><table><thead><tr><th>{t('name')}</th><th>{t('size')}</th><th>{t('actions')}</th></tr></thead><tbody>{page.items.map(item => <tr key={item.id}><td>{item.relative_path}</td><td className="numeric">{item.size.toLocaleString(language)} B</td><td><button disabled={!!unavailable} aria-label={`${t('selectLegacy')} ${item.name}`} onClick={() => { setSelected(item); setDestinationName(item.name); setReceipt(null); setError(null); }}>{t('selectLegacy')}</button></td></tr>)}</tbody></table></div> : <p>{t('legacyNone')}</p>}
      <div className="section-toolbar pagination"><span>{page.total ? `${page.offset + 1}–${Math.min(page.offset + page.items.length, page.total)} / ${page.total}` : '0'} · {t('legacyFiles')}</span><div className="actions"><button disabled={!!unavailable || page.offset === 0} onClick={() => void list(Math.max(0, page.offset - page.limit))}>{t('previousPage')}</button><button disabled={!!unavailable || page.offset + page.limit >= page.total} onClick={() => void list(page.offset + page.limit)}>{t('nextPage')}</button></div></div>
    </> : <p className="supporting">{t('legacyNotLoaded')}</p>}
    {selected ? <section className="workflow-result"><h3>{selected.name}</h3><dl className="import-paths"><dt>{t('legacyPath')}</dt><dd>ComfyColab/{selectedSourceFolder}/{selected.relative_path}</dd><dt>{t('legacyTarget')}</dt><dd>EasyComfyColab2/{sourceFolder}/{destinationName}</dd><dt>{t('size')}</dt><dd>{selected.size.toLocaleString(language)} B</dd></dl>
      <div className="field"><label htmlFor={`${id}-name`}>{t('legacyName')}</label><input id={`${id}-name`} value={destinationName} disabled={locked} maxLength={255} aria-required="true" aria-invalid={!validName} aria-describedby={`${id}-hint${!validName ? ` ${id}-invalid` : ''}`} onChange={event => { setDestinationName(event.target.value); setReceipt(null); }} /><small id={`${id}-hint`}>{t('legacyNameHint')}</small>{!validName ? <small id={`${id}-invalid`}>{errorText(language, 'legacy_selection_invalid')}</small> : null}</div><button className="primary" disabled={!!unavailable || !validName} onClick={() => void copy()}>{t('copyLegacyFile')}</button>
    </section> : null}
    {error ? <p role="alert">{errorText(language, error)}</p> : null}
    {receipt ? <section className="workflow-result" role="status"><h3>{receipt.unchanged ? t('legacyUnchanged') : t('legacyCopied')}</h3><p>{receipt.name} · {receipt.size.toLocaleString(language)} B</p><details><summary>{t('legacyHash')}</summary><pre>{receipt.sha256}</pre></details></section> : null}
  </details>;
}
