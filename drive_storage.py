from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
from pathlib import Path


DEFAULT_DRIVE_ROOT_ID = "1WBCjukQS3lMJFfsfjGb3_mGhSFZJpsXd"
INDONESIAN_MONTHS = (
    "", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)


class DriveStorageError(RuntimeError):
    pass


def safe_drive_name(value: str, fallback: str = "Pegawai") -> str:
    value = re.sub(r"[\x00-\x1f/\\]+", " ", value or "")
    value = " ".join(value.split()).strip(" .")
    return (value or fallback)[:150]


def _folder_id(value: str) -> str:
    value = (value or "").strip()
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else value


class GoogleDriveStorage:
    """Penyimpanan bukti dan laporan memakai Google Drive API."""

    def __init__(self, root_folder_id: str | None = None):
        self.root_folder_id = _folder_id(
            root_folder_id or os.getenv("DRIVE_ROOT_FOLDER_ID", DEFAULT_DRIVE_ROOT_ID))
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", self.root_folder_id):
            raise DriveStorageError("DRIVE_ROOT_FOLDER_ID tidak valid.")
        self._lock = threading.RLock()
        self._service = None
        self._auth_mode = None

    @staticmethod
    def _credentials_info() -> dict:
        raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        encoded = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", "").strip()
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        try:
            if encoded:
                raw = base64.b64decode(encoded, validate=True).decode("utf-8")
            if raw:
                info = json.loads(raw)
            elif credentials_path:
                path = Path(credentials_path)
                if not path.is_file():
                    raise DriveStorageError("File Google Service Account tidak ditemukan.")
                info = json.loads(path.read_text(encoding="utf-8"))
            else:
                raise DriveStorageError(
                    "Google Drive belum dikonfigurasi. Isi kredensial OAuth pengguna "
                    "atau GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 untuk Shared Drive.")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DriveStorageError("Konfigurasi Google Service Account tidak valid.") from exc
        if info.get("type") != "service_account" or not info.get("client_email"):
            raise DriveStorageError("Kredensial harus berupa Google Service Account.")
        return info

    def _get_service(self):
        with self._lock:
            if self._service is not None:
                return self._service
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise DriveStorageError(
                    "Paket Google Drive belum terpasang. Jalankan instalasi requirements.txt.") from exc
            scopes = ["https://www.googleapis.com/auth/drive"]
            refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
            client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
            client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
            if any((refresh_token, client_id, client_secret)):
                if not all((refresh_token, client_id, client_secret)):
                    raise DriveStorageError(
                        "Konfigurasi OAuth Google Drive belum lengkap.")
                from google.oauth2.credentials import Credentials
                credentials = Credentials(
                    token=None,
                    refresh_token=refresh_token,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=client_id,
                    client_secret=client_secret,
                    scopes=scopes,
                )
                self._auth_mode = "oauth"
            else:
                from google.oauth2.service_account import Credentials
                credentials = Credentials.from_service_account_info(
                    self._credentials_info(), scopes=scopes)
                self._auth_mode = "service_account"
            self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
            return self._service

    @staticmethod
    def _escape_query(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def healthcheck(self) -> str:
        try:
            item = self._get_service().files().get(
                fileId=self.root_folder_id,
                fields="id,name,mimeType,driveId,capabilities(canAddChildren)",
                supportsAllDrives=True,
            ).execute()
        except DriveStorageError:
            raise
        except Exception as exc:
            raise DriveStorageError(
                "Folder Google Drive belum dapat diakses oleh akun yang dikonfigurasi.") from exc
        if item.get("mimeType") != "application/vnd.google-apps.folder":
            raise DriveStorageError("DRIVE_ROOT_FOLDER_ID bukan folder Google Drive.")
        if not item.get("capabilities", {}).get("canAddChildren"):
            raise DriveStorageError("Akun Google Drive tidak memiliki izin menambahkan file.")
        if self._auth_mode == "service_account" and not item.get("driveId"):
            raise DriveStorageError(
                "Service Account hanya dapat mengunggah ke Shared Drive. "
                "Untuk folder My Drive, gunakan konfigurasi OAuth pengguna.")
        return str(item.get("name") or "Google Drive")

    def _find_folder(self, parent_id: str, name: str) -> str | None:
        safe_name = safe_drive_name(name)
        query = (
            f"'{parent_id}' in parents and trashed=false and "
            "mimeType='application/vnd.google-apps.folder' and "
            f"name='{self._escape_query(safe_name)}'"
        )
        result = self._get_service().files().list(
            q=query,
            spaces="drive",
            fields="files(id,name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = result.get("files", [])
        return str(files[0]["id"]) if files else None

    def ensure_folder(self, parent_id: str, name: str) -> str:
        with self._lock:
            existing = self._find_folder(parent_id, name)
            if existing:
                return existing
            metadata = {
                "name": safe_drive_name(name),
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            try:
                created = self._get_service().files().create(
                    body=metadata,
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
            except Exception as exc:
                raise DriveStorageError(f"Folder {metadata['name']} gagal dibuat.") from exc
            return str(created["id"])

    def employee_folder(self, employee_name: str, cached_folder_id: str | None = None) -> str:
        if cached_folder_id:
            return cached_folder_id
        return self.ensure_folder(self.root_folder_id, safe_drive_name(employee_name))

    def day_folder(self, employee_folder_id: str, year: int, month: int, day: int) -> str:
        # Struktur yang disepakati: Pegawai / Nama Bulan / Nomor Hari.
        month_id = self.ensure_folder(employee_folder_id, INDONESIAN_MONTHS[month])
        return self.ensure_folder(month_id, str(day))

    def report_folder(self, employee_folder_id: str, year: int, month: int) -> str:
        month_id = self.ensure_folder(employee_folder_id, INDONESIAN_MONTHS[month])
        return self.ensure_folder(month_id, "Laporan WFH")

    def upload_bytes(self, *, content: bytes, file_name: str, mime_type: str,
                     parent_id: str) -> tuple[str, str]:
        try:
            from googleapiclient.http import MediaIoBaseUpload
            media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
            created = self._get_service().files().create(
                body={"name": safe_drive_name(file_name, "bukti.jpg"), "parents": [parent_id]},
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            raise DriveStorageError("Bukti foto gagal diunggah ke Google Drive.") from exc
        file_id = str(created["id"])
        return file_id, str(created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view")

    def upload_or_update_file(self, *, local_path: str | Path, file_name: str,
                              mime_type: str, parent_id: str,
                              existing_file_id: str | None = None) -> tuple[str, str]:
        try:
            from googleapiclient.http import MediaFileUpload
            media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
            if existing_file_id:
                try:
                    item = self._get_service().files().update(
                        fileId=existing_file_id,
                        body={"name": safe_drive_name(file_name, "Laporan_WFH.docx")},
                        media_body=media,
                        fields="id,webViewLink",
                        supportsAllDrives=True,
                    ).execute()
                except Exception as update_exc:
                    if getattr(getattr(update_exc, "resp", None), "status", None) != 404:
                        raise
                    existing_file_id = None
            if not existing_file_id:
                media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
                item = self._get_service().files().create(
                    body={"name": safe_drive_name(file_name, "Laporan_WFH.docx"),
                          "parents": [parent_id]},
                    media_body=media,
                    fields="id,webViewLink",
                    supportsAllDrives=True,
                ).execute()
        except Exception as exc:
            raise DriveStorageError("File laporan gagal diunggah ke Google Drive.") from exc
        file_id = str(item["id"])
        return file_id, str(item.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view")

    def download_file(self, file_id: str, target_path: str | Path) -> Path:
        try:
            from googleapiclient.http import MediaIoBaseDownload
            request = self._get_service().files().get_media(
                fileId=file_id, supportsAllDrives=True)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            target = Path(target_path)
            target.write_bytes(buffer.getvalue())
            return target
        except Exception as exc:
            raise DriveStorageError("Bukti foto gagal diunduh untuk laporan.") from exc
