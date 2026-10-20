"""Identity-verified training actions and bounded SFTP dataset import."""
import hashlib
from pathlib import Path
import uuid
import time

from controller.state import AppError

MAX_ARCHIVE = 1024**3
ACTIONS = {"training.status", "training.import", "training.prepare", "training.start", "training.resume", "training.cancel", "training.preserve", "training.export"}


def handle(controller, action, params):
    if action not in ACTIONS: raise AppError("unknown_action", "Unknown training action.")
    remote = controller.require_remote()
    # Never route a late request to a new account/session after a UI switch.
    if params.get("account_id") != controller.session.get("account_id") or params.get("session_id") != controller.session.get("session_id"):
        raise AppError("identity_mismatch", "Refresh training in the selected owned session.")
    if action == "training.status": return remote.call("/training")
    if action != "training.import":
        payload = {k: v for k, v in params.items() if k not in {"account_id", "session_id"}}
        return remote.call("/training/"+action.split(".")[1], payload, timeout=1200 if action in {"training.preserve", "training.export"} else 600)
    status = remote.call("/training")
    if status.get("busy") is not False or status.get("preserved") is not True:
        raise AppError("training_busy", "Finish training before replacing its dataset.")
    path_value = params.get("path")
    if not isinstance(path_value, str): raise AppError("training_dataset", "Choose a local dataset ZIP.")
    path = Path(path_value)
    if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".zip" or not 1 <= path.stat().st_size <= MAX_ARCHIVE:
        raise AppError("training_dataset", "Choose a regular dataset ZIP up to 1 GiB.")
    transport = controller.transport
    session = dict(controller.session)
    def current():
        if controller.transport is not transport or any(controller.session.get(k) != session.get(k) for k in ("account_id", "session_id", "endpoint")):
            raise AppError("session_changed", "Session changed during dataset upload; no training was started.")
    before = path.stat()
    with path.open("rb") as source: sha = hashlib.file_digest(source, "sha256").hexdigest()
    health = remote.health()
    # The companion supplies the currently mounted NEW Drive root, not a renderer path.
    models = health.get("paths", {}).get("models", "")
    if not isinstance(models, str) or not models.startswith("/content/drive/") or not models.endswith("/models") or ".." in models.split("/"):
        raise AppError("training_dataset", "Owned Drive destination could not be verified.")
    folder = models[:-len("/models")]+"/training/uploads"
    temporary = folder+"/"+uuid.uuid4().hex+".part"
    target = folder+"/"+sha+".zip"
    current(); transport.mkdir(folder)
    last_progress = [0.0]
    def progress(done, total):
        current()
        now = time.monotonic()
        if done == total or now-last_progress[0] >= 0.5:
            last_progress[0] = now
            controller.emit("training.upload", {"bytes": done, "total": total})
    try:
        transport.sftp.put(str(path), temporary, callback=progress)
        current()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise AppError("training_dataset", "Local dataset changed while uploading.")
        # Stream the remote copy over the authenticated transport for verification.
        with transport.sftp.open(temporary, "rb") as stream:
            checksum = hashlib.sha256()
            while chunk := stream.read(1024*1024):
                current(); checksum.update(chunk)
        if checksum.hexdigest() != sha: raise AppError("training_dataset", "Dataset upload checksum failed.")
        try:
            transport.sftp.stat(target)
        except FileNotFoundError:
            transport.sftp.rename(temporary, target)
        else:
            # Validate any existing content-addressed upload before reusing it.
            with transport.sftp.open(target, "rb") as stream:
                checksum = hashlib.sha256()
                while chunk := stream.read(1024*1024): checksum.update(chunk)
            if checksum.hexdigest() != sha: raise AppError("training_dataset", "Existing dataset checksum failed.")
        return {"dataset_sha256": sha, "size": after.st_size, "verified": True}
    finally:
        try: transport.sftp.remove(temporary)
        except OSError: pass
