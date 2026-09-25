# Changelog

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
