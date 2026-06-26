"""Google Drive uploader — OAuth2 + retry.

Setup:
  1. Google Cloud Console → APIs & Services → Credentials
  2. Create OAuth 2.0 Client ID (Desktop app) → download client_secret.json
  3. First run: browser opens for auth → saves token.json (silent afterwards)

Dependencies (install once):
  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
"""
import os
import time
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class GDriveUploader:
    def __init__(
        self,
        secret_path="client_secret.json",
        token_path="token.json",
        folder_name="ReliveStudio_Videos",
    ):
        self.secret_path = str(secret_path)
        self.token_path = str(token_path)
        self.folder_name = folder_name
        self._service = None
        self._folder_id = None

    # ---------------------------------------------------------------- auth

    def _get_service(self):
        if self._service:
            return self._service
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.secret_path):
                    raise FileNotFoundError(
                        f"Không tìm thấy {self.secret_path}. "
                        "Tải client_secret.json từ Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.secret_path, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_path, "w") as fh:
                fh.write(creds.to_json())

        self._service = build("drive", "v3", credentials=creds)
        return self._service

    def _get_or_create_folder(self):
        if self._folder_id:
            return self._folder_id
        service = self._get_service()
        q = (
            f"name='{self.folder_name}' and "
            f"mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        results = service.files().list(q=q, fields="files(id,name)").execute()
        files = results.get("files", [])
        if files:
            self._folder_id = files[0]["id"]
        else:
            meta = {
                "name": self.folder_name,
                "mimeType": "application/vnd.google-apps.folder",
            }
            f = service.files().create(body=meta, fields="id").execute()
            self._folder_id = f["id"]
        return self._folder_id

    # ---------------------------------------------------------------- public API

    def upload(self, local_path: str) -> str:
        """Upload file to Drive. Return shareable 'anyone with link' URL. Retry 3×."""
        from googleapiclient.http import MediaFileUpload

        local_path = str(local_path)
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"File không tồn tại: {local_path}")

        name = Path(local_path).name
        folder_id = self._get_or_create_folder()
        service = self._get_service()
        last_err = None

        for attempt in range(3):
            try:
                media = MediaFileUpload(local_path, resumable=True)
                meta = {"name": name, "parents": [folder_id]}
                f = service.files().create(
                    body=meta, media_body=media, fields="id"
                ).execute()
                file_id = f["id"]
                service.permissions().create(
                    fileId=file_id,
                    body={"type": "anyone", "role": "reader"},
                ).execute()
                return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2**attempt)

        raise RuntimeError(f"Upload Drive thất bại sau 3 lần: {last_err}")

    def is_authenticated(self) -> bool:
        """True nếu token tồn tại và còn hạn (refresh nếu cần)."""
        if not os.path.exists(self.token_path):
            return False
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request

            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            if creds and creds.valid:
                return True
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(self.token_path, "w") as fh:
                    fh.write(creds.to_json())
                return True
        except Exception:
            pass
        return False
