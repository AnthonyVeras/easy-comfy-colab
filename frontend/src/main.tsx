import { createRoot } from 'react-dom/client';
import { ControllerError, installTauriBridge } from './api';
import App from './App';

try { await installTauriBridge(); }
catch {
  window.easyComfy = {
    async request() { throw new ControllerError('controller_unavailable', 'Native bridge initialization failed. Reopen or reinstall the app.'); },
    subscribe: () => () => {},
  };
}
createRoot(document.getElementById('root')!).render(<App />);
