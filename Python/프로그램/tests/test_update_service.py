from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from update_service import (
    ReleaseAsset,
    UpdateError,
    download_asset,
    is_newer_version,
    parse_release,
)


def release_payload(content: bytes = b"setup-data") -> dict:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "tag_name": "v1.1.8",
        "name": "C Call Hierarchy Explorer 1.1.8",
        "body": "notes",
        "html_url": "https://github.com/Choi-KwangHo/C-Call-Hierarchy-Explorer/releases/tag/v1.1.8",
        "published_at": "2026-07-23T00:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "C-Call-Hierarchy-Explorer-Setup-1.1.8.exe",
                "browser_download_url": "https://github.com/Choi-KwangHo/C-Call-Hierarchy-Explorer/releases/download/v1.1.8/setup.exe",
                "size": len(content),
                "digest": f"sha256:{digest}",
            }
        ],
    }


class UpdateServiceTests(unittest.TestCase):
    def test_semantic_version_comparison(self) -> None:
        self.assertTrue(is_newer_version("1.10.0", "1.9.9"))
        self.assertTrue(is_newer_version("v1.1.9", "1.1.8"))
        self.assertFalse(is_newer_version("1.1.8", "1.1.8"))
        self.assertFalse(is_newer_version("1.1", "1.1.0"))

    def test_parse_release_selects_signed_setup_asset(self) -> None:
        release = parse_release(release_payload())
        self.assertEqual(release.version, "1.1.8")
        self.assertEqual(release.setup.name, "C-Call-Hierarchy-Explorer-Setup-1.1.8.exe")
        self.assertEqual(len(release.setup.sha256), 64)

    def test_parse_release_rejects_missing_digest_and_untrusted_url(self) -> None:
        payload = release_payload()
        payload["assets"][0]["digest"] = None
        with self.assertRaises(UpdateError):
            parse_release(payload)
        payload = release_payload()
        payload["assets"][0]["browser_download_url"] = "https://example.invalid/setup.exe"
        with self.assertRaises(UpdateError):
            parse_release(payload)

    def test_download_verifies_size_and_sha256(self) -> None:
        content = b"verified installer bytes"
        asset = ReleaseAsset(
            name="C-Call-Hierarchy-Explorer-Setup-1.1.8.exe",
            url="https://github.com/example/setup.exe",
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        progress: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as temporary, patch(
            "urllib.request.urlopen", return_value=io.BytesIO(content)
        ):
            output = download_asset(asset, Path(temporary) / asset.name, lambda current, total: progress.append((current, total)))
            self.assertEqual(output.read_bytes(), content)
            self.assertEqual(progress[-1], (len(content), len(content)))

    def test_download_removes_partial_on_hash_failure(self) -> None:
        content = b"changed"
        asset = ReleaseAsset(
            name="C-Call-Hierarchy-Explorer-Setup-1.1.8.exe",
            url="https://github.com/example/setup.exe",
            size=len(content),
            sha256="0" * 64,
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "urllib.request.urlopen", return_value=io.BytesIO(content)
        ):
            output = Path(temporary) / asset.name
            with self.assertRaises(UpdateError):
                download_asset(asset, output)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(output.suffix + ".part").exists())


if __name__ == "__main__":
    unittest.main()
