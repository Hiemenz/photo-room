"""
LAN culling: ``photo-room cull-serve <library> --lan``.

A SEPARATE, minimal Flask app (not the editor) so a phone on the same Wi-Fi can
swipe through smart-preview JPEGs and set picks, rejects and stars. The editor
itself stays bound to 127.0.0.1 and keeps refusing every non-local Host.

Security model (see docs/KNOWN-LIMITS.md):

* Opt-in, per run. Nothing is listening until you start it; it stops when you
  press Ctrl+C or after an idle timeout, and then every token and session is dead.
* A random one-time pairing token is printed (and shown as a QR code when the
  optional ``qrcode`` package is installed). Opening ``/?t=<token>`` swaps it for a
  random session cookie (HttpOnly, SameSite=Strict) and redirects, so the token
  leaves the URL bar. The token works for ``--devices`` pairings (default 1).
* Constant-time comparison everywhere; failed attempts are rate limited per IP.
* Strict allowlist of four routes. Anything else is a 404 before it is parsed.
  The client never sends a path: photos are addressed by opaque per-run ids that
  the server maps to files, and every file it opens is re-checked with
  ``security.safe_join``. Originals are never served, only JPEG smart previews.
* Writes are limited to rating and pick/reject flags, through the editor's own
  index write path (XMP sidecars and the flag store, with the same undo-safe
  pre-reject memory).
* Plain HTTP: anyone who can sniff your Wi-Fi can read the token. Use a network
  you trust.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import socket
import threading
import time
from collections import OrderedDict, deque

from flask import Flask, Response, abort, g, jsonify, make_response, redirect, request

import security

COOKIE = "pr_cull"
PREVIEW_EDGE = 1600
PREVIEW_QUALITY = 82
CACHE_ITEMS = 48
MAX_FAILS = 8
FAIL_WINDOW_S = 60.0
ALLOWED = {("GET", "page"), ("GET", "items"), ("GET", "preview"), ("POST", "mark")}


# ---------------------------------------------------------------------------
# Pairing, sessions, rate limit
# ---------------------------------------------------------------------------

class Gate:
    """One-time pairing token, session cookies, per-IP rate limit, idle expiry."""

    def __init__(self, idle_seconds: float = 1200.0, devices: int = 1, clock=time.monotonic,
                 token: str | None = None):
        self.token = token or secrets.token_urlsafe(24)
        self.sessions: set[str] = set()
        self.devices_left = max(1, int(devices))
        self.idle = float(idle_seconds)
        self.clock = clock
        self.last = clock()
        self.expired = False
        self._fails: dict[str, deque] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def touch(self) -> None:
        self.last = self.clock()

    def expire(self) -> None:
        with self._lock:
            self.expired = True
            self.token = ""
            self.sessions.clear()

    def check_idle(self) -> bool:
        """Expire when idle too long; True once expired."""
        if not self.expired and self.clock() - self.last > self.idle:
            self.expire()
        return self.expired

    # -- rate limit ---------------------------------------------------------
    def blocked(self, ip: str) -> float:
        """Seconds until ``ip`` may try again (0 when it may)."""
        with self._lock:
            q = self._fails.get(ip)
            now = self.clock()
            while q and now - q[0] > FAIL_WINDOW_S:
                q.popleft()
            if q and len(q) >= MAX_FAILS:
                return max(1.0, FAIL_WINDOW_S - (now - q[0]))
        return 0.0

    def fail(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, deque(maxlen=MAX_FAILS * 2)).append(self.clock())
            if len(self._fails) > 256:                      # bounded memory
                self._fails.pop(next(iter(self._fails)))

    # -- credentials --------------------------------------------------------
    def redeem(self, presented: str) -> str | None:
        """A new session id for a correct, unused pairing token, else None."""
        with self._lock:
            if self.expired or not self.token or self.devices_left <= 0:
                ok = False
            else:
                ok = hmac.compare_digest(presented.encode("utf-8", "replace"), self.token.encode())
            if not ok:
                return None
            self.devices_left -= 1
            if self.devices_left == 0:
                self.token = ""                             # one-time
            sid = secrets.token_urlsafe(32)
            self.sessions.add(sid)
            return sid

    def valid_session(self, presented: str) -> bool:
        if not presented or self.expired:
            return False
        probe = presented.encode("utf-8", "replace")
        found = False
        with self._lock:
            for sid in self.sessions:                       # no early exit: constant time per session
                if hmac.compare_digest(probe, sid.encode()):
                    found = True
        return found


# ---------------------------------------------------------------------------
# Backend: the only code that touches the library
# ---------------------------------------------------------------------------

class LibraryBackend:
    """Photos of one library (optionally one sub-folder) via the editor's index."""

    def __init__(self, root, folder: str = ""):
        import editor

        self.ed = editor
        from pathlib import Path

        editor.LIBRARY_ROOT = self.root = Path(root).resolve()
        self.folder = folder.strip("/")
        if self.folder:
            sub = (self.root / self.folder).resolve()
            if sub == self.root or self.root not in sub.parents or not sub.is_dir():
                raise ValueError("folder must be an existing sub-folder of the library")

    def _recs(self) -> list[dict]:
        idx = self.ed._idx()
        pref = self.folder + "/" if self.folder else ""
        recs = [r for r in idx.items() if not r.get("virtual") and r["path"].startswith(pref)]
        return sorted(recs, key=lambda r: (r.get("date") or "", r["path"]))

    def _groups(self, recs: list[dict]) -> dict[str, int]:
        """Burst groups from the smart-cull feature cache when it has them."""
        try:
            import cull
            import cull_store
            import thumbs

            st = cull_store.CullStore(self.root)
            keyed = {r["path"]: thumbs._cache_key(self.root / r["path"]) for r in recs}
            feats = st.get_features(keyed)
            rows = [{"path": p, "t": cull.parse_time(r.get("date")), "hash": feats[p]["hash"]}
                    for r in recs for p in [r["path"]] if p in feats and "hash" in feats[p]]
            out = {}
            for n, grp in enumerate(cull.group_photos(rows)):
                for p in grp:
                    out[p] = n
            return out
        except Exception:
            return {}

    def items(self) -> list[dict]:
        recs = self._recs()
        groups = self._groups(recs)
        return [{"rel": r["path"], "name": r["name"], "rating": r.get("rating", 0),
                 "flag": r.get("flag", "none"), "group": groups.get(r["path"])}
                for r in recs if security.safe_join(self.root, r["path"]) is not None]

    def get(self, rel: str) -> dict | None:
        idx = self.ed._idx(fresh=False)
        return idx.get(rel)

    def preview(self, rel: str) -> bytes:
        import smartpreview
        from PIL import Image

        path = security.safe_join(self.root, rel)
        if path is None:
            raise FileNotFoundError(rel)
        rgb = smartpreview.get(path, self.root)[0]
        img = Image.fromarray(rgb)
        img.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=PREVIEW_QUALITY)
        return buf.getvalue()

    def mark(self, rel: str, flag: str | None, rating: int | None) -> None:
        path = security.safe_join(self.root, rel)
        if path is None:
            raise FileNotFoundError(rel)
        if flag is not None:
            self.ed._flag_paths([path], flag)
        if rating is not None:
            self.ed._set_fields({rel: {"rating": rating}})


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------

