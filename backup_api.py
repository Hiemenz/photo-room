"""Backup endpoints for the editor: status, settings, run now."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import backup

bp = Blueprint("backup_api", __name__)


def _root():
    import editor  # deferred: editor registers this blueprint
    return editor.LIBRARY_ROOT


@bp.get("/api/backup/status")
def status():
    s = backup.load_settings()
    last = backup.list_backups(s["dest"])[-1:] if s["dest"] else []
    return jsonify({"settings": s, "due": backup.is_due(s),
                    "latest": last[0].name if last else None})


@bp.post("/api/backup/settings")
def settings():
    try:
        return jsonify(backup.save_settings(request.get_json(silent=True) or {}))
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@bp.post("/api/backup/run")
def run():
    payload = request.get_json(silent=True) or {}
    try:
        if payload.get("dest"):
            backup.save_settings({"dest": payload["dest"]})
        return jsonify(backup.run_configured(_root()))
    except (backup.BackupError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
