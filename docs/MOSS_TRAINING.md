# MOSS-Image LoRA in the desktop application

## Português

A aba **Treinamento LoRA** usa a sessão GPU que o aplicativo já controla. Ela não cria um notebook e não aloca outra VM. O treinador roda em um ambiente Python separado; o PyTorch e os nodes do ComfyUI não são substituídos.

1. Abra uma distribuição do aplicativo com o OAuth Google configurado, conecte a conta e inicie uma sessão GPU. Use a opção G4 se ela disponibilizar a RTX PRO 6000 na sua conta. A disponibilidade é do Colab; o app mostra a GPU realmente recebida.
2. Na aba **Treinamento LoRA**, escolha um ZIP com uma única pasta `imagens` ou `images`, contendo cada imagem e seu `.txt` UTF-8 de mesmo nome. Também aceita uma única pasta de pares com outro nome. As pastas de revisão do pacote de seleção são ignoradas. Todas as fotos da pasta devem ter legenda; todas as legendas devem conter o trigger configurado. Formatos: JPG/JPEG, PNG e WebP; 5–5.000 imagens, ZIP até 1 GiB, expansão até 3 GiB.
3. Clique **Enviar dataset ao Drive**. O app copia esse arquivo privado para o Drive da conta conectada e verifica SHA-256. Nada é enviado ao GitHub ou a um serviço de treinamento de terceiros.
4. Defina nome, trigger e parâmetros. Para o dataset de 150 fotos legendadas: trigger `hrn_person`, 500 etapas, rank/alpha 16, resolução alvo 768, LR `0.0001`, seed 42, checkpoint a cada 100 etapas. Perfil **Automático** escolhe o plano de memória a partir da GPU real; `low` e `balanced` são alternativas explícitas. Batch máximo 1 e acumulação 2 mantêm o batch efetivo da receita local. Rank e resolução altos podem exceder a VRAM; não há promessa de caber qualquer configuração.
5. Clique **Preparar projeto**. A primeira vez instala o treinador, baixa aproximadamente 15,5 GB de pesos, verifica imagens/configuração e cria caches de texto e latentes. Requer Linux, GPU NVIDIA com BF16 e pelo menos 16 GB de VRAM, driver 580 ou mais recente, e 35 GiB livres no disco temporário antes da instalação. Reserve espaço adicional no Drive para pesos e checkpoints. A primeira preparação consome tempo da sessão; não inicia o treino.
6. Quando aparecer **Pronto para iniciar**, clique **Iniciar treinamento**. Etapa, loss e estimativa restante vêm de `metrics.jsonl`, sem estimativas de velocidade inventadas para uma GPU ainda não testada. A estimativa ignora preparações, salvamentos e variação futura de velocidade.
7. **Solicitar parada** envia SIGINT ao grupo de processos verificado. Aguarde sua saída; a parada pode levar tempo e não garante um novo checkpoint. **Retomar checkpoint** usa o último checkpoint completo, com otimizador e RNG. Etapas depois dele são perdidas. Preparar o mesmo projeto em outra VM refaz somente o ambiente/caches temporários e permite a retomada pelo Drive. O número de etapas é o total alvo, não etapas adicionais.
8. Ao terminar, **Adicionar LoRA à biblioteca ComfyUI** copia e verifica o export `*-comfy.safetensors` em `models/loras`, com nome derivado do SHA-256. Não sobrescreve um modelo diferente. Atualize a lista de LoRAs no ComfyUI e use o mesmo modelo MOSS SFT; o trigger continua sendo o das legendas. Este fluxo não executa geração de imagens automaticamente.

Salvar outro nome ou outros parâmetros cria outro projeto; não sobrescreve o treino anterior. Para repetir a mesma receita do zero, dê um nome novo. Até 100 projetos aparecem na lista. O ZIP original é mantido para preparar novamente em outra VM.

### Persistência e recuperação

Todos os caminhos ficam dentro da raiz nova do Drive, normalmente `MyDrive/EasyComfyColab2`:

| Caminho | Conteúdo |
| --- | --- |
| `training/uploads/<sha256>.zip` | Dataset original verificado |
| `training/projects/<id>.json` | Receita reutilizável e revisões fixas |
| `training/projects/<id>.receipt` | Manifesto SHA-256 dos resultados verificados |
| `training/weights/` | Pesos MOSS SFT, Qwen INT8 e VAE, reutilizados entre VMs |
| `output/training/<id>/<nome>/` | Métricas, logs, até 3 checkpoints completos e exports |
| `models/loras/<nome>_<hash>.safetensors` | Cópia explícita do export ComfyUI |

Resultados de treino sempre vão para o Drive, **inclusive se a geração de imagens estiver configurada para PC**. O otimizador é necessário para retomar; o export do LoRA sozinho serve para inferência. Os caches ficam apenas no disco da VM. Uma falha de escrita no Drive pode impedir checkpoint e preservação.

