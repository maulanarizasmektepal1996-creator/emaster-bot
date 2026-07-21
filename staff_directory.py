from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


DIRECTORY_PATH = Path(__file__).with_name("data_jabatan.json")
MAX_DIRECTORY_BYTES = 512 * 1024
MAX_RECORDS = 1000


class StaffDirectoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class StaffRecord:
    name: str
    position: str


def normalize_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lstrip("'")
    if "e" in normalized.casefold():
        return ""
    digits = re.sub(r"\D", "", normalized)
    return digits if 10 <= len(digits) <= 25 else ""


def identifier_fingerprint(value: str) -> str:
    identifier = normalize_identifier(value)
    if not identifier:
        return ""
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


class LocalStaffDirectory:
    """Direktori lokal hasil ekstraksi file Excel yang sudah diverifikasi."""

    def __init__(self, path: str | Path = DIRECTORY_PATH):
        self.path = Path(path)
        self._records = self._load()

    @staticmethod
    def _clean_text(value: object, field: str) -> str:
        if not isinstance(value, str):
            raise StaffDirectoryError(f"Kolom {field} pada direktori jabatan tidak valid.")
        text = " ".join(unicodedata.normalize("NFKC", value).split())
        if not text or len(text) > 200 or any(ord(character) < 32 for character in text):
            raise StaffDirectoryError(f"Kolom {field} pada direktori jabatan tidak valid.")
        return text

    def _load(self) -> dict[str, StaffRecord]:
        try:
            if self.path.stat().st_size > MAX_DIRECTORY_BYTES:
                raise StaffDirectoryError("Direktori jabatan terlalu besar.")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StaffDirectoryError("Direktori jabatan lokal tidak dapat dibaca.") from exc
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise StaffDirectoryError("Versi direktori jabatan tidak didukung.")
        rows = payload.get("records")
        if not isinstance(rows, list) or not rows or len(rows) > MAX_RECORDS:
            raise StaffDirectoryError("Isi direktori jabatan tidak valid.")

        records: dict[str, StaffRecord] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise StaffDirectoryError("Baris direktori jabatan tidak valid.")
            fingerprint = row.get("nip_sha256")
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                raise StaffDirectoryError("Sidik NIP pada direktori jabatan tidak valid.")
            record = StaffRecord(
                name=self._clean_text(row.get("name"), "nama"),
                position=self._clean_text(row.get("position"), "jabatan"),
            )
            if fingerprint in records and records[fingerprint] != record:
                raise StaffDirectoryError("Ada sidik NIP duplikat dengan profil berbeda.")
            records[fingerprint] = record
        return records

    def find_by_nip(self, nip: str) -> StaffRecord | None:
        fingerprint = identifier_fingerprint(nip)
        return self._records.get(fingerprint) if fingerprint else None

    @property
    def count(self) -> int:
        return len(self._records)
