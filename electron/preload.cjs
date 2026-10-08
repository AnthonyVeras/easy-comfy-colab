'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('easyComfy', {
  request: (action, params = {}) => ipcRenderer.invoke('easy-comfy:request', action, params).then(response => {
    if (!response.ok) { const error = new Error(response.error?.message ?? 'Controller unavailable. Reopen the app.'); error.code = response.error?.code; throw error; }
    return response.result;
  }),
  subscribe: listener => {
    const handler = (_event, payload) => listener(payload);
    ipcRenderer.on('easy-comfy:event', handler);
    return () => ipcRenderer.removeListener('easy-comfy:event', handler);
  },
});
