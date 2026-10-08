const { test } = require('node:test');
const assert = require('node:assert/strict');
const { authorizationURL, validateRequest, comfyURL } = require('./policy.cjs');
test('allows only reviewed Google authorization origins', () => {
  for (const host of ['accounts.google.com', 'sdk.cloud.google.com']) assert.ok(authorizationURL(`https://${host}/auth`).startsWith(`https://${host}/`));
  for (const value of ['http://accounts.google.com/auth', 'https://accounts.google.com.evil.test/', 'https://user:secret@accounts.google.com/', 'file:///C:/secret', 'https://accounts.google.com:444/', 'javascript:alert(1)']) assert.throws(() => authorizationURL(value));
});
test('rejects arbitrary IPC commands and non-object/oversized parameters', () => {
  validateRequest('session.start', { account_id: 'owned' });
  validateRequest('history.export', { account_id: 'owned' });
  validateRequest('legacy.list', { kind: 'model', category: 'checkpoints', offset: 0, limit: 25 });
  validateRequest('legacy.import', { kind: 'workflow', relative_path: 'example.json', destination_name: 'copied.json' });
  assert.throws(() => validateRequest('shell.exec', {}));
  assert.throws(() => validateRequest('app.snapshot', []));
  assert.throws(() => validateRequest('app.snapshot', { value: 'a'.repeat(300000) }));
});
test('opens only controller-returned non-legacy loopback ComfyUI roots', () => {
  assert.equal(comfyURL('http://127.0.0.1:28188/'), 'http://127.0.0.1:28188/');
  for (const value of ['http://127.0.0.1:18188/', 'http://127.0.0.1:18189/', 'http://evil.test:28188/', 'https://127.0.0.1:28188/', 'http://127.0.0.1:28188/secret', 'http://127.0.0.1:28188/?url=evil']) assert.throws(() => comfyURL(value));
});
