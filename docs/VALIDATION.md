# Validação da primeira publicação

Verificação local da edição 2.0.1, em 25/09/2026. Nenhuma VM foi alocada para preparar esta publicação.

| Verificação | Resultado |
| --- | --- |
| Regressões de downloads, Drive simulado, perfis, créditos e inatividade | 20 testes passaram no Windows |
| Instalação limpa, configuração WSL, caminhos com espaços e isolamento de perfis | 5 testes passaram |
| Aba/rolagem durante polling e término da autorização | 2 testes gráficos passaram |
| SSH compartilhado e recuperação HTTP 429 | Teste Bash simulado passou no WSL |
| Cancelamento de início e término de processos filhos | Teste Bash simulado passou no WSL |
| Sintaxe dos scripts Bash e PowerShell | Aprovada |
| Ruff: nomes, imports e erros estáticos | Aprovado |

Os testes de Drive usam respostas simuladas; não comprovam a cópia real de arquivos entre contas. Os testes de SSH não comprovam a disponibilidade do Colab. O build Windows comprova o empacotamento local; a instalação integral em uma máquina nova ainda precisa de validação.

## Revisão para publicação

A distribuição parte de uma lista explícita de código e recursos. Não inclui dados da instalação pessoal: perfis, tokens, chaves, logs, histórico, workflows, imagens ou modelos. O repositório tem histórico novo, sem commits da instalação anterior.

`scripts/check_public_tree.py` verifica os arquivos rastreados; uma varredura complementar com Gitleaks é executada antes da publicação. As verificações reduzem o risco de exposição, mas não substituem revisão humana de futuras contribuições. O pacote Windows é criado com arquivos rastreados e um build novo; dados locais ignorados pelo Git não entram no ZIP.

O [GitHub Actions](https://github.com/AnthonyVeras/easy-comfy-colab/actions) mostra o resultado atualizado das verificações em Windows e Linux com Python 3.11. Testes de GUI ficam fora desse pipeline porque exigem uma sessão gráfica.

## Ainda não comprovado nesta edição pública

- Instalação do zero em outro computador e outras distribuições WSL.
- Nova autorização Google e montagem de Drive usando o namespace público de credenciais.
- Disponibilidade e funcionamento de G4/RTX PRO 6000 no Colab.
- Cópia real entre duas contas, quotas do Drive e atalhos da biblioteca no DriveFS.
- Compatibilidade de todos os custom nodes e de cada família de modelos.
- Desempenho ou custo para uma GPU específica.

O fluxo original foi usado em uma instalação pessoal. A edição pública modifica os caminhos e o isolamento da autenticação; essa experiência anterior não deve ser interpretada como validação ponta a ponta desta distribuição em todas as máquinas.
