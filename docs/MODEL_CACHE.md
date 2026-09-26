# Cache de modelos

O cache reduz leituras repetidas de arquivos grandes pelo DriveFS. É independente da imagem da instalação: um guarda pesos selecionados no disco temporário, a outra guarda código e dependências prontas no Drive.

## Usar

1. Abra uma sessão GPU e a aba **Modelos**.
2. Atualize a biblioteca. Selecione arquivos com Ctrl ou use **Selecionar por workflow**.
3. Ative **Usar cache da VM** e clique **Preparar selecionados**.
4. Opcionalmente ative **Preparar antecipadamente na RAM** e escolha o limite.

O cache começa habilitado, com seleção vazia e pré-leitura desligada. Não copia toda a biblioteca automaticamente. A seleção e as preferências ficam em `ComfyColab/user/comfy-colab-cache.json`; a próxima sessão tenta preparar os arquivos selecionados. São feitas até duas cópias paralelas. O worker aguarda a fila de geração e pausa entre blocos quando necessário.

Os originais ficam no Drive. Arquivos completos são publicados no cache após a cópia; parciais não são usados pelos loaders. O resolvedor verifica tamanho/data da origem e tamanho da cópia, sem calcular um hash completo a cada carregamento. Se a cópia não for válida, usa a origem. Loaders com caminhos próprios podem ignorar esse resolvedor.

## RAM e GPU

A pré-leitura usa o cache de arquivos do Linux, sujeito a descarte pelo sistema, com limite e reserva de RAM. Não é RAM fixada, não aumenta a VRAM e não elimina conversão/desquantização nem transferência CPU→GPU. A cópia inicial do Drive continua necessária em cada VM nova.

**Cancelar preparação** conserva cópias completas. **Limpar cache da VM** remove somente as cópias catalogadas e exige fila vazia; nunca apaga os originais do Drive. Falta de disco ou memória é mostrada por arquivo. Preparação ativa conta como atividade para impedir o encerramento por inatividade.

A eficácia depende do modelo, loader, cache já aquecido e vazão do Drive. Não há promessa de uma duração fixa para carregar pesos ou produzir a primeira imagem.
