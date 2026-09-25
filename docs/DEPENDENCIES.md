# Versões e componentes externos

| Componente | Referência |
| --- | --- |
| Aplicativo | 2.0.1, primeira distribuição comunitária |
| Colab CLI no WSL | `google-colab-cli==0.7.2` |
| Easy Install | Branch MAC-Linux, commit `2a979fae03ac6c4adc607634b0ef8432e80ecc3e` |
| ComfyUI | Commit `1568e6cfd04586a4b3c4e1817ea7dde09b1bf9e7` |
| Comfy MCP | `comfy-mcp==0.10.0` |
| comfy-cli na VM | `comfy-cli==1.21.0` |
| PyTorch / CUDA | Fornecidos pelo runtime Colab; o instalador procura preservá-los |
| Dependências Windows | Fixadas em `requirements.txt` / `requirements-dev.txt` |

O instalador usa a lista de nodes do Easy Install, ignorando o Manager legado quando a versão do ComfyUI oferece sua integração atual. A lista é extraída da revisão fixada, mas cada node é clonado do estado disponível no seu repositório e seus requisitos podem mudar. Portanto, a instalação não é completamente reprodutível.

`assignment_guard.py`, `colab_auth_hook.py` e a autorização de Drive usam partes internas do CLI. Atualizar o CLI sem rever esses pontos pode quebrar alocação ou autenticação. Pins tornam a base revisável, mas não garantem compatibilidade futura com os serviços Google.

Há uma correção localizada de compatibilidade para o identificador `qwen_image21` no loader ComfyUI-GGUF. Ela valida o formato esperado antes de modificar a cópia temporária do node. Nenhum peso desse modelo é distribuído aqui.

Antes de atualizar revisões, confira: nova sessão, cancelamento, restauração de perfil, montagem do Drive, transporte SSH compartilhado, importação de nodes e inferência pequena. Documente os resultados e quaisquer custos de testes remotos.
