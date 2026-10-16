"""
Tests for the Google Photos and Amazon Photos uploaders.

Every network call is faked. Nothing here contacts a real service, opens a
browser, or touches the user's real token files — the token and credential
paths are redirected into tmp_path.
"""

import json
from pathlib import Path

import pytest

import uploader
from uploader import upload_to_amazon_photos, upload_to_google_photos


class FakeResponse:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload if payload is not None else {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Records posts and replays a queued list of responses."""

    def __init__(self, responses):
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.posts: list[dict] = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected extra POST to {url}")
        return self.responses.pop(0)


@pytest.fixture
def photos(tmp_path):
    files = []
    for name in ("IMG_1.dng", "IMG_2.dng"):
        p = tmp_path / name
        p.write_bytes(b"raw-bytes-" + name.encode())
        files.append(p)
    return files


@pytest.fixture
def isolate_tokens(tmp_path, monkeypatch):
    """Point every credential and token path at the sandbox."""
    monkeypatch.setattr(uploader, "_G_TOKEN", tmp_path / "google_token.json")
    monkeypatch.setattr(uploader, "_G_CREDS", tmp_path / "credentials.json")
    monkeypatch.setattr(uploader, "_A_TOKEN_FILE", tmp_path / "amazon_token.json")
    monkeypatch.setattr(uploader, "_A_CREDS_FILE", tmp_path / "amazon_credentials.json")
    return tmp_path


# ---------------------------------------------------------------------------
# Google Photos
# ---------------------------------------------------------------------------

@pytest.fixture
def google_creds(monkeypatch):
    class Creds:
        token = "google-access-token"

    monkeypatch.setattr(uploader, "_google_credentials", lambda: Creds())
    return Creds


def test_google_uploads_each_file_then_creates_media_items(monkeypatch, photos, google_creds):
    session = FakeSession([
        FakeResponse(text="token-1"),
        FakeResponse(text="token-2"),
        FakeResponse(payload={"newMediaItemResults": [
            {"mediaItem": {"id": "a"}}, {"mediaItem": {"id": "b"}},
        ]}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    assert upload_to_google_photos(photos) == 2

    # Two byte uploads, then one batchCreate.
    assert [p["url"] for p in session.posts] == [
        uploader._G_UPLOAD_URL, uploader._G_UPLOAD_URL, uploader._G_BATCH_URL,
    ]


def test_google_sends_the_bearer_token(monkeypatch, photos, google_creds):
    session = FakeSession([
        FakeResponse(text="t1"), FakeResponse(text="t2"),
        FakeResponse(payload={"newMediaItemResults": []}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    upload_to_google_photos(photos)
    assert session.headers["Authorization"] == "Bearer google-access-token"


def test_google_sends_file_bytes_and_name(monkeypatch, photos, google_creds):
    session = FakeSession([
        FakeResponse(text="t1"), FakeResponse(text="t2"),
        FakeResponse(payload={"newMediaItemResults": []}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    upload_to_google_photos(photos)
    first = session.posts[0]
    assert first["data"] == photos[0].read_bytes()
    assert first["headers"]["X-Goog-Upload-File-Name"] == "IMG_1.dng"


def test_google_pairs_upload_tokens_with_filenames(monkeypatch, photos, google_creds):
    """A mismatch here would file photos under the wrong names."""
    session = FakeSession([
        FakeResponse(text="token-1"), FakeResponse(text="token-2"),
        FakeResponse(payload={"newMediaItemResults": []}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    upload_to_google_photos(photos)
    items = session.posts[-1]["json"]["newMediaItems"]
    assert [i["simpleMediaItem"]["fileName"] for i in items] == ["IMG_1.dng", "IMG_2.dng"]
    assert [i["simpleMediaItem"]["uploadToken"] for i in items] == ["token-1", "token-2"]


def test_google_strips_whitespace_from_upload_tokens(monkeypatch, photos, google_creds):
    """The API returns the token with a trailing newline; sending it back breaks."""
    session = FakeSession([
        FakeResponse(text="token-1\n"), FakeResponse(text="  token-2  "),
        FakeResponse(payload={"newMediaItemResults": []}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    upload_to_google_photos(photos)
    tokens = [i["simpleMediaItem"]["uploadToken"]
              for i in session.posts[-1]["json"]["newMediaItems"]]
    assert tokens == ["token-1", "token-2"]


def test_google_counts_only_items_the_api_confirmed(monkeypatch, photos, google_creds):
    """A per-item failure in batchCreate must not be counted as a success."""
    session = FakeSession([
        FakeResponse(text="t1"), FakeResponse(text="t2"),
        FakeResponse(payload={"newMediaItemResults": [
            {"mediaItem": {"id": "a"}},
            {"status": {"message": "quota exceeded"}},
        ]}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    assert upload_to_google_photos(photos) == 1


def test_google_empty_input_makes_no_requests(monkeypatch, google_creds):
    session = FakeSession([])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    assert upload_to_google_photos([]) == 0
    assert session.posts == []


def test_google_upload_failure_aborts_the_batch(monkeypatch, photos, google_creds):
    """
    Documents current behaviour: one bad byte-upload raises, so nothing is
    filed — the successful uploads in the same run are discarded too.
    """
    session = FakeSession([FakeResponse(text="t1"), FakeResponse(status=500)])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        upload_to_google_photos(photos)


def test_google_missing_credentials_file_raises(isolate_tokens, capsys):
    """With no credentials.json the user gets setup instructions, not a stack trace."""
    with pytest.raises(FileNotFoundError, match="credentials.json"):
        uploader._google_credentials()
    assert "console.cloud.google.com" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Amazon Photos
# ---------------------------------------------------------------------------

@pytest.fixture
def amazon_creds(isolate_tokens):
    uploader._A_CREDS_FILE.write_text(
        json.dumps({"client_id": "cid", "client_secret": "csecret"})
    )
    return isolate_tokens


def test_amazon_uploads_are_refused(photos):
    """
    Amazon closed the Drive API to third parties in 2019 and shut the service
    down in 2023; Amazon Photos has no public API. Attempting the upload would
    send the photographer through a browser authorisation that cannot succeed.
    """
    with pytest.raises(uploader.AmazonUnavailable, match="no public API"):
        upload_to_amazon_photos(photos)


def test_the_refusal_explains_that_nothing_will_fix_it(photos):
    """A transient-sounding error would invite retrying forever."""
    try:
        upload_to_amazon_photos(photos)
    except uploader.AmazonUnavailable as exc:
        assert "404" in str(exc)
        assert "Nothing this tool can do" in str(exc)


def test_amazon_uploads_each_file(monkeypatch, photos, amazon_creds):
    monkeypatch.setattr(uploader, "upload_to_amazon_photos",
                        uploader._upload_to_amazon_photos_unreachable)
    monkeypatch.setattr(uploader, "_amazon_token", lambda: ("tok", "cid", "csec"))
    session = FakeSession([FakeResponse(), FakeResponse()])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    assert uploader._upload_to_amazon_photos_unreachable(photos) == 2
    assert session.headers["Authorization"] == "Bearer tok"


def test_amazon_continues_past_a_failed_file(monkeypatch, photos, amazon_creds, capsys):
    """
    Unlike Google, one failure must not abandon the rest of the batch.

