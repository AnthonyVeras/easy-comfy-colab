# Segurança

## Relatar vulnerabilidades

Use a área **Security → Report a vulnerability** do GitHub quando estiver disponível. Se o recurso não estiver habilitado, abra uma issue pedindo um canal privado, sem publicar detalhes exploráveis, tokens, URLs assinadas ou arquivos privados.

Nunca envie credenciais reais para reproduzir um problema. Se uma credencial foi exposta, revogue-a no provedor; remover a linha de um commit não invalida a credencial.

## Dados sensíveis

O repositório e os pacotes de release não devem conter dados de conta. O instalador cria seus próprios diretórios de runtime fora do Git. Tokens de modelos no Windows usam DPAPI; isso dificulta leitura por outro usuário/máquina, mas não protege contra código malicioso executado como o mesmo usuário.

Tokens OAuth do CLI/Drive e a chave SSH vivem no usuário Linux, com permissões locais restritas. A autorização de cópia de Drive pede acesso amplo aos arquivos, separado da autorização do CLI. Confira a conta e as permissões no fluxo oficial do Google.

Logs e diagnósticos podem conter e-mail, caminhos, identificadores de sessão e nomes de arquivos. Revise antes de compartilhá-los. O aplicativo não transmite diagnósticos automaticamente aos mantenedores.

Imagens de instalação em `ComfyColab/.runtime-images` são privadas e não devem ser anexadas a issues/releases. O builder exclui dados do usuário e nomes conhecidos de arquivos de credenciais, mas isso não detecta segredos arbitrariamente embutidos por nodes. O pacote público contém apenas código, recursos e o aplicativo Windows compilado; nunca contém essas imagens remotas.

## Portas e código de terceiros

ComfyUI/MCP/downloads são expostos somente em loopback através de SSH. Não existe autenticação adicional entre processos locais e essas portas. Não encaminhe `18188`/`18189` para a internet, nem ligue serviços em `0.0.0.0` sem adicionar controles apropriados.

Custom nodes executam código na VM com acesso ao Drive montado. Instale somente código em que confie. Modelos também têm formatos com riscos distintos; obtenha-os de fontes confiáveis. A lista de nodes upstream não constitui uma auditoria de todos eles.

## Escopo de suporte

Esta é uma versão experimental mantida pela comunidade. Relatos serão triados conforme disponibilidade, sem SLA. A varredura de publicação reduz risco de exposição acidental; não certifica que o aplicativo inteiro esteja livre de vulnerabilidades.
