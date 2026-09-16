"""Small file-backed fallback so a failed SQLite database can still raise one alarm."""

import json
import time


def queue(runtime_dir, scope, error):
    path = runtime_dir / "database-emergency.json"
    if path.exists():
        return "database"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"scope": scope, "error": error, "at": time.time(), "attempts": 0, "sent": False}
        )
    )
    tmp.replace(path)
    return "database"


def load(runtime_dir):
    path = runtime_dir / "database-emergency.json"
    return json.loads(path.read_text()) if path.exists() else None


def save(runtime_dir, data):
    path = runtime_dir / "database-emergency.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def clear(runtime_dir):
    (runtime_dir / "database-emergency.json").unlink(missing_ok=True)
