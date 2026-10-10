import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))
import server


class UrlSafetyTests(unittest.TestCase):
    def test_rejects_non_http(self):
        for bad in ["file:///etc/passwd", "ftp://x/y.pdf", "javascript:alert(1)", ""]:
            with self.assertRaises(ValueError, msg=bad):
                server.fetch_pdf_bytes(bad)

    def test_rejects_credentials(self):
        with self.assertRaises(ValueError):
            server.fetch_pdf_bytes("https://user:pass@example.com/a.pdf")

    def test_rejects_loopback_and_private_hosts(self):
        for host in ["localhost", "127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254"]:
            with self.assertRaises(ValueError, msg=host):
                server._host_is_public(host)


class TranslatePayloadTests(unittest.TestCase):
    def test_rejects_bad_target(self):
        with self.assertRaises(ValueError):
            server.validate_translate_payload({"target": "de", "texts": ["hi"]})

    def test_rejects_empty_and_missing_texts(self):
        with self.assertRaises(ValueError):
            server.validate_translate_payload({"target": "ru", "texts": []})
        with self.assertRaises(ValueError):
            server.validate_translate_payload({"target": "ru", "texts": ["  "]})
        with self.assertRaises(ValueError):
            server.validate_translate_payload({"target": "ru"})

    def test_enforces_caps(self):
        with self.assertRaises(ValueError):
            server.validate_translate_payload({"target": "fa", "texts": ["x"] * 101})
        with self.assertRaises(ValueError):
            server.validate_translate_payload({"target": "fa", "texts": ["x" * 4001]})
        with self.assertRaises(ValueError):
            server.validate_translate_payload({"target": "ru", "texts": ["x" * 4000] * 16})

    def test_accepts_valid_payload(self):
        target, texts = server.validate_translate_payload(
            {"target": "fa", "texts": ["Hello world", "Second line"]}
        )
        self.assertEqual((target, texts), ("fa", ["Hello world", "Second line"]))


class MappingTests(unittest.TestCase):
    def test_mock_preserves_order_and_length(self):
        texts = ["Surrender and hope", "Accountability matters", "Plain sentence here."]
        for target in ("ru", "fa"):
            out, name = server.translate_texts(texts, target)
            self.assertEqual(name, "mock")  # no keys in test env
            self.assertEqual(len(out), len(texts))
            joined = " ".join(out)
            if target == "ru":
                self.assertRegex(joined, "[\u0400-\u04FF]")
            else:
                self.assertRegex(joined, "[\u0600-\u06FF]")

    def test_mock_translates_known_words(self):
        out, _ = server.translate_texts(["Surrender"], "ru")
        self.assertIn("Капитуляция", out[0])
        out, _ = server.translate_texts(["Hope"], "fa")
        self.assertIn("امید", out[0])


if __name__ == "__main__":
    unittest.main()
