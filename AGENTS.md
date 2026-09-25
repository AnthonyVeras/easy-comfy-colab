# Orientação para contribuidores e agentes

- Este é o código público do Easy Comfy Colab, não um backup de uma máquina pessoal.
- Não incluir credenciais, dados de conta, modelos, imagens privadas, logs reais ou workflows de usuários.
- Usar os caminhos dinâmicos de `app/environment.sh` e `app/runtime_config.py`; não fixar nomes de usuário ou diretórios pessoais.
- Executar somente testes locais por padrão. Iniciar VM/geração real exige autorização de quem controla a conta e pode consumir créditos.
- Manter um único transporte SSH por perfil. Não abrir SSH externo enquanto o master do aplicativo estiver ativo.
- Preservar aba/rolagem durante polling. Credenciais nunca devem ser registradas em logs ou URLs.
- Para alterações na integração MCP, verificar o endpoint real e `server_info` antes de operar um workspace. Caminhos de ferramentas remotas são Linux da VM.
- Não alterar configurações globais de clientes MCP automaticamente. Não publicar pacotes contendo perfis locais.
- Documentar resultados reais e distinguir verificação local de teste em Colab/Drive.
