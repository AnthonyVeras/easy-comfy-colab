import { app } from "../../scripts/app.js";

const downloads = new Map();
let notice;

function renderNotice() {
  if (!notice) {
    notice = document.createElement("aside");
    notice.setAttribute("aria-live", "polite");
    Object.assign(notice.style, {
      position: "fixed",
      right: "20px",
      bottom: "20px",
      zIndex: "100000",
      maxWidth: "360px",
      maxHeight: "45vh",
      overflow: "auto",
      padding: "12px 16px",
      border: "1px solid #555",
      borderRadius: "10px",
      background: "#202020",
      color: "#fff",
      boxShadow: "0 8px 30px #0008",
      font: "13px sans-serif",
    });
    document.body.append(notice);
  }
  notice.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = "Downloads no Colab / Google Drive";
  notice.append(title);
  const close = document.createElement("button");
  close.textContent = "×";
  close.title = "Ocultar avisos (downloads continuam)";
  Object.assign(close.style, {
    float: "right",
    border: "0",
    background: "transparent",
    color: "white",
    cursor: "pointer",
    fontSize: "18px",
  });
  close.addEventListener("click", () => {
    notice.remove();
    notice = undefined;
  });
  notice.append(close);
  for (const [name, item] of downloads) {
    const row = document.createElement("div");
    row.style.marginTop = "8px";
    row.textContent = `${name}: ${item.message}`;
    notice.append(row);
  }
}

function update(name, message) {
  downloads.set(name, { message });
  renderNotice();
}

async function watchJob(name, jobId) {
  while (true) {
    const response = await fetch(
      `/comfy-colab/model-download/${encodeURIComponent(jobId)}`,
    );
    const job = await response.json();
    if (!response.ok) throw new Error(job.error || "Erro ao consultar download");
    if (job.status === "complete") {
      update(name, "salvo no Drive");
      try {
        await app.refreshComboInNodes?.();
      } catch (error) {
        console.warn("[Comfy Colab] Não foi possível atualizar os nós:", error);
      }
      return;
    }
    if (job.status === "error") {
      throw new Error(job.error || "Falha no download");
    }
    if (["cancelled", "interrupted"].includes(job.status)) {
      update(name, "interrompido · retome pela aba Modelos do aplicativo");
      return;
    }
    const progress = job.total
      ? `${Math.floor((100 * job.bytes) / job.total)}%`
      : `${Math.floor(job.bytes / 1048576)} MiB`;
    update(name, job.status === "queued" ? "na fila da VM" : `baixando na VM (${progress})`);
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}

async function downloadInColab(url, name, directory) {
  const label = `${directory}/${name}`;
  update(label, "aguardando o Colab");
  try {
    const response = await fetch("/comfy-colab/model-download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, name, directory }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Download recusado");
    await watchJob(label, result.job_id);
  } catch (error) {
    update(label, `erro: ${error.message || error}`);
    console.error("[Comfy Colab] Falha ao baixar modelo na VM:", error);
  }
}

// O frontend consulta esta ponte no momento do clique. Ela precisa continuar
// presente: algumas versões processam o clique depois do listener de captura.
window.__comfyDesktop2 = {
  ...(window.__comfyDesktop2 || {}),
  isRemote: () => false,
  downloadModel: downloadInColab,
};

app.registerExtension({ name: "comfyColab.remoteModelDownload" });
console.info("[Comfy Colab] Downloads de modelos apontam para a VM.");
