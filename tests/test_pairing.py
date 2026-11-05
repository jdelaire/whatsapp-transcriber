import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import manage


class PairingTests(unittest.TestCase):
    def fixture(self, root, events):
        config = root / "config.json"
        config.write_text(json.dumps({"wacli": "wacli", "store": str(root / "store")}))
        (root / "NEEDS_PAIRING").write_text("unlinked")
        process = SimpleNamespace(pid=2**30, stderr=io.StringIO(events), wait=lambda: 0, poll=lambda: 0)
        return config, process

    def test_logged_out_zero_exit_preserves_pairing_requirement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root, '{"event":"logged_out"}\n')
            started = []
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process), \
                 patch.object(manage, "start", side_effect=lambda: started.append(True)), \
                 patch.object(manage, "prepare_pair", create=True), patch.object(manage, "authenticated", return_value=True, create=True):
                try:
                    manage.pair()
                except SystemExit:
                    pass
            self.assertTrue((root / "NEEDS_PAIRING").exists())
            self.assertEqual(started, [])

    def test_unverified_authentication_never_starts_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root, "")
            started = []
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process), \
                 patch.object(manage, "start", side_effect=lambda: started.append(True)), \
                 patch.object(manage, "prepare_pair", create=True), patch.object(manage, "authenticated", return_value=False, create=True):
                try:
                    manage.pair()
                except SystemExit:
                    pass
            self.assertTrue((root / "NEEDS_PAIRING").exists())
            self.assertEqual(started, [])

    def test_revoked_session_cleared_before_fresh_pairing(self):
        commands = []
        def command(arguments, timeout):
            commands.append(arguments)
            if arguments[-2:] == ["auth", "status"]:
                return '{"success":true,"data":{"authenticated":false}}'
            return '{"success":true,"data":{"logged_out":true}}'
        manage.prepare_pair({"wacli": "wacli", "store": "/tmp/test"}, command=command)
        self.assertTrue(any(arguments[-2:] == ["auth", "logout"] for arguments in commands))

    def test_pairing_error_reports_upstream_event_message(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root,
                '{"event":"error","data":{"message":"QR code timed out; run `wacli auth` again to get a new code"}}\n')
            output = io.StringIO()
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process), \
                 patch.object(manage, "prepare_pair"), redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    manage.pair()
            self.assertIn("QR code timed out", output.getvalue())
            self.assertNotIn("unknown error", output.getvalue())
            self.assertTrue((root / "NEEDS_PAIRING").exists())

    def test_rotating_qr_is_displayed_and_removed_after_failure(self):
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root,
                '{"event":"qr_code","data":{"code":"test-qr-first"}}\n'
                '{"event":"qr_code","data":{"code":"test-qr-second"}}\n'
                '{"event":"logged_out"}\n')
            config.write_text(json.dumps({"wacli": "wacli", "store": str(root / "store"), "qrencode": shutil.which("qrencode")}))
            output = io.StringIO()
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", side_effect=lambda *args, **kwargs: process if args[0][0] == "wacli" else original_popen(*args, **kwargs)), \
                 patch.object(manage, "prepare_pair"), redirect_stdout(output):
                try:
                    manage.pair()
                except SystemExit:
                    pass
            self.assertEqual(output.getvalue().count("QR ready:"), 2)
            self.assertIn("\033[", output.getvalue())
            self.assertFalse((root / "pairing.png").exists())

    def test_phone_pairing_shows_code_without_disclosing_number(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root,
                '{"event":"pair_code","data":{"phone":"15551234567","code":"ABCD-EFGH"}}\n')
            output = io.StringIO()
            started = []
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process) as launch, \
                 patch.object(manage, "prepare_pair"), patch.object(manage, "authenticated", return_value=True), \
                 patch.object(manage, "start", side_effect=lambda: started.append(True)), redirect_stdout(output):
                manage.pair(phone="+15551234567")
            arguments = launch.call_args.args[0]
            self.assertEqual(arguments[arguments.index("--phone") + 1], "+15551234567")
            self.assertIn("ABCD-EFGH", output.getvalue())
            self.assertNotIn("15551234567", output.getvalue())
            self.assertEqual(started, [True])

    def test_pairing_trace_never_records_qr_payload_phone_or_linking_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(manage, "DATA", root):
                manage.record_pair_event({"event": "qr_code", "data": {"code": "secret-qr-payload"}})
                manage.record_pair_event({"event": "pair_code", "data": {"phone": "15551234567", "code": "ABCD-EFGH"}})
                manage.record_pair_event({"event": "error", "data": {"message": "QR code timed out for 15551234567"}})
            trace = (root / "pairing-events.jsonl").read_text()
            self.assertIn("qr_code", trace)
            self.assertIn("timeout", trace)
            for secret in ("secret-qr-payload", "15551234567", "ABCD-EFGH"):
                self.assertNotIn(secret, trace)

    def test_invalid_phone_preserves_running_service_and_pairing_state(self):
        for number in ("", "   ", "missing-country-code", "12345678"):
            with self.subTest(number=number), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config, process = self.fixture(root, "")
                with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                     patch.object(manage, "stop") as stop, patch.object(manage, "prepare_pair") as prepare, \
                     patch.object(manage.subprocess, "Popen", return_value=process) as launch, \
                     patch.object(manage, "authenticated", return_value=False):
                    with self.assertRaises(SystemExit):
                        manage.pair(phone=number)
                stop.assert_not_called()
                prepare.assert_not_called()
                launch.assert_not_called()
                self.assertEqual((root / "NEEDS_PAIRING").read_text(), "unlinked")

    def test_authenticated_connection_hands_off_without_waiting_for_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root, '{"event":"connected"}\n')
            process.returncode = None
            def wait():
                if process.returncode is None:
                    self.fail("Authenticated pairing waited for history to become idle")
                return process.returncode
            process.wait = wait
            process.poll = lambda: process.returncode
            events = []
            def settle(child):
                child.returncode = 0
                events.append("settled")
            def start():
                self.assertEqual(process.returncode, 0)
                events.append("started")
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process), \
                 patch.object(manage, "prepare_pair"), patch.object(manage, "authenticated", return_value=True), \
                 patch.object(manage, "stop_process", side_effect=settle), patch.object(manage, "start", side_effect=start), \
                 redirect_stdout(io.StringIO()):
                manage.pair()
            self.assertEqual(events[:2], ["settled", "started"])
            self.assertFalse((root / "NEEDS_PAIRING").exists())

    def test_buffered_logout_after_handoff_never_starts_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root, '{"event":"connected"}\n{"event":"logged_out"}\n')
            process.returncode = 0
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process), \
                 patch.object(manage, "prepare_pair"), patch.object(manage, "authenticated", return_value=True), \
                 patch.object(manage, "stop_process"), patch.object(manage, "start") as start, redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    manage.pair()
            start.assert_not_called()
            self.assertTrue((root / "NEEDS_PAIRING").exists())

    def test_revoked_status_after_bootstrap_stops_handoff(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root, '{"event":"connected"}\n')
            process.returncode = 0
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process), \
                 patch.object(manage, "prepare_pair"), patch.object(manage, "authenticated", side_effect=[True, False]), \
                 patch.object(manage, "stop_process"), patch.object(manage, "start") as start, redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    manage.pair()
            start.assert_not_called()
            self.assertTrue((root / "NEEDS_PAIRING").exists())

    def test_intentional_bootstrap_cancellation_allows_verified_handoff(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root,
                '{"event":"connected"}\n{"event":"error","data":{"message":"context canceled"}}\n')
            process.returncode = 1
            process.wait = lambda: 1
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process), \
                 patch.object(manage, "prepare_pair"), patch.object(manage, "authenticated", return_value=True), \
                 patch.object(manage, "stop_process"), patch.object(manage, "start") as start, redirect_stdout(io.StringIO()):
                manage.pair()
            start.assert_called_once()
            self.assertFalse((root / "NEEDS_PAIRING").exists())

    def test_forced_bootstrap_stop_never_clears_pairing_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, process = self.fixture(root, '{"event":"connected"}\n')
            process.returncode = -9
            process.wait = lambda: -9
            with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                 patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process), \
                 patch.object(manage, "prepare_pair"), patch.object(manage, "authenticated", return_value=True), \
                 patch.object(manage, "stop_process"), patch.object(manage, "start") as start, redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    manage.pair()
            start.assert_not_called()
            self.assertTrue((root / "NEEDS_PAIRING").exists())

    def test_unexpected_bootstrap_exits_do_not_start_service(self):
        for exit_code in (1, 2, -11):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config, process = self.fixture(root, '{"event":"connected"}\n')
                process.returncode = exit_code
                process.wait = lambda: exit_code
                with patch.object(manage, "DATA", root), patch.object(manage, "CONFIG", config), \
                     patch.object(manage, "stop"), patch.object(manage.subprocess, "Popen", return_value=process), \
                     patch.object(manage, "prepare_pair"), patch.object(manage, "authenticated", return_value=True), \
                     patch.object(manage, "stop_process"), patch.object(manage, "start") as start, redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        manage.pair()
                start.assert_not_called()
                self.assertTrue((root / "NEEDS_PAIRING").exists())


original_popen = manage.subprocess.Popen
