import { describe, expect, it } from 'vitest';
import { errorCodes, errorText, phaseText } from './messages';

describe('localized controller contracts', () => {
  it('covers every statically declared controller and runtime error code', () => {
    const sources = import.meta.glob<string>(['../../controller/*.py', '../../remote/*.py'], { query: '?raw', import: 'default', eager: true });
    expect(Object.keys(sources).length).toBeGreaterThan(10);
    for (const source of Object.values(sources)) for (const match of source.matchAll(/(?:AppError|Fault)\(["']([a-z_]+)["']/g)) expect(errorCodes, match[1]).toContain(match[1]);
  });
  it('provides both languages and a localized recovery for future unknown errors', () => {
    for (const code of errorCodes) { expect(errorText('pt-BR', code)).not.toBe(errorText('en', code)); expect(errorText('pt-BR', code).length).toBeGreaterThan(15); }
    expect(errorText('pt-BR', 'new_code')).toContain('Confira a atividade');
    expect(phaseText('pt-BR', 'runtime_preservation')).toBe('Salvando e verificando o ambiente');
    expect(phaseText('en', 'unknown')).toBe('Operation in progress');
  });
  it('explains output destination recovery and dynamically assigned delivery faults in both languages', () => {
    expect(errorText('pt-BR', 'output_mode_conflict')).toContain('destino real');
    expect(errorText('en', 'output_mode_unverified')).toContain('actual destination');
    expect(errorText('pt-BR', 'output_switch_not_ready')).toContain('mesma VM');
    expect(errorText('en', 'output_switch_not_ready')).toContain('same VM');
    expect(errorText('pt-BR', 'output_delivery_failed')).toContain('A VM é preservada');
    expect(errorText('en', 'output_delivery_failed')).toContain('The VM is preserved');
  });
});
