"""Isola o token do Colab CLI quando o aplicativo seleciona outro perfil.

Carregado pelo arquivo .pth do ambiente virtual, inclusive no keep-alive
criado pelo próprio CLI. Todos os perfis do aplicativo são isolados.
"""

import os

token_path = os.environ.get("COMFY_COLAB_TOKEN_PATH")
if token_path:
    from colab_cli import auth

    auth.TOKEN_CONFIG_PATH = token_path
