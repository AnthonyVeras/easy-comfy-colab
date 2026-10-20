'use strict';
const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const { randomUUID } = require('node:crypto');
const { writeFileSync } = require('node:fs');
const path = require('node:path');
const { validateRequest, authorizationURL, comfyURL } = require('./policy.cjs');

let window, controller;
let firstSnapshot = false;
let allowClose = false;
const pending = new Map();
const root = path.resolve(__dirname, '..');
app.setName('Easy Comfy Colab 2 Electron');
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.focus(); } });
  app.whenReady().then(createWindow);
}
function failure(code, message) { return { ok: false, error: { code, message } }; }
function startController() {
  const executable = app.isPackaged ? path.join(process.resourcesPath, 'controller', 'easy-comfy-controller.exe') : process.env.EASY_COMFY_PYTHON || 'python';
  const args = app.isPackaged ? [] : ['-u', path.join(root, 'controller', 'main.py')];
  controller = spawn(executable, args, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, cwd: app.isPackaged ? process.resourcesPath : root });
  controller.stdin.on('error', () => {});
  controller.stderr.on('data', () => {});
  const lines = createInterface({ input: controller.stdout, crlfDelay: Infinity });
  lines.on('line', async line => {
    if (line.length > 1024 * 1024) return;
    let response;
    try { response = JSON.parse(line); } catch { return; }
    if (response.id && pending.has(response.id)) {
      const request = pending.get(response.id); pending.delete(response.id); clearTimeout(request.timeout);
      if (request.action === 'app.snapshot' && response.ok) firstSnapshot = true;
      try {
        if (request.action === 'session.open' && response.ok) await shell.openExternal(comfyURL(response.result.url));
        if (['app.diagnostics', 'history.export'].includes(request.action) && response.ok) {
          const selected = await dialog.showSaveDialog(window, { defaultPath: request.action === 'history.export' ? 'easy-comfy-colab-history.json' : 'easy-comfy-diagnostic.json', filters: [{ name: 'JSON', extensions: ['json'] }] });
          if (!selected.canceled && selected.filePath) writeFileSync(selected.filePath, JSON.stringify(response.result, null, 2), { encoding: 'utf8' });
          response.result = { saved: !selected.canceled && !!selected.filePath };
        }
      } catch { request.resolve(failure('native_action_failed', 'Could not open the browser or save the file. Check the location and reconnect if needed.')); return; }
      request.resolve(response);
    } else if (response.event && window && !window.isDestroyed()) window.webContents.send('easy-comfy:event', { event: response.event, data: response.data ?? {} });
  });
  const unavailable = () => {
    for (const request of pending.values()) { clearTimeout(request.timeout); request.resolve(failure('controller_unavailable', 'The native controller stopped. Reopen the app to reconnect safely.')); }
    pending.clear();
    if (window && !window.isDestroyed()) window.webContents.send('easy-comfy:event', { event: 'controller.unavailable', data: { message: 'The native controller stopped. Reopen the app to reconnect safely.' } });
  };
  controller.on('error', unavailable);
  controller.on('exit', unavailable);
}
async function createWindow() {
  startController();
  window = new BrowserWindow({ width: 1260, height: 860, minWidth: 320, minHeight: 400, title: 'Easy Comfy Colab 2', backgroundColor: '#141416', show: false, webPreferences: { preload: path.join(__dirname, 'preload.cjs'), sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  window.once('ready-to-show', () => window.show());
  window.on('close', event => { if (!allowClose && firstSnapshot) { event.preventDefault(); window.webContents.send('easy-comfy:event', { event: 'app.close-request', data: {} }); } });
  ipcMain.handle('easy-comfy:request', async (event, action, params = {}) => {
    if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) return failure('forbidden_origin', 'Request blocked. Use the local application.');
    try {
      validateRequest(action, params);
      if (action === 'shell.pickFile') {
        if (!['workflow', 'training-dataset'].includes(params.kind)) return failure('invalid_file_kind', 'Select a workflow JSON or training ZIP.');
        const result = await dialog.showOpenDialog(window, { properties: ['openFile'], filters: params.kind === 'training-dataset' ? [{ name: 'Training dataset', extensions: ['zip'] }] : [{ name: 'ComfyUI workflow', extensions: ['json'] }] });
        return { ok: true, result: result.canceled ? null : result.filePaths[0] };
      }
      if (action === 'shell.openExternal') { await shell.openExternal(authorizationURL(params.url)); return { ok: true, result: {} }; }
      if (action === 'shell.close') { allowClose = true; window.close(); return { ok: true, result: {} }; }
      if (action === 'shell.ready') {
        if (firstSnapshot && process.env.ECC_BENCHMARK_READY_FILE) writeFileSync(process.env.ECC_BENCHMARK_READY_FILE, JSON.stringify({ ready_at: Date.now(), shell: 'electron' }), { encoding: 'utf8' });
        return { ok: true, result: {} };
      }
      if (!controller || controller.exitCode !== null || controller.killed) return failure('controller_unavailable', 'The native controller is unavailable. Reopen the app.');
      const id = randomUUID();
      return await new Promise(resolve => {
        const timeout = setTimeout(() => { pending.delete(id); resolve(failure('request_timeout', 'Check Activity and reconnect; the operation did not reply.')); }, ['legacy.import', 'training.import'].includes(action) ? 1_260_000 : 600_000);
        pending.set(id, { resolve, timeout, action });
        controller.stdin.write(JSON.stringify({ id, action, params }) + '\n', error => { if (error) { clearTimeout(timeout); pending.delete(id); resolve(failure('controller_unavailable', 'Reopen the app to reconnect the controller.')); } });
      });
    } catch { return failure('invalid_request', 'Request unavailable. Check the selected file or reconnect the Google account.'); }
  });
  if (process.env.ECC_UI_DEV === '1' && !app.isPackaged) await window.loadURL('http://127.0.0.1:1420');
  else await window.loadFile(path.join(app.isPackaged ? app.getAppPath() : root, 'dist', 'frontend', 'index.html'));
}
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { if (controller && !controller.killed) controller.stdin.end(); });
