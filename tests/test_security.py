import stat
import tempfile
import unittest
from pathlib import Path

from config import Config
from metadata_cli.wordpress import WordPress


class CredentialStorageTests(unittest.TestCase):
    def test_spotify_token_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config.__new__(Config)
            config.token_file = Path(directory) / ".spotify_tokens"

            config.save_tokens("access", "refresh")

            self.assertEqual(stat.S_IMODE(config.token_file.stat().st_mode), 0o600)
            self.assertEqual(
                config.token_file.read_text(),
                '{"access_token": "access", "refresh_token": "refresh"}',
            )


class WordPressUrlValidationTests(unittest.TestCase):
    def test_rejects_non_http_wordpress_url(self):
        with self.assertRaisesRegex(ValueError, "http"):
            WordPress("file:///etc", "user", "password")

    def test_accepts_https_wordpress_url(self):
        wordpress = WordPress("https://example.test", "user", "password")
        self.assertEqual(wordpress.api, "https://example.test/wp-json/wp/v2")


if __name__ == "__main__":
    unittest.main()
