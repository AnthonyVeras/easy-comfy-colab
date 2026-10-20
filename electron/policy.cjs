'use strict';
const actions = new Set(['training.status', 'training.import', 'training.prepare', 'training.start', 'training.resume', 'training.cancel', 'training.preserve', 'training.export', 'app.snapshot', 'app.preferences', 'app.diagnostics', 'account.importLegacy', 'account.connect', 'account.submitCode', 'account.select', 'account.usage', 'account.remove', 'account.update', 'account.credentials', 'session.start', 'session.cancel', 'session.reconnect', 'session.restart', 'session.stop', 'session.freeMemory', 'session.runtimeSave', 'session.open', 'model.list', 'download.list', 'node.catalog', 'node.versions', 'workflow.list', 'legacy.list', 'legacy.import', 'download.add', 'download.cancel', 'download.resume', 'cache.prepare', 'cache.cancel', 'cache.clear', 'workflow.check', 'workflow.import', 'node.list', 'node.install', 'node.update', 'node.remove', 'drive.copy', 'drive.share', 'history.list', 'history.export', 'shell.pickFile', 'shell.openExternal', 'shell.ready', 'shell.close']);
function validateRequest(action, params) {
  if (typeof action !== 'string' || !actions.has(action)) throw new Error('Action unavailable. Reopen the app.');
  if (!params || typeof params !== 'object' || Array.isArray(params) || JSON.stringify(params).length > 256 * 1024) throw new Error('Invalid request. Check the fields and try again.');
}
function authorizationURL(value) {
  if (typeof value !== 'string' || value.length > 16384) throw new Error('Invalid authorization URL. Reconnect the account.');
  const url = new URL(value);
  if (url.protocol !== 'https:' || !['accounts.google.com', 'sdk.cloud.google.com'].includes(url.hostname) || url.username || url.password || (url.port && url.port !== '443')) throw new Error('Only Google authorization links can be opened. Reconnect the account.');
  return url.href;
}
function comfyURL(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || Number(url.port) < 1024 || ['18188', '18189'].includes(url.port) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('Invalid owned ComfyUI URL. Reconnect the session.');
  return url.href;
}
module.exports = { validateRequest, authorizationURL, comfyURL };
