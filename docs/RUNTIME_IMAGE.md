# Imagem de instalação no Drive

A versão 2.0.5 reutiliza uma instalação pronta guardada no Drive da própria conta. Ela não fornece uma VM persistente nem uma imagem Docker. O pacote é extraído nos caminhos Linux esperados em `/content/comfy-colab`.

## Fluxo

1. Alocar/reconectar a VM, abrir SSH compartilhado e montar o Drive.
2. Verificar driver e base: Python/ABI, arquitetura, libc, PyTorch e versões dos pacotes globais.
3. Se existir imagem compatível, copiá-la para o disco local, verificar SHA-256 e extrair em pasta temporária. Validar os ambientes antes de movê-los para os caminhos finais.
4. Sincronizar os nodes do app e dados do usuário; iniciar ComfyUI/MCP e executar a verificação pequena de inferência existente.
5. Sem imagem compatível, usar a instalação convencional e preparar uma imagem em segundo plano, com baixa prioridade. Essa primeira sessão pode demorar mais.

A imagem não é aplicada sobre uma instalação existente. Uma base incompatível é recusada. O log separa o tempo das etapas de alocação/SSH, montagem, instalação/restauração, sincronização, ComfyUI, MCP e verificação final.

## Atualizar

**Sessão → Atualizar imagem do Drive** salva a instalação atual sem reiniciar o servidor. Aguarde instaladores e atualizações de nodes terminarem primeiro. A rotina usa uma cópia separada; não executa pip nem troca versões durante o salvamento. Se detectar alterações nos arquivos durante a cópia, recusa a publicação.

**Encerrar VM**, **Encerrar VM e sair** e o encerramento por inatividade atualizam e verificam a imagem antes de desligar. Falhas mantêm a VM ligada. **Sair com VM ativa** apenas fecha o app; não atualiza a imagem. Expiração da VM e desligamento pelo site do Colab não executam esse salvamento. Sessões CPU e instalações que nunca foram concluídas não têm imagem ComfyUI para atualizar.

## Conteúdo e espaço

- Código versionado do ComfyUI, alterações nesses arquivos, nodes de Git/Registry/ZIP e arquivos adicionais dos nodes.
- Venv do ComfyUI e venv do MCP; dependências instaladas fora delas não são capturadas como pacotes adicionais.
- Manifesto com base, revisões, tamanho, data e SHA-256. O arquivo parcial é verificado antes de substituir o manifesto selecionado.
- Biblioteca padrão de modelos, inputs, outputs e histórico do usuário não entram no pacote. Os modelos continuam em `ComfyColab/models`.
- Caches, logs, diretórios de outputs e arquivos conhecidos de credenciais dos nodes são excluídos. Pesos dentro de um node podem aumentar bastante o tamanho. Links externos não suportados bloqueiam a cópia.

O builder exige pelo menos 12 GiB livres no disco temporário; instalações maiores podem precisar de mais. O Drive precisa acomodar a nova imagem além das anteriores, que não são apagadas automaticamente. Cópia e compactação usam CPU/disco da VM; a GPU continua consumindo créditos enquanto a VM está alocada.

**A imagem é privada.** A exclusão de nomes conhecidos não detecta todo segredo que um custom node possa embutir em código ou configuração. Não publique imagens pessoais. Nodes, modelos e dependências mantêm suas próprias licenças.

## Diagnóstico e recuperação

Na VM, consulte `runtime-image-build-result.json` em `/content/comfy-colab`. A primeira preparação em segundo plano escreve `runtime-image-build.log`; atualizações manuais mostram a saída na atividade do app. `ok: true` confirma a gravação verificada. Manifests e arquivos ficam em `Meu Drive/ComfyColab/.runtime-images/`.

Se falhar, mantenha a sessão, corrija falta de espaço, montagem do Drive ou instalação em andamento e tente novamente. A imagem anterior permanece guardada. O aplicativo não oferece um desligamento que ignore essa proteção. Fechar a sessão diretamente fora do aplicativo pode perder alterações ainda não salvas.

O executável público não contém o ambiente remoto pronto. Uma instalação nova cria a imagem na conta autorizada pelo próprio usuário. O relato de inicialização em quatro minutos vem da instalação de referência; não mede todas as GPUs, bases, redes ou autorizações.
