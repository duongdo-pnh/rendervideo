"""Tự động đẩy video render xong lên Google Drive, vào 1 thư mục mặc định.

Secret thật sự (per-user/per-máy) nằm trong .env / jobs.db (KHÔNG hard-code, KHÔNG commit file JSON):
  GOOGLE_DRIVE_UPLOAD_ENABLED=1
  GOOGLE_DRIVE_FOLDER_ID=...        # ID thư mục đích: drive.google.com/drive/folders/<ID>
  # Cách A — OAuth (khuyên dùng, kể cả với Gmail cá nhân): CLIENT_ID/SECRET đã nhúng sẵn
  # trong code (_DEFAULT_CLIENT_ID/_DEFAULT_CLIENT_SECRET — cùng 1 OAuth app cho mọi máy,
  # .env override được nếu muốn dùng Google Cloud project khác). Chỉ cần bấm "Kết nối
  # Drive" trên UI hoặc chạy --auth — KHÔNG cần --extract nữa.
  # (REFRESH_TOKEN không nằm trong .env — --auth lưu thẳng vào jobs.db, per-máy, để
  #  .env đi theo máy khác không kéo theo tài khoản Google của máy đó)
  # Cách B — Service Account (nhớ share thư mục cho SA email quyền Editor):
  GOOGLE_DRIVE_SA_CLIENT_EMAIL=... / GOOGLE_DRIVE_SA_PRIVATE_KEY="..."
  # Fallback khi chưa bóc JSON vào env: GOOGLE_DRIVE_CREDENTIALS_JSON=secrets/google_drive.json

Setup lần đầu (Cách A — chỉ cần điền FOLDER_ID, CLIENT_ID/SECRET đã có sẵn):
  python google_drive_upload.py --auth      # mở trình duyệt lấy refresh token (1 lần)
  python google_drive_upload.py --check     # kiểm tra credentials + quyền vào thư mục
  python google_drive_upload.py video.mp4   # test upload thật
"""
import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

import database
from env_path import resolve_env_path

ROOT = Path(__file__).parent
ENV_PATH = resolve_env_path(ROOT / ".env")

# Full drive scope: scope hẹp drive.file KHÔNG upload được vào thư mục có sẵn do user tạo tay
# (API trả 404 vì app không "thấy" thư mục đó).
SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# OAuth "Desktop app" client identity cho Byscom Render — CÙNG một giá trị cho
# mọi máy cài app (không phải secret per-user, khác hẳn REFRESH_TOKEN). Theo
# tài liệu OAuth của Google, client_secret của loại "Desktop app" KHÔNG được
# xem là bí mật thật sự (app phân phối ra ngoài thì ai cũng lấy được), nên
# nhúng thẳng vào đây là an toàn — mục đích chỉ để mọi máy mới cài app bấm
# "Kết nối Drive" là mở trình duyệt được ngay, không cần chạy --extract tay.
# .env vẫn override được nếu ai đó muốn dùng một Google Cloud project khác.
_DEFAULT_CLIENT_ID = ""
_DEFAULT_CLIENT_SECRET = ""

_env_mtime = None


def _reload_if_changed():
    """Re-read .env when it changes on disk — without this, queue_worker.py
    (a long-lived process, imports this module once at startup) never sees
    GOOGLE_DRIVE_UPLOAD_ENABLED/FOLDER_ID changes saved later through the UI
    until the worker itself restarts: _start_drive_upload() would keep
    silently no-op'ing (drive_enabled() stuck False) forever, with no error
    logged anywhere since it never even attempts the upload. Same pattern as
    latentsync/tts/factory.py's reload_config()."""
    global _env_mtime
    try:
        mtime = ENV_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if mtime != _env_mtime:
        load_dotenv(ENV_PATH, override=True)
        _env_mtime = mtime


class DriveConfigError(RuntimeError):
    """Thiếu/sai cấu hình .env — message nói rõ cần điền gì."""


