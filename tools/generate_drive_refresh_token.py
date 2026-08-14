"""Buat OAuth refresh token Google Drive di komputer admin, bukan di Railway."""

from __future__ import annotations

import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    parser = argparse.ArgumentParser(
        description="Membuat GOOGLE_OAUTH_REFRESH_TOKEN untuk E-Master Jatim.")
    parser.add_argument("client_secrets", type=Path,
                        help="Path file OAuth Desktop client JSON dari Google Cloud Console")
    args = parser.parse_args()
    if not args.client_secrets.is_file():
        parser.error("File OAuth client JSON tidak ditemukan.")
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), SCOPES)
    credentials = flow.run_local_server(
        host="localhost", port=0, open_browser=True,
        authorization_prompt_message="Buka URL berikut jika browser tidak terbuka:\n{url}",
        success_message="Izin Google Drive berhasil. Kembali ke Terminal.",
        access_type="offline", prompt="consent")
    print("\nSimpan tiga nilai berikut sebagai Railway Variables.")
    print("Jangan kirim atau unggah nilainya ke repository/chat.\n")
    print(f"GOOGLE_OAUTH_CLIENT_ID={credentials.client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={credentials.client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={credentials.refresh_token}")


if __name__ == "__main__":
    main()
