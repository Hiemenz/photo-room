"""
Request guards for the local editor.

The server listens on 127.0.0.1, but any web page open in the same browser
can still talk to it: a cross-site POST, or DNS rebinding (an attacker's
hostname re-resolved to 127.0.0.1, which makes the attacker's page
"same-origin" and lets it read every response). Three layers:

* Host allowlist on EVERY request (GET thumbnails included): only
  127.0.0.1, localhost and [::1], on the bound port.
* Origin allowlist (same set) whenever the browser sends an Origin.
* A per-process random token, embedded in the page the editor serves, that
  state-changing requests must echo in a header. Pages on other sites can
  neither read the token nor add the header cross-origin without a
  preflight the server never grants.
"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from flask import jsonify, request

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
TOKEN_HEADER = "X-PhotoRoom-Token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _split_host(value: str) -> tuple[str, int | None] | None:
    """('localhost', 5000) from 'localhost:5000'; None if unparseable."""
    if not value or any(c in value for c in "/\\@ \t"):
        return None
    try:
        parts = urlsplit("//" + value)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    return host, port


def host_allowed(value: str | None, bound_port: int | None) -> bool:
    parsed = _split_host(value or "")
    if parsed is None:
        return False
    host, port = parsed
    if host not in ALLOWED_HOSTS:
        return False
    if bound_port is not None and (port or 80) != bound_port:
        return False
    return True


def origin_allowed(origin: str, bound_port: int | None) -> bool:
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return False
    netloc = parts.netloc
    parsed = _split_host(netloc)
    if parsed is None:
        return False
    host, port = parsed
    if host not in ALLOWED_HOSTS:
        return False
    if bound_port is not None:
        default = 443 if parts.scheme == "https" else 80
        if (port or default) != bound_port:
            return False
    return True


def safe_join(root, rel: str):
    """``root / rel`` resolved, or None unless it is an existing regular file
    that really lives under ``root`` (no ``..``, absolute paths, NULs or symlink
    escapes). Used by the LAN cull server, which never accepts a path from a
    client but still checks every one it builds."""
    from pathlib import Path

    if not isinstance(rel, str) or not rel or "\0" in rel or "\\" in rel:
        return None
    if rel.startswith("/") or ".." in Path(rel).parts:
        return None
    try:
        base = Path(root).resolve()
        cand = (base / rel).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if base not in cand.parents or not cand.is_file():
        return None
    return cand


def lan_host_allowed(value: str | None, bound_port: int | None, names=()) -> bool:
    """Host check for the LAN cull server: an IP literal, localhost or one of
    ``names`` (this machine's own hostnames), on the bound port. An attacker's
    domain re-resolved to this machine (DNS rebinding) is refused."""
    import ipaddress

    parsed = _split_host(value or "")
    if parsed is None:
        return False
    host, port = parsed
    if bound_port is not None and (port or 80) != bound_port:
        return False
    bare = host.strip("[]")
    try:
        ipaddress.ip_address(bare)
        return True
    except ValueError:
        pass
    return bare == "localhost" or bare in {n.lower() for n in names}


def install(app) -> None:
    """Attach the guards to ``app``. Config: BOUND_PORT, PHOTOROOM_TOKEN."""
    app.config.setdefault("BOUND_PORT", None)
    app.config.setdefault("PHOTOROOM_TOKEN", secrets.token_urlsafe(32))

    @app.before_request
    def _guard():
        port = app.config.get("BOUND_PORT")
        if not host_allowed(request.headers.get("Host"), port):
            return jsonify({"error": "Refused: unexpected Host header. "
                                     "Open the editor at http://127.0.0.1."}), 403
        origin = request.headers.get("Origin")
        if origin is not None and not origin_allowed(origin, port):
            return jsonify({
                "error": "Refused: this request came from another site. "
                         "The editor only accepts requests from its own page.",
            }), 403
        if request.method not in SAFE_METHODS:
            sent = request.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(sent.encode(), app.config["PHOTOROOM_TOKEN"].encode()):
                return jsonify({"error": "Refused: missing or wrong session token. "
                                         "Reload the editor page."}), 403
        return None


# ---------------------------------------------------------------------------
# Library path checks (Folders panel: move / rename / create)
# ---------------------------------------------------------------------------

import os                      # noqa: E402
from pathlib import Path       # noqa: E402

MAX_NAME_LEN = 120


def safe_name(name) -> str:
    """One path component chosen by the user (new folder or photo name); ValueError if unsafe."""
    if not isinstance(name, str):
        raise ValueError("A name is required")
    name = name.strip()
    if not name or name in (".", ".."):
        raise ValueError("A name is required")
    if len(name) > MAX_NAME_LEN:
        raise ValueError("That name is too long")
    if name.startswith("."):
        raise ValueError("Names cannot start with a dot")
    if any(c in name for c in '/\\:*?"<>|\0') or any(ord(c) < 32 for c in name):
        raise ValueError("Names cannot contain / \\ : * ? \" < > | or control characters")
    return name


def safe_relpath(root, raw, *, allow_root: bool = False, must_exist: bool = True) -> Path:
    """
    Resolve a client-supplied library-relative path to an absolute one.

    Refuses absolute paths, ``..``, backslashes, NUL, hidden components (the
    library's own ``.photo-room-*`` files and thumbnail cache), and anything
    that reaches outside ``root`` through a symlink. ``allow_root`` accepts
    the empty string as the root itself.
    """
    if not isinstance(raw, str) or "\0" in raw or "\\" in raw or raw.startswith("/"):
        raise ValueError("Bad path")
    raw = raw.strip("/")
    root = Path(os.path.realpath(root))
    if not raw:
        if allow_root:
            return root
        raise ValueError("A path is required")
    parts = raw.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise ValueError("Bad path")
    lexical = root.joinpath(*parts)
    if os.path.realpath(lexical) != str(lexical):        # a symlink somewhere on the way
        raise ValueError("Path goes through a symbolic link")
    if must_exist and not lexical.exists():
        raise ValueError(f"No such path: {raw}")
    return lexical
