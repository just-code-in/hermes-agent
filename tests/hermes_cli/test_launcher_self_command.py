"""A Hermes child re-exec must find its installation from any working directory.

Regression: under the PM launcher the gateway runs on PM's store Python, which sees the checkout
only through the launcher's in-process ``sys.path`` insert. ``find_spec("hermes_cli")`` therefore
succeeds in the parent while ``sys.executable -m hermes_cli.main`` started from a Kanban task
workspace dies with ``No module named 'hermes_cli'`` -- every dispatched worker crashed at
interpreter start.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import hermes_constants
from hermes_cli import _launchers
from pm import environments


def _fixture_checkout(root: Path) -> None:
    """Real launcher + store detection, a stub bootstrap, and a marker entry point."""
    (root / "hermes_cli").mkdir(parents=True)
    (root / "pm").mkdir()
    (root / "hermes_cli" / "__init__.py").write_text("")
    (root / "pm" / "__init__.py").write_text("")
    (root / "hermes_cli" / "_launchers.py").write_bytes(Path(_launchers.__file__).read_bytes())
    (root / "pm" / "environments.py").write_bytes(Path(environments.__file__).read_bytes())
    (root / "hermes_constants.py").write_bytes(Path(hermes_constants.__file__).read_bytes())
    (root / "hermes_bootstrap.py").write_text("")
    (root / "hermes_cli" / "main.py").write_text("import sys\nprint('child-ran', *sys.argv[1:])\n")


def test_self_command_runs_from_foreign_cwd_on_store_python(tmp_path):
    root = tmp_path / "checkout"
    _fixture_checkout(root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # A bare (non-venv) interpreter inside the PM store, seeing the checkout only in-process --
    # exactly how the launcher boots the gateway.
    store_python = getattr(sys, "_base_executable", sys.executable)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env["HERMES_RUNTIME_DIR"] = str(Path(sys.base_prefix).resolve())
    env["HERMES_HOME"] = str(tmp_path / "home")
    parent = (
        "import json, subprocess, sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from hermes_cli._launchers import self_command\n"
        "r = subprocess.run(self_command(['--version']), capture_output=True, text=True)\n"
        "print(json.dumps([r.returncode, r.stdout, r.stderr]))\n"
    )
    result = subprocess.run([store_python, "-I", "-c", parent], cwd=workspace, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    rc, out, err = json.loads(result.stdout.strip().splitlines()[-1])
    assert rc == 0, err
    assert out.strip() == "child-ran --version"
