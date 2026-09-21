# tests/test_start.py
"""The container entrypoint. Loads the secret into its own environment, then
launches the two processes as children so they inherit it. A shell script that
printed `export NAME=value` would put every value into process listings; this
writes nothing."""

import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

START = Path(__file__).resolve().parent.parent / "docker" / "start.py"


def load_start():
    spec = importlib.util.spec_from_file_location("start", START)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CommandTests(unittest.TestCase):
    def test_streamlit_is_bound_to_localhost_under_the_playground_path(self):
        start = load_start()
        cmd = start.streamlit_command()
        self.assertEqual(cmd[:3], ["streamlit", "run", "src/emotorad_ai/playground.py"])
        self.assertIn("--server.address", cmd)
        self.assertEqual(cmd[cmd.index("--server.address") + 1], "127.0.0.1")
        self.assertEqual(cmd[cmd.index("--server.baseUrlPath") + 1], "playground")
        self.assertEqual(cmd[cmd.index("--server.port") + 1], "8501")

    def test_uvicorn_serves_the_api_on_every_interface(self):
        start = load_start()
        self.assertEqual(
            start.uvicorn_command(),
            ["uvicorn", "emotorad_ai.api:app", "--host", "0.0.0.0", "--port", "8000"],
        )


class SrcDirTests(unittest.TestCase):
    def test_finds_src_beside_start_py_in_the_image_layout(self):
        start = load_start()
        image_src = start.os.path.join("/app", "src")
        with mock.patch.object(start.os.path, "isdir", side_effect=lambda p: p == image_src):
            self.assertEqual(start.src_dir("/app"), image_src)

    def test_finds_src_one_level_up_in_a_checkout(self):
        start = load_start()
        checkout_src = start.os.path.join("/repo/docker", start.os.pardir, "src")
        with mock.patch.object(start.os.path, "isdir", side_effect=lambda p: p == checkout_src):
            self.assertEqual(start.src_dir("/repo/docker"), start.os.path.normpath(checkout_src))


class MainTests(unittest.TestCase):
    def test_main_loads_then_launches_and_execs(self):
        start = load_start()
        calls = []
        child = mock.Mock()
        with mock.patch.object(start, "load_into_environ", return_value=["A"]) as load, \
             mock.patch.object(start.subprocess, "Popen", side_effect=lambda cmd: calls.append(("popen", cmd)) or child) as popen, \
             mock.patch.object(start.os, "execvp", side_effect=lambda f, argv: calls.append(("exec", argv))):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(start.main(), 0)
        load.assert_called_once_with()
        self.assertEqual(calls[0], ("popen", start.streamlit_command()))
        self.assertEqual(popen.call_args, mock.call(start.streamlit_command()))
        self.assertEqual(calls[1], ("exec", start.uvicorn_command()))
        self.assertIn("exported A", stdout.getvalue())

    def test_a_successful_load_exports_the_count_for_health(self):
        start = load_start()
        child = mock.Mock()
        with mock.patch.dict(start.os.environ, {}, clear=False), \
             mock.patch.object(start, "load_into_environ", return_value=["A", "B", "C"]), \
             mock.patch.object(start.subprocess, "Popen", return_value=child), \
             mock.patch.object(start.os, "execvp", side_effect=lambda f, argv: None):
            start.os.environ.pop("EMOTORAD_AI_CONFIG_EXPORTED", None)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(start.main(), 0)
            self.assertEqual(start.os.environ["EMOTORAD_AI_CONFIG_EXPORTED"], "3")

    def test_a_config_failure_stops_before_anything_launches(self):
        start = load_start()
        with mock.patch.object(start, "load_into_environ", side_effect=start.ConfigStoreError("could not read secret 'x': Boom")), \
             mock.patch.object(start.subprocess, "Popen") as popen, \
             mock.patch.object(start.os, "execvp") as execvp:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(start.main(), 1)
        popen.assert_not_called()
        execvp.assert_not_called()
        self.assertIn("could not read secret", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_a_failed_exec_terminates_the_streamlit_child_and_returns_1(self):
        start = load_start()
        child = mock.Mock()
        with mock.patch.object(start, "load_into_environ", return_value=["A"]), \
             mock.patch.object(start.subprocess, "Popen", return_value=child), \
             mock.patch.object(start.os, "execvp", side_effect=OSError("no such file")):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(start.main(), 1)
        child.terminate.assert_called_once_with()
        self.assertIn("could not exec uvicorn: OSError", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
