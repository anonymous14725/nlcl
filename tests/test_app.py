"""Application module: one description -> desktop app (Tkinter) + installable web app (PWA)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nlcl.runtime import Session  # noqa: E402

LINES = [
    'create an app called tally with title "Tally Counter" and size 360 by 320',
    "add a label called display to tally with value 0",
    'add a button called plus to tally with text "+1"',
    'add a button called reset to tally with text "Reset"',
    'add an input called step to tally with value 1 and label "Step"',
    "add a progress called bar to tally with max 20",
    "when plus is pressed, increase value of display by value of step",
    "when reset is pressed, set value of display to 0",
    "when value of display changes, set value of bar to value of display",
    "when value of display is above 19, print 'limit reached: {value}'",
]
DRIVER = """
import app as g
from nlcl_app import App
a = App(g.SPEC, g.HANDLERS, g.CALLS, headless=True); a.start(net=False)
for _ in range(3): a.click("plus")
a.set("step", "value", 5); a.click("plus")
print("RESULT", a.get("display", "value"), a.get("bar", "value"))
a.set("display", "value", 25); a.step(); print(a.logs[-1])
a.click("reset"); print("RESET", a.get("display", "value"), a.get("bar", "value"))
"""


def build():
    tmp = tempfile.mkdtemp(prefix="nlcl_app_")
    s = Session("tally", backend="off", vocab_path=os.path.join(tmp, "v.json"), outdir=os.path.join(tmp, "b"))
    out = []
    for ln in LINES:
        out += s.handle(ln)
    return s, "\n".join(out)


class AppModule(unittest.TestCase):
    def test_ir(self):
        s, out = build()
        self.assertNotIn("What kind", out)
        self.assertEqual({e.domain for e in s.state.entities.values()}, {"app"})
        step = s.state.resolve("step")
        self.assertEqual((step.props["value"], step.props["label"]), (1, "Step"))
        d = s.state.resolve("display")
        self.assertEqual(d.behaviors[0]["do"][0]["value"], "step.value")
        self.assertEqual(s.state.resolve("bar").behaviors[1]["if"], "display.value > 19")

    def test_files_for_every_os(self):
        s, _ = build()
        s.build()
        d = s.build_dir / "app"
        for f in ("app.py", "nlcl_app.py", "build_windows.bat", "build_macos.sh", "build_linux.sh",
                  "web/index.html", "web/app.js", "web/manifest.webmanifest", "web/sw.js", "web/icon.svg"):
            self.assertTrue((d / f).exists(), f)

    def test_desktop_logic(self):
        s, _ = build()
        s.build()
        d = s.build_dir / "app"
        r = subprocess.run([sys.executable, "-c", DRIVER], cwd=d, capture_output=True, text=True, timeout=60)
        self.assertIn("RESULT 8 8", r.stdout, r.stderr)
        self.assertIn("limit reached: 25", r.stdout)
        self.assertIn("RESET 0 0", r.stdout)

    def test_selftests_run(self):
        s, _ = build()
        out = "\n".join(s.run("app"))
        self.assertIn("desktop (python app.py --selftest): OK", out)
        self.assertNotIn("FAILED", out)

    def test_check_app_code(self):
        s, _ = build()
        self.assertIn("no problems", "\n".join(s.handle("check the app")))

    def test_app_talks_to_other_modules(self):
        s, _ = build()
        for ln in ("add an esp32 called board", "add a temperature sensor called temp to board with temperature 20",
                   "add a label called reading to tally with temperature 0",
                   "connect temp to reading via http sending temperature every 2 seconds"):
            s.handle(ln)
        s.build()
        src = (s.build_dir / "app" / "app.py").read_text(encoding="utf-8")
        self.assertIn('"dir": "in"', src)
        self.assertIn("/channel/temp_to_reading", src)
        self.assertTrue((s.build_dir / "python" / "main.py").exists())      # the hub is generated automatically


if __name__ == "__main__":
    unittest.main()