Enquanto prepara ou treina, o guard do ComfyUI rejeita novos prompts. Reinício, limpeza de memória, alterações de nodes/cache/downloads, entrega de resultados e encerramento aguardam o treino e a verificação dos arquivos. O desligamento automático também considera esse estado. Se o worker morrer, o app verifica o grupo de processos: um filho ainda ativo mantém o bloqueio. Estado ou identidade desconhecidos não liberam a VM. **Verificar resultados no Drive** tenta recuperar o estado depois que todos os processos saírem. Um diário perdido exige recuperação manual; não apague marcadores para forçar a liberação.

Retomada após queda da VM depende dos arquivos já gravados no Drive e de um checkpoint completo (`state.json`, `trainable.safetensors`, `optim.pt`, `rng.0.pt`). Checkpoints `.partial` não são oferecidos para retomada. Arquivos parciais são preservados para inspeção, sem serem tratados como checkpoints válidos.

### Versões e limites da validação

- KohakuMossTrainer: `da5dd97e15492e4dfb3caa171f023388004884ff`.
- Pesos `Comfy-Org/MOSS-Image`: `ee64003102c8470ee32501cf883419235db81884`, variante SFT INT8 ConvRot.
- Ambiente separado: PyTorch `2.14.1`, CUDA `13.0`; dependências principais fixadas em `remote/training_worker.py`. Dependências transitivas são resolvidas pelo pip e não constituem um lock Linux completo.
- A receita usa LoRA BF16, texto/VAE em cache, sem compilação de modelo nem amostras automáticas. Não usa Spectrum na otimização do LoRA.
- Testes locais cobrem validação de ZIP/configuração, vínculo de sessão, preservação, retomada, export, shutdown e interface. **Nenhuma VM foi alocada e nenhum treino foi executado no Colab durante esta implementação.** Compatibilidade do conjunto Linux/CUDA/driver e velocidade na RTX 6000 ainda exigem um primeiro ensaio real autorizado.
- A distribuição Windows de avaliação não inclui credenciais nem configuração privada OAuth. Use a configuração Google do projeto/distribuição existente. As fontes Tauri foram atualizadas, mas a compilação Rust não foi validada nesta máquina sem Cargo.

## English

The **LoRA training** tab uses the desktop app's existing owned GPU session. It never allocates another VM or starts a notebook. Upload a ZIP with one image folder and matching UTF-8 `.txt` sidecars, select the caption trigger, then **Prepare project**. Preparation installs a separate pinned trainer environment, downloads the SFT INT8 model and creates offline caches. Click **Start training** separately when ready.

Default recipe: 500 total steps, LoRA rank/alpha 16, target area 768², learning rate 1e-4, seed 42, maximum batch 1 with accumulation 2, checkpoint every 100 steps, latest 3 retained. Memory planning uses the actual GPU. Requirements: Linux, BF16 NVIDIA GPU with at least 16 GB VRAM, driver 580+, 35 GiB initial free VM disk, plus Drive storage for about 15.5 GB of weights and training results. Larger custom settings can exceed memory. First preparation consumes session time.

Datasets are limited to 1 GiB compressed / 3 GiB expanded, 5–5,000 images. Every image needs a sidecar containing the exact trigger. Symlinks, traversal, duplicate paths, ambiguous folders, encrypted and oversized ZIP entries are rejected. Uploads and exports are SHA-256 verified. Private datasets are copied only to the connected account's new Drive tree, not GitHub.

Projects, datasets and weights persist under `EasyComfyColab2/training`; checkpoints and exports always persist under `EasyComfyColab2/output/training`, even with PC image delivery selected. Reopen a saved project, prepare it on a new VM, and **Resume checkpoint**. Only complete checkpoints with optimizer/RNG are resumable; progress after the last checkpoint is lost. New settings/name create a separate project. Use a new name to repeat a recipe from scratch.

Stop requests send SIGINT to the verified worker process group. Admission, restart and shutdown stay blocked until the worker and its children exit and Drive files are verified. Unknown state preserves the VM. **Verify Drive results** recovers an exited worker; missing identity journals require manual recovery. **Add LoRA to ComfyUI library** copies the real ComfyUI export into `models/loras` without overwriting different content. Sampling is never automatic.

Local controller/runtime/UI tests and a Windows sidecar smoke test have been run. **No Colab allocation, cloud GPU training or RTX 6000 benchmark has been run.** The first authorized cloud run must validate the pinned Linux environment and actual received GPU. The Windows evaluation package excludes private OAuth configuration and credentials. Tauri allowlists were updated; Rust compilation was not validated without Cargo. Upstream pins and license references are recorded below and in `remote/THIRD_PARTY_NOTICES.md`.
