from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken


BASE_URL = "https://master.bkd.jatimprov.go.id/"


class EMasterError(RuntimeError):
    pass


class AuthenticationRequired(EMasterError):
    pass


@dataclass(frozen=True)
class KamusItem:
    code: str
    activity: str
    unit: str
    wpt: int
    description: str
    object_hint: str


@dataclass(frozen=True)
class WorkTarget:
    name: str
    breakdown_id: str
    target_id: str
    informasi_id: str
    add_url: str


def _find_field(form, candidates: Iterable[str], input_type: str | None = None) -> str | None:
    lowered = tuple(x.lower() for x in candidates)
    for tag in form.select("input, select, textarea"):
        name = tag.get("name")
        if not name:
            continue
        if input_type and tag.get("type", "text").lower() != input_type:
            continue
        blob = " ".join([name, tag.get("id", ""), tag.get("placeholder", "")]).lower()
        if any(c in blob for c in lowered):
            return name
    return None


class EMasterClient:
    def __init__(self, nip: str, password: str, encryption_key: str, session_path: str):
        self.nip = nip
        self.password = password
        self.fernet = Fernet(encryption_key.encode())
        self.session_path = Path(session_path)
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "Mozilla/5.0 EMasterPersonalTelegramBot/1.0"})
        self._restore()

    def _restore(self) -> None:
        if not self.session_path.exists():
            return
        try:
            raw = self.fernet.decrypt(self.session_path.read_bytes())
            self.http.cookies.update(json.loads(raw.decode()))
        except (InvalidToken, ValueError, OSError):
            pass

    def _persist(self) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(requests.utils.dict_from_cookiejar(self.http.cookies)).encode()
        self.session_path.write_bytes(self.fernet.encrypt(payload))

    @staticmethod
    def _form_payload(form) -> dict[str, str]:
        payload: dict[str, str] = {}
        for tag in form.select("input[name], select[name], textarea[name]"):
            if tag.name == "input" and tag.get("type", "text").lower() in {"submit", "button", "image", "file"}:
                continue
            if tag.get("type", "").lower() in {"checkbox", "radio"} and not tag.has_attr("checked"):
                continue
            payload[tag["name"]] = tag.get("value", "")
        return payload

    def is_authenticated(self) -> bool:
        r = self.http.get(urljoin(BASE_URL, "essmedia.php?module=aktifitas_bulan"), timeout=30)
        text = r.text.lower()
        return r.ok and "aktivitas harian tahun" in text and "login area" not in text

    def begin_login(self) -> bool:
        """Login NIP/password. Returns True when OTP is required."""
        r = self.http.get(BASE_URL, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form")
        if not form:
            raise EMasterError("Form login e-Master tidak ditemukan.")
        payload = self._form_payload(form)
        user_field = _find_field(form, ("nip", "user", "username", "login"))
        pass_field = _find_field(form, ("pass", "password"), "password")
        if not user_field or not pass_field:
            raise EMasterError("Nama field login berubah; diperlukan pembaruan konektor.")
        payload[user_field] = self.nip
        payload[pass_field] = self.password
        action = urljoin(r.url, form.get("action") or r.url)
        out = self.http.post(action, data=payload, timeout=30, allow_redirects=True)
        low = out.text.lower()
        if "two factor authentication" in low or "kode otp" in low or "index_mfa" in out.url:
            return True
        if "aktivitas harian tahun" in low or self.is_authenticated():
            self._persist()
            return False
        raise EMasterError("Login ditolak. Periksa NIP/password atau perubahan halaman e-Master.")

    def submit_otp(self, otp: str) -> None:
        if not re.fullmatch(r"\d{6}", otp):
            raise EMasterError("OTP harus tepat 6 digit.")
        url = urljoin(BASE_URL, f"index_mfa.php?user={self.nip}")
        r = self.http.get(url, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form")
        if not form:
            raise EMasterError("Form OTP e-Master tidak ditemukan.")
        payload = self._form_payload(form)
        otp_field = _find_field(form, ("otp", "code", "kode", "token"))
        if not otp_field:
            candidates = form.select('input[type="text"][name], input[type="number"][name]')
            otp_field = candidates[0].get("name") if candidates else None
        if not otp_field:
            raise EMasterError("Nama field OTP berubah; diperlukan pembaruan konektor.")
        payload[otp_field] = otp
        action = urljoin(r.url, form.get("action") or r.url)
        self.http.post(action, data=payload, timeout=30, allow_redirects=True)
        if not self.is_authenticated():
            raise EMasterError("OTP ditolak atau sudah kedaluwarsa.")
        self._persist()

    def search_kamus(self, keyword: str, limit: int = 8) -> list[KamusItem]:
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        url = urljoin(BASE_URL, "popup_skp/popup_aktifitas.php")
        r = self.http.get(url, params={"aktifitas": keyword}, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        found: list[KamusItem] = []
        for tr in soup.select("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.select("td")]
            if len(cells) < 6 or not cells[0].isdigit():
                continue
            code_activity = cells[1]
            match = re.match(r"(\d+)[-–](.*)", code_activity)
            code, activity = (match.group(1), match.group(2).strip()) if match else (cells[0], code_activity)
            try:
                wpt = int(re.sub(r"\D", "", cells[3]))
            except ValueError:
                continue
            item = KamusItem(code, activity, cells[2], wpt, cells[4], cells[5])
            if keyword.lower() in (activity + " " + cells[4]).lower():
                found.append(item)
            if len(found) >= limit:
                break
        return found

    def list_work_targets(self, month: str) -> list[WorkTarget]:
        """Read every personal Kegiatan Tugas Jabatan and its hidden IDs."""
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        page = self.http.get(urljoin(BASE_URL, "essmedia.php"),
                             params={"module": "aktifitas_bulan", "bulan": month}, timeout=30)
        soup = BeautifulSoup(page.text, "html.parser")
        links = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if "module=aktifitas_bulan" in href and "act=realisasi" in href and "id_breakdown=" in href:
                links.append(urljoin(page.url, href))
        targets: list[WorkTarget] = []
        seen: set[str] = set()
        for detail_url in links:
            detail = self.http.get(detail_url, timeout=30)
            dsoup = BeautifulSoup(detail.text, "html.parser")
            add_link = next((urljoin(detail.url, a.get("href")) for a in dsoup.select("a[href]")
                             if "act=tambahaktifitas" in a.get("href", "")), None)
            if not add_link or add_link in seen:
                continue
            seen.add(add_link)
            form_page = self.http.get(add_link, timeout=30)
            fsoup = BeautifulSoup(form_page.text, "html.parser")
            form = next((f for f in fsoup.find_all("form")
                         if f.select_one('[name="breakdown_id"]')), None)
            if not form:
                continue
            values = self._form_payload(form)
            target_name = values.get("detail_kegiatan", "").strip()
            if not target_name:
                label = fsoup.find(string=re.compile("Kegiatan Tugas Jabatan", re.I))
                target_name = label.find_next("textarea").get_text(strip=True) if label else ""
            if all(values.get(k) for k in ("breakdown_id", "target_id", "informasi_id")):
                targets.append(WorkTarget(target_name, values["breakdown_id"], values["target_id"],
                                          values["informasi_id"], add_link))
        return targets

    def submit_activity(self, *, month: str, target: WorkTarget, date: str,
                        item: KamusItem, volume: int, object_work: str) -> None:
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        endpoint = urljoin(BASE_URL, "modul_essmankin/mod_aktifitas_bulan/aksi_aktifitas_bulan.php")
        data = {
            "bulan": month, "breakdown_id": target.breakdown_id, "target_id": target.target_id,
            "informasi_id": target.informasi_id, "tugas": "", "detail_kegiatan": target.name,
            "tgl_kegiatan": date, "rk": f"{item.code}-{item.activity}",
            "satuan": item.unit, "wpt": str(item.wpt), "volume": str(volume),
            "objek_kerja": object_work, "submit": "",
        }
        files = {name: (None, value) for name, value in data.items()}
        r = self.http.post(endpoint, params={"module": "aktifitas_bulan", "act": "input"},
                           files=files, timeout=30, allow_redirects=False)
        location = r.headers.get("Location", "")
        if r.status_code not in (301, 302, 303) or "info=insert" not in location:
            raise EMasterError(f"e-Master tidak mengonfirmasi penyimpanan (HTTP {r.status_code}).")
        self._persist()
