"""
Photo uploaders for Google Photos and Amazon Photos.

Google Photos setup:
  1. https://console.cloud.google.com/ → create project → enable 'Photos Library API'
  2. Create OAuth 2.0 credentials (Desktop app) → download as credentials.json

Amazon Photos setup:
  1. https://developer.amazon.com/ → create Security Profile (free)
  2. Note Client ID and Client Secret → save as amazon_credentials.json:
       {"client_id": "...", "client_secret": "..."}
  Both APIs are free; Amazon requires an active Prime membership for unlimited photo storage.
"""

import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


def _write_secret(path: Path, text: str) -> None:
    """
    Write a file containing live OAuth tokens, readable only by this user.

    The default 0644 would expose the token to every other account on the
    machine. The mode is set before any content is written so there is no
    window where the file exists with wider permissions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    # An existing file keeps its old mode through O_CREAT, so set it explicitly.
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Google Photos
# ---------------------------------------------------------------------------

_G_SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly"]
_G_TOKEN = Path.home() / ".photo-room" / "google_token.json"
_G_CREDS = Path("credentials.json")

_G_UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
_G_BATCH_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"


def _google_credentials() -> Credentials:
    _G_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    creds = None
    if _G_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(_G_TOKEN), _G_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not _G_CREDS.exists():
                print(
                    "\nGoogle Photos credentials not found.\n"
                    "  1. https://console.cloud.google.com/ → create project\n"
                    "  2. Enable 'Photos Library API'\n"
                    "  3. Create OAuth 2.0 credentials (Desktop app)\n"
                    "  4. Download as 'credentials.json' into this folder\n"
                    "  (Google Photos API is free — no billing required)\n",
                    file=sys.stderr,
                )
                raise FileNotFoundError("credentials.json not found")
            flow = InstalledAppFlow.from_client_secrets_file(str(_G_CREDS), _G_SCOPES)
            creds = flow.run_local_server(port=0)
        _write_secret(_G_TOKEN, creds.to_json())
    return creds


def upload_to_google_photos(paths: list[Path], verbose: bool = False) -> int:
    """Upload DNG files to Google Photos. Returns count of successes."""
    creds = _google_credentials()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {creds.token}"

    upload_tokens = []
    for path in paths:
        if verbose:
            print(f"  Uploading  {path.name} → Google Photos...")
        with open(path, "rb") as f:
            resp = session.post(
                _G_UPLOAD_URL,
                data=f.read(),
                headers={
                    "Content-type": "application/octet-stream",
                    "X-Goog-Upload-File-Name": path.name,
                    "X-Goog-Upload-Protocol": "raw",
                },
            )
        resp.raise_for_status()
        upload_tokens.append((path.name, resp.text.strip()))

    if not upload_tokens:
        return 0

    new_items = [
        {"description": "", "simpleMediaItem": {"fileName": name, "uploadToken": tok}}
        for name, tok in upload_tokens
    ]
    resp = session.post(_G_BATCH_URL, json={"newMediaItems": new_items})
    resp.raise_for_status()

    # Since March 2025 batchCreate answers 207 MULTI-STATUS when only some
    # items were created. That is a 2xx, so it does not raise — without
    # reading the per-item results a partial upload looks like a smaller
    # successful one, and the photos that failed are never mentioned.
    results = resp.json().get("newMediaItemResults", [])
    by_token = {token: name for name, token in upload_tokens}

    created = 0
    failures: list[tuple[str, str]] = []
    for item in results:
        name = by_token.get(item.get("uploadToken", ""), "unknown file")
        if item.get("mediaItem"):
            created += 1
        else:
            reason = (item.get("status") or {}).get("message", "no reason given")
            failures.append((name, reason))

    if failures:
        print(
            f"  {len(failures)} of {len(results)} item(s) were not created by "
            "Google Photos:",
            file=sys.stderr,
        )
        for name, reason in failures[:10]:
            print(f"    {name}: {reason}", file=sys.stderr)
        if len(failures) > 10:
            print(f"    ...and {len(failures) - 10} more", file=sys.stderr)

    return created


# ---------------------------------------------------------------------------
# Amazon Photos
# ---------------------------------------------------------------------------

_A_TOKEN_FILE = Path.home() / ".photo-room" / "amazon_token.json"
_A_CREDS_FILE = Path("amazon_credentials.json")

_A_AUTH_URL = "https://www.amazon.com/ap/oa"
_A_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
_A_UPLOAD_URL = "https://content-na.drive.amazonaws.com/cdproxy/nodes"
_A_SCOPE = "clouddrive:read_all clouddrive:write"
_A_REDIRECT = "http://localhost:9876"


def _amazon_oauth_flow(client_id: str, client_secret: str) -> dict:
    """Run local-server OAuth2 flow for Login with Amazon. Returns token dict."""
    auth_code: list[str] = []
    server_error: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # suppress access logs

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if "code" in params:
                auth_code.append(params["code"])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Authorized! You can close this tab.</h2>")
            else:
                server_error.append(params.get("error", "unknown"))
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h2>Authorization failed.</h2>")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    server = HTTPServer(("localhost", 9876), _Handler)

    params = {
        "client_id": client_id,
        "scope": _A_SCOPE,
        "response_type": "code",
        "redirect_uri": _A_REDIRECT,
    }
    url = _A_AUTH_URL + "?" + urllib.parse.urlencode(params)
    print(f"\nOpening browser for Amazon Photos authorization...")
    webbrowser.open(url)
    server.serve_forever()

    if server_error:
        raise RuntimeError(f"Amazon OAuth error: {server_error[0]}")
    if not auth_code:
        raise RuntimeError("No authorization code received from Amazon.")

    resp = requests.post(
        _A_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": auth_code[0],
            "redirect_uri": _A_REDIRECT,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    return resp.json()


def _amazon_token() -> tuple[str, str, str]:
    """Return (access_token, client_id, client_secret), refreshing if needed."""
    _A_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not _A_CREDS_FILE.exists():
        print(
            "\nAmazon Photos credentials not found.\n"
            "  1. Go to https://developer.amazon.com/ (free account)\n"
            "  2. Apps & Services → Login with Amazon → Create a New Security Profile\n"
            "  3. Under 'Web Settings', add Allowed Return URL: http://localhost:9876\n"
            "  4. Note your Client ID and Client Secret\n"
            "  5. Save as amazon_credentials.json in this folder:\n"
            '       {"client_id": "...", "client_secret": "..."}\n'
            "  (Amazon Photos API is free; uses your existing Prime storage)\n",
            file=sys.stderr,
        )
        raise FileNotFoundError("amazon_credentials.json not found")

    creds = json.loads(_A_CREDS_FILE.read_text())
    client_id = creds["client_id"]
    client_secret = creds["client_secret"]

    token_data: dict = {}
    if _A_TOKEN_FILE.exists():
        token_data = json.loads(_A_TOKEN_FILE.read_text())

    # Refresh if we have a refresh token
    if token_data.get("refresh_token"):
        resp = requests.post(
            _A_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token_data["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if resp.ok:
            token_data.update(resp.json())
            _write_secret(_A_TOKEN_FILE, json.dumps(token_data))
            return token_data["access_token"], client_id, client_secret

    # Full OAuth flow
    token_data = _amazon_oauth_flow(client_id, client_secret)
    _write_secret(_A_TOKEN_FILE, json.dumps(token_data))
    return token_data["access_token"], client_id, client_secret


class AmazonUnavailable(RuntimeError):
    """Raised because there is no API left to upload to."""


def upload_to_amazon_photos(paths: list[Path], verbose: bool = False) -> int:
    """
    Upload DNG files to Amazon Photos. Returns count of successes.

    Currently raises instead: there is no API to upload to.

    Amazon closed the Drive API to third parties in August 2019 and shut the
    Drive service down entirely in December 2023. The endpoint this uses,
    content-na.drive.amazonaws.com/cdproxy/nodes, answers 404, and the
    clouddrive:write scope can no longer be granted. Amazon Photos has no
    public API that replaces it.

    The code below is left intact rather than deleted, so that if Amazon
    ever publishes an API this is a change of endpoint rather than a
    rewrite. But running it would send the photographer through a browser
    authorisation that cannot succeed, so it stops here.
    """
    raise AmazonUnavailable(
        "Amazon Photos uploads are not possible: Amazon closed the Drive API "
        "to third parties in 2019 and shut the service down in 2023, and "
        "Amazon Photos has no public API. Nothing this tool can do will make "
        "this work — the upload endpoint returns 404."
    )


def _upload_to_amazon_photos_unreachable(paths: list[Path], verbose: bool = False) -> int:
    """Preserved for the day Amazon publishes a replacement API."""
    access_token, _, _ = _amazon_token()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {access_token}"

    success = 0
    for path in paths:
        if verbose:
            print(f"  Uploading  {path.name} → Amazon Photos...")
        with open(path, "rb") as f:
            resp = session.post(
                _A_UPLOAD_URL,
                params={
                    "name": path.name,
                    "kind": "FILE",
                    "conflictResolution": "RENAME",
                },
                data=f.read(),
                headers={"Content-Type": "image/x-adobe-dng"},
            )
        if resp.ok:
            success += 1
        else:
            print(f"  WARNING: Amazon upload failed for {path.name}: {resp.status_code} {resp.text[:200]}", file=sys.stderr)

    return success
