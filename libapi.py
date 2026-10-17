#!/usr/bin/env python3
"""
HTTP endpoints for the library index: the change feed and write-queue control.

``GET /api/library/changes?since=<seq>&timeout=<s>``   long-poll
    Returns as soon as there is a change after ``since`` (or after ``timeout``
    seconds, with an empty list). Body: ``{seq, reset, changes: [{path, item,
    removed}]}``. ``reset: true`` means the client fell too far behind (or the
    server restarted): reload the page of items instead of patching.

``GET /api/library/events?since=<seq>``   Server-Sent Events
    The same changes as ``event: changes`` messages, with a heartbeat comment
    every 15 s. Resumes from ``Last-Event-ID`` after a reconnect.

``POST /api/library/flush``   write every queued sidecar now.
``GET  /api/library/status``  ``{seq, pending, errors}``.
"""

from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, request, stream_with_context

import libindex

bp = Blueprint("libapi", __name__)


def _index():
    import editor            # late: editor owns LIBRARY_ROOT (and imports this module)
    return editor._idx()


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


@bp.route("/api/library/changes")
def changes():
    idx = _index()
    since = _int("since", idx.seq, 0, 2**62)
    timeout = _int("timeout", 25, 0, 60)
    return jsonify(idx.changes_since(since, timeout))


@bp.route("/api/library/events")
def events():
    idx = _index()
    since = _int("since", idx.seq, 0, 2**62)
    try:
        since = int(request.headers.get("Last-Event-ID", since))
    except ValueError:
        pass
    once = request.args.get("once") == "1"      # tests: stop after the first message

    def stream():
        nonlocal since
        yield "retry: 2000\n\n"
        while True:
            body = idx.changes_since(since, 15)
            if body["changes"] or body["reset"]:
                since = body["seq"]
                yield f"id: {since}\nevent: changes\ndata: {json.dumps(body)}\n\n"
                if once:
                    return
            else:
                yield ": keep-alive\n\n"
                if once:
                    return

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@bp.route("/api/library/flush", methods=["POST"])
def flush():
    ok = _index().flush(timeout=60)
    return jsonify({"ok": ok, "pending": _index().pending_count()})


@bp.route("/api/library/status")
def status():
    idx = _index()
    return jsonify({"seq": idx.seq, "pending": idx.pending_count(), "errors": idx.errors[-5:],
                    "photos": len(idx)})
