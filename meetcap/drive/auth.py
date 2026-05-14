from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

USER_CONFIG_DIR = Path.home() / ".config" / "meetcap"
CLIENT_SECRET_PATH = USER_CONFIG_DIR / "google-client-secret.json"
TOKEN_PATH = USER_CONFIG_DIR / "google-token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class ClientSecretMissingError(RuntimeError):
    """Canonical client-secret JSON not present."""


class TokenMissingError(RuntimeError):
    """No saved OAuth token; user must run `meetcap auth google`."""


def _save_token(creds: Credentials) -> None:
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    os.chmod(TOKEN_PATH, 0o600)


def run_oauth_bootstrap() -> Credentials:
    """Open a browser, complete the desktop OAuth flow, persist the token."""
    if not CLIENT_SECRET_PATH.exists():
        raise ClientSecretMissingError(str(CLIENT_SECRET_PATH))
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    return cast(Credentials, creds)


def load_credentials() -> Credentials:
    """Load (and refresh) saved Google credentials. Raises if no token yet."""
    if not TOKEN_PATH.exists():
        raise TokenMissingError(str(TOKEN_PATH))
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            raise TokenMissingError("token invalid and unrefreshable")
    return cast(Credentials, creds)


def find_misplaced_secret() -> Path | None:
    """Locate a client_secret_*.json sitting in ~/.config/ but not under meetcap/."""
    matches = sorted(
        (Path.home() / ".config").glob("client_secret_*.apps.googleusercontent.com.json")
    )
    return matches[0] if matches else None
