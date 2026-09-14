"""Google Photos uploader with OAuth2 authentication."""

import json
import sys
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly"]
TOKEN_FILE = Path.home() / ".photo-room" / "token.json"
CREDS_FILE = Path("credentials.json")

UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
BATCH_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"


def _get_credentials() -> Credentials:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                print(
                    "\nGoogle Photos credentials not found.\n"
                    "To set up Google Photos upload:\n"
                    "  1. Go to https://console.cloud.google.com/\n"
                    "  2. Create a project and enable 'Photos Library API'\n"
                    "  3. Create OAuth 2.0 credentials (Desktop app)\n"
                    "  4. Download as 'credentials.json' into this folder\n"
                    "  5. Re-run with --upload\n",
                    file=sys.stderr,
                )
                raise FileNotFoundError("credentials.json not found")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return creds


def _upload_bytes(session: requests.Session, path: Path) -> str:
    """Upload raw bytes, return upload token."""
    with open(path, "rb") as f:
        data = f.read()
    resp = session.post(
        UPLOAD_URL,
        data=data,
        headers={
            "Content-type": "application/octet-stream",
            "X-Goog-Upload-File-Name": path.name,
            "X-Goog-Upload-Protocol": "raw",
        },
    )
    resp.raise_for_status()
    return resp.text.strip()


def upload_to_google_photos(paths: list[Path], verbose: bool = False) -> int:
    """Upload a list of DNG files to Google Photos. Returns count of successes."""
    creds = _get_credentials()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {creds.token}"

    upload_tokens = []
    for path in paths:
        if verbose:
            print(f"  Uploading  {path.name} to Google Photos...")
        token = _upload_bytes(session, path)
        upload_tokens.append((path.name, token))

    if not upload_tokens:
        return 0

    # Create media items in one batch call (max 50 per request)
    new_items = [
        {"description": "", "simpleMediaItem": {"fileName": name, "uploadToken": tok}}
        for name, tok in upload_tokens
    ]
    resp = session.post(BATCH_CREATE_URL, json={"newMediaItems": new_items})
    resp.raise_for_status()

    results = resp.json().get("newMediaItemResults", [])
    success = sum(
        1 for r in results if r.get("status", {}).get("message", "").lower() == "success"
        or r.get("mediaItem")
    )
    return success
