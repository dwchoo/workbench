"""Small installed-OMP, local-provider preflight for the fast developer runner."""

from __future__ import annotations

import os
from pathlib import Path
import time
import unittest
from uuid import uuid4

from tests.integration import cw16_harness as h
from tests.integration import cw16_flow_rig as flow


@unittest.skipUnless(os.environ.get("WB_DVL_REAL_SMOKE") == "1", "explicit installed-OMP smoke only")
class InstalledFastSmoke(unittest.TestCase):
    def test_plain_bash_bridge_and_host_command(self):
        namespace = os.environ.get("WB_DVL_NAMESPACE")
        provider = h.ScriptedProvider()
        sandbox = None
        cleanup = None
        try:
            sandbox = h.Sandbox("dvl01", host_shell="bash", omp=os.environ.get("WB_DVL_OMP"),
                                root_parent=Path(namespace) if namespace else None, provider=provider)
            started = sandbox.start(timeout=120)
            self.assertEqual(started.returncode, 0, started.stderr[-500:])
            ready = sandbox.wait_ready(timeout=30)
            self.assertEqual(ready["phase"], "ready")
            self.assertEqual((ready.get("omp_isolation") or {}).get("state"), "ok")
            self.assertTrue((ready.get("bridge") or {}).get("manager"))
            self.assertTrue((ready.get("bridge") or {}).get("worker"))
            ui = sandbox.ui(label="dvl01-plain-bash")
            self.assertTrue(ui.wait(lambda: all(title in ui.text() for title in h.TITLES.values()), 30))
            ui.send(h.PREFIX + h.FOCUS_KEYS["host_shell"], settle=0)
            sandbox.wait_status(lambda s: s["focus"] == "host_shell", 15, "host-shell focus", pump=[ui])
            marker = f"DVL01_{uuid4().hex}"
            output = sandbox.project / "smoke.txt"
            ui.send(f"printf '%s\\n' {marker} > {output}\r".encode(), settle=0)
            self.assertEqual(h.wait_file(output, contains=marker, timeout=15, pump=[ui]), marker + "\n")
            hangul = "한글 字 🙂"
            unicode_out = sandbox.project / "unicode.txt"
            ui.send(f"printf '%s\\n' '{hangul}' > {unicode_out}\r".encode(), settle=0)
            self.assertEqual(h.wait_file(unicode_out, contains="한글", timeout=15, pump=[ui]), hangul + "\n")
            paste_out = sandbox.project / "paste.txt"
            ui.paste(f"cat > {paste_out} <<'EOF'\n한글 첫줄\nsecond 🙂\nEOF\n".encode(), settle=0)
            if not paste_out.exists():
                ui.send(b"\r", settle=0)
            self.assertEqual(h.wait_file(paste_out, contains="second", timeout=15, pump=[ui]),
                             "한글 첫줄\nsecond 🙂\n")
            for pane in ("manager_omp", "worker_omp"):
                ui.send(h.PREFIX + h.FOCUS_KEYS[pane], settle=0)
                sandbox.wait_status(lambda s, name=pane: s["focus"] == name, 15, pane, pump=[ui])
                composer = f"한글{pane[:1]}{marker[-6:]}"
                ui.type(composer, gap=0)
                self.assertTrue(ui.wait(lambda value=composer: value in ui.text(), 10))
                ui.send(b"\x15", settle=0)
            ui.resize(34, 150)
            expected = h.pane_sizes_for(34, 150)
            self.assertTrue(ui.wait(lambda: all(h.pty_size_of(sandbox.known[name][0]) == expected[name]
                                               for name in ("manager_omp", "worker_omp")), 10))
            ui.send(h.PREFIX + h.FOCUS_KEYS["host_shell"], settle=0)
            sandbox.wait_status(lambda s: s["focus"] == "host_shell", 15, "host-shell focus", pump=[ui])
            size_out = sandbox.project / "size.txt"
            ui.send(f"stty size > {size_out}\r".encode(), settle=0)
            self.assertEqual(h.wait_file(size_out, timeout=15, pump=[ui]).strip(),
                             "%d %d" % expected["host_shell"])
            ui.send(h.PREFIX + b"p", settle=0)
            self.assertTrue(ui.wait_text(r"자동화를 일시정지합니다", 10))
            ui.send(b"p", settle=0)
            sandbox.wait_status(lambda s: s["automation"]["state"] == "paused", 30, "paused", pump=[ui])
            call = sandbox.provider.call("manager", "to_worker", flow.to_worker_args(
                kind="work", message="DVL-01 paused request", spec=flow.spec("paused", ["notes/"])))
            sandbox.provider.on_text("dvl01-paused-call", h.tools(call), role="manager", once=True)
            ui.send(h.PREFIX + h.FOCUS_KEYS["manager_omp"], settle=0)
            sandbox.wait_status(lambda s: s["focus"] == "manager_omp", 15, "manager focus", pump=[ui])
            ui.type("dvl01-paused-call", gap=0)
            ui.send(b"\r", settle=0)
            deadline = time.monotonic() + 30
            while call[2] not in sandbox.provider.tool_results and time.monotonic() < deadline:
                ui.pump(0.1)
            self.assertEqual(sandbox.provider.tool_results.get(call[2]),
                             {"status": "held", "reason": "paused"})
            done = sandbox.shutdown(timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr[-500:])
            self.assertEqual(h.shutdown_result(done.stdout).get("verified"), True)
            self.assertEqual(sandbox.wait_no_residue(20), {})
        finally:
            if sandbox is not None:
                cleanup = sandbox.close()
            provider.close()
        self.assertIsNotNone(cleanup)
        self.assertFalse(cleanup.get("residue_before_fallback"), cleanup)
        self.assertFalse(cleanup.get("residue_after_fallback"), cleanup)
        self.assertTrue(cleanup.get("root_removed"), cleanup)
