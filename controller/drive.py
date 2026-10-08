"""Account-explicit Drive copy/share; model bytes never pass through the PC."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import PurePosixPath

from controller.state import AppError

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
FIELDS = "id,name,mimeType,size,md5Checksum,modifiedTime,version,appProperties,shortcutDetails"


class Drive:
    def __init__(self, session):
        self.session = session

    def request(self, method, path, **kwargs):
        for attempt in range(4):
            response = self.session.request(method, "https://www.googleapis.com/drive/v3/" + path, timeout=60, **kwargs)
            if method == "GET" and response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if not response.ok:
                raise AppError("drive_operation_failed", "Drive refused the operation. Check access and free space; original files were preserved.")
            return response.json() if response.content else {}

    def children(self, parent):
        cursor = None
        while True:
            result = self.request("GET", "files", params={"q": f"'{parent}' in parents and trashed=false", "pageSize": 1000,
                                  "fields": f"nextPageToken,files({FIELDS})", **({"pageToken": cursor} if cursor else {})})
            yield from result.get("files", [])
            cursor = result.get("nextPageToken")
            if not cursor:
                return

    def root(self, create=False):
        matches = [item for item in self.children("root") if item["name"] == "EasyComfyColab2"]
        if len(matches) > 1 or matches and matches[0]["mimeType"] != FOLDER:
            raise AppError("drive_root_conflict", "The new Drive root is ambiguous or a shortcut. Resolve it before copying.")
        if matches:
            return matches[0]
        if not create:
            raise AppError("drive_root_missing", "Start the new runtime once to create its EasyComfyColab2 Drive folder.")
        return self.request("POST", "files", json={"name": "EasyComfyColab2", "mimeType": FOLDER, "parents": ["root"]}, params={"fields": FIELDS})

    def share(self, folder, email, role):
        result = self.request("GET", f"files/{folder}/permissions", params={"fields": "permissions(id,emailAddress,role)"})
        existing = next((p for p in result.get("permissions", []) if p.get("emailAddress", "").casefold() == email.casefold()), None)
        if existing:
            if existing["role"] in ("owner", "organizer", "fileOrganizer", "writer") or role == "reader":
                return
            self.request("PATCH", f"files/{folder}/permissions/{existing['id']}", json={"role": role})
        else:
            self.request("POST", f"files/{folder}/permissions", params={"sendNotificationEmail": "false"}, json={"type": "user", "role": role, "emailAddress": email})


def version(item):
    return hashlib.sha256((item["id"] + "|" + str(item.get("version", item.get("modifiedTime", "")))).encode()).hexdigest()


def verified(source, target):
    if source["mimeType"] != target.get("mimeType") or source.get("size") != target.get("size"):
        return False
    tracked = target.get("appProperties", {}).get("easySource") == source["id"] and target.get("appProperties", {}).get("easyVersion") == version(source)
    if target.get("name") != source.get("name") and not tracked:
        return False
    if source.get("md5Checksum"):
        return source["md5Checksum"] == target.get("md5Checksum")
    return tracked


def transfer(auth, params, action, emit):
    source_id, dest_id = params.get("source_account_id"), params.get("destination_account_id")
    source_account, dest_account = auth.store.account(source_id), auth.store.account(dest_id)
    if not source_account["subject"] or source_account["subject"] == dest_account["subject"]:
        raise AppError("drive_same_account", "Choose two different verified Google accounts.")
    source, dest = Drive(auth.context(source_id, "drive")), Drive(auth.context(dest_id, "drive"))
    folder_id = params.get("folder_id")
    if folder_id:
        if not isinstance(folder_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id):
            raise AppError("drive_folder_invalid", "Enter a valid new Drive folder ID.")
        root = source.request("GET", "files/" + folder_id, params={"fields": FIELDS})
        if root.get("name") != "EasyComfyColab2" or root.get("mimeType") != FOLDER:
            raise AppError("drive_root_invalid", "Choose the new EasyComfyColab2 root. Legacy runtime images are excluded from this app.")
    else:
        root = source.root()
    if action == "share":
        existing = [item for item in dest.children("root") if item["name"] == "EasyComfyColab2"]
        if existing and (len(existing) != 1 or existing[0].get("shortcutDetails", {}).get("targetId") != root["id"]):
            raise AppError("drive_share_conflict", "The destination already has another EasyComfyColab2 root. Originals were preserved.")
        source.share(root["id"], dest_account["email"], "writer")
        if not existing:
            dest.request("POST", "files", json={"name": "EasyComfyColab2", "mimeType": SHORTCUT, "parents": ["root"], "shortcutDetails": {"targetId": root["id"]}})
        auth.store.set("shared_drive_" + dest_id, {"source_account_id": source_id, "root_id": root["id"]})
        return {"shared": True, "write_access": True}
    target = dest.root(create=True)
    quota = dest.request("GET", "about", params={"fields": "storageQuota"}).get("storageQuota", {})
    free = int(quota["limit"]) - int(quota.get("usage", 0)) if quota.get("limit") else None
    source.share(root["id"], dest_account["email"], "reader")
    stack = [(root["id"], target["id"])]
    files = copied_bytes = 0
    renamed = []
    while stack:
        original_parent, destination_parent = stack.pop()
        existing = list(dest.children(destination_parent))
        for item in source.children(original_parent):
            if item["mimeType"] == SHORTCUT:
                raise AppError("drive_shortcut", "The source contains a shortcut; originals are intact. Copy the actual new runtime root.")
            if item["mimeType"] == FOLDER:
                matched = [f for f in existing if f["name"] == item["name"] and f["mimeType"] == FOLDER]
                if len(matched) > 1:
                    raise AppError("drive_folder_conflict", "Duplicate destination folders prevent safe resume.")
                folder = matched[0] if matched else dest.request("POST", "files", json={"name": item["name"], "mimeType": FOLDER, "parents": [destination_parent]}, params={"fields": FIELDS})
                stack.append((item["id"], folder["id"]))
                continue
            matched = next((f for f in existing if verified(item, f)), None)
            if not matched:
                needed = int(item.get("size", 0))
                if free is not None and needed > free:
                    raise AppError("drive_quota", "Destination space is insufficient. Existing verified copies and originals were preserved.")
                name = item["name"]
                if any(f["name"] == name for f in existing):
                    suffix = PurePosixPath(name).suffix
                    name = name[:-len(suffix)] + " (copy " + version(item)[:8] + ")" + suffix if suffix else name + " (copy " + version(item)[:8] + ")"
                    renamed.append({"source_name": item["name"], "destination_name": name})
                matched = dest.request("POST", f"files/{item['id']}/copy", json={"name": name, "parents": [destination_parent], "appProperties": {"easySource": item["id"], "easyVersion": version(item)}}, params={"fields": FIELDS})
                if not verified(item, matched):
                    raise AppError("drive_copy_unverified", "Drive copy verification failed. Keep the source and retry recovery.")
                existing.append(matched)
                copied_bytes += needed
                if free is not None:
                    free -= needed
            files += 1
            emit("progress", {"phase": "drive_copy", "files_verified": files, "bytes_copied": copied_bytes})
    return {"verified": True, "files": files, "bytes_copied": copied_bytes, "renamed_conflicts": renamed, "originals_preserved": True}
