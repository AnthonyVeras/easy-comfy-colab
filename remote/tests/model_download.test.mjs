import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const source = fs.readFileSync(new URL("../web/remote_download.js", import.meta.url), "utf8").replace(/^import[^\n]*\n/, "");
const settle = () => new Promise(resolve => setImmediate(resolve));

function frontend(fetch) {
  const dom = new JSDOM('<html lang="pt-BR"><body></body></html>');
  let refreshes = 0;
  let localDownloads = 0;
  dom.window.open = () => { localDownloads++; };
  dom.window.showSaveFilePicker = () => { localDownloads++; };
  const app = { registerExtension() {}, async refreshComboInNodes() { refreshes++; } };
  vm.runInNewContext(source, { app, window: dom.window, document: dom.window.document, navigator: dom.window.navigator, fetch, AbortSignal, setTimeout, Map });
  // The official pinned frontend branches through this contract for individual and bulk buttons.
  async function button(model) {
    const hook = dom.window.__comfyDesktop2;
    if (hook?.downloadModel && !hook.isRemote()) await hook.downloadModel(model.url, model.name, model.directory);
    else dom.window.open(model.url);
  }
  return { dom, button, refreshes: () => refreshes, localDownloads: () => localDownloads };
}

const model = { url: "https://huggingface.co/org/repo/resolve/main/model.safetensors", name: "model.safetensors", directory: "vae" };
const reply = result => ({ ok: true, json: async () => ({ ok: true, result }) });

test("Download and Download All use same-origin VM queue, preserve categories and refresh loaders", async () => {
  const requests = [];
  let sequence = 0;
  let posting = false;
  const ui = frontend(async (path, options) => {
    requests.push({ path, options });
    if (options.method === "POST") {
      assert.equal(posting, false, "Bulk queue admission must not collide with itself");
      posting = true;
      await settle();
      posting = false;
      return reply({ job_id: (++sequence).toString(16).padStart(32, "0") });
    }
    return reply({ status: "complete", bytes: 42, total: 42 });
  });
  await ui.button(model);
  await Promise.all([ui.button({ ...model, name: "clip.safetensors", directory: "text_encoders" }), ui.button({ ...model, name: "lora.safetensors", directory: "loras" })]);
  await settle();
  const posts = requests.filter(row => row.options.method === "POST");
  assert.equal(posts.length, 3);
  assert.ok(posts.every(row => row.path === "/easy-comfy/model-download" && row.options.credentials === "same-origin"));
  assert.deepEqual(posts.map(row => JSON.parse(row.options.body).directory), ["vae", "text_encoders", "loras"]);
  assert.deepEqual(JSON.parse(posts[0].options.body), model);
  assert.equal(ui.localDownloads(), 0);
  assert.equal(ui.refreshes(), 3);
  assert.match(ui.dom.window.document.body.textContent, /Concluído na VM/);
  assert.equal(posts[0].options.headers.Authorization, undefined);
  ui.dom.window.close();
});

test("VM authorization failure rejects the button and never falls back to saving on the PC", async () => {
  const ui = frontend(async () => ({ ok: false, json: async () => ({ ok: false, error: { code: "model_auth_required" } }) }));
  await assert.rejects(ui.button(model), /Configure HF\/Civitai/);
  assert.equal(ui.localDownloads(), 0);
  assert.match(ui.dom.window.document.body.textContent, /retome pela aba Modelos/);
  ui.dom.window.close();
});

test("Unavailable bridge shows recovery without opening a download tab; filenames remain text", async () => {
  const ui = frontend(async () => { throw new Error("Session disconnected"); });
  await assert.rejects(ui.button({ ...model, name: '<img src="x" onerror="alert(1)">' }), /Session disconnected/);
  assert.equal(ui.localDownloads(), 0);
  assert.equal(ui.dom.window.document.querySelector("img"), null);
  ui.dom.window.close();
});
