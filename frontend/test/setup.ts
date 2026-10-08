import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
afterEach(() => { cleanup(); delete window.easyComfy; });
HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
globalThis.requestAnimationFrame = callback => setTimeout(() => callback(performance.now()), 0) as unknown as number;
globalThis.cancelAnimationFrame = id => clearTimeout(id);
