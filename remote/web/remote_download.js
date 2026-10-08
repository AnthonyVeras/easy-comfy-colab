import { app } from "../../scripts/app.js";

const pt = () => (document.documentElement.lang || navigator.language).toLowerCase().startsWith("pt");
const say = (br, en) => pt() ? br : en;
const active = new Map();
let panel;
let submissions = Promise.resolve();

function notice(name) {
  if (!panel) {
    panel = document.createElement("div");
    panel.setAttribute("role", "status");
    panel.setAttribute("aria-live", "polite");
    Object.assign(panel.style, { position: "fixed", right: "18px", bottom: "18px", zIndex: "10000", maxWidth: "440px", maxHeight: "40vh", overflow: "auto", background: "#18181b", color: "#fafafa", border: "1px solid #52525b", borderRadius: "8px", padding: "12px", font: "13px monospace" });
    document.body.append(panel);
  }
  const row = document.createElement("div");
  const text = document.createElement("span");
  const close = document.createElement("button");
  close.textContent = "×";
  close.setAttribute("aria-label", say("Ocultar aviso", "Dismiss notification"));
  close.title = say("O download continua na VM", "The download continues in the VM");
  close.onclick = () => row.remove();
  row.append(text, close);
  panel.append(row);
  while (panel.children.length > 50) panel.firstElementChild.remove();
  return message => { text.textContent = `${name}: ${message} `; };
}

function problem(code) {
  if (code === "model_auth_required") return say("Configure HF/Civitai no aplicativo e retome pela aba Modelos.", "Configure HF/Civitai in the app and resume from Models.");
  if (code === "invalid_url") return say("Link incompatível. Use um link direto HF/Civitai na aba Modelos.", "Unsupported link. Use a direct HF/Civitai link in Models.");
  if (code === "model_conflict") return say("Há outro arquivo nesse destino. Resolva o conflito na aba Modelos.", "Another file occupies this destination. Resolve the conflict in Models.");
  return say("Confira a sessão e a fila na aba Modelos do Easy Comfy Colab 2.", "Check the session and queue in Easy Comfy Colab 2 Models.");
}

async function call(path, payload) {
  const response = await fetch(path, { method: payload ? "POST" : "GET", credentials: "same-origin", headers: payload ? { "Content-Type": "application/json" } : {}, ...(payload ? { body: JSON.stringify(payload) } : {}), signal: AbortSignal.timeout(15000) });
  const reply = await response.json();
  if (!response.ok || reply.ok !== true) throw new Error(problem(reply.error?.code));
  return reply.result;
}

async function watch(id, update) {
  try {
    while (true) {
      const job = await call(`/easy-comfy/model-download/${id}`);
      if (job.status === "complete") {
        update(say("Concluído na VM / Drive", "Complete in VM / Drive"));
        try { await app.refreshComboInNodes(); }
        catch { update(say("Concluído na VM. Atualize as listas de modelos.", "Complete in VM. Refresh the model lists.")); }
        return;
      }
      if (["error", "cancelled", "paused"].includes(job.status)) throw new Error(problem(job.error?.code));
      const size = (job.bytes / 1048576).toFixed(1);
      const progress = job.total ? `${Math.min(100, Math.floor(job.bytes * 100 / job.total))}% · ${size} MiB` : `${size} MiB`;
      update(say(`Baixando na VM · ${progress}`, `Downloading in VM · ${progress}`));
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
  } catch (error) {
    update(say("Acompanhamento interrompido. ", "Tracking stopped. ") + (error.message || problem()));
  } finally {
    active.delete(id);
  }
}

async function downloadInVM(url, name, directory) {
  const update = notice(`${directory}/${name}`);
  update(say("Enviando para a fila da VM…", "Adding to the VM queue…"));
  try {
    // Bulk clicks serialize queue admission; the VM still downloads in parallel.
    const request = submissions.then(() => call("/easy-comfy/model-download", { url, name, directory }));
    submissions = request.catch(() => {});
    const result = await request;
    if (!/^[a-f0-9]{32}$/.test(result?.job_id)) throw new Error(problem());
    update(say("Na fila da VM", "Queued in the VM"));
    if (!active.has(result.job_id) && active.size < 50) {
      const tracker = watch(result.job_id, update);
      active.set(result.job_id, tracker);
    } else {
      update(say("Download na fila da VM; acompanhe o aviso existente ou a aba Modelos.", "Download in the VM queue; follow its existing notification or the Models tab."));
    }
  } catch (error) {
    update(error.message || problem());
    throw error;
  }
}

// The pinned frontend uses this hook for both Download and Download All.
window.__comfyDesktop2 = { ...(window.__comfyDesktop2 || {}), isRemote: () => false, downloadModel: downloadInVM };
app.registerExtension({ name: "easyComfy.remoteModelDownload" });
