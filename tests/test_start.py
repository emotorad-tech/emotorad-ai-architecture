# tests/test_start.py
"""The container entrypoint. Loads the secret into its own environment, then
launches the two processes as children so they inherit it. A shell script that
printed `export NAME=value` would put every value into process listings; this
writes nothing."""

import importlib.util
import os
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


class MainTests(unittest.TestCase):
    def test_main_loads_then_launches_and_execs(self):
        start = load_start()
        calls = []
        with mock.patch.object(start, "load_into_environ", return_value=["A"]) as load, \
             mock.patch.object(start.subprocess, "Popen", side_effect=lambda cmd: calls.append(("popen", cmd))), \
             mock.patch.object(start.os, "execvp", side_effect=lambda f, argv: calls.append(("exec", argv))):
            self.assertEqual(start.main(), 0)
        load.assert_called_once_with()
        self.assertEqual(calls[0], ("popen", start.streamlit_command()))
        self.assertEqual(calls[1], ("exec", start.uvicorn_command()))

    def test_a_config_failure_stops_before_anything_launches(self):
        start = load_start()
        with mock.patch.object(start, "load_into_environ", side_effect=start.ConfigStoreError("could not read secret 'x': Boom")), \
             mock.patch.object(start.subprocess, "Popen") as popen, \
             mock.patch.object(start.os, "execvp") as execvp:
            self.assertEqual(start.main(), 1)
        popen.assert_not_called()
        execvp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
