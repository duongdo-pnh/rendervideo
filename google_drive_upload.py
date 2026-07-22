"""Tự động đẩy video render xong lên Google Drive, vào 1 thư mục mặc định.

Mọi secret nằm trong .env (KHÔNG hard-code, KHÔNG commit file JSON):
  GOOGLE_DRIVE_UPLOAD_ENABLED=1
  GOOGLE_DRIVE_FOLDER_ID=...        # ID thư mục đích: drive.google.com/drive/folders/<ID>
  # Cách A — OAuth (file thuộc tài khoản Google của bạn — khuyên dùng với Gmail cá nhân):
  GOOGLE_DRIVE_CLIENT_ID=... / GOOGLE_DRIVE_CLIENT_SECRET=... / GOOGLE_DRIVE_REFRESH_TOKEN=...
  # Cách B — Service Account (nhớ share thư mục cho SA email quyền Editor):
  GOOGLE_DRIVE_SA_CLIENT_EMAIL=... / GOOGLE_DRIVE_SA_PRIVATE_KEY="..."
  # Fallback khi chưa bóc JSON vào env: GOOGLE_DRIVE_CREDENTIALS_JSON=secrets/google_drive.json

Setup lần đầu (đặt file JSON vào secrets/ rồi):
  python google_drive_upload.py --extract secrets/google_drive.json   # bóc secret vào .env
  python google_drive_upload.py --auth      # chỉ Cách A: mở trình duyệt lấy refresh token (1 lần)
  python google_drive_upload.py --check     # kiểm tra credentials + quyền vào thư mục
  python google_drive_upload.py video.mp4   # test upload thật
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"

# Full drive scope: scope hẹp drive.file KHÔNG upload được vào thư mục có sẵn do user tạo tay
# (API trả 404 vì app không "thấy" thư mục đó).
SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

load_dotenv(ENV_PATH)


class DriveConfigError(RuntimeError):
    """Thiếu/sai cấu hình .env — message nói rõ cần điền gì."""


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def drive_enabled():
    return _env("GOOGLE_DRIVE_UPLOAD_ENABLED").lower() in ("1", "true", "yes", "on")


def default_folder_id():
    folder = _env("GOOGLE_DRIVE_FOLDER_ID")
    # Cho phép dán nguyên URL thư mục — tự bóc ID ra.
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", folder)
    return m.group(1) if m else folder


# ---------------------------------------------------------------- credentials

def _sa_info_from_env():
    email = _env("GOOGLE_DRIVE_SA_CLIENT_EMAIL")
    key = _env("GOOGLE_DRIVE_SA_PRIVATE_KEY")
    if not (email and key):
        return None
    # chấp nhận cả key paste tay dạng "\n" literal; trả lại newline cuối bị _env() strip mất
    key = key.replace("\\n", "\n")
    if not key.endswith("\n"):
        key += "\n"
    return {
        "type": "service_account",
        "client_email": email,
        "private_key": key,
        "token_uri": TOKEN_URI,
        "project_id": _env("GOOGLE_DRIVE_PROJECT_ID") or None,
    }


def build_credentials():
    """Thứ tự ưu tiên: SA trong env -> OAuth trong env -> file JSON (fallback)."""
    sa = _sa_info_from_env()
    if sa:
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_info(sa, scopes=SCOPES)

    cid, csec = _env("GOOGLE_DRIVE_CLIENT_ID"), _env("GOOGLE_DRIVE_CLIENT_SECRET")
    rtok = _env("GOOGLE_DRIVE_REFRESH_TOKEN")
    if cid and csec and rtok:
        from google.oauth2.credentials import Credentials
        return Credentials(
            None, refresh_token=rtok, token_uri=TOKEN_URI,
            client_id=cid, client_secret=csec, scopes=SCOPES,
        )

    json_path = _env("GOOGLE_DRIVE_CREDENTIALS_JSON")
    if json_path and Path(json_path).exists():
        data = json.loads(Path(json_path).read_text())
        if data.get("type") == "service_account":
            from google.oauth2 import service_account
            return service_account.Credentials.from_service_account_file(json_path, scopes=SCOPES)
        if "installed" in data or "web" in data:
            raise DriveConfigError(
                f"{json_path} là OAuth client JSON — cần refresh token:\n"
                f"  1) python google_drive_upload.py --extract {json_path}\n"
                f"  2) python google_drive_upload.py --auth"
            )

    if cid and csec:
        raise DriveConfigError(
            "Có CLIENT_ID/SECRET nhưng thiếu GOOGLE_DRIVE_REFRESH_TOKEN — chạy 1 lần: "
            "python google_drive_upload.py --auth"
        )
    raise DriveConfigError(
        "Chưa có credentials Google Drive trong .env — đặt file JSON vào secrets/ rồi chạy: "
        "python google_drive_upload.py --extract secrets/<file>.json"
    )


def get_service():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=build_credentials(), cache_discovery=False)


# ---------------------------------------------------------------- upload

def upload_file(path, folder_id=None, name=None):
    """Upload 1 file vào thư mục Drive. Trả về {'id', 'name', 'webViewLink'}.

    Raise DriveConfigError (thiếu config) hoặc lỗi API — caller tự try/except
    (queue_worker bọc sẵn, upload hỏng không được làm chết worker).
    """
    from googleapiclient.http import MediaFileUpload

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File không tồn tại: {path}")
    folder_id = folder_id or default_folder_id()
    if not folder_id:
        raise DriveConfigError("Chưa điền GOOGLE_DRIVE_FOLDER_ID trong .env")

    service = get_service()
    media = MediaFileUpload(str(path), resumable=True, chunksize=10 * 1024 * 1024)
    body = {"name": name or path.name, "parents": [folder_id]}
    info = (
        service.files()
        .create(body=body, media_body=media, fields="id,name,webViewLink",
                supportsAllDrives=True)
        .execute(num_retries=3)     # tự retry lỗi mạng/5xx tạm thời
    )
    return info


# ---------------------------------------------------------------- .env helpers

def _quote_env(value):
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]*", value):
        return value
    # double-quote + escape \n — dotenv đọc lại đúng nguyên bản (ensure_ascii vì dotenv không decode \uXXXX)
    return json.dumps(value, ensure_ascii=False)


def _update_env_file(updates, env_path=None):
    """Ghi/đè các KEY=VALUE vào .env, giữ nguyên mọi dòng khác. chmod 600."""
    env_path = env_path or ENV_PATH
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={_quote_env(updates[key])}"
            seen.add(key)
    missing = [k for k in updates if k not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{k}={_quote_env(updates[k])}" for k in missing)
    env_path.write_text("\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)
    for k, v in updates.items():
        os.environ[k] = v


# ---------------------------------------------------------------- CLI actions

def cmd_extract(json_path):
    data = json.loads(Path(json_path).read_text())
    if data.get("type") == "service_account":
        updates = {
            "GOOGLE_DRIVE_SA_CLIENT_EMAIL": data["client_email"],
            "GOOGLE_DRIVE_SA_PRIVATE_KEY": data["private_key"],
            "GOOGLE_DRIVE_PROJECT_ID": data.get("project_id", ""),
        }
        _update_env_file(updates)
        print(f"Đã bóc Service Account vào {ENV_PATH}")
        print(f"→ Share thư mục Drive cho email này (quyền Editor): {data['client_email']}")
        print("→ Lưu ý: SA KHÔNG có quota lưu trữ riêng — Gmail cá nhân nên dùng OAuth (Cách A).")
    elif "installed" in data or "web" in data:
        node = data.get("installed") or data["web"]
        updates = {
            "GOOGLE_DRIVE_CLIENT_ID": node["client_id"],
            "GOOGLE_DRIVE_CLIENT_SECRET": node["client_secret"],
            "GOOGLE_DRIVE_PROJECT_ID": node.get("project_id", ""),
        }
        if node.get("redirect_uris"):
            updates["GOOGLE_DRIVE_REDIRECT_URI"] = node["redirect_uris"][0]
        _update_env_file(updates)
        print(f"Đã bóc OAuth client vào {ENV_PATH}")
        print("→ Bước tiếp: python google_drive_upload.py --auth   (lấy refresh token, chỉ 1 lần)")
    else:
        sys.exit(f"Không nhận dạng được {json_path}: không phải service account hay OAuth client JSON")
    print(f"→ Xong có thể XÓA file JSON: rm {json_path}")


def cmd_auth():
    """OAuth 1 lần: mở trình duyệt -> đồng ý -> lưu refresh token vào .env."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    cid, csec = _env("GOOGLE_DRIVE_CLIENT_ID"), _env("GOOGLE_DRIVE_CLIENT_SECRET")
    if not (cid and csec):
        sys.exit("Thiếu GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET trong .env "
                 "— chạy --extract trước.")
    flow = InstalledAppFlow.from_client_config(
        {"installed": {
            "client_id": cid, "client_secret": csec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }},
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        sys.exit("Google không trả refresh token — thử lại (đảm bảo màn hình consent hiện ra).")
    _update_env_file({"GOOGLE_DRIVE_REFRESH_TOKEN": creds.refresh_token})
    print(f"Đã lưu GOOGLE_DRIVE_REFRESH_TOKEN vào {ENV_PATH}")
    print("→ Điền nốt GOOGLE_DRIVE_FOLDER_ID rồi: python google_drive_upload.py --check")


def cmd_check():
    folder_id = default_folder_id()
    if not folder_id:
        sys.exit("Chưa điền GOOGLE_DRIVE_FOLDER_ID trong .env")
    service = get_service()
    info = service.files().get(
        fileId=folder_id, fields="id,name,mimeType", supportsAllDrives=True
    ).execute()
    if info.get("mimeType") != "application/vnd.google-apps.folder":
        sys.exit(f"ID {folder_id} không phải thư mục (mimeType={info.get('mimeType')})")
    print(f"OK — credentials hợp lệ, thư mục đích: '{info['name']}' ({folder_id})")
    if not drive_enabled():
        print("Lưu ý: GOOGLE_DRIVE_UPLOAD_ENABLED chưa bật (=1) nên worker sẽ KHÔNG tự upload.")


def main():
    ap = argparse.ArgumentParser(description="Upload file lên Google Drive (thư mục mặc định từ .env)")
    ap.add_argument("file", nargs="?", help="file cần upload (test tay / upload lại job lỗi)")
    ap.add_argument("--folder", help="ID thư mục đích (mặc định: GOOGLE_DRIVE_FOLDER_ID)")
    ap.add_argument("--extract", metavar="JSON", help="bóc secret từ file JSON của Google vào .env")
    ap.add_argument("--auth", action="store_true", help="OAuth: lấy refresh token 1 lần qua trình duyệt")
    ap.add_argument("--check", action="store_true", help="kiểm tra credentials + quyền vào thư mục")
    args = ap.parse_args()

    if args.extract:
        cmd_extract(args.extract)
    elif args.auth:
        cmd_auth()
    elif args.check:
        cmd_check()
    elif args.file:
        info = upload_file(args.file, folder_id=args.folder)
        print(f"Uploaded '{info['name']}' -> {info.get('webViewLink')}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
