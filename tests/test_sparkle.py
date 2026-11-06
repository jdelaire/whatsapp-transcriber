import base64
import tempfile
import unittest
from pathlib import Path

from scripts.sparkle import validate_version, validate_appcast


class SparkleReleaseTests(unittest.TestCase):
    def test_invalid_versions_cannot_be_used_as_release_paths(self):
        for version, build in [("../1.1.0", "2"), ("1.1", "2"), ("1.1.0", "0"), ("1.1.0", "2/3")]:
            with self.subTest(version=version, build=build), self.assertRaises(ValueError):
                validate_version(version, build)
        validate_version("1.1.0", "2")

    def test_feed_must_describe_exact_final_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "Transcriber-1.1.0-arm64.zip"
            archive.write_bytes(b"final signed archive")
            url = "https://github.com/jdelaire/whatsapp-transcriber/releases/download/v1.1.0/Transcriber-1.1.0-arm64.zip"
            signature = base64.b64encode(bytes(64)).decode()
            xml = f'<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel><item><sparkle:version>2</sparkle:version><sparkle:shortVersionString>1.1.0</sparkle:shortVersionString><sparkle:minimumSystemVersion>15.0</sparkle:minimumSystemVersion><enclosure url="{url}" length="20" sparkle:edSignature="{signature}" /></item></channel></rss>'
            feed = Path(folder) / "appcast.xml"
            feed.write_text(xml)
            self.assertEqual(validate_appcast(feed, archive, "1.1.0", "2", url), signature)
            for bad in [xml.replace('length="20"', 'length="21"'), xml.replace("<sparkle:version>2", "<sparkle:version>1"), xml.replace("v1.1.0/", "latest/"), xml.replace(signature, "invalid"), xml.replace("15.0", "27.0"), xml.replace("1.1.0</", "1.0.0</")]:
                with self.subTest(xml=bad), self.assertRaises(ValueError):
                    feed.write_text(bad)
                    validate_appcast(feed, archive, "1.1.0", "2", url)

    def test_missing_enclosure_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            feed = Path(folder) / "appcast.xml"
            feed.write_text("<rss><channel><item /></channel></rss>")
            with self.assertRaises(ValueError):
                validate_appcast(feed, feed, "1.1.0", "2", "https://example.com/update.zip")
