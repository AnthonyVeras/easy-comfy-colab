# Solução de problemas

## O aplicativo pede Setup.ps1

Execute a preparação na raiz do projeto. O `.exe` não substitui WSL, o ambiente do CLI ou a chave SSH. Confira `wsl --list --verbose` e o nome passado em `Setup.ps1 -Distro ...`. O usuário Linux pode ser informado com `-WslUser`.

## HTTP 429: Already-active SSH session

O Colab limita o transporte SSH desta integração. A partir da 2.0.1, comandos e túneis compartilham um master. Feche clientes SSH externos conectados à mesma VM. Use Reconectar; a abertura aguarda brevemente a liberação da conexão anterior. Não abra um `colab ssh` paralelo enquanto o aplicativo estiver ligado.

## Autorização aparece novamente

Colab CLI, montagem do Drive e API de cópia de Drive são autorizações distintas. Confira qual conta está selecionada. Envie o código no modo de código; na montagem do Drive, conclua no navegador e use **Já autorizei**. Não publique o URL de autorização, códigos ou tokens em issues.

## Abas ou scroll voltam ao início

Esse comportamento foi corrigido na 2.0.1. Confira a versão no título do aplicativo. Atualizar métricas não deve mudar a aba. Se ocorrer novamente, descreva se havia uma autorização pendente e se foi usado o `.exe` antigo; remova dados pessoais de capturas.

## ComfyUI não abre, mas a VM existe

Use **Reconectar** e confira a atividade. A porta local é `18188`, não `8188`. Verifique se outro programa usa essa porta e se Windows alcança serviços localhost do WSL. Não abra dois aplicativos com VMs diferentes nessas mesmas portas.

## GPU indisponível ou memória insuficiente

O hardware é atribuído pelo Colab. Veja o nome e VRAM reais no Monitor. Encerre a sessão antes de mudar a preferência de GPU. Reduza resolução, lote, pesos ou quantização conforme o workflow. Nenhuma opção do aplicativo garante memória além do hardware recebido.

## Download 401 / 403 / 404

Use o link de um arquivo/versão. Verifique credencial da conta selecionada, permissões do token, aceite de condições no provedor e se o arquivo ainda existe. Para Civitai, informe `URL | nome.extensão`. O aplicativo não contorna restrições de acesso do provedor.

## Download interrompido

Reconecte/inicie a VM e use **Retomar**. A fila e o parcial estão no Drive. Quando a origem não fornece ETag ou não suporta intervalos, a transferência recomeça. Confira espaço disponível e não renomeie um `.part` para tentar usá-lo como modelo completo.

## Modelo não aparece no ComfyUI

Confira a categoria exigida pelo loader. Atualize as listas no ComfyUI ou reinicie somente ComfyUI. Baixar GGUF não instala um loader GGUF. Modelos compostos podem exigir vários arquivos; o downloader não clona repositórios completos.

## Cópia de Drive falhou

Confirme as duas autorizações e o espaço da conta de destino. Uma organização pode restringir compartilhamento/cópia. Atalhos na origem são recusados: use o ID da pasta original. Ao retomar, cópias verificadas são preservadas. Arquivos renomeados por conflito precisam ser conferidos nos workflows.

## Um custom node falhou ao instalar

A lista vem do Easy Install e as dependências dos nodes podem mudar independentemente. O instalador imprime falhas e conserva `node-install-failures.txt` no diretório temporário da VM. Informe o node e o erro sanitizado; não inclua tokens ou um dump completo do ambiente.

## O saldo continua caindo

Fechar o navegador ou o painel pode deixar a VM ativa. Use **Encerrar VM** e confira as sessões da conta no Colab. CU/h representa a conta inteira, portanto outras sessões também podem consumir. A automação por inatividade só atua com o aplicativo aberto e dados recentes.

## Como relatar um bug

Inclua versão, Windows/WSL/Python, hardware solicitado, etapa, resultado esperado e erro sanitizado. O diagnóstico exportado não inclui tokens deliberadamente, mas pode conter nomes de conta e informações da máquina: revise antes de publicar. Consulte [SECURITY.md](../SECURITY.md) para suspeitas de vulnerabilidade.
