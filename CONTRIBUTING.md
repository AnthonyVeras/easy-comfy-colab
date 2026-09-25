# Contribuindo

Issues e pull requests são bem-vindos em português ou inglês. Comece por uma mudança pequena e descreva o problema concreto que ela resolve. Para mudanças grandes, abra uma issue antes de alterar a arquitetura.

## Ambiente

Use Python 3.11+ no Windows. `Setup.ps1 -SkipWsl` prepara o ambiente para desenvolvimento local; o caminho normal de operação também requer WSL. Dependências ficam em `requirements-dev.txt`.

```powershell
.\.venv\Scripts\python.exe -m ruff check app remote tests scripts
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_v2.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_portability.py -v
```

No Linux/WSL, use `bash tests/test_ssh_transport.sh` e `bash tests/test_cancel_start.sh`. A regressão `test_hotfix_201.py` precisa de uma sessão gráfica. Testes de PR devem usar mocks/diretórios temporários; **não aloque VMs nem use credenciais reais em CI**.

## Organização e invariantes

- `ui.py` organiza telas; `session.py` controla operações; `main.py` coordena serviços.
- Rede e processos longos ficam em workers, com resultados enviados à fila Tk.
- Atualização de estado não deve navegar entre abas ou mover scroll.
- Um master SSH por perfil deve servir comandos e túneis.
- Downloads incompletos não recebem o nome final do modelo.
- Estado de fila desconhecido não autoriza desligamento automático.
- Tokens não entram em URLs, logs, workflows, fixtures ou argumentos de processo.
- Armazenamento pessoal fica fora do Git; não copie seu AppData ou `.ssh` para fixtures.
- Mantenha scripts Bash com LF e alterações de configuração compatíveis com `environment.sh` / `runtime_config.py`.

## Antes de abrir PR

Descreva mudanças, motivo, verificações feitas e limitações. Inclua testes quando houver risco de perda de arquivos, autenticação, cobrança ou regressão funcional. Para mudanças visuais, use capturas com dados fictícios. Nunca inclua conteúdos privados de contas.

Execute `git diff --cached --stat`, confira os arquivos e rode um scanner de segredos, por exemplo `gitleaks git --redact`. O `.gitignore` é uma barreira auxiliar, não uma prova de ausência de segredos. Pesos, resultados, tokens, logs de sessões e arquivos privados não pertencem ao repositório.

Contribuições de código original são oferecidas sob a licença MIT do projeto. Preserve atribuições e licenças de código externo. Não inclua modelos ou assets sem licença de redistribuição.
