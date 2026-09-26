# Changelog

## 2.0.7 — correção do seletor de idioma

- Corrige a troca pelo menu Português / English: a limpeza da interface agora remove apenas callbacks globais de rolagem, preservando os timers internos do CustomTkinter e cancelando os timers dos widgets substituídos.
- Regressão aciona o menu real nos dois sentidos e verifica erros de callback, preservação de campos e ausência de comandos de VM.

## 2.0.6 — português e inglês

- Seletor Português (Brasil) / English em Configurações, com preferência persistente neste computador.
- Tradução da interface, tabelas, confirmações, mensagens locais e progresso resumido das operações. Créditos usam o formato numérico do idioma escolhido.
- Troca imediata preserva sessão, autorizações pendentes, campos preenchidos, seleções e aba/rolagem; não reinicia ComfyUI nem a VM.
- Catálogo local sem dependências novas. Logs técnicos remotos e conteúdo do usuário mantêm o idioma original.
- Testes de catálogo, persistência, alternância com operação simulada, falha de salvamento e preservação de comandos.

## 2.0.5 — imagem de instalação no Drive e cache de modelos

Esta publicação reúne as melhorias desenvolvidas nas versões pessoais 2.0.3–2.0.5.

- Restauração padrão de uma imagem privada do Drive para o disco da VM, com verificação SHA-256 e compatibilidade da base Python/PyTorch.
- Preparação da primeira imagem em segundo plano após uma instalação convencional; o pacote é criado na conta do usuário e não acompanha a distribuição.
- Botão **Atualizar imagem do Drive**, sem reiniciar ComfyUI, e atualização antes do encerramento da VM. Falhas de salvamento mantêm a VM ligada.
- Preservação de nodes Git, Registry e ZIP e das dependências adicionais instaladas. A imagem anterior continua guardada no Drive.
- Cache opcional de modelos no disco da VM, seleção por workflow e pré-leitura opcional na RAM com limite e reserva de memória.
- Tempos por etapa, timeout nas verificações HTTP, segunda tentativa de montagem do Drive e reutilização das dependências do MCP.
- Cache pip local, restrições para preservar a pilha Torch e interrupção de instalações incompletas. FishAudioS2 fica fora da lista enquanto sua origem fixada está indisponível.
- Testes de integridade, falha de gravação, encerramento protegido, preservação do Drive e isolamento de perfis.

Na instalação de referência, o usuário reportou redução de aproximadamente 17 para 4 minutos em uma nova sessão. Esse resultado não é um benchmark universal. A primeira instalação sem imagem continua mais demorada; veja [validação](docs/VALIDATION.md).

## 2.0.2 — destino dos resultados

- Escolha por conta entre outputs no Drive e no PC, aplicada no início ou no reinício apenas do ComfyUI.
- Cópia automática de outputs temporários com o aplicativo aberto e fila vazia.
- Encerramento bloqueado se a cópia final verificada falhar; tentativa de retomar o servidor pausado.
- Pastas locais `Comfy Colab Results/input` e `output`, com separação por conta e VM.
- Sem migração ou exclusão dos arquivos antigos no Drive ou nas pastas anteriores.

## 2.0.1 — primeira edição comunitária

- Publicação do aplicativo Windows com sete abas, downloads paralelos por URL, perfis de conta, monitor, histórico, diagnóstico de workflows e controles de sessão.
- Interface local com inferência na VM e armazenamento persistente no Google Drive.
- Transporte SSH compartilhado corrige conexões concorrentes HTTP 429.
- Atualizações de status preservam aba e rolagem; envio de autorização limpa o estado pendente.
- Caminhos pessoais removidos; preparação Windows/WSL e credenciais isoladas por instalação/perfil.
- Documentação pública, política de segurança, contribuição, testes e licença MIT.

### Ainda experimental

G4, cópia/compartilhamento real entre duas contas e instalação integral em novas máquinas. Veja `docs/VALIDATION.md`.
