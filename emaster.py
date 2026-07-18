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


@dataclass(frozen=True)
class MonthProgress:
    activities: int
    minutes: int


@dataclass(frozen=True)
class RemoteActivity:
    realization_id: str
    date: str
    activity: str
    object_work: str
    unit: str
    wpt: int
    volume: int
    total: int
    delete_url: str


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
        self._mfa_action: str | None = None
        self._mfa_payload: dict[str, str] | None = None
        self._mfa_referer: str | None = None
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
        if not r.ok or "login area" in text:
            return False
        soup = BeautifulSoup(r.text, "html.parser")
        login_form = soup.select_one('form input[name="username"]') and soup.select_one('form input[name="password"]')
        if login_form:
            return False
        # Sesudah MFA e-Master dapat mengembalikan home/dashboard terlebih dahulu.
        # Menu logout hanya tersedia pada halaman ESS yang sudah terautentikasi.
        authenticated_markers = (
            "aktivitas harian tahun",
            "logout.php",
            "module=home",
            "dashboard - 2026",
        )
        return any(marker in text for marker in authenticated_markers)

    def begin_login(self) -> bool:
        """Login NIP/password. Returns True when OTP is required."""
        # Satu request ini sekaligus mengecek cookie lama. Jika sesi habis,
        # e-Master mengarahkan respons ke halaman login sehingga tidak perlu GET kedua.
        r = self.http.get(urljoin(BASE_URL, "essmedia.php?module=aktifitas_bulan"), timeout=20)
        low_initial = r.text.lower()
        if r.ok and "aktivitas harian tahun" in low_initial and "login area" not in low_initial:
            self._persist()
            return False
        if not BeautifulSoup(r.text, "html.parser").find("form"):
            r = self.http.get(BASE_URL, timeout=20)
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
        out = self.http.post(action, data=payload, timeout=20, allow_redirects=True)
        low = out.text.lower()
        if "two factor authentication" in low or "kode otp" in low or "index_mfa" in out.url:
            mfa_soup = BeautifulSoup(out.text, "html.parser")
            mfa_form = mfa_soup.find("form")
            if not mfa_form:
                raise EMasterError("Form OTP e-Master tidak ditemukan setelah login.")
            self._mfa_action = urljoin(out.url, mfa_form.get("action") or out.url)
            self._mfa_payload = self._form_payload(mfa_form)
            self._mfa_referer = out.url
            return True
        if "aktivitas harian tahun" in low or self.is_authenticated():
            self._persist()
            return False
        raise EMasterError("Login ditolak. Periksa NIP/password atau perubahan halaman e-Master.")

    def submit_otp(self, otp: str) -> None:
        if not re.fullmatch(r"\d{6}", otp):
            raise EMasterError("OTP harus tepat 6 digit.")
        if not self._mfa_action or self._mfa_payload is None:
            raise EMasterError("Konteks login OTP sudah hilang. Jalankan /login kembali.")
        payload = dict(self._mfa_payload)
        payload["username"] = self.nip
        payload["one_code"] = otp
        headers = {"Origin": BASE_URL.rstrip("/")}
        if self._mfa_referer:
            headers["Referer"] = self._mfa_referer
        out = self.http.post(self._mfa_action, data=payload, headers=headers,
                             timeout=20, allow_redirects=False)
        self._mfa_action = None
        self._mfa_payload = None
        self._mfa_referer = None
        location = out.headers.get("Location", "")
        # HAR browser menunjukkan keberhasilan resmi sebagai 302 ke halaman home.
        if out.status_code not in (301, 302, 303) or "essmedia.php?module=home" not in location:
            if "index_mfa" in location or "mfa" in location.lower():
                raise EMasterError("OTP ditolak e-Master atau kode sudah berganti. Jalankan /login dan gunakan kode terbaru.")
            raise EMasterError(f"e-Master tidak memberi konfirmasi login (HTTP {out.status_code}).")
        # Ikuti redirect sukses sekali agar state server sama dengan browser.
        self.http.get(urljoin(out.url, location), headers={"Referer": self._mfa_action or BASE_URL}, timeout=20)
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
        links: list[str] = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if "module=aktifitas_bulan" in href and "act=realisasi" in href and "id_breakdown=" in href:
                links.append(urljoin(page.url, href))
        # e-Master versi lama menggunakan onclick pada tombol bergambar kunci,
        # bukan elemen <a>. Ambil URL dari JavaScript sederhana tersebut.
        for tag in soup.select("[onclick]"):
            onclick = tag.get("onclick", "")
            match = re.search(r"(?:href|location)(?:\.href)?\s*=\s*['\"]([^'\"]+act=realisasi[^'\"]+)['\"]", onclick)
            if match and "id_breakdown=" in match.group(1):
                links.append(urljoin(page.url, match.group(1)))
        targets: list[WorkTarget] = []
        seen: set[str] = set()
        for detail_url in links:
            detail = self.http.get(detail_url, timeout=30)
            dsoup = BeautifulSoup(detail.text, "html.parser")
            add_link = next((urljoin(detail.url, a.get("href")) for a in dsoup.select("a[href]")
                             if "act=tambahaktifitas" in a.get("href", "")), None)
            if not add_link:
                for tag in dsoup.select("[onclick]"):
                    match = re.search(r"['\"]([^'\"]+act=tambahaktifitas[^'\"]+)['\"]", tag.get("onclick", ""))
                    if match:
                        add_link = urljoin(detail.url, match.group(1))
                        break
            if not add_link or add_link in seen:
                continue
            seen.add(add_link)
            form = next((f for f in dsoup.find_all("form")
                         if f.select_one('[name="breakdown_id"]')), None)
            if not form:
                continue
            values = self._form_payload(form)
            target_name = ""
            target_table = form.select_one("table")
            if target_table:
                rows = target_table.select("tbody tr")
                for row in rows:
                    cells = [x.get_text(" ", strip=True) for x in row.select("td")]
                    if len(cells) >= 3 and cells[0].isdigit():
                        target_name = cells[2]
                        break
            if all(values.get(k) for k in ("breakdown_id", "target_id", "informasi_id")):
                targets.append(WorkTarget(target_name, values["breakdown_id"], values["target_id"],
                                          values["informasi_id"], add_link))
        return targets

    def get_month_progress(self, month: str) -> MonthProgress:
        """Read the current server totals, including entries made outside this bot."""
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        r = self.http.get(urljoin(BASE_URL, "essmedia.php"),
                          params={"module": "aktifitas_bulan", "bulan": month}, timeout=30)
        plain = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        activity_match = re.search(r"Total\s+Aktifitas\s+Bulan\s+\d+\s*:\s*([\d.,]+)", plain, re.I)
        wpt_match = re.search(r"Total\s+WPT\s+Aktifitas\s+Bulan\s+\d+\s*:\s*([\d.,]+)\s*Menit", plain, re.I)
        if not wpt_match:
            raise EMasterError("Total WPT terbaru tidak ditemukan pada halaman e-Master.")
        clean = lambda value: int(re.sub(r"\D", "", value or "0"))
        return MonthProgress(clean(activity_match.group(1) if activity_match else "0"), clean(wpt_match.group(1)))

    def list_recent_activities(self, month: str, limit: int = 10) -> list[RemoteActivity]:
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        activities: list[RemoteActivity] = []
        for target in self.list_work_targets(month):
            detail_url = target.add_url.replace("act=tambahaktifitas", "act=realisasi")
            r = self.http.get(detail_url, timeout=30)
            soup = BeautifulSoup(r.text, "html.parser")
            for tr in soup.select("tbody tr"):
                cells = [x.get_text(" ", strip=True) for x in tr.select("td")]
                delete = tr.select_one('a[title="Delete"], a[title="delete"]')
                if len(cells) < 10 or not cells[0].isdigit() or not delete:
                    continue
                delete_url = urljoin(r.url, delete.get("href", ""))
                rid = re.search(r"id_realisasi=(\d+)", delete_url)
                try:
                    activities.append(RemoteActivity(
                        rid.group(1) if rid else "", cells[2].replace("-", "/"), cells[3], cells[4],
                        cells[5], int(cells[6]), int(cells[7]), int(cells[8]), delete_url))
                except ValueError:
                    continue
        activities.sort(key=lambda x: int(x.realization_id or 0), reverse=True)
        return activities[:limit]

    def delete_activity(self, delete_url: str) -> None:
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        if not delete_url.startswith(BASE_URL) or "act=delete" not in delete_url or "id_realisasi=" not in delete_url:
            raise EMasterError("Alamat penghapusan tidak valid.")
        r = self.http.get(delete_url, timeout=30, allow_redirects=True)
        if not r.ok or "proses hapus" not in r.text.lower() and "info=delete" not in r.url.lower():
            raise EMasterError("e-Master tidak mengonfirmasi penghapusan.")

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