    Exercises the preserved implementation, kept against the day Amazon
    publishes a replacement API.
    """
    monkeypatch.setattr(uploader, "_amazon_token", lambda: ("tok", "cid", "csec"))
    session = FakeSession([FakeResponse(status=507, text="insufficient storage"),
                           FakeResponse()])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    assert uploader._upload_to_amazon_photos_unreachable(photos) == 1
    assert "IMG_1.dng" in capsys.readouterr().err


def test_amazon_sends_rename_on_conflict(monkeypatch, photos, amazon_creds):
    """Conflict resolution must not be 'overwrite' — that would destroy photos."""
    monkeypatch.setattr(uploader, "_amazon_token", lambda: ("tok", "cid", "csec"))
    session = FakeSession([FakeResponse(), FakeResponse()])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    uploader._upload_to_amazon_photos_unreachable(photos)
    assert session.posts[0]["params"]["conflictResolution"] == "RENAME"
    assert session.posts[0]["params"]["name"] == "IMG_1.dng"


def test_amazon_missing_credentials_file_raises(isolate_tokens, capsys):
    with pytest.raises(FileNotFoundError, match="amazon_credentials.json"):
        uploader._amazon_token()
    assert "developer.amazon.com" in capsys.readouterr().err


def test_amazon_refreshes_an_existing_token(monkeypatch, amazon_creds):
    uploader._A_TOKEN_FILE.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "refresh-me",
    }))
    calls = []

    def fake_post(url, data=None, **kwargs):
        calls.append(data)
        return FakeResponse(payload={"access_token": "fresh"})

    monkeypatch.setattr(uploader.requests, "post", fake_post)

    token, cid, secret = uploader._amazon_token()

    assert token == "fresh"
    assert (cid, secret) == ("cid", "csecret")
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "refresh-me"


def test_amazon_refresh_preserves_the_refresh_token(monkeypatch, amazon_creds):
    """
    Amazon omits refresh_token from a refresh response. Replacing the stored
    dict instead of updating it would lose it and force a browser re-auth.
    """
    uploader._A_TOKEN_FILE.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "keep-me",
    }))
    monkeypatch.setattr(
        uploader.requests, "post",
        lambda url, **kw: FakeResponse(payload={"access_token": "fresh"}),
    )

    uploader._amazon_token()

    stored = json.loads(uploader._A_TOKEN_FILE.read_text())
    assert stored["refresh_token"] == "keep-me"
    assert stored["access_token"] == "fresh"


def test_amazon_falls_back_to_full_oauth_when_refresh_is_rejected(monkeypatch, amazon_creds):
    uploader._A_TOKEN_FILE.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "expired",
    }))
    monkeypatch.setattr(
        uploader.requests, "post",
        lambda url, **kw: FakeResponse(status=400, text="invalid_grant"),
    )
    monkeypatch.setattr(
        uploader, "_amazon_oauth_flow",
        lambda cid, secret: {"access_token": "brand-new", "refresh_token": "new-refresh"},
    )

    assert uploader._amazon_token()[0] == "brand-new"
    assert json.loads(uploader._A_TOKEN_FILE.read_text())["access_token"] == "brand-new"


def test_amazon_runs_oauth_when_no_token_exists(monkeypatch, amazon_creds):
    """No stored token must go straight to the browser flow, not attempt a refresh."""
    monkeypatch.setattr(
        uploader.requests, "post",
        lambda *a, **k: pytest.fail("must not attempt a refresh without a refresh token"),
    )
    monkeypatch.setattr(
        uploader, "_amazon_oauth_flow",
        lambda cid, secret: {"access_token": "first", "refresh_token": "r"},
    )

    assert uploader._amazon_token()[0] == "first"


def test_amazon_persists_the_token_for_next_run(monkeypatch, amazon_creds):
    monkeypatch.setattr(
        uploader, "_amazon_oauth_flow",
        lambda cid, secret: {"access_token": "abc", "refresh_token": "r"},
    )
    uploader._amazon_token()
    assert uploader._A_TOKEN_FILE.exists()
    assert json.loads(uploader._A_TOKEN_FILE.read_text())["access_token"] == "abc"


# ---------------------------------------------------------------------------
# Credential handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("writer,token_attr", [
    ("amazon", "_A_TOKEN_FILE"),
    ("google", "_G_TOKEN"),
])
def test_token_files_are_not_world_readable(monkeypatch, amazon_creds, writer, token_attr):
    """
    These files hold live OAuth tokens. On a shared machine, default 0644
    permissions would let any other user read them.
    """
    if writer == "amazon":
        monkeypatch.setattr(
            uploader, "_amazon_oauth_flow",
            lambda cid, secret: {"access_token": "abc", "refresh_token": "r"},
        )
        uploader._amazon_token()
    else:
        class Creds:
            token = "t"
            valid = False          # forces the refresh-and-rewrite path
            expired = True
            refresh_token = "r"

            def refresh(self, request):
                pass

            def to_json(self):
                return '{"token": "t"}'

        monkeypatch.setattr(
            uploader.Credentials, "from_authorized_user_file",
            staticmethod(lambda *a, **k: Creds()),
        )
        monkeypatch.setattr(uploader, "Request", lambda: None)
        # Pre-create it world-readable: rewriting must tighten the mode, not
        # inherit whatever was already on disk.
        uploader._G_TOKEN.parent.mkdir(parents=True, exist_ok=True)
        uploader._G_TOKEN.write_text("{}")
        uploader._G_TOKEN.chmod(0o644)
        uploader._google_credentials()

    mode = getattr(uploader, token_attr).stat().st_mode & 0o077
    assert mode == 0, f"token file is readable by others (mode {oct(mode)})"


# ---------------------------------------------------------------------------
# What the user sees when nothing is configured
# ---------------------------------------------------------------------------

def test_a_missing_google_credential_is_reported_on_stdout(monkeypatch, capsys, photos):
    """
    The setup instructions go to stderr. Saying nothing on stdout left
    "Uploading..." as the last line, which reads as still in progress.
    """
    import process

    def missing(paths, verbose=False):
        raise FileNotFoundError("credentials.json not found")

    monkeypatch.setattr("uploader.upload_to_google_photos", missing)
    process._upload_google(photos, verbose=False)

    out = capsys.readouterr().out
    assert "Uploading" in out
    assert "skipped" in out
    assert "credentials.json" in out


def test_a_missing_amazon_credential_is_reported_on_stdout(monkeypatch, capsys, photos):
    import process

    def missing(paths, verbose=False):
        raise FileNotFoundError("amazon_credentials.json not found")

    monkeypatch.setattr("uploader.upload_to_amazon_photos", missing)
    process._upload_amazon(photos, verbose=False)

    out = capsys.readouterr().out
    assert "skipped" in out
    assert "amazon_credentials.json" in out


def test_an_upload_error_reaches_both_streams(monkeypatch, capsys, photos):
    """
    stdout so it appears in order with the rest of the run, stderr so it is
    still visible when stdout is piped somewhere else.
    """
    import process

    def broken(paths, verbose=False):
        raise RuntimeError("HTTP 503 service unavailable")

    monkeypatch.setattr("uploader.upload_to_google_photos", broken)
    process._upload_google(photos, verbose=False)

    captured = capsys.readouterr()
    assert "503" in captured.out
    assert "503" in captured.err


def test_a_successful_upload_reports_the_count(monkeypatch, capsys, photos):
    import process

    monkeypatch.setattr("uploader.upload_to_google_photos", lambda paths, verbose=False: len(paths))
    process._upload_google(photos, verbose=False)

    assert f"Uploaded {len(photos)} file(s)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Partial success (HTTP 207)
# ---------------------------------------------------------------------------

def test_a_partial_batch_is_not_treated_as_a_failure(monkeypatch, photos, google_creds):
    """
    Since March 2025 batchCreate answers 207 MULTI-STATUS when only some
    items were created. It is a 2xx, so it must not raise.
    """
    session = FakeSession([
        FakeResponse(text="token-1"), FakeResponse(text="token-2"),
        FakeResponse(status=207, payload={"newMediaItemResults": [
            {"uploadToken": "token-1", "mediaItem": {"id": "a"}},
            {"uploadToken": "token-2", "status": {"message": "Internal error"}},
        ]}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    assert upload_to_google_photos(photos) == 1


def test_items_the_api_rejected_are_named(monkeypatch, photos, google_creds, capsys):
    """
    Counting only the successes made a partial upload look like a smaller
    successful one, with the lost photos never mentioned.
    """
    session = FakeSession([
        FakeResponse(text="token-1"), FakeResponse(text="token-2"),
        FakeResponse(status=207, payload={"newMediaItemResults": [
            {"uploadToken": "token-1", "mediaItem": {"id": "a"}},
            {"uploadToken": "token-2", "status": {"message": "Quota exceeded"}},
        ]}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    upload_to_google_photos(photos)

    err = capsys.readouterr().err
    assert "IMG_2.dng" in err, "the rejected photo must be named"
    assert "Quota exceeded" in err
    assert "IMG_1.dng" not in err, "the one that succeeded must not be listed"


def test_a_rejection_without_a_reason_still_reports(monkeypatch, photos, google_creds, capsys):
    session = FakeSession([
        FakeResponse(text="t1"), FakeResponse(text="t2"),
        FakeResponse(status=207, payload={"newMediaItemResults": [
            {"uploadToken": "t1"}, {"uploadToken": "t2"},
        ]}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    assert upload_to_google_photos(photos) == 0
    assert "no reason given" in capsys.readouterr().err


def test_a_fully_successful_batch_reports_nothing(monkeypatch, photos, google_creds, capsys):
    session = FakeSession([
        FakeResponse(text="t1"), FakeResponse(text="t2"),
        FakeResponse(payload={"newMediaItemResults": [
            {"uploadToken": "t1", "mediaItem": {"id": "a"}},
            {"uploadToken": "t2", "mediaItem": {"id": "b"}},
        ]}),
    ])
    monkeypatch.setattr(uploader.requests, "Session", lambda: session)

    assert upload_to_google_photos(photos) == 2
    assert capsys.readouterr().err == ""


def test_the_scope_is_one_that_still_exists():
    """
    April 2025 removed photoslibrary.readonly, .sharing and the full
    photoslibrary scope. appendonly — what uploading needs — survived.
    """
    removed = {
        "https://www.googleapis.com/auth/photoslibrary.readonly",
        "https://www.googleapis.com/auth/photoslibrary.sharing",
        "https://www.googleapis.com/auth/photoslibrary",
    }
    assert not (set(uploader._G_SCOPES) & removed)
    assert uploader._G_SCOPES == [
        "https://www.googleapis.com/auth/photoslibrary.appendonly"
    ]