def create_app(backend, gate: Gate, port: int | None = None, host_names=()) -> Flask:
    app = Flask("lancull", static_folder=None)
    app.config["BOUND_PORT"] = port
    app.gate = gate
    ids: dict[str, str] = {}                      # opaque id -> relative path
    salt = secrets.token_bytes(16)
    cache: OrderedDict[str, bytes] = OrderedDict()
    lock = threading.Lock()

    def ident(rel: str) -> str:
        i = hashlib.sha256(salt + rel.encode()).hexdigest()[:16]
        ids[i] = rel
        return i

    def deny(status: int, msg: str):
        resp = jsonify({"error": msg})
        resp.status_code = status
        return resp

    @app.before_request
    def _guard():
        ep = request.endpoint
        if (request.method, ep) not in ALLOWED:       # strict allowlist, before anything else
            abort(404)
        if gate.check_idle():
            return deny(410, "This cull session has ended. Start `photo-room cull-serve` again.")
        if not security.lan_host_allowed(request.headers.get("Host"), app.config["BOUND_PORT"], host_names):
            return deny(403, "Refused: unexpected Host header.")
        origin = request.headers.get("Origin")
        if origin is not None:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc != request.headers.get("Host"):
                return deny(403, "Refused: cross-site request.")
        ip = request.remote_addr or "?"
        wait = gate.blocked(ip)
        if wait:
            resp = deny(429, "Too many failed attempts. Wait a minute.")
            resp.headers["Retry-After"] = str(int(wait) + 1)
            return resp
        if ep == "page" and request.args.get("t") is not None:
            return None                                # handled by the view (redeem)
        if not gate.valid_session(request.cookies.get(COOKIE, "")):
            gate.fail(ip)
            if ep == "page":
                return Response("Scan the QR code or open the pairing link shown in the terminal.",
                                401, mimetype="text/plain")
            return deny(401, "Not paired.")
        gate.touch()
        return None

    @app.after_request
    def _headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; img-src 'self' blob:; "
                                                   "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                                   "connect-src 'self'; base-uri 'none'; form-action 'none'")
        return resp

    @app.get("/", endpoint="page")
    def page():
        presented = request.args.get("t")
        if presented is not None:
            sid = gate.redeem(presented)
            if sid is None:
                gate.fail(request.remote_addr or "?")
                return Response("This pairing link is wrong, already used or expired.", 401, mimetype="text/plain")
            gate.touch()
            resp = make_response(redirect("/", 303))
            resp.set_cookie(COOKIE, sid, httponly=True, samesite="Strict", path="/")
            return resp
        return Response(PAGE, mimetype="text/html")

    @app.get("/api/items", endpoint="items")
    def items():
        out = []
        for it in backend.items():
            out.append({"id": ident(it["rel"]), "name": it["name"], "rating": it["rating"],
                        "flag": it["flag"], "group": it["group"]})
        return jsonify({"items": out, "folder": getattr(backend, "folder", "")})

    @app.get("/api/preview/<pid>", endpoint="preview")
    def preview(pid):
        rel = ids.get(pid)
        if rel is None:
            abort(404)
        with lock:
            data = cache.get(pid)
            if data is not None:
                cache.move_to_end(pid)
        if data is None:
            try:
                data = backend.preview(rel)
            except Exception:
                abort(404)
            with lock:
                cache[pid] = data
                while len(cache) > CACHE_ITEMS:
                    cache.popitem(last=False)
        return Response(data, mimetype="image/jpeg")

    @app.post("/api/mark", endpoint="mark")
    def mark():
        if request.mimetype != "application/json" or (request.content_length or 0) > 1024:
            return deny(415, "JSON only")
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"id", "flag", "rating"}:
            return deny(400, "bad request")
        rel = ids.get(body.get("id")) if isinstance(body.get("id"), str) else None
        if rel is None:
            return deny(404, "unknown photo")
        flag, rating = body.get("flag"), body.get("rating")
        if flag is None and rating is None:
            return deny(400, "flag or rating required")
        if flag is not None and flag not in ("pick", "reject", "none"):
            return deny(400, "flag must be pick, reject or none")
        if rating is not None and (type(rating) is not int or not 0 <= rating <= 5):
            return deny(400, "rating must be an integer 0 to 5")
        try:
            backend.mark(rel, flag, rating)
        except Exception:
            return deny(500, "could not save")
        return jsonify({"ok": True})

    return app


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------