def _env(name, default=""):
    _reload_if_changed()
    return os.environ.get(name, default).strip()


def _oauth_client():
    """(client_id, client_secret) — .env nếu có, không thì dùng mặc định nhúng sẵn."""
    return (_env("GOOGLE_DRIVE_CLIENT_ID") or _DEFAULT_CLIENT_ID,
            _env("GOOGLE_DRIVE_CLIENT_SECRET") or _DEFAULT_CLIENT_SECRET)


def _get_refresh_token():
    """Refresh token lives in jobs.db (see database.get_config), not .env.

    Old installs may still have GOOGLE_DRIVE_REFRESH_TOKEN sitting in .env
    from before this changed — migrate it into the DB once on read, then
    ignore .env for this key going forward.
    """
    token = database.get_config("google_drive_refresh_token")
    if token:
        return token
    legacy = _env("GOOGLE_DRIVE_REFRESH_TOKEN")
    if legacy:
        database.set_config("google_drive_refresh_token", legacy)
    return legacy


def drive_enabled():
    return _env("GOOGLE_DRIVE_UPLOAD_ENABLED").lower() in ("1", "true", "yes", "on")


def default_folder_id():
    folder = _env("GOOGLE_DRIVE_FOLDER_ID")
    # Cho phép dán nguyên URL thư mục — tự bóc ID ra.
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", folder)
    return m.group(1) if m else folder


def save_settings(folder_id=None, enabled=None):
    """Lưu FOLDER_ID / UPLOAD_ENABLED qua UI (tab Google Drive) thay vì bắt
    sửa tay .env — cùng cơ chế persist với tts_config.save_config()."""
    updates = {}
    if folder_id is not None:
        updates["GOOGLE_DRIVE_FOLDER_ID"] = folder_id.strip()
    if enabled is not None:
        updates["GOOGLE_DRIVE_UPLOAD_ENABLED"] = "1" if enabled else "0"
    if updates:
        _update_env_file(updates)


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

    cid, csec = _oauth_client()
    rtok = _get_refresh_token()
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

# find-or-create thư mục con: cache + lock để nhiều thread upload song song
# (queue_worker đẩy nền từng job) không tạo trùng 2 thư mục cùng tên trên Drive.
_folder_cache = {}
_folder_lock = threading.Lock()


