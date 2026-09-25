# Dependências e atribuições

A licença MIT deste repositório cobre o código original do Easy Comfy Colab. Cada dependência, custom node e modelo mantém sua própria licença. Os projetos abaixo são instalados separadamente, não copiados integralmente para este repositório:

| Projeto | Uso |
| --- | --- |
| [Google Colab CLI](https://github.com/googlecolab/google-colab-cli) | Alocação, autorização, execução e transporte SSH da VM; 0.7.2 na preparação local |
| [ComfyUI](https://github.com/Comfy-Org/ComfyUI) | Servidor de inferência e editor de workflows |
| [ComfyUI-Easy-Install](https://github.com/Tavris1/ComfyUI-Easy-Install) | Lista de custom nodes e recursos do instalador MAC-Linux |
| [Comfy MCP](https://github.com/Comfy-Org/comfy-mcp) | Ferramentas MCP, instaladas dentro da VM |
| [comfy-cli](https://github.com/Comfy-Org/comfy-cli) | Integração do MCP com o workspace remoto |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | Interface Windows |
| [Pillow](https://python-pillow.github.io/) | Geração do ícone original do aplicativo |
| [psutil](https://github.com/giampaolo/psutil) | Métricas locais e remotas |
| [aiohttp](https://github.com/aio-libs/aiohttp) e [Requests](https://github.com/psf/requests) | Serviço de downloads na VM |
| [PyInstaller](https://pyinstaller.org/) | Empacotamento do executável |

O teste de inferência usa [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN), baixado da distribuição [Comfy-Org no Hugging Face](https://huggingface.co/Comfy-Org/Real-ESRGAN_repackaged). Os pesos não estão no Git nem no pacote Windows. Consulte as respectivas licenças antes de redistribuir modelos ou nodes.

O aplicativo é uma integração independente. Não é um produto oficial do Google, Comfy Org ou Tavris1. Marcas e nomes de terceiros pertencem aos respectivos titulares.
