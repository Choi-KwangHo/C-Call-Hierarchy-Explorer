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
    fetch_latest_release,
    is_newer_version,
    parse_checksum_manifest,
    parse_release,
    parse_release_feed,
    verify_downloaded_asset,
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
    def test_release_feed_selects_highest_tag_and_checksum_for_exact_build(self) -> None:
        feed = b"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>older</title><updated>2026-07-22T00:00:00Z</updated>
            <link href="https://github.com/Choi-KwangHo/C-Call-Hierarchy-Explorer/releases/tag/v1.1.15"/>
            <content type="html">old</content></entry>
          <entry><title>latest</title><updated>2026-07-23T00:00:00Z</updated>
            <link href="https://github.com/Choi-KwangHo/C-Call-Hierarchy-Explorer/releases/tag/v1.1.16"/>
            <content type="html">&lt;b&gt;notes&lt;/b&gt;</content></entry>
        </feed>"""
        version, tag, title, metadata = parse_release_feed(feed)
        self.assertEqual((version, tag, title), ("1.1.16", "v1.1.16", "latest"))
        self.assertIn("notes", metadata)
        name = "C-Call-Hierarchy-Explorer-Setup-1.1.16.exe"
        digest = "a" * 64
        self.assertEqual(parse_checksum_manifest(f"{digest}  {name}\n", name), digest)

    def test_latest_release_requires_matching_tag_checksum_and_build(self) -> None:
        feed = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>C Call Hierarchy Explorer v1.1.16</title>
          <updated>2026-07-23T00:00:00Z</updated>
          <link href="https://github.com/Choi-KwangHo/C-Call-Hierarchy-Explorer/releases/tag/v1.1.16"/>
          <content type="html">notes</content>
        </entry></feed>"""
        name = "C-Call-Hierarchy-Explorer-Setup-1.1.16.exe"
        digest = "b" * 64

        class Response(io.BytesIO):
            def __init__(self, value: bytes, url: str = "https://github.com/", size: int = 0):
                super().__init__(value)
                self._url = url
                self.headers = {"Content-Length": str(size)}

            def geturl(self) -> str:
                return self._url

        responses = [
            Response(feed),
            Response(f"{digest}  {name}\n".encode()),
            Response(b"", "https://objects.githubusercontent.com/setup.exe", 12345),
        ]
        with patch("urllib.request.urlopen", side_effect=responses):
            release = fetch_latest_release()
        self.assertEqual(release.tag, "v1.1.16")
        self.assertEqual(release.setup.name, name)
        self.assertEqual(release.setup.size, 12345)
        self.assertEqual(release.setup.sha256, digest)

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

    def test_existing_installer_is_reverified_before_retry(self) -> None:
        content = b"previously verified installer"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "setup.exe"
            output.write_bytes(content)
            self.assertEqual(verify_downloaded_asset(output, len(content), digest), output)
            output.write_bytes(content + b"tampered")
            with self.assertRaises(UpdateError):
                verify_downloaded_asset(output, len(content), digest)


if __name__ == "__main__":
    unittest.main()
