# Validação das publicações

## Atualização 2.0.7 — 26/09/2026

- Reproduzido o defeito acionando o menu nativo do Tk: a preferência era salva, mas a exclusão de um callback DPI já expirado interrompia a reconstrução da tela.
- A limpeza remove apenas callbacks globais de rolagem e cancela timers dos widgets substituídos; mantém os timers de sessão e do CustomTkinter.
- Cinco regressões gráficas e dois testes de catálogo/persistência passaram. O teste de alternância agora invoca o menu nos dois sentidos e detecta exceções de callback. Ruff aprovado.
- Executável pessoal 2.0.7 verificado por cliques reais em Português / English, em prévia isolada. Nenhuma VM foi iniciada, reiniciada ou encerrada para essa correção.

## Atualização 2.0.6 — 26/09/2026

- 46 testes Python locais aprovados: catálogo, placeholders, preferência persistente e inválida, troca de idioma durante operação simulada, campos/aba/rolagem e falha ao salvar.
- Ruff e sintaxe Python 3.11 aprovados. Testes de idioma sem interface incluídos no CI Windows/Linux; testes gráficos executados localmente.
- Interface, confirmações e mensagens do aplicativo traduzidas. Logs técnicos externos mantêm o idioma original; o idioma do editor ComfyUI é independente.
- A mudança de idioma não inicia comandos de VM. Nenhuma VM real foi reiniciada para testar a tradução.
- Executáveis público e pessoal compilados e abertos em prévia isolada; a instância pessoal anterior continuou aberta. O pacote público foi separado dos dados e credenciais pessoais.

## Atualização 2.0.5 — 26/09/2026

- 42 testes Python passaram no Windows, incluindo instalação limpa, isolamento de perfis, cache de modelos, imagem corrompida/incompatível, preservação de manifests e controles de interface.
- Ruff aprovado. Os novos testes de imagem/cache foram incluídos no CI Windows/Linux com Python 3.11.
- Quatro testes Bash simulados passaram no WSL: SSH compartilhado, cancelamento, cópia de outputs e imagem antes do encerramento. Sintaxe Bash/PowerShell aprovada.
- Executável Windows compilado e aberto em modo de prévia, sem autenticação ou conexão com VM.
- Política da árvore pública aprovada para 76 arquivos. Gitleaks 8.30.1 sem achados nos fontes, histórico inspecionado e arquivos do build; marcadores pessoais também verificados nos binários e no código Python empacotado, sem achados.
- A publicação não aloca, reinicia ou encerra VMs e não modifica a instalação pessoal.

Na instalação de referência, antes desta publicação, a restauração isolada do pacote levou aproximadamente 41 segundos com o Drive já aquecido. O usuário depois reportou inicialização completa de uma nova sessão em quatro minutos, frente a aproximadamente 17 minutos anteriormente. Também foi concluída uma atualização real da imagem com verificação de integridade, sem reiniciar o servidor. São evidências da instalação de referência; não são benchmarks de outras máquinas nem validação de uma conta nova na edição comunitária.

A edição pública usa configuração dinâmica e perfis inicialmente desconectados. Não importa contas, imagem pronta, modelos, logs ou workflows da instalação de referência. A distribuição do zero em outro computador e cópia real entre contas continuam pendentes de validação externa.

## Atualização 2.0.2

A escolha de destino e a persistência ao reiniciar foram verificadas com diretórios temporários. O teste Bash de encerramento confirma que falha na cópia impede desligar a VM e tenta retomar o servidor; cópia aprovada permite encerrar. A VM em uso não foi reiniciada para testar esta atualização. O fluxo de cópia PC ainda não foi validado ponta a ponta em uma sessão real.

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
| Build Windows e abertura do executável em modo de prévia | Aprovados, sem autenticação ou VM |
| Política da árvore pública | 64 arquivos rastreados, sem achados |
| Gitleaks 8.30.1 no histórico e no pacote Windows | Nenhum segredo detectado |
| Marcadores pessoais em fontes, binários e código Python empacotado | Nenhum achado |

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
