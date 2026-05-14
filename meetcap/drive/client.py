from __future__ import annotations

from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"


def build_drive(creds: Credentials) -> Any:
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def create_folder(service: Any, name: str, parent_id: str) -> str:
    body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    folder = service.files().create(body=body, fields="id,webViewLink").execute()
    return str(folder["id"])


def folder_web_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def upload_file(
    service: Any,
    src: Path,
    parent_id: str,
    *,
    mimetype: str,
    target_name: str | None = None,
    convert_to_doc: bool = False,
) -> str:
    media = MediaFileUpload(str(src), mimetype=mimetype, resumable=True)
    body: dict[str, Any] = {
        "name": target_name or src.name,
        "parents": [parent_id],
    }
    if convert_to_doc:
        body["mimeType"] = GOOGLE_DOC_MIME
    uploaded = service.files().create(body=body, media_body=media, fields="id").execute()
    return str(uploaded["id"])
