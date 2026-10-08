"""Admission policy shared with the tiny installed ComfyUI guard."""
import hmac


def protected(method, path):
    return method == "POST" and (path == "/prompt" or path.startswith("/v2/"))


def controlled(path, headers, config):
    if not path.startswith("/v2/"): return False
    supplied = headers.get("X-Easy-Control", "")
    return isinstance(supplied, str) and supplied.isascii() and hmac.compare_digest(supplied, config["bearer_token"])
