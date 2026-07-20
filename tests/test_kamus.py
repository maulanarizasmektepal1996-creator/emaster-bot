import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cryptography.fernet import Fernet

from emaster import (EMasterActivity, EMasterClient, EMasterError, KamusItem,
                     WorkTarget, filter_catalog, load_local_catalog)
from storage import Storage


class KamusCatalogTests(unittest.TestCase):
    def test_pdf_catalog_contains_72_valid_items(self):
        items = load_local_catalog()
        self.assertEqual(72, len(items))
        self.assertEqual(list(range(1, 73)), [int(item.code) for item in items])
        self.assertTrue(all(item.activity and item.unit and item.wpt > 0 for item in items))

    def test_surat_returns_all_10_expected_items(self):
        items = filter_catalog(load_local_catalog(), "  SURAT  ")
        self.assertEqual(["19", "20", "21", "27", "28", "40", "50", "51", "59", "65"],
                         [item.code for item in items])

    def test_html_parser_has_no_eight_item_limit(self):
        rows = "".join(
            f"<tr><td>{code}</td><td>{code}-Aktivitas surat {code}</td>"
            f"<td>Surat</td><td>15</td><td>Deskripsi</td><td>Objek</td></tr>"
            for code in range(1, 11)
        )
        items = EMasterClient._parse_kamus_html(f"<table>{rows}</table>")
        self.assertEqual(10, len(items))