def _escape_query(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def ensure_folder(service, name, parent_id):
    """Trả về ID thư mục con `name` trong `parent_id` — tìm thấy thì dùng lại, chưa có thì tạo."""
    key = (parent_id, name)
    with _folder_lock:
        if key in _folder_cache:
            return _folder_cache[key]
        q = (
            f"name = '{_escape_query(name)}' and '{_escape_query(parent_id)}' in parents "
            "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        found = service.files().list(
            q=q, fields="files(id)", pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(num_retries=3).get("files")
        if found:
            fid = found[0]["id"]
        else:
            fid = service.files().create(
                body={"name": name, "parents": [parent_id],
                      "mimeType": "application/vnd.google-apps.folder"},
                fields="id", supportsAllDrives=True,
            ).execute(num_retries=3)["id"]
        _folder_cache[key] = fid
        return fid


def list_files(folder_id=None, page_size=200):
    """Liệt kê file/thư mục con TRỰC TIẾP bên trong `folder_id` (mặc định = folder gốc .env).

    Trả list dict {id, name, mimeType, webViewLink, modifiedTime, size, is_folder}, thư mục
    xếp trước. Chỉ đọc — an toàn gọi lại nhiều lần, không đổi credentials/token.
    """
    folder_id = folder_id or default_folder_id()
    if not folder_id:
        raise DriveConfigError("Chưa điền GOOGLE_DRIVE_FOLDER_ID trong .env")

    service = get_service()
    q = f"'{_escape_query(folder_id)}' in parents and trashed = false"
    items = service.files().list(
        q=q, pageSize=page_size,
        fields="files(id,name,mimeType,webViewLink,modifiedTime,size)",
        orderBy="folder,name_natural",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute(num_retries=3).get("files", [])

    out = []
    for f in items:
        is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"
        out.append({
            "id": f["id"], "name": f["name"], "mime_type": f.get("mimeType", ""),
            "web_view_link": f.get("webViewLink"), "modified_time": f.get("modifiedTime"),
            "size": int(f["size"]) if f.get("size") else None, "is_folder": is_folder,
        })
    return out


def download_folder(folder_id, dest_dir):
    """Tải mọi FILE (không đệ quy vào thư mục con) nằm trực tiếp trong `folder_id`
    về `dest_dir` (tự tạo nếu chưa có). Dùng cho akali_bridge.py tải folder batch
    (đúng 1 Excel + 1 video, xem doc_api/APi_RENDER_QUEUE.md mục 4) về máy trước
    khi render — hợp đồng đó không có thư mục lồng nên bỏ qua item is_folder.

    Trả về list Path đã tải (thứ tự theo list_files: thư mục trước, tên sau —
    ở đây luôn rỗng thư mục do bị bỏ qua). Lỗi mạng/API để nguyên cho caller
    tự quyết retry (akali_bridge coi lỗi tải folder là retryable).
    """
    import io

    from googleapiclient.http import MediaIoBaseDownload

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    service = get_service()

    downloaded = []
    for item in list_files(folder_id, page_size=1000):
        if item["is_folder"]:
            continue
        dest_path = dest_dir / item["name"]
        request = service.files().get_media(fileId=item["id"], supportsAllDrives=True)
        with io.FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk(num_retries=3)
        downloaded.append(dest_path)
    return downloaded


def folder_info(folder_id=None):
    """Tên + link thư mục (dùng cho breadcrumb) — raise nếu ID không phải thư mục hợp lệ."""
    folder_id = folder_id or default_folder_id()
    if not folder_id:
        raise DriveConfigError("Chưa điền GOOGLE_DRIVE_FOLDER_ID trong .env")
    service = get_service()
    info = service.files().get(
        fileId=folder_id, fields="id,name,mimeType,webViewLink", supportsAllDrives=True
    ).execute()
    if info.get("mimeType") != "application/vnd.google-apps.folder":
        raise DriveConfigError(f"ID {folder_id} không phải thư mục")
    return {"id": info["id"], "name": info["name"], "web_view_link": info.get("webViewLink")}


def _create_file(service, path, name, parent_id):
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(path), resumable=True, chunksize=10 * 1024 * 1024)
    body = {"name": name, "parents": [parent_id]}
    return (
        service.files()
        .create(body=body, media_body=media, fields="id,name,webViewLink",
                supportsAllDrives=True)
        .execute(num_retries=3)     # tự retry lỗi mạng/5xx tạm thời
    )


def upload_file(path, folder_id=None, name=None, subfolder=None):
    """Upload 1 file vào thư mục Drive. Trả về {'id', 'name', 'webViewLink'}.

    subfolder: tên thư mục con trong folder đích (job import Excel = tên file Excel) —
    tự tìm-hoặc-tạo. None/rỗng = upload thẳng vào folder đích.

    Raise DriveConfigError (thiếu config) hoặc lỗi API — caller tự try/except
    (queue_worker bọc sẵn, upload hỏng không được làm chết worker).
    """
    from googleapiclient.errors import HttpError

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File không tồn tại: {path}")
    folder_id = folder_id or default_folder_id()
    if not folder_id:
        raise DriveConfigError("Chưa điền GOOGLE_DRIVE_FOLDER_ID trong .env")

    service = get_service()
    subfolder = str(subfolder).strip() if subfolder else ""
    target_id = ensure_folder(service, subfolder, folder_id) if subfolder else folder_id
    try:
        return _create_file(service, path, name or path.name, target_id)
    except HttpError as e:
        # ensure_folder()'s cache never expires — if someone deletes that subfolder
        # straight from Drive after it's cached, every upload into it 404s forever
        # until this process restarts. Evict the stale entry and retry once with a
        # fresh find-or-create before giving up (only for a resolved SUBfolder — a
        # 404 on the top-level configured folder_id itself needs a human to fix the
        # Drive config, retrying won't help).
        if subfolder and target_id != folder_id and e.status_code == 404:
            _folder_cache.pop((folder_id, subfolder), None)
            target_id = ensure_folder(service, subfolder, folder_id)
            return _create_file(service, path, name or path.name, target_id)
        raise


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
    env_path.parent.mkdir(parents=True, exist_ok=True)
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


def run_oauth_flow(open_browser=True, timeout_seconds=None):
    """OAuth 1 lần: mở trình duyệt -> đồng ý -> lưu refresh token vào DB (jobs.db).

    Blocking — chạy `flow.run_local_server()` (tự mở trình duyệt + đợi callback trên
    localhost). Raise DriveConfigError nếu Google không trả refresh token. Trả về
    refresh token mới. Tách khỏi cmd_auth() để backend/drive.py gọi được từ một
    thread nền mà không làm chết tiến trình (sys.exit) khi lỗi.

    timeout_seconds: nếu đặt, bỏ chờ sau ngần ấy giây nếu không nhận được callback
    (tab bị đóng, người dùng không đồng ý...). Mặc định None = chờ vô hạn, giữ
    nguyên hành vi cũ cho cmd_auth() (CLI). backend/drive.py truyền một giá trị cụ
    thể vì nó chạy trong thread nền — không có timeout thì _reauth_state kẹt ở
    "pending" mãi mãi, chặn mọi lần thử lại cho tới khi backend restart.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    cid, csec = _oauth_client()
    flow = InstalledAppFlow.from_client_config(
        {"installed": {
            "client_id": cid, "client_secret": csec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }},
        scopes=SCOPES,
    )
    try:
        creds = flow.run_local_server(
            port=0, open_browser=open_browser, access_type="offline", prompt="consent",
            timeout_seconds=timeout_seconds,
        )
    except AttributeError:
        # run_local_server's own timeout path doesn't raise: handle_request() just
        # returns with no request received, and it then blows up trying to read the
        # (never-set) authorization response off None. Confirmed against the pinned
        # google-auth-oauthlib==1.2.1 directly — this is the timeout, not a real bug.
        raise DriveConfigError(f"Hết {timeout_seconds}s chờ đăng nhập trên trình duyệt — thử lại.") from None
    if not creds.refresh_token:
        raise DriveConfigError("Google không trả refresh token — thử lại (đảm bảo màn hình consent hiện ra).")
    database.set_config("google_drive_refresh_token", creds.refresh_token)
    return creds.refresh_token


def cmd_auth():
    run_oauth_flow()
    print(f"Đã lưu refresh token vào {database.DB_PATH} (không phải .env — mỗi máy tự xác thực riêng)")
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
    ap.add_argument("--subfolder", help="tên thư mục con trong thư mục đích (tự tạo nếu chưa có)")
    ap.add_argument("--extract", metavar="JSON", help="bóc secret từ file JSON của Google vào .env")
    ap.add_argument("--auth", action="store_true", help="OAuth: lấy refresh token 1 lần qua trình duyệt")
    ap.add_argument("--check", action="store_true", help="kiểm tra credentials + quyền vào thư mục")
    args = ap.parse_args()

    try:
        if args.extract:
            cmd_extract(args.extract)
        elif args.auth:
            cmd_auth()
        elif args.check:
            cmd_check()
        elif args.file:
            info = upload_file(args.file, folder_id=args.folder, subfolder=args.subfolder)
            print(f"Uploaded '{info['name']}' -> {info.get('webViewLink')}")
        else:
            ap.print_help()
    except DriveConfigError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
