import { fireEvent, render, screen, waitFor, act } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import Training from './Training';
import type { Params } from './types';

const project = { id: 'project', settings: { name: 'identity', trigger: 'hrn_person', steps: 500, rank: 16, resolution: 768, lr: 0.0001, seed: 42, save_every: 100, profile: 'auto' }, dataset_sha256: 'a'.repeat(64), run: '/drive/output/training/project/identity' };
const idle = { phase: 'idle', busy: false, preserved: true, projects: [project], checkpoints: [] };
const props = { active: true, ready: true, accountId: 'account', sessionId: 'session', language: 'pt-BR' as const, locked: false };
function bridge(respond: (action: string, params?: Params) => unknown) {
  const request = vi.fn(async (action: string, params?: Params) => respond(action, params));
  window.easyComfy = { request: request as NonNullable<Window['easyComfy']>['request'], subscribe: () => () => {} };
  return request;
}

describe('native MOSS training', () => {
  it('offline preview never allocates or prepares a VM', () => {
    const request = bridge(() => idle);
    render(<Training {...props} ready={false} />);
    expect(screen.getByText(/Esta prévia não aloca/)).toBeTruthy();
    expect((screen.getByText('Preparar projeto') as HTMLButtonElement).disabled).toBe(true);
    expect(request).not.toHaveBeenCalled();
  });

  it('prepares a saved reusable recipe only after an explicit click', async () => {
    const request = bridge(action => action === 'training.status' ? idle : { project_id: project.id });
    render(<Training {...props} />);
    await screen.findByText('identity · project');
    fireEvent.change(screen.getByLabelText('Projetos no Drive'), { target: { value: project.id } });
    fireEvent.click(screen.getByText('Preparar projeto'));
    await waitFor(() => expect(request).toHaveBeenCalledWith('training.prepare', expect.objectContaining({ ...project.settings, dataset_sha256: project.dataset_sha256, account_id: 'account', session_id: 'session' })));
    expect(request.mock.calls.some(([action]) => action === 'training.start' || action === 'session.start')).toBe(false);
  });

  it('blocks new work while training and routes cancellation to the same session', async () => {
    const request = bridge(action => action === 'training.status' ? { ...idle, phase: 'training', busy: true, preserved: false, project_id: project.id, metrics: { step: 100, loss: .3, step_time: 2 }, checkpoints: ['step_00000100'] } : {});
    render(<Training {...props} />);
    await screen.findByText('Treinando');
    expect((screen.getByText('Preparar projeto') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByText('Iniciar treinamento') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByText('Solicitar parada'));
    await waitFor(() => expect(request).toHaveBeenCalledWith('training.cancel', { account_id: 'account', session_id: 'session' }));
  });

  it('discards a late status response after the account changes', async () => {
    let resolve!: (value: unknown) => void;
    let count = 0;
    bridge(() => ++count === 1 ? new Promise(done => { resolve = done; }) : idle);
    const view = render(<Training {...props} />);
    await waitFor(() => expect(resolve).toBeTypeOf('function'));
    view.rerender(<Training {...props} accountId="other" sessionId="other-session" ready={false} />);
    await act(async () => resolve({ ...idle, phase: 'training', busy: true, preserved: false }));
    expect(screen.queryByText('Treinando')).toBeNull();
    expect((screen.getByText('Solicitar parada') as HTMLButtonElement).disabled).toBe(true);
  });

  it('offers the same workflow in English', () => {
    render(<Training {...props} ready={false} language="en" />);
    expect(screen.getByText('Prepare project')).toBeTruthy();
    expect(screen.getByText('Resume checkpoint')).toBeTruthy();
  });
});