class ActivityDeletionTests(unittest.TestCase):
    breakdown = "a" * 32
    detail_url = ("https://master.bkd.jatimprov.go.id/essmedia.php?"
                  f"module=aktifitas_bulan&act=realisasi&bulan=07&id_breakdown={breakdown}")
    delete_url = ("https://master.bkd.jatimprov.go.id/modul_essmankin/"
                  "mod_aktifitas_bulan/aksi_aktifitas_bulan.php?"
                  f"module=aktifitas_bulan&act=delete&bulan=07&id_breakdown={breakdown}"
                  "&id_realisasi=5863986")

    @classmethod
    def activity(cls, delete_url=None):
        return EMasterActivity(
            id_realisasi="5863986", breakdown_id=cls.breakdown, month="07",
            date="18-07-2026", detail="Membuat Konten Video",
            object_work="Video promosi Jawa Timur", unit="Kegiatan", wpt=120,
            volume=1, total_minutes=120, target_name="Mengolah konten media",
            delete_url=delete_url or cls.delete_url, detail_url=cls.detail_url)

    def test_parser_reads_realization_id_from_delete_link(self):
        html = f"""
        <table><tr>
          <td>1</td><td>Sabtu</td><td>18-07-2026</td><td>Membuat Konten Video</td>
          <td>Video promosi Jawa Timur</td><td>Kegiatan</td><td>120</td><td>1</td><td>120</td>
          <td>18-07-2026 10:31:29</td>
          <td><a title='Delete' href='{self.delete_url}'>hapus</a></td>
        </tr></table>
        """
        items = EMasterClient._parse_activity_detail(html, self.detail_url, "Mengolah konten media")
        self.assertEqual(1, len(items))
        self.assertEqual("5863986", items[0].id_realisasi)
        self.assertEqual(120, items[0].total_minutes)
        EMasterClient._validate_delete_url(items[0])

    def test_external_delete_url_is_rejected(self):
        item = self.activity("https://evil.example/delete?id_realisasi=5863986")
        with self.assertRaises(EMasterError):
            EMasterClient._validate_delete_url(item)

    def test_delete_is_verified_against_detail_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = EMasterClient("199001010000000001", "password",
                                   Fernet.generate_key().decode(), str(Path(tmp) / "session.bin"))
            deleted_response = Mock(ok=True, text="berhasil")
            deleted_response.raise_for_status.return_value = None
            verify_response = Mock(ok=True, text="<html>aktivitas lain</html>")
            verify_response.raise_for_status.return_value = None
            client.http.get = Mock(side_effect=[deleted_response, verify_response])
            with patch.object(client, "is_authenticated", return_value=True):
                client.delete_activity(self.activity())
        self.assertEqual(2, client.http.get.call_count)

    def test_successful_deletion_can_be_audited_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(str(Path(tmp) / "audit.db"))
            storage.add_deleted(10001, self.activity())
            row = storage.db.execute(
                "SELECT telegram_id,emaster_id,activity FROM deletion_audit").fetchone()
            columns = {item[1] for item in storage.db.execute("PRAGMA table_info(deletion_audit)")}
        self.assertEqual((10001, "5863986", "Membuat Konten Video"), row)
        self.assertNotIn("password", columns)
        self.assertNotIn("otp", columns)

    def test_local_catalog_fills_results_missing_from_live_popup(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = EMasterClient("199001010000000001", "password",
                                   Fernet.generate_key().decode(), str(Path(tmp) / "session.bin"))
            response = Mock()
            response.text = "<table></table>"
            response.raise_for_status.return_value = None
            client.http.get = Mock(return_value=response)
            with patch.object(client, "is_authenticated", return_value=True):
                items = client.search_kamus("surat")
        self.assertEqual(10, len(items))

    def test_edit_uses_official_form_and_verifies_same_realization(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = EMasterClient("199001010000000001", "password",
                                   Fernet.generate_key().decode(), str(Path(tmp) / "session.bin"))
            page = Mock(ok=True, text=f"""
              <form method='post' enctype='multipart/form-data'
                action='/modul_essmankin/mod_aktifitas_bulan/aksi_aktifitas_bulan.php?module=aktifitas_bulan&amp;act=update'>
                <input name='id_realisasi' value='5863986'>
                <input name='tgl_kegiatan' value='18/07/2026'>
                <input name='rk' value='24-Membuat Konten Video'>
                <input name='satuan' value='Kegiatan'>
                <input name='wpt' value='120'>
                <input name='volume' value='1'>
                <textarea name='objek_kerja'>Video promosi Jawa Timur</textarea>
              </form>""", url=EMasterClient._edit_url(self.activity()))
            page.raise_for_status.return_value = None
            posted = Mock(ok=True, text="berhasil")
            posted.raise_for_status.return_value = None
            verify = Mock(ok=True, text=f"""
              <table><tr>
                <td>1</td><td>Sabtu</td><td>19-07-2026</td><td>Membuat Konten Foto</td>
                <td>Foto promosi terbaru Jawa Timur</td><td>Kegiatan</td><td>60</td><td>2</td><td>120</td>
                <td>19-07-2026 10:31:29</td>
                <td><a title='Delete' href='{self.delete_url}'>hapus</a></td>
              </tr></table>""")
            verify.raise_for_status.return_value = None
            client.http.get = Mock(side_effect=[page, verify])
            client.http.post = Mock(return_value=posted)
            replacement = KamusItem("23", "Membuat Konten Foto", "Kegiatan", 60,
                                    "Dokumentasi", "File foto")
            with patch.object(client, "is_authenticated", return_value=True):
                updated = client.update_activity(
                    self.activity(), date="19/07/2026", volume=2,
                    object_work="Foto promosi terbaru Jawa Timur", item=replacement)
        self.assertEqual("5863986", updated.id_realisasi)
        self.assertEqual("Membuat Konten Foto", updated.detail)
        files = client.http.post.call_args.kwargs["files"]
        self.assertEqual((None, "5863986"), files["id_realisasi"])
        self.assertEqual((None, "23-Membuat Konten Foto"), files["rk"])

    def test_external_edit_action_is_rejected(self):
        with self.assertRaises(EMasterError):
            EMasterClient._validate_edit_action("https://evil.example/action?module=aktifitas_bulan&act=update")


class TelegramPaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
        os.environ.setdefault("TELEGRAM_USER_ID", "10001")
        os.environ.setdefault("EMASTER_NIP", "199001010000000001")
        os.environ.setdefault("EMASTER_PASSWORD", "test-password")
        os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
        os.environ["DATABASE_PATH"] = str(Path(cls.tmp.name) / "test.db")
        os.environ["SESSION_PATH"] = str(Path(cls.tmp.name) / "session.bin")
        from main import build_history_page, build_kamus_page
        cls.build_kamus_page = staticmethod(build_kamus_page)
        cls.build_history_page = staticmethod(build_history_page)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_ten_results_are_reachable_across_two_pages(self):
        items = filter_catalog(load_local_catalog(), "surat")
        first_text, first_markup = self.build_kamus_page(items, "surat", 0)
        second_text, second_markup = self.build_kamus_page(items, "surat", 1)

        first_picks = [button.callback_data for row in first_markup.inline_keyboard
                       for button in row if button.callback_data.startswith("pick:")]
        second_picks = [button.callback_data for row in second_markup.inline_keyboard
                        for button in row if button.callback_data.startswith("pick:")]
        self.assertEqual([f"pick:{i}" for i in range(8)], first_picks)
        self.assertEqual(["pick:8", "pick:9"], second_picks)
        self.assertIn("1–8 dari 10", first_text)
        self.assertIn("9–10 dari 10", second_text)

    def test_all_history_items_are_reachable_for_deletion(self):
        rows = [ActivityDeletionTests.activity() for _ in range(12)]
        first_text, first_markup = self.build_history_page(rows, 0)
        last_text, last_markup = self.build_history_page(rows, 2)
        first_deletes = [button.callback_data for row in first_markup.inline_keyboard
                         for button in row if button.callback_data.startswith("delete:pick:")]
        last_deletes = [button.callback_data for row in last_markup.inline_keyboard
                        for button in row if button.callback_data.startswith("delete:pick:")]
        self.assertEqual([f"delete:pick:{i}" for i in range(5)], first_deletes)
        self.assertEqual(["delete:pick:10", "delete:pick:11"], last_deletes)
        self.assertIn("1–5 dari 12", first_text)
        self.assertIn("11–12 dari 12", last_text)

    def test_each_history_item_has_edit_copy_and_delete_actions(self):
        rows = [ActivityDeletionTests.activity() for _ in range(2)]
        _, markup = self.build_history_page(rows, 0)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("edit:pick:0", callbacks)
        self.assertIn("copy:pick:0", callbacks)
        self.assertIn("delete:pick:0", callbacks)


class PersonalDataTests(unittest.TestCase):
    def test_drafts_and_favorites_are_isolated_per_employee(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(str(Path(tmp) / "personal.db"))
            item = KamusItem("22", "Membuat konten berita", "Naskah", 30,
                             "Deskripsi", "Naskah berita")
            storage.save_draft(1001, '{"stage":"OBJECT"}')
            storage.add_favorite(1001, item)
            self.assertIsNotNone(storage.get_draft(1001))
            self.assertIsNone(storage.get_draft(1002))
            self.assertEqual(1, len(storage.list_favorites(1001)))
            self.assertEqual([], storage.list_favorites(1002))


class AddFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
        os.environ.setdefault("TELEGRAM_USER_ID", "10001")
        os.environ.setdefault("EMASTER_NIP", "199001010000000001")
        os.environ.setdefault("EMASTER_PASSWORD", "test-password")
        os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
        os.environ["DATABASE_PATH"] = str(Path(cls.tmp.name) / "flow.db")
        os.environ["SESSION_PATH"] = str(Path(cls.tmp.name) / "flow-session.bin")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_confirmation_replies_to_the_supplied_message_and_checks_daily_wpt(self):
        import main
        item = KamusItem("22", "Membuat konten berita", "Naskah", 30,
                         "Deskripsi", "Naskah berita")
        target = WorkTarget("Mengolah konten", "1", "2", "3", "https://example.invalid")
        context = SimpleNamespace(user_data={
            "date": "20/07/2026", "item": item, "target": target,
            "volume": 1, "object": "Naskah berita pariwisata",
        })
        message = SimpleNamespace(reply_text=AsyncMock())
        with patch.object(main, "get_client", return_value=Mock()), \
             patch.object(main, "day_assessment", new=AsyncMock(return_value=(300, []))):
            state = asyncio.run(main.prepare_add_confirmation(message, context, 10001))
        self.assertEqual(main.CONFIRM, state)
        message.reply_text.assert_awaited_once()
        sent_text = message.reply_text.await_args.args[0]
        self.assertIn("300 → 330/660", sent_text)


if __name__ == "__main__":
    unittest.main()
