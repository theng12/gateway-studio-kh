"""The shipped console scripts must run from the bundled interpreter, not the build path.

Measured on the installed 0.2.281 app: seven entry points under Contents/Resources/python/bin
carried `#!/Users/<builder>/...` — the build directory's absolute python — so the app could not
be mirrored. `menubar/tools/strip_python_headers.py` rewrites them onto the script-relative
polyglot header the runtime already uses for pip's own scripts. These tests pin the rewrite and
prove a rewritten script actually runs.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "menubar" / "tools" / "strip_python_headers.py"

_spec = importlib.util.spec_from_file_location("strip_python_headers", TOOL)
assert _spec and _spec.loader
strip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(strip)

ABS = b"#!/Users/someone/Developer/build/vendor/python/bin/python3\nimport sys\nprint('hi')\n"


class TheHeaderStripTest(unittest.TestCase):
    def test_an_absolute_shebang_keeps_the_body_and_uses_the_beside_interpreter(self):
        body = strip.strip_absolute_python_header(ABS)
        self.assertEqual(body, b"import sys\nprint('hi')\n")
        self.assertEqual(strip.HEADER + body, strip.HEADER + b"import sys\nprint('hi')\n")
        self.assertNotIn(b"/Users/", strip.HEADER)
        self.assertIn(b'"$(dirname "$0")/python3"', strip.HEADER)

    def test_uvs_three_line_polyglot_is_stripped(self):
        contents = (
            b'#!/bin/sh\n'
            b'\'\'\'exec\' "/Users/someone/build/python/bin/python3.11" "$0" "$@"\n'
            b"' '''\n"
            b"import sys\n"
        )
        self.assertEqual(strip.strip_absolute_python_header(contents), b"import sys\n")

    def test_the_relative_polyglot_pip_already_writes_is_left_alone(self):
        contents = (
            b'#!/bin/sh\n'
            b'\'\'\'exec\' "$(dirname -- "$(realpath -- "$0")")/python3.11" "$0" "$@"\n'
            b"' '''\n"
            b"import sys\n"
        )
        self.assertIsNone(strip.strip_absolute_python_header(contents))

    def test_binaries_and_plain_scripts_are_left_alone(self):
        self.assertIsNone(strip.strip_absolute_python_header(b"\xcf\xfa\xed\xfe not a script"))
        self.assertIsNone(strip.strip_absolute_python_header(b"#!/bin/sh\necho hi\n"))


class ABuildDirectoryTest(unittest.TestCase):
    def test_every_console_script_is_rewritten_and_a_rewritten_script_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            # The interpreter beside the scripts, as in the bundle.
            (bin_dir / "python3").symlink_to(sys.executable)
            (bin_dir / "dotenv").write_bytes(ABS)
            (bin_dir / "fastapi").write_bytes(
                ABS.replace(b"print('hi')", b"raise SystemExit(7)")
            )
            (bin_dir / "pip").write_bytes(
                b'#!/bin/sh\n'
                b'\'\'\'exec\' "$(dirname -- "$(realpath -- "$0")")/python3.11" "$0" "$@"\n'
                b"' '''\nprint('pip')\n"
            )
            (bin_dir / "python3.11").write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32)
            for name in ("dotenv", "fastapi", "pip"):
                (bin_dir / name).chmod(0o755)

            changed = strip.normalize(root)

            self.assertEqual(changed, ["dotenv", "fastapi"])
            for name in changed:
                self.assertEqual(
                    (bin_dir / name).read_bytes().split(b"\n")[0], b"#!/bin/sh"
                )
            self.assertIn(b"print('pip')", (bin_dir / "pip").read_bytes())

            out = subprocess.run(
                [str(bin_dir / "dotenv")], capture_output=True, text=True, check=True
            )
            self.assertEqual(out.stdout.strip(), "hi")
            failed = subprocess.run([str(bin_dir / "fastapi")], capture_output=True)
            self.assertEqual(failed.returncode, 7)

    def test_a_leftover_build_path_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            (bin_dir / "python3").symlink_to(sys.executable)
            # Not a python shebang, but still a home path in the header area: the guard must see it.
            (bin_dir / "odd").write_bytes(b"#!/Users/someone/tool\n")
            with self.assertRaises(SystemExit):
                strip.normalize(Path(tmp))


if __name__ == "__main__":
    unittest.main()
