import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from emaster import EMasterClient, filter_catalog, load_local_catalog


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
        from main import build_kamus_page
        cls.build_kamus_page = staticmethod(build_kamus_page)

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


if __name__ == "__main__":
    unittest.main()
