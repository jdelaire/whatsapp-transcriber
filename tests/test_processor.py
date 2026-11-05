import tempfile
import unittest
from pathlib import Path

from transcriber import Processor, output_path
from test_transcriber import audio


class ProcessorTests(unittest.TestCase):
    def test_restart_after_publish_does_not_download_or_transcribe_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            message = audio()
            path = output_path(root / "out", message)
            path.parent.mkdir(parents=True)
            path.write_text("Bonjour\n")
            def unexpected(command, timeout):
                self.fail("Published transcript was processed again")
            processor = Processor({"output": str(root / "out"), "data": folder}, command=unexpected)
            result = processor.process({"key": "abc", "message": message})
            self.assertEqual(result, path)
            self.assertEqual(path.read_text(), "Bonjour\n")

    def test_failed_transcription_never_publishes_partial_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "input.ogg"
            source.touch()
            def command(arguments, timeout):
                if arguments[0] == "probe":
                    return "4.0"
                if arguments[0] == "ffmpeg":
                    Path(arguments[-1]).write_bytes(b"wav")
                    return ""
                base = Path(arguments[arguments.index("-of") + 1])
                base.with_suffix(".txt").write_text("partial")
                raise RuntimeError("GPU interrupted")
            config = {"data": folder, "ffprobe": "probe", "ffmpeg": "ffmpeg", "whisper": "whisper", "model": "model"}
            dest = root / "published.txt"
            with self.assertRaises(RuntimeError):
                Processor(config, command=command).transcribe(source, dest)
            self.assertFalse(dest.exists())

    def test_oversized_decoded_audio_is_rejected_before_conversion(self):
        with tempfile.TemporaryDirectory() as folder:
            def command(arguments, timeout):
                self.assertEqual(arguments[0], "probe")
                return "7200"
            config = {"data": folder, "ffprobe": "probe"}
            with self.assertRaisesRegex(ValueError, "60 minutes"):
                Processor(config, command=command).transcribe(Path(folder) / "source", Path(folder) / "dest.txt")
