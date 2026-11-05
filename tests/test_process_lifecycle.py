import contextlib
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from transcriber import stop_process


class ProcessLifecycleTests(unittest.TestCase):
    def test_zombie_only_group_does_not_abort_shutdown_before_reaping(self):
        process = Mock(pid=2**30)
        process.poll.return_value = 0
        with patch("transcriber.os.killpg", side_effect=PermissionError):
            stop_process(process)
        process.wait.assert_called_once()

    def test_stubborn_descendant_is_stopped_after_group_leader_exits(self):
        code = """import os,signal,time
pid=os.fork()
if pid:
    os._exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(os.getpid(),flush=True)
os.close(1)
while True:
    time.sleep(1)
"""
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, text=True)
        try:
            child = int(process.stdout.readline())
            process.wait(timeout=3)
            stop_process(process)
            state = subprocess.run(["/bin/ps", "-p", str(child), "-o", "stat="], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), state)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            process.stdout.close()
