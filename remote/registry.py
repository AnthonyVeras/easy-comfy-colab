"""Read-only, bounded discovery through the public Comfy Registry contract."""
import json
import re
import threading
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from .common import Fault
except ImportError:
    from common import Fault

BASE = "https://api.comfy.org"
CAPACITY = threading.BoundedSemaphore(3)
NODE_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
STABLE = re.compile(r"\d+\.\d+\.\d+(?:\+[a-zA-Z0-9.-]+)?")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args): return None


def fetch(route, params):
    if not CAPACITY.acquire(blocking=False): raise Fault("registry_busy", "Wait for the current Registry requests before refreshing.")
    try:
        # URLs, headers and route names are constructed here, never provided by callers.
        url = BASE + route + ("?" + urlencode(params) if params else "")
        with build_opener(NoRedirect).open(Request(url, headers={"Accept": "application/json"}), timeout=12) as response:
            payload = response.read(2*1024*1024+1)
        if len(payload) > 2*1024*1024: raise Fault("registry_too_large", "Registry metadata exceeded the bounded response limit.")
        return json.loads(payload)
    except Fault: raise
    except Exception: raise Fault("registry_unavailable", "The official Registry could not be verified. Refresh when its connection is available.") from None
    finally: CAPACITY.release()


def identifier(value):
    if not isinstance(value, str) or not NODE_ID.fullmatch(value): raise Fault("invalid_node", "Choose a registered node identifier.")
    return value


def stable(row):
    return isinstance(row, dict) and isinstance(row.get("version"), str) and STABLE.fullmatch(row["version"]) is not None and row.get("status") == "NodeVersionStatusActive" and row.get("deprecated") is False


class Registry:
    def __init__(self, core_version, frontend_version, request=fetch):
        self.core_version, self.frontend_version, self.request = core_version, frontend_version, request

    def compatibility(self, row):
        checks, unknown = 0, False
        for field, accepted in (("supported_os", {"os independent", "posix", "posix :: linux", "posix :: linux :: ubuntu", "linux"}), ("supported_accelerators", {"cuda", "nvidia", "nvidia cuda"})):
            values = row.get(field, [])
            if not isinstance(values, list) or len(values) > 30 or any(not isinstance(x, str) for x in values):
                unknown = True; continue
            if values:
                checks += 1
                if not accepted.intersection(x.lower() for x in values):
                    return {"state": "incompatible", "reason": "Declared operating system or accelerator excludes this Linux CUDA runtime."}
        for field, actual in (("supported_comfyui_version", self.core_version), ("supported_comfyui_frontend_version", self.frontend_version)):
            value = row.get(field)
            if not value: continue
            try:
                from packaging.specifiers import SpecifierSet
                if not isinstance(value, str) or len(value) > 200 or actual is None: raise ValueError()
                spec = "==" + value if re.fullmatch(r"\d+\.\d+\.\d+", value) else value
                spec = "==" + spec[1:] if spec.startswith("=") and not spec.startswith("==") else spec
                if not SpecifierSet(spec).contains(actual):
                    return {"state": "incompatible", "reason": "Declared ComfyUI or frontend version excludes this pinned runtime."}
                checks += 1
            except Exception: unknown = True
        if unknown or not checks: return {"state": "unknown", "reason": "Compatibility is not fully declared in a supported format; execution has not been tested."}
        return {"state": "compatible", "reason": "Declared constraints match this runtime; node execution has not been tested."}

    def catalog(self, query="", offset=0, limit=50, installed=None):
        if not isinstance(query, str) or len(query) > 200 or any(ord(x) < 32 for x in query): raise Fault("invalid_query", "Use a node search up to 200 characters.")
        route = "/nodes/search" if query.strip() else "/nodes"
        params = {"limit": 100}
        if query.strip(): params["search"] = query.strip()
        else: params.update(comfyui_version=self.core_version or "unknown", form_factor="git-linux", sort="id")
        rows, total = [], None
        first = offset//100+1
        for number in range(first, (offset+limit-1)//100+2):
            result = self.request(route, dict(params, page=number))
            if not isinstance(result, dict) or result.get("page") != number or result.get("limit") != 100 or type(result.get("total")) is not int or not 0 <= result["total"] <= 1000000 or not isinstance(result.get("nodes"), list) or len(result["nodes"]) > 100:
                raise Fault("registry_state", "Registry pagination could not be verified; no incomplete catalog was reported.")
            if total is not None and total != result["total"]: raise Fault("registry_changed", "The Registry changed while reading this page. Refresh the catalog.")
            total = result["total"]
            rows.extend(result["nodes"])
            if number*100 >= total: break
        selected = rows[offset % 100:offset % 100+limit]
        items = []
        for row in selected:
            if not isinstance(row, dict): raise Fault("registry_state", "Registry returned invalid node metadata.")
            key = identifier(row.get("id"))
            if row.get("status") != "NodeStatusActive": continue
            newest = row.get("latest_version", {})
            current = (installed or {}).get(key, {})
            items.append({"id": key, "name": str(row.get("name") or key)[:200], "description": str(row.get("description") or "")[:1500], "installed": bool(current), "version": current.get("ver"), "latest_version": newest.get("version") if stable(newest) else None, "compatibility": self.compatibility(newest if stable(newest) else row)})
        return {"items": items, "total": total or 0, "offset": offset, "limit": limit}

    def versions(self, node_id, offset=0, limit=50):
        key = identifier(node_id)
        rows = self.request("/nodes/"+key+"/versions", {"statuses": "NodeVersionStatusActive"})
        # The official versions endpoint is an array, not paginated. Bound it before local pagination.
        if not isinstance(rows, list) or len(rows) > 5000: raise Fault("registry_state", "Registry version metadata exceeded the supported limit.")
        values = {row["version"]: {"version": row["version"], "compatibility": self.compatibility(row)} for row in rows if stable(row) and row.get("node_id") == key}
        items = sorted(values.values(), key=lambda row: tuple(int(x) for x in row["version"].split("+")[0].split(".")), reverse=True)
        return {"id": key, "items": items[offset:offset+limit], "total": len(items), "offset": offset, "limit": limit}

    def verify_version(self, node_id, version):
        if not isinstance(version, str) or not STABLE.fullmatch(version): raise Fault("invalid_version", "Choose an explicit stable node version.")
        # Validation uses the same complete, bounded active version set as the picker, not the install URL.
        key = identifier(node_id)
        rows = self.request("/nodes/"+key+"/versions", {"statuses": "NodeVersionStatusActive"})
        if not isinstance(rows, list) or len(rows) > 5000: raise Fault("registry_state", "Registry version metadata exceeded the supported limit.")
        selected = next((row for row in rows if stable(row) and row.get("node_id") == key and row["version"] == version), None)
        if selected is None: raise Fault("node_version_unavailable", "This version is not an active, stable Registry release. Refresh its version choices.")
        report = self.compatibility(selected)
        if report["state"] == "incompatible": raise Fault("node_incompatible", report["reason"])
        return report