def lan_ip() -> str | None:
    """This machine's LAN address (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def print_qr(url: str, out=print) -> bool:
    try:
        import qrcode
    except ImportError:
        return False
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    out(buf.getvalue())
    return True


def serve(backend, host: str, port: int, idle_minutes: float = 20.0, devices: int = 1,
          out=print) -> int:
    from werkzeug.serving import make_server

    gate = Gate(idle_seconds=idle_minutes * 60, devices=devices)
    names = {socket.gethostname(), socket.gethostname() + ".local"}
    app = create_app(backend, gate, port=port, host_names=names)
    server = make_server(host, port, app, threaded=True)
    url = f"http://{host}:{port}/?t={gate.token}"
    out(f"Photo Room LAN cull on {host}:{port}")
    out("Open this on your phone (same Wi-Fi), one time only:")
    out(f"  {url}")
    if not print_qr(url, out):
        out("  (install `qrcode` for a scannable code in the terminal)")
    out(f"Stops after {idle_minutes:g} min idle or Ctrl+C. Originals are never served.")

    def watchdog():
        while not gate.expired:
            time.sleep(min(5.0, max(0.2, gate.idle / 4)))
            if gate.check_idle():
                out("Idle timeout: LAN cull session ended.")
                server.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True, name="lancull-idle").start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gate.expire()
        server.server_close()
    return 0


def main(argv: list[str]) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="photo-room cull-serve",
                                description="Cull from your phone over the LAN (opt-in, token protected).")
    p.add_argument("library")
    p.add_argument("--lan", action="store_true", help="listen on your LAN address (default: this machine only)")
    p.add_argument("-p", "--port", type=int, default=5055)
    p.add_argument("--folder", default="", help="only this sub-folder of the library")
    p.add_argument("--idle-minutes", type=float, default=20.0)
    p.add_argument("--devices", type=int, default=1, help="how many phones may pair with the token")
    a = p.parse_args(argv)
    from pathlib import Path

    root = Path(a.library).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1
    host = "127.0.0.1"
    if a.lan:
        host = lan_ip()
        if host is None:
            print("Could not find a LAN address. Are you on Wi-Fi?", file=sys.stderr)
            return 1
    try:
        backend = LibraryBackend(root, a.folder)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return serve(backend, host, a.port, a.idle_minutes, a.devices)


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Photo Room cull</title><style>
html,body{margin:0;height:100%;background:#111;color:#eee;font:16px system-ui,sans-serif;overflow:hidden;overscroll-behavior:none}
#top,#bot{position:fixed;left:0;right:0;display:flex;justify-content:space-between;align-items:center;padding:8px 12px;z-index:2;background:#000a}
#top{top:0}#bot{bottom:0;padding-bottom:calc(10px + env(safe-area-inset-bottom))}
#stage{position:fixed;inset:0;touch-action:none;display:flex;align-items:center;justify-content:center}
#img{max-width:100%;max-height:100%;will-change:transform;user-select:none;-webkit-user-drag:none;pointer-events:none}
button{font:inherit;color:#eee;background:#333;border:0;border-radius:10px;padding:12px 16px;min-width:56px}
#hint{position:fixed;left:0;right:0;top:50%;text-align:center;font-size:40px;font-weight:700;opacity:0;pointer-events:none;z-index:3}
.pick{color:#4c8}.reject{color:#e55}.star{color:#fc3}
#msg{position:fixed;inset:0;display:none;align-items:center;justify-content:center;text-align:center;padding:24px}
</style></head><body>
<div id=top><span id=name></span><span id=state></span></div>
<div id=stage><img id=img alt=""></div><div id=hint></div><div id=msg></div>
<div id=bot><button id=prev>&larr;</button><button id=undo>Undo</button><button id=next>&rarr;</button></div>
<script>
const $=id=>document.getElementById(id);let items=[],i=0,zoom=false,pan={x:0,y:0},hist=[];
async function api(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error((await r.json().catch(()=>({}))).error||r.status);return r}
function stateText(it){const f=it.flag==='pick'?'PICK':it.flag==='reject'?'REJECT':'';return (f+' '+(it.rating>0?'\\u2605'.repeat(it.rating):'')).trim()}
function show(){const it=items[i];if(!it){$('msg').style.display='flex';$('msg').textContent=items.length?'All done.':'No photos.';$('img').removeAttribute('src');return}
$('msg').style.display='none';zoom=false;pan={x:0,y:0};tf();$('img').src='/api/preview/'+it.id;
$('name').textContent=(i+1)+'/'+items.length+'  '+it.name+(it.group!=null?'  \\u00b7 burst '+(it.group+1):'');$('state').textContent=stateText(it);
for(const n of [1,2]){const x=items[i+n];if(x)new Image().src='/api/preview/'+x.id}}
function tf(){$('img').style.transform=zoom?'translate('+pan.x+'px,'+pan.y+'px) scale(2.5)':'none'}
function flash(t,c){const h=$('hint');h.textContent=t;h.className=c;h.style.opacity=1;setTimeout(()=>h.style.opacity=0,400)}
async function mark(body,t,c){const it=items[i];if(!it)return;hist.push({i,flag:it.flag==='pick'?'pick':it.flag==='reject'?'reject':'none',rating:Math.max(0,it.rating)});
try{await api('/api/mark',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:it.id,...body})})}catch(e){hist.pop();$('state').textContent='Error: '+e.message;return}
if(body.flag)it.flag=body.flag;if(body.rating!=null){it.rating=body.rating;if(body.flag==null&&it.flag==='reject')it.flag='none'}
flash(t,c);i=Math.min(i+1,items.length);show()}
async function undo(){const h=hist.pop();if(!h)return;const it=items[h.i];try{await api('/api/mark',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:it.id,flag:h.flag,rating:h.rating})});
it.flag=h.flag;it.rating=h.rating}catch(e){hist.push(h);return}i=h.i;show()}
$('undo').onclick=undo;$('prev').onclick=()=>{i=Math.max(0,i-1);show()};$('next').onclick=()=>{i=Math.min(items.length,i+1);show()};
let sx,sy,t0,moved,px,py;const st=$('stage');
st.onpointerdown=e=>{st.setPointerCapture(e.pointerId);sx=px=e.clientX;sy=py=e.clientY;t0=Date.now();moved=0};
st.onpointermove=e=>{if(sx==null)return;moved=Math.max(moved,Math.hypot(e.clientX-sx,e.clientY-sy));if(zoom){pan.x+=e.clientX-px;pan.y+=e.clientY-py;tf()}px=e.clientX;py=e.clientY};
st.onpointerup=e=>{if(sx==null)return;const dx=e.clientX-sx,dy=e.clientY-sy;sx=null;
if(moved<10&&Date.now()-t0<400){zoom=!zoom;pan={x:0,y:0};tf();return}if(zoom)return;
if(Math.abs(dx)>=Math.abs(dy)&&Math.abs(dx)>80)dx>0?mark({flag:'pick'},'PICK','pick'):mark({flag:'reject'},'REJECT','reject');
else if(-dy>80&&-dy>Math.abs(dx)){const it=items[i];mark(it&&it.flag==='reject'?{flag:'none',rating:5}:{rating:5},'\\u2605\\u2605\\u2605\\u2605\\u2605','star')}};
st.onpointercancel=()=>{sx=null};
api('/api/items').then(r=>r.json()).then(d=>{items=d.items;show()}).catch(e=>{$('msg').style.display='flex';$('msg').textContent='Not paired or session ended. '+e.message});
</script></body></html>"""
