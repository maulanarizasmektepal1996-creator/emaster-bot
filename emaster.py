from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken


BASE_URL = "https://master.bkd.jatimprov.go.id/"
CATALOG_PATH = Path(__file__).with_name("kamus_aktivitas.json")


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
    detail_url: str = ""


@dataclass(frozen=True)
class EMasterActivity:
    id_realisasi: str
    breakdown_id: str
    month: str
    date: str
    detail: str
    object_work: str
    unit: str
    wpt: int
    volume: int
    total_minutes: int
    target_name: str
    delete_url: str
    detail_url: str


@dataclass(frozen=True)
class MonthProgress:
    activities: int
    minutes: int


@dataclass(frozen=True)
class EmployeeProfile:
    name: str
    nip: str
    position: str


def _normalize_search(value: str) -> str:
    """Normalisasi ringan agar pencarian konsisten tanpa mengubah data kiriman."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def load_local_catalog(path: Path = CATALOG_PATH) -> list[KamusItem]:
    """Muat kamus terverifikasi; data rusak diabaikan agar bot tetap dapat berjalan."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("items", [])
    except (OSError, ValueError, AttributeError):
        return []

    catalog: list[KamusItem] = []
    for row in rows:
        try:
            code = str(row["code"]).strip()
            activity = str(row["activity"]).strip()
            unit = str(row["unit"]).strip()
            wpt = int(row["wpt"])
            if not code or not activity or not unit or wpt <= 0:
                continue
            catalog.append(KamusItem(
                code=code,
                activity=activity,
                unit=unit,
                wpt=wpt,
                description=str(row.get("description", "")).strip(),
                object_hint=str(row.get("object_hint", activity)).strip() or activity,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return catalog


def filter_catalog(items: Iterable[KamusItem], keyword: str) -> list[KamusItem]:
    """Cari pada nama aktivitas dan kembalikan seluruh hasil secara berurutan."""
    needle = _normalize_search(keyword)
    if not needle:
        return []
    found = [item for item in items if needle in _normalize_search(item.activity)]
    return sorted(found, key=lambda item: (int(item.code) if item.code.isdigit() else 10**9,
                                            _normalize_search(item.activity)))


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
        self.http.headers.update({"User-Agent": "Mozilla/5.0 EMasterPersonalTelegramBot/21.2.0"})
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
        try:
            self.session_path.parent.chmod(0o700)
        except OSError:
            pass
        payload = json.dumps(requests.utils.dict_from_cookiejar(self.http.cookies)).encode()
        self.session_path.write_bytes(self.fernet.encrypt(payload))
        try:
            self.session_path.chmod(0o600)
        except OSError:
            pass

    def reset_session(self) -> None:
        """Buang cookie kedaluwarsa tanpa mengganti ENCRYPTION_KEY."""
        self.http.cookies.clear()
        try:
            self.session_path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _form_payload(form) -> dict[str, str]:
        payload: dict[str, str] = {}
        for tag in form.select("input[name], select[name], textarea[name]"):
            if tag.name == "input" and tag.get("type", "text").lower() in {"submit", "button", "image", "file"}:
                continue
            if tag.get("type", "").lower() in {"checkbox", "radio"} and not tag.has_attr("checked"):
                continue
            if tag.name == "textarea":
                value = tag.get_text()
            elif tag.name == "select":
                selected = tag.select_one("option[selected]") or tag.select_one("option")
                value = selected.get("value", selected.get_text(strip=True)) if selected else ""
            else:
                value = tag.get("value", "")
            payload[tag["name"]] = value
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

    @staticmethod
    def _parse_profile_html(html: str, fallback_nip: str) -> EmployeeProfile:
        """Baca identitas secara defensif dari halaman Info Jabatan e-Master."""
        soup = BeautifulSoup(html, "html.parser")
        name = ""
        welcome = soup.select_one(".welcome .note a, .welcome a")
        if welcome:
            name = welcome.get_text(" ", strip=True)
        if not name:
            heading = next((tag.get_text(" ", strip=True) for tag in soup.select("h1, h2, h3")
                            if "aktivitas harian tahun" in tag.get_text(" ", strip=True).casefold()), "")
            match = re.search(r"\s[-–]\s(?:detail\s[-–]\s)?(.+)$", heading, re.I)
            name = match.group(1).strip() if match else ""

        plain = soup.get_text(" ", strip=True)
        nip_match = re.search(r"(?:NIP|Login)\s*:?\s*(\d{10,25})", plain, re.I)
        nip = nip_match.group(1) if nip_match else fallback_nip

        position = ""
        accepted_labels = {
            "jabatan", "nama jabatan", "namajabatan", "jabatan saat ini",
            "nama jabatan saat ini", "jabatan sekarang", "jabatan aktif",
            "jabatan definitif", "nomenklatur jabatan", "jabatan terakhir",
        }
        for row in soup.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.select("th, td")]
            if len(cells) < 2:
                continue
            for index, cell in enumerate(cells[:-1]):
                label = _normalize_search(cell).rstrip(" :")
                if label not in accepted_labels:
                    continue
                candidate = next((value.strip() for value in cells[index + 1:]
                                  if value.strip().strip(":-") and value.strip() != "0"), "")
                if candidate and candidate not in {"-", "0"}:
                    position = candidate
                    break
            if position:
                break
        if not position:
            for label in soup.select("label"):
                label_text = _normalize_search(label.get_text(" ", strip=True)).rstrip(" :")
                if label_text not in accepted_labels:
                    continue
                target = soup.find(id=label.get("for")) if label.get("for") else None
                if target:
                    position = (target.get("value") or target.get_text(" ", strip=True)).strip()
                    if position:
                        break
        if not position:
            for field in soup.select("input[name], textarea[name], select[name]"):
                field_name = _normalize_search(field.get("name", "")).replace("_", " ")
                if field_name not in accepted_labels:
                    continue
                if field.name == "select":
                    selected = field.select_one("option[selected]") or field.select_one("option")
                    position = selected.get_text(" ", strip=True) if selected else ""
                else:
                    position = (field.get("value") or field.get_text(" ", strip=True)).strip()
                if position:
                    break
        return EmployeeProfile(name=name, nip=nip, position=position)

    def get_profile(self) -> EmployeeProfile:
        """Ambil nama, NIP, dan jabatan dari akun yang sedang login."""
        try:
            response = self.http.get(
                urljoin(BASE_URL, "essmedia.php"), params={"module": "jabatan"}, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise EMasterError("Profil pegawai e-Master belum dapat dibuka.") from exc
        if "login area" in response.text.casefold() or "index_mfa" in response.url:
            raise AuthenticationRequired("Sesi e-Master habis.")
        profile = self._parse_profile_html(response.text, self.nip)
        if not profile.name:
            # Halaman Info Jabatan dapat berubah, tetapi NIP akun tetap diketahui
            # dari kredensial pegawai dan aman digunakan sebagai fallback.
            profile = EmployeeProfile(name="", nip=self.nip, position=profile.position)
        return profile

    @staticmethod
    def _parse_kamus_html(html: str) -> list[KamusItem]:
        """Parse seluruh baris pada hasil pencarian e-Master tanpa batas delapan item."""
        soup = BeautifulSoup(html, "html.parser")
        found: list[KamusItem] = []
        for tr in soup.select("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.select("td")]
            if len(cells) < 6 or not cells[0].isdigit():
                continue
            code_activity = cells[1]
            match = re.match(r"\s*(\d+)\s*[-–]\s*(.*)", code_activity)
            code, activity = (match.group(1), match.group(2).strip()) if match else (cells[0], code_activity)
            try:
                wpt = int(re.sub(r"\D", "", cells[3]))
            except ValueError:
                continue
            if code and activity and cells[2].strip() and wpt > 0:
                found.append(KamusItem(code, activity, cells[2], wpt, cells[4], cells[5]))
        return found

    def search_kamus(self, keyword: str, limit: int | None = None) -> list[KamusItem]:
        """Cari kamus terbaru secara lengkap.

        Daftar PDF terverifikasi menjadi jaring pengaman agar hasil tidak hilang akibat
        pagination/limit pada popup e-Master. Data live tetap dipakai untuk memperbarui
        metadata kode yang sama. ``limit`` hanya dipertahankan untuk kompatibilitas dan
        tidak membatasi hasil secara default.
        """
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")

        local_items = load_local_catalog()
        url = urljoin(BASE_URL, "popup_skp/popup_aktifitas.php")
        live_items: list[KamusItem] = []
        try:
            response = self.http.get(url, params={"aktifitas": keyword}, timeout=20)
            response.raise_for_status()
            if "login area" in response.text.casefold():
                raise AuthenticationRequired("Sesi e-Master habis.")
            live_items = self._parse_kamus_html(response.text)
        except AuthenticationRequired:
            raise
        except requests.RequestException as exc:
            if not local_items:
                raise EMasterError("Kamus aktivitas e-Master tidak dapat dibuka.") from exc

        # PDF memastikan daftar lengkap; hasil live dengan kode sama memperbarui
        # satuan/WPT jika server e-Master telah berubah setelah PDF diterbitkan.
        merged: dict[str, KamusItem] = {item.code: item for item in local_items}
        for item in live_items:
            merged[item.code] = item

        found = filter_catalog(merged.values(), keyword)
        return found[:limit] if limit is not None else found

    def _work_target_detail_links(self, month: str) -> list[str]:
        try:
            page = self.http.get(urljoin(BASE_URL, "essmedia.php"),
                                 params={"module": "aktifitas_bulan", "bulan": month}, timeout=30)
            page.raise_for_status()
        except requests.RequestException as exc:
            raise EMasterError("Daftar tugas e-Master tidak dapat dibuka.") from exc
        soup = BeautifulSoup(page.text, "html.parser")
        links: list[str] = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            if "module=aktifitas_bulan" in href and "act=realisasi" in href and "id_breakdown=" in href:
                links.append(urljoin(page.url, href))
        for tag in soup.select("[onclick]"):
            onclick = tag.get("onclick", "")
            match = re.search(r"(?:href|location)(?:\.href)?\s*=\s*['\"]([^'\"]+act=realisasi[^'\"]+)['\"]", onclick)
            if match and "id_breakdown=" in match.group(1):
                links.append(urljoin(page.url, match.group(1)))
        # Pertahankan urutan halaman sambil membuang duplikat.
        unique = list(dict.fromkeys(links))
        return [link for link in unique if self._is_valid_detail_url(link)]

    @staticmethod
    def _is_valid_detail_url(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            port = parsed.port
        except ValueError:
            return False
        return (parsed.scheme == "https"
                and parsed.hostname == "master.bkd.jatimprov.go.id"
                and not parsed.username and not parsed.password and port in (None, 443)
                and parsed.path == "/essmedia.php"
                and query.get("module") == ["aktifitas_bulan"]
                and query.get("act") == ["realisasi"]
                and bool(re.fullmatch(r"[a-fA-F0-9]{32}", (query.get("id_breakdown") or [""])[0])))

    @staticmethod
    def _target_name_from_detail(soup: BeautifulSoup) -> str:
        form = next((f for f in soup.find_all("form") if f.select_one('[name="breakdown_id"]')), None)
        target_table = form.select_one("table") if form else None
        if target_table:
            for row in target_table.select("tbody tr"):
                cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
                if len(cells) >= 3 and cells[0].isdigit():
                    return cells[2]
        return ""

    @staticmethod
    def _parse_activity_detail(html: str, detail_url: str, target_name: str = "") -> list[EMasterActivity]:
        soup = BeautifulSoup(html, "html.parser")
        activities: list[EMasterActivity] = []
        for row in soup.select("tr"):
            delete_anchor = next((anchor for anchor in row.select("a[href]")
                                  if "act=delete" in anchor.get("href", "")
                                  and "id_realisasi=" in anchor.get("href", "")), None)
            if not delete_anchor:
                continue
            delete_url = urljoin(detail_url, delete_anchor.get("href", ""))
            query = parse_qs(urlsplit(delete_url).query)
            realization_id = (query.get("id_realisasi") or [""])[0]
            breakdown_id = (query.get("id_breakdown") or [""])[0]
            month = (query.get("bulan") or [""])[0]
            cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
            if len(cells) < 9 or not realization_id:
                continue
            try:
                wpt = int(re.sub(r"\D", "", cells[6]))
                volume = int(re.sub(r"\D", "", cells[7]))
                total = int(re.sub(r"\D", "", cells[8]))
            except ValueError:
                continue
            activities.append(EMasterActivity(
                id_realisasi=realization_id,
                breakdown_id=breakdown_id,
                month=month,
                date=cells[2],
                detail=cells[3],
                object_work=cells[4],
                unit=cells[5],
                wpt=wpt,
                volume=volume,
                total_minutes=total,
                target_name=target_name,
                delete_url=delete_url,
                detail_url=detail_url,
            ))
        return activities

    def list_activities(self, month: str, limit: int | None = None) -> list[EMasterActivity]:
        """Ambil riwayat live milik pegawai beserta URL hapus yang diterbitkan e-Master."""
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        activities: list[EMasterActivity] = []
        for detail_url in self._work_target_detail_links(month):
            try:
                response = self.http.get(detail_url, timeout=30)
                response.raise_for_status()
            except requests.RequestException as exc:
                raise EMasterError("Riwayat aktivitas e-Master tidak dapat dibuka.") from exc
            if not response.ok or "login area" in response.text.casefold():
                raise AuthenticationRequired("Sesi e-Master habis.")
            soup = BeautifulSoup(response.text, "html.parser")
            target_name = self._target_name_from_detail(soup)
            activities.extend(self._parse_activity_detail(response.text, detail_url, target_name))
        activities.sort(key=lambda item: int(item.id_realisasi) if item.id_realisasi.isdigit() else 0,
                        reverse=True)
        return activities[:max(1, min(limit, 200))] if limit is not None else activities

    @staticmethod
    def _validate_delete_url(activity: EMasterActivity) -> None:
        try:
            parsed = urlsplit(activity.delete_url)
            port = parsed.port
        except ValueError as exc:
            raise EMasterError("Tautan hapus e-Master tidak valid.") from exc
        query = parse_qs(parsed.query)
        detail_query = parse_qs(urlsplit(activity.detail_url).query)
        allowed_keys = {"module", "act", "bulan", "id_breakdown", "id_realisasi"}
        if (parsed.scheme != "https" or parsed.hostname != "master.bkd.jatimprov.go.id"
                or parsed.username or parsed.password or port not in (None, 443)
                or parsed.path != "/modul_essmankin/mod_aktifitas_bulan/aksi_aktifitas_bulan.php"
                or set(query) != allowed_keys
                or query.get("module") != ["aktifitas_bulan"]
                or query.get("act") != ["delete"]
                or query.get("bulan") != [activity.month]
                or query.get("id_breakdown") != [activity.breakdown_id]
                or query.get("id_realisasi") != [activity.id_realisasi]
                or not re.fullmatch(r"(?:0[1-9]|1[0-2])", activity.month)
                or not re.fullmatch(r"[a-fA-F0-9]{32}", activity.breakdown_id)
                or not re.fullmatch(r"\d+", activity.id_realisasi)
                or not EMasterClient._is_valid_detail_url(activity.detail_url)
                or detail_query.get("id_breakdown") != [activity.breakdown_id]
                or detail_query.get("bulan") != [activity.month]):
            raise EMasterError("Tautan hapus e-Master tidak valid.")

    def delete_activity(self, activity: EMasterActivity) -> None:
        """Hapus satu aktivitas yang dipilih dari halaman akun aktif, lalu verifikasi hasilnya."""
        self._validate_delete_url(activity)
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        try:
            response = self.http.get(activity.delete_url, timeout=30, allow_redirects=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise EMasterError("Permintaan hapus ke e-Master gagal.") from exc
        if not response.ok or "login area" in response.text.casefold():
            raise AuthenticationRequired("Sesi e-Master habis.")
        try:
            verify = self.http.get(activity.detail_url, timeout=30,
                                   headers={"Cache-Control": "no-cache"})
            verify.raise_for_status()
        except requests.RequestException as exc:
            raise EMasterError("Penghapusan tidak dapat diverifikasi.") from exc
        if "login area" in verify.text.casefold():
            raise AuthenticationRequired("Sesi e-Master habis.")
        if not verify.ok:
            raise EMasterError("Penghapusan tidak dapat diverifikasi.")
        if re.search(rf"[?&]id_realisasi={re.escape(activity.id_realisasi)}(?:[&'\"\s>]|$)", verify.text):
            raise EMasterError("e-Master belum menghapus aktivitas tersebut.")
        self._persist()

    @staticmethod
    def _edit_url(activity: EMasterActivity) -> str:
        return urljoin(BASE_URL, "essmedia.php") + (
            "?module=aktifitas_bulan&act=editaktifitas"
            f"&bulan={activity.month}&id_breakdown={activity.breakdown_id}"
            f"&id_realisasi={activity.id_realisasi}")

    @staticmethod
    def _validate_edit_action(url: str) -> None:
        try:
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            port = parsed.port
        except ValueError as exc:
            raise EMasterError("Form edit e-Master tidak valid.") from exc
        action = (query.get("act") or [""])[0].casefold()
        if (parsed.scheme != "https" or parsed.hostname != "master.bkd.jatimprov.go.id"
                or parsed.username or parsed.password or port not in (None, 443)
                or parsed.path != "/modul_essmankin/mod_aktifitas_bulan/aksi_aktifitas_bulan.php"
                or query.get("module") != ["aktifitas_bulan"]
                or not re.fullmatch(r"(?:update|edit|updateaktifitas|editaktifitas)", action)):
            raise EMasterError("Tujuan form edit e-Master berubah; pembaruan dibatalkan demi keamanan.")

    def update_activity(self, activity: EMasterActivity, *, date: str, volume: int,
                        object_work: str, item: KamusItem | None = None) -> EMasterActivity:
        """Perbarui aktivitas melalui form edit resmi dan verifikasi baris yang sama."""
        self._validate_delete_url(activity)
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        edit_url = self._edit_url(activity)
        try:
            page = self.http.get(edit_url, timeout=30)
            page.raise_for_status()
        except requests.RequestException as exc:
            raise EMasterError("Form edit aktivitas tidak dapat dibuka.") from exc
        if "login area" in page.text.casefold():
            raise AuthenticationRequired("Sesi e-Master habis.")
        soup = BeautifulSoup(page.text, "html.parser")
        form = next((candidate for candidate in soup.find_all("form")
                     if _find_field(candidate, ("tgl_kegiatan", "tanggal"))
                     and _find_field(candidate, ("volume",))
                     and _find_field(candidate, ("objek_kerja", "objek"))), None)
        if not form:
            raise EMasterError("Form edit aktivitas e-Master tidak ditemukan.")
        payload = self._form_payload(form)
        date_field = _find_field(form, ("tgl_kegiatan", "tanggal"))
        volume_field = _find_field(form, ("volume",))
        object_field = _find_field(form, ("objek_kerja", "objek"))
        if not date_field or not volume_field or not object_field:
            raise EMasterError("Field edit aktivitas e-Master berubah.")
        payload[date_field] = date
        payload[volume_field] = str(volume)
        payload[object_field] = object_work
        if item is not None:
            activity_field = _find_field(form, ("rk", "aktifitas", "aktivitas"))
            unit_field = _find_field(form, ("satuan",))
            wpt_field = _find_field(form, ("wpt",))
            if not activity_field or not unit_field or not wpt_field:
                raise EMasterError("Field kamus pada form edit e-Master berubah.")
            payload[activity_field] = f"{item.code}-{item.activity}"
            payload[unit_field] = item.unit
            payload[wpt_field] = str(item.wpt)
        realization_field = _find_field(form, ("id_realisasi", "realisasi_id"))
        if realization_field:
            payload[realization_field] = activity.id_realisasi
        action_url = urljoin(page.url, form.get("action") or "")
        self._validate_edit_action(action_url)
        multipart = {name: (None, str(value)) for name, value in payload.items()}
        try:
            response = self.http.post(action_url, files=multipart, timeout=30, allow_redirects=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise EMasterError("Perubahan aktivitas gagal dikirim ke e-Master.") from exc
        if "login area" in response.text.casefold():
            raise AuthenticationRequired("Sesi e-Master habis.")
        try:
            verify = self.http.get(activity.detail_url, timeout=30,
                                   headers={"Cache-Control": "no-cache"})
            verify.raise_for_status()
        except requests.RequestException as exc:
            raise EMasterError("Perubahan aktivitas tidak dapat diverifikasi.") from exc
        if "login area" in verify.text.casefold():
            raise AuthenticationRequired("Sesi e-Master habis.")
        updated = next((row for row in self._parse_activity_detail(
            verify.text, activity.detail_url, activity.target_name)
                        if row.id_realisasi == activity.id_realisasi), None)
        expected_wpt = item.wpt if item is not None else activity.wpt
        expected_detail = item.activity if item is not None else activity.detail
        normalized_date = date.replace("/", "-")
        if (not updated or updated.date.replace("/", "-") != normalized_date
                or updated.volume != volume or updated.wpt != expected_wpt
                or _normalize_search(updated.object_work) != _normalize_search(object_work)
                or _normalize_search(updated.detail) != _normalize_search(expected_detail)):
            raise EMasterError("e-Master belum mengonfirmasi seluruh perubahan aktivitas.")
        self._persist()
        return updated

    def list_work_targets(self, month: str) -> list[WorkTarget]:
        """Read every personal Kegiatan Tugas Jabatan and its hidden IDs."""
        if not self.is_authenticated():
            raise AuthenticationRequired("Sesi e-Master habis.")
        links = self._work_target_detail_links(month)
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
            target_name = self._target_name_from_detail(dsoup)
            if all(values.get(k) for k in ("breakdown_id", "target_id", "informasi_id")):
                targets.append(WorkTarget(target_name, values["breakdown_id"], values["target_id"],
                                          values["informasi_id"], add_link, detail_url))
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
