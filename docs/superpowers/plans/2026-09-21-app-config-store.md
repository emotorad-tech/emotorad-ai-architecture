# App Config Store (AWS Secrets Manager) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The container reads every secret it needs from one AWS Secrets Manager secret at startup through the instance role; the Anthropic API key becomes the default model path with Bedrock as a one-variable switch; no secret travels through GitHub Actions or the SSM deploy command.

**Architecture:** A pure loader module (`config_store.py`) fetches a JSON secret with `boto3` and exports its fields into `os.environ`, environment winning over the secret. A Python entrypoint (`docker/start.py`) runs the loader then launches Streamlit and uvicorn as children that inherit the environment. `llm.py` gains `AnthropicClaude` and a `select_llm(mode, settings)` factory that `api.py` uses; the deploy workflow passes only non-sensitive variables. A CloudFormation template creates the secret shell and the role policy; the human sets the value from a terminal.

**Tech Stack:** Python 3.12, `unittest`, `boto3` (new), `botocore.stub.Stubber`, `anthropic` SDK, CloudFormation, GitHub Actions. Spec: `docs/superpowers/specs/2026-09-21-app-config-store-design.md`.

## Global Constraints

- Repo `~/emotorad/emotorad-ai-architecture`, branch `feat/aws-secrets`. 4-space indentation, `from __future__ import annotations`, docstrings explain *why*.
- Tests: `.venv/bin/python3 -m unittest discover -s tests -t .` from the repo root must stay green (468 today, 1 pre-existing local-env error in `test_video` from a missing `imageio_ffmpeg` in this venv; that error is not ours and must not grow). Single module: `.venv/bin/python3 -m unittest tests.<module> -v`.
- Secret name: `/emotorad/<env>/ai/app`; staging is `/emotorad/stage/ai/app`. Fields, exactly: `API_KEY_CLAUDE`, `EMOTORAD_OMS_API_KEY`, `EMOTORAD_AI_PLAYGROUND_USER`, `EMOTORAD_AI_PLAYGROUND_PASSWORD`. `API_KEY_CLAUDE` is additionally exported as `ANTHROPIC_API_KEY`.
- Environment wins over the secret: a field is exported only when that variable is unset.
- Secret values are never logged, printed, written to disk, or included in an exception message.
- `EMOTORAD_AI_MODE` values: `offline` (code default), `anthropic` (deploy default), `bedrock`. `anthropic` mode without `ANTHROPIC_API_KEY` fails at startup with a named error.
- Any path containing `secret` or `credential` is gitignored; name files accordingly (`config_store`, `config-store`).
- No secret value is ever set from a Claude session. The human runs `put-secret-value`.
- AWS CLI calls use `--profile emotorad-staging`, region `ap-south-1`, account `851725486214`. Instance role `emotorad-ai-stage-ec2-role`. Deploy role `emotorad-ai-github-deploy` is not changed.
- Commit after every task, conventional-commit subject, ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Never push without the person's yes.

---

## File structure

| File | Responsibility |
|---|---|
| `requirements.txt` (modify) | add `boto3` |
| `src/emotorad_ai/config_store.py` (new) | fetch the JSON secret, export fields, aliases, `ConfigStoreError` |
| `docker/start.py` (new), `docker/start.sh` (delete), `Dockerfile` (modify) | Python entrypoint: load, then launch both processes |
| `src/emotorad_ai/llm.py` (modify) | `response_to_llm`, `AnthropicClaude`, `select_llm`, `LLMConfigError` |
| `src/emotorad_ai/api.py` (modify) | use `select_llm`; `/health` reports `secrets` |
| `src/emotorad_ai/playground.py` (modify) | hide the key box when the environment has one |
| `infra/config-store.yaml` (new) | secret shell + managed policy attached to the instance role |
| `docs/runbooks/config-store.md` (new) | create, set value, redeploy, rotate |
| `.github/workflows/deploy-staging.yml` (modify) | drop `-e` secrets, add mode, model and secret id |
| `docs/Emotorad_AWS_Deployment_Plan.md` (modify) | §2.4 points at the runbook |

---

### Task 1: `config_store.py` loader

**Files:**
- Modify: `requirements.txt`
- Create: `src/emotorad_ai/config_store.py`
- Test: `tests/test_config_store.py`

**Interfaces:**
- Produces: `config_store.load_into_environ(secret_id: Optional[str] = None, client: Any = None, environ: Optional[MutableMapping[str, str]] = None) -> List[str]`; `config_store.ConfigStoreError(Exception)`; constants `SECRET_ID_ENV = "EMOTORAD_AI_SECRET_ID"`, `ALIASES = {"API_KEY_CLAUDE": ("ANTHROPIC_API_KEY",)}`.

- [ ] **Step 1: Add boto3 to requirements**

Append to `requirements.txt`:

```text

# Startup config: src/emotorad_ai/config_store.py reads one Secrets Manager
# secret through the instance role and exports its fields as env vars. Local
# runs without EMOTORAD_AI_SECRET_ID never import boto3's network paths.
boto3>=1.34
```

Run: `.venv/bin/pip install -q -r requirements.txt && .venv/bin/python3 -c "import boto3, botocore; print(boto3.__version__)"`
Expected: a version string, no error.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_config_store.py
"""The startup loader: one secret in, env vars out, and nothing sensitive
anywhere else. Tests use botocore's Stubber so no AWS call is ever made."""

import json
import unittest

import boto3
from botocore.stub import Stubber

from emotorad_ai.config_store import ALIASES, SECRET_ID_ENV, ConfigStoreError, load_into_environ

SECRET_ID = "/emotorad/stage/ai/app"
FIELDS = {
    "API_KEY_CLAUDE": "sk-ant-test",
    "EMOTORAD_OMS_API_KEY": "oms-test",
    "EMOTORAD_AI_PLAYGROUND_USER": "u",
    "EMOTORAD_AI_PLAYGROUND_PASSWORD": "p",
}


def stubbed(secret_string=None, error=None):
    client = boto3.client("secretsmanager", region_name="ap-south-1")
    stub = Stubber(client)
    if error:
        stub.add_client_error("get_secret_value", service_error_code=error, expected_params={"SecretId": SECRET_ID})
    else:
        stub.add_response(
            "get_secret_value",
            {"Name": SECRET_ID, "SecretString": secret_string},
            expected_params={"SecretId": SECRET_ID},
        )
    stub.activate()
    return client


class LoadTests(unittest.TestCase):
    def test_every_field_is_exported_and_the_names_are_returned(self):
        env = {}
        exported = load_into_environ(SECRET_ID, client=stubbed(json.dumps(FIELDS)), environ=env)
        for name, value in FIELDS.items():
            self.assertEqual(env[name], value)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-test")
        self.assertEqual(sorted(exported), sorted(list(FIELDS) + ["ANTHROPIC_API_KEY"]))

    def test_environment_wins_over_the_secret(self):
        env = {"EMOTORAD_OMS_API_KEY": "from-shell", "ANTHROPIC_API_KEY": "shell-key"}
        exported = load_into_environ(SECRET_ID, client=stubbed(json.dumps(FIELDS)), environ=env)
        self.assertEqual(env["EMOTORAD_OMS_API_KEY"], "from-shell")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "shell-key")
        self.assertNotIn("EMOTORAD_OMS_API_KEY", exported)
        self.assertNotIn("ANTHROPIC_API_KEY", exported)
        self.assertEqual(env["API_KEY_CLAUDE"], "sk-ant-test")

    def test_the_alias_table_is_the_one_place_both_names_meet(self):
        self.assertEqual(ALIASES, {"API_KEY_CLAUDE": ("ANTHROPIC_API_KEY",)})

    def test_no_secret_id_means_no_call_and_nothing_exported(self):
        env = {}
        # A stub with no responses queued raises if get_secret_value is called.
        client = boto3.client("secretsmanager", region_name="ap-south-1")
        Stubber(client).activate()
        self.assertEqual(load_into_environ(None, client=client, environ=env), [])
        self.assertEqual(env, {})

    def test_the_secret_id_defaults_to_the_env_var(self):
        env = {SECRET_ID_ENV: SECRET_ID}
        load_into_environ(client=stubbed(json.dumps(FIELDS)), environ=env)
        self.assertEqual(env["API_KEY_CLAUDE"], "sk-ant-test")

    def test_values_are_strings_even_when_the_json_has_numbers(self):
        env = {}
        load_into_environ(SECRET_ID, client=stubbed(json.dumps({"API_KEY_CLAUDE": 123})), environ=env)
        self.assertEqual(env["API_KEY_CLAUDE"], "123")


class FailureTests(unittest.TestCase):
    def test_a_missing_secret_raises_a_named_error(self):
        with self.assertRaises(ConfigStoreError) as caught:
            load_into_environ(SECRET_ID, client=stubbed(error="ResourceNotFoundException"), environ={})
        self.assertIn(SECRET_ID, str(caught.exception))

    def test_non_json_raises_without_echoing_the_value(self):
        with self.assertRaises(ConfigStoreError) as caught:
            load_into_environ(SECRET_ID, client=stubbed("sk-ant-plaintext-oops"), environ={})
        self.assertNotIn("sk-ant-plaintext-oops", str(caught.exception))

    def test_json_that_is_not_an_object_raises(self):
        with self.assertRaises(ConfigStoreError):
            load_into_environ(SECRET_ID, client=stubbed(json.dumps(["a", "b"])), environ={})

    def test_a_failure_leaves_the_environment_untouched(self):
        env = {"KEEP": "me"}
        with self.assertRaises(ConfigStoreError):
            load_into_environ(SECRET_ID, client=stubbed(error="AccessDeniedException"), environ=env)
        self.assertEqual(env, {"KEEP": "me"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python3 -m unittest tests.test_config_store -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'emotorad_ai.config_store'`

- [ ] **Step 4: Write the loader**

```python
# src/emotorad_ai/config_store.py
"""Startup config from AWS Secrets Manager.

One secret per environment (`/emotorad/<env>/ai/app`), a flat JSON object whose
keys are the environment variable names the code already reads. This module
fetches it once, through the instance role, and exports each field into the
process environment before anything else starts. `config.py`, `oms.py`, the
playground and the anthropic SDK keep reading `os.environ`; none of them know
Secrets Manager exists.

Two rules the tests pin:

* **The environment wins.** A field is exported only if that variable is not
  already set, so `docker run -e` overrides and local shells keep working and
  the loader can never clobber a deliberate per-run value.
* **Values are never logged.** A failure names the secret id and the failure
  class; it never includes what was fetched. A malformed value that gets echoed
  into a stack trace is a leak into CloudWatch.

Why raise rather than continue: a container that starts without its secrets
serves 503s with no explanation. Failing at startup with a named error is the
cheaper failure.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, MutableMapping, Optional, Tuple

SECRET_ID_ENV = "EMOTORAD_AI_SECRET_ID"

# The secret stores the Anthropic key under the name the team uses for it; the
# SDK and the playground read ANTHROPIC_API_KEY. This table is the only place
# that knows both names.
ALIASES: Dict[str, Tuple[str, ...]] = {"API_KEY_CLAUDE": ("ANTHROPIC_API_KEY",)}


class ConfigStoreError(Exception):
    """The secret could not be fetched or parsed. Never carries a value."""


def load_into_environ(
    secret_id: Optional[str] = None,
    client: Any = None,
    environ: Optional[MutableMapping[str, str]] = None,
) -> List[str]:
    """Fetch the JSON secret and export each field as an env var.

    Returns the names exported (aliases included). Does nothing and returns []
    when no secret id is given and EMOTORAD_AI_SECRET_ID is unset, so local runs
    keep using the shell environment.
    """
    env: MutableMapping[str, str] = environ if environ is not None else os.environ
    secret_id = secret_id or env.get(SECRET_ID_ENV)
    if not secret_id:
        return []

    fields = _fetch(secret_id, client)

    exported: List[str] = []
    for name, value in fields.items():
        for target in (name,) + ALIASES.get(name, ()):
            if target in env:
                continue
            env[target] = str(value)
            exported.append(target)
    return exported


def _fetch(secret_id: str, client: Any) -> Dict[str, Any]:
    if client is None:
        import boto3  # imported lazily: tests and local runs never need it

        client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except Exception as exc:  # botocore's exceptions are generated classes; one net is honest here
        raise ConfigStoreError(
            "could not read secret %r: %s" % (secret_id, type(exc).__name__)
        ) from None

    raw = response.get("SecretString")
    if raw is None:
        raise ConfigStoreError("secret %r has no SecretString (binary secrets are not supported)" % secret_id)
    try:
        fields = json.loads(raw)
    except ValueError:
        raise ConfigStoreError("secret %r is not valid JSON" % secret_id) from None
    if not isinstance(fields, dict):
        raise ConfigStoreError("secret %r must be a JSON object of name -> value" % secret_id)
    return fields
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python3 -m unittest tests.test_config_store -v`
Expected: 10 tests, OK. Then the full suite: `.venv/bin/python3 -m unittest discover -s tests -t .` — same result as before plus 10.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt src/emotorad_ai/config_store.py tests/test_config_store.py
git commit -m "feat(config): load startup secrets from AWS Secrets Manager into the environment

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `docker/start.py` entrypoint

**Files:**
- Create: `docker/start.py`
- Delete: `docker/start.sh`
- Modify: `Dockerfile:9-10`
- Test: `tests/test_start.py`

**Interfaces:**
- Consumes: `config_store.load_into_environ()`, `config_store.ConfigStoreError`.
- Produces: `docker/start.py` with `streamlit_command() -> List[str]`, `uvicorn_command() -> List[str]`, `main() -> int`. Importable as a module for tests via `importlib` (it lives outside `src/`).

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m unittest tests.test_start -v`
Expected: FAIL with `FileNotFoundError` or `AttributeError` (no `docker/start.py`).

- [ ] **Step 3: Write the entrypoint, delete the shell script, update the Dockerfile**

```python
# docker/start.py
"""Container entrypoint: load startup config, then run both processes.

Order matters. `load_into_environ` fills this process's environment from the
Secrets Manager secret named by EMOTORAD_AI_SECRET_ID (a no-op when unset).
Streamlit is started as a child and uvicorn replaces this process via exec; both
inherit the environment, so no value is ever written to a file or a command line.

The Streamlit server binds to localhost only: api.py reverse-proxies /playground
to it, so it never needs a port opened in the security group.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"))

from emotorad_ai.config_store import ConfigStoreError, load_into_environ  # noqa: E402


def streamlit_command() -> List[str]:
    return [
        "streamlit", "run", "src/emotorad_ai/playground.py",
        "--server.port", "8501",
        "--server.address", "127.0.0.1",
        "--server.baseUrlPath", "playground",
        "--server.headless", "true",
        "--server.enableCORS", "false",
        "--server.enableXsrfProtection", "false",
        "--browser.gatherUsageStats", "false",
        "--client.toolbarMode", "viewer",
    ]


def uvicorn_command() -> List[str]:
    return ["uvicorn", "emotorad_ai.api:app", "--host", "0.0.0.0", "--port", "8000"]


def main() -> int:
    try:
        exported = load_into_environ()
    except ConfigStoreError as exc:
        # Names only, never values — this line lands in CloudWatch.
        print("startup config: %s" % exc, file=sys.stderr)
        return 1
    print("startup config: exported %s" % (", ".join(exported) or "nothing (no secret id set)"))

    subprocess.Popen(streamlit_command())
    os.execvp("uvicorn", uvicorn_command())
    return 0  # unreachable after a successful exec; kept for the tests' patched path


if __name__ == "__main__":
    raise SystemExit(main())
```

Delete `docker/start.sh` (`git rm docker/start.sh`).

In `Dockerfile`, replace

```dockerfile
COPY docker/start.sh start.sh
RUN chmod +x start.sh
```

with

```dockerfile
COPY docker/start.py start.py
```

and replace `CMD ["./start.sh"]` with `CMD ["python", "start.py"]`. The `ENV PYTHONPATH=/app/src` line stays; `start.py` also inserts `src/` itself so it works when run from a checkout.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python3 -m unittest tests.test_start -v`
Expected: 4 tests, OK. Then `.venv/bin/python3 -m unittest discover -s tests -t .` — green as before plus 4.

- [ ] **Step 5: Build the image locally to prove the entrypoint starts**

Run: `docker build -q -t emotorad-ai:local . && docker run --rm -d --name emotorad-ai-local -p 127.0.0.1:8000:8000 emotorad-ai:local && sleep 6 && curl -sf http://127.0.0.1:8000/health; echo; docker logs emotorad-ai-local 2>&1 | grep "startup config"; docker rm -f emotorad-ai-local`
Expected: `{"status":"ok","mode":"offline"}` and the log line `startup config: exported nothing (no secret id set)`. If Docker is not running on this machine, note that in the report and rely on the unit tests; the deploy workflow builds the image on the instance.

- [ ] **Step 6: Commit**

```bash
git add docker/start.py Dockerfile tests/test_start.py
git rm -q docker/start.sh
git commit -m "feat(docker): Python entrypoint loads startup config before launching the processes

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `AnthropicClaude` and `select_llm` in `llm.py`

**Files:**
- Modify: `src/emotorad_ai/llm.py:41-88`
- Test: `tests/test_llm_select.py`

**Interfaces:**
- Produces: `response_to_llm(response) -> LLMResponse`; `AnthropicClaude(settings, api_key: str, model: Optional[str] = None, client=None)` with `.model` and `.create(system, messages, tools) -> LLMResponse`; `LLMConfigError(Exception)`; `select_llm(mode: str, settings: Settings, environ: Optional[Mapping[str, str]] = None)` returning `OfflinePlanner | AnthropicClaude | BedrockClaude`; `MODES = ("offline", "anthropic", "bedrock")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_select.py
"""Two clients, one request shape, one response mapping — and a factory that
turns EMOTORAD_AI_MODE into a client or a named startup error."""

import unittest

from emotorad_ai.config import Settings
from emotorad_ai.llm import (
    MODES,
    AnthropicClaude,
    BedrockClaude,
    LLMConfigError,
    OfflinePlanner,
    select_llm,
)


class _Block:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if not (exclude_none and v is None)}


class _Usage:
    def model_dump(self):
        return {"input_tokens": 12, "output_tokens": 7}


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Messages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _Client:
    def __init__(self, response):
        self.messages = _Messages(response)


TOOL = _Response(
    [_Block(type="text", text="Checking."), _Block(type="tool_use", id="tu_1", name="search_knowledge", input={"query": "x"})],
    stop_reason="tool_use",
)


class AnthropicClaudeTests(unittest.TestCase):
    def test_it_sends_the_same_request_shape_as_bedrock(self):
        client = _Client(TOOL)
        llm = AnthropicClaude(Settings(effort="low", max_tokens=4096), api_key="k", model="claude-sonnet-5", client=client)
        llm.create(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[{"name": "t"}])
        call = client.messages.calls[0]
        self.assertEqual(call["model"], "claude-sonnet-5")
        self.assertEqual(call["max_tokens"], 4096)
        self.assertEqual(call["system"], "sys")
        self.assertEqual(call["tools"], [{"name": "t"}])
        self.assertEqual(call["thinking"], {"type": "adaptive"})
        self.assertEqual(call["output_config"], {"effort": "low"})

    def test_the_model_defaults_to_settings(self):
        llm = AnthropicClaude(Settings(model="claude-opus-5"), api_key="k", client=_Client(TOOL))
        self.assertEqual(llm.model, "claude-opus-5")

    def test_responses_map_identically_to_bedrock(self):
        a = AnthropicClaude(Settings(), api_key="k", client=_Client(TOOL)).create(system="s", messages=[], tools=[])
        b = BedrockClaude(Settings(), client=_Client(TOOL)).create(system="s", messages=[], tools=[])
        self.assertEqual(a, b)
        self.assertTrue(a.wants_tools)
        self.assertEqual(a.tool_uses[0].arguments, {"query": "x"})
        self.assertEqual(a.text, "Checking.")
        self.assertEqual(a.usage, {"input_tokens": 12, "output_tokens": 7})


class SelectTests(unittest.TestCase):
    def test_modes_are_the_three_the_deploy_can_name(self):
        self.assertEqual(MODES, ("offline", "anthropic", "bedrock"))

    def test_offline_needs_nothing(self):
        self.assertIsInstance(select_llm("offline", Settings(), environ={}), OfflinePlanner)

    def test_anthropic_needs_a_key_in_the_environment(self):
        llm = select_llm("anthropic", Settings(), environ={"ANTHROPIC_API_KEY": "k"}, client=_Client(TOOL))
        self.assertIsInstance(llm, AnthropicClaude)

    def test_anthropic_without_a_key_fails_with_a_named_error(self):
        with self.assertRaises(LLMConfigError) as caught:
            select_llm("anthropic", Settings(), environ={})
        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))

    def test_bedrock_uses_the_role_and_takes_no_key(self):
        self.assertIsInstance(select_llm("bedrock", Settings(), environ={}, client=_Client(TOOL)), BedrockClaude)

    def test_an_unknown_mode_is_a_named_error(self):
        with self.assertRaises(LLMConfigError) as caught:
            select_llm("cloud", Settings(), environ={})
        self.assertIn("cloud", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m unittest tests.test_llm_select -v`
Expected: FAIL with `ImportError: cannot import name 'AnthropicClaude'`

- [ ] **Step 3: Implement**

In `src/emotorad_ai/llm.py`, add `Mapping` to the `typing` import and `import os`. Replace the whole `BedrockClaude` class (lines 41–88) with:

```python
def response_to_llm(response: Any) -> LLMResponse:
    """The SDK message -> our LLMResponse. One mapping for both clients, so the
    Anthropic API path and the Bedrock path cannot disagree about what a tool
    call looks like."""
    api_content: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    tool_uses: List[ToolUse] = []
    for block in response.content:
        api_content.append(block.model_dump(exclude_none=True))
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_uses.append(ToolUse(id=block.id, name=block.name, arguments=dict(block.input or {})))

    return LLMResponse(
        stop_reason=response.stop_reason or "end_turn",
        text="\n".join(part for part in text_parts if part).strip(),
        tool_uses=tool_uses,
        api_content=api_content,
        usage=response.usage.model_dump() if response.usage else None,
    )


def _create(client: Any, model: str, settings: Settings, system: str, messages: Sequence[Dict[str, Any]], tools: Sequence[Dict[str, Any]]) -> LLMResponse:
    response = client.messages.create(
        model=model,
        max_tokens=settings.max_tokens,
        system=system,
        messages=list(messages),
        tools=list(tools),
        # Adaptive thinking with a low effort default: battery triage is a
        # bounded problem and the turn is in front of a waiting customer.
        thinking={"type": "adaptive"},
        output_config={"effort": settings.effort},
    )
    return response_to_llm(response)


class BedrockClaude:
    """Claude via Bedrock, in Emotorad's own AWS account and region."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        if client is not None:
            self._client = client
        else:
            from anthropic import AnthropicBedrockMantle  # imported lazily: tests never need it

            self._client = AnthropicBedrockMantle(aws_region=settings.aws_region)

    def create(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> LLMResponse:
        return _create(self._client, self.settings.model, self.settings, system, messages, tools)


class AnthropicClaude:
    """Claude via the first-party Anthropic API.

    The deploy default since 2026-09-21 (spec: app-config-store). Same request
    shape as BedrockClaude on purpose, so switching between them is a change of
    EMOTORAD_AI_MODE and nothing else. The key is held in memory for the
    process and never logged.
    """

    def __init__(self, settings: Settings, api_key: str, model: Optional[str] = None, client: Any = None) -> None:
        self.settings = settings
        self.model = model or settings.model
        if client is not None:
            self._client = client
        else:
            import anthropic  # imported lazily: tests never need it

            self._client = anthropic.Anthropic(api_key=api_key)

    def create(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> LLMResponse:
        return _create(self._client, self.model, self.settings, system, messages, tools)


MODES = ("offline", "anthropic", "bedrock")


class LLMConfigError(Exception):
    """EMOTORAD_AI_MODE names a path this process cannot serve. Raised at startup."""


def select_llm(mode: str, settings: Settings, environ: Optional[Mapping[str, str]] = None, client: Any = None) -> Any:
    """The model client for EMOTORAD_AI_MODE, or a named error before any request.

    A missing key must fail here, at import of api.py, not on the first customer
    message: the health check then fails the deploy instead of the customer
    finding out.
    """
    env = environ if environ is not None else os.environ
    if mode == "offline":
        return OfflinePlanner()
    if mode == "anthropic":
        key = env.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise LLMConfigError(
                "EMOTORAD_AI_MODE=anthropic but ANTHROPIC_API_KEY is not set "
                "(the config store exports it from API_KEY_CLAUDE)"
            )
        return AnthropicClaude(settings, api_key=key, client=client)
    if mode == "bedrock":
        return BedrockClaude(settings, client=client)
    raise LLMConfigError("unknown EMOTORAD_AI_MODE %r; expected one of %s" % (mode, ", ".join(MODES)))
```

`OfflinePlanner` is defined later in the file; `select_llm` references it at call time, so the order is fine.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python3 -m unittest tests.test_llm_select -v`
Expected: 9 tests, OK. Full suite green as before plus 9.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/llm.py tests/test_llm_select.py
git commit -m "feat(llm): AnthropicClaude client and select_llm(mode) with named startup errors

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `api.py` uses `select_llm`; `/health` reports config state

**Files:**
- Modify: `src/emotorad_ai/api.py:1-12` (docstring), `:47`, `:52-66`, `:85-88`
- Test: `tests/test_api_health.py`

**Interfaces:**
- Consumes: `select_llm`, `LLMConfigError`, `config_store.SECRET_ID_ENV`.
- Produces: `GET /health` → `{"status": "ok", "mode": <mode>, "secrets": "loaded" | "not configured"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_health.py
"""The health check is what the deploy workflow reads. It has to say which model
path is live and whether the config store was used, so a container that started
without its secret is visible in the workflow log rather than in a customer chat."""

import importlib
import os
import unittest
from unittest import mock


def fresh_api(env):
    with mock.patch.dict(os.environ, env, clear=False):
        import emotorad_ai.api as api

        return importlib.reload(api)


class HealthTests(unittest.TestCase):
    def test_offline_reports_no_secret(self):
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": ""})
        self.assertEqual(api.health(), {"status": "ok", "mode": "offline", "secrets": "not configured"})

    def test_a_secret_id_reports_loaded(self):
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": "/emotorad/stage/ai/app"})
        self.assertEqual(api.health()["secrets"], "loaded")

    def test_anthropic_mode_without_a_key_fails_at_import(self):
        from emotorad_ai.llm import LLMConfigError

        with self.assertRaises(LLMConfigError):
            fresh_api({"EMOTORAD_AI_MODE": "anthropic", "ANTHROPIC_API_KEY": ""})

    @classmethod
    def tearDownClass(cls):
        fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": ""})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m unittest tests.test_api_health -v`
Expected: `test_offline_reports_no_secret` FAILS (no `secrets` key); `test_anthropic_mode_without_a_key_fails_at_import` FAILS (today `anthropic` mode silently builds `BedrockClaude`).

- [ ] **Step 3: Wire `api.py`**

Replace `from .llm import OfflinePlanner` with `from .llm import select_llm` and add `from .config_store import SECRET_ID_ENV`. Replace lines 52–66 with:

```python
MODE = os.environ.get("EMOTORAD_AI_MODE", "offline")
# Set by docker/start.py's loader. Reported by /health so a container that
# started without its secret is visible from the deploy log.
SECRETS_STATE = "loaded" if os.environ.get(SECRET_ID_ENV) else "not configured"

settings = load_settings()
registry = build_registry()
resolver = IdentityResolver(registry)
log = EventLog(path=settings.log_path, to_stdout=settings.log_to_stdout)
runtime = Runtime(
    settings=settings,
    registry=registry,
    # Raises LLMConfigError at import when the mode cannot be served, so the
    # deploy's health check fails instead of the first customer message.
    llm=select_llm(MODE, settings),
    log=log,
    resolver=resolver,
)
adapter = WebsiteChatAdapter(resolver)
```

Change `health()` to:

```python
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": MODE, "secrets": SECRETS_STATE}
```

Update the module docstring's `EMOTORAD_AI_MODE=bedrock uvicorn ...` example to list the three modes:

```
    EMOTORAD_AI_MODE=anthropic ANTHROPIC_API_KEY=... uvicorn emotorad_ai.api:app   # deploy default
    EMOTORAD_AI_MODE=bedrock uvicorn emotorad_ai.api:app                          # instance role
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python3 -m unittest tests.test_api_health -v`
Expected: 3 tests, OK. Full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/api.py tests/test_api_health.py
git commit -m "feat(api): select the model path from EMOTORAD_AI_MODE and report config state on /health

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Playground hides the key box when the environment has one

**Files:**
- Modify: `src/emotorad_ai/playground.py:1326-1332`

**Interfaces:** none new. Behaviour: with `ANTHROPIC_API_KEY` set, no password field is rendered; a caption says the key came from the environment; `api_key` is the env value. Without it, the field renders exactly as today.

- [ ] **Step 1: Make the change**

Replace lines 1326–1332:

```python
        api_key = settings.text_input(
            "Anthropic API key",
            value=os.environ.get("ANTHROPIC_API_KEY", ""),
            type="password",
            help="Session-only — never written to disk. Falls back to ANTHROPIC_API_KEY if set.",
        )
        st.session_state["_key_set"] = bool(api_key)
```

with:

```python
        env_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if env_key:
            # On staging the config store exports the key into the environment,
            # so nobody pastes a shared key into a browser. The field stays for
            # local runs with no key set.
            settings.caption("Anthropic API key: from environment")
            api_key = env_key
        else:
            api_key = settings.text_input(
                "Anthropic API key",
                value="",
                type="password",
                help="Session-only — never written to disk. Or set ANTHROPIC_API_KEY before starting.",
            )
        st.session_state["_key_set"] = bool(api_key)
```

- [ ] **Step 2: Static check and the playground tests**

Run: `.venv/bin/python3 -m pyflakes src/emotorad_ai/playground.py && .venv/bin/python3 -m unittest tests.test_playground_loop -v 2>&1 | tail -3`
Expected: no pyflakes output; playground tests OK.

- [ ] **Step 3: Verify by hand**

Run: `ANTHROPIC_API_KEY=dummy PYTHONPATH=src .venv/bin/streamlit run src/emotorad_ai/playground.py --server.headless true --server.port 8501` and open http://127.0.0.1:8501. Expected: "Model settings" shows the caption "Anthropic API key: from environment" and no password box. Stop it, run again without the variable: the password box is back. Stop the server.

- [ ] **Step 4: Commit**

```bash
git add src/emotorad_ai/playground.py
git commit -m "feat(playground): take the Anthropic key from the environment when it is there

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Infrastructure template, runbook, deploy workflow

**Files:**
- Create: `infra/config-store.yaml`, `docs/runbooks/config-store.md`
- Modify: `.github/workflows/deploy-staging.yml:66-87`, `docs/Emotorad_AWS_Deployment_Plan.md` §2.4

**Interfaces:**
- Produces: stack `emotorad-ai-<env>-config-store`; secret `/emotorad/<env>/ai/app`; managed policy `emotorad-ai-<env>-config-store-read` attached to the instance role.

- [ ] **Step 1: Write the template**

```yaml
# infra/config-store.yaml
# The application config store: one Secrets Manager secret per environment and
# read permission for the EC2 instance role. Creates the secret's *shell* only —
# the value is set by a person from a terminal (docs/runbooks/config-store.md),
# never by a template, a workflow, or a chat session.
#
#   aws cloudformation deploy --profile emotorad-staging --region ap-south-1 \
#     --stack-name emotorad-ai-stage-config-store \
#     --template-file infra/config-store.yaml \
#     --parameter-overrides Environment=stage InstanceRoleName=emotorad-ai-stage-ec2-role \
#     --capabilities CAPABILITY_NAMED_IAM
AWSTemplateFormatVersion: "2010-09-09"
Description: EMotorad AI platform - application config store (Secrets Manager) and instance-role read access

Parameters:
  Environment:
    Type: String
    AllowedValues: [stage, prod]
    Description: Environment segment of the secret name /emotorad/<env>/ai/app
  InstanceRoleName:
    Type: String
    Default: emotorad-ai-stage-ec2-role
    Description: Existing EC2 instance role that must be able to read the secret

Resources:
  AppConfig:
    Type: AWS::SecretsManager::Secret
    Properties:
      Name: !Sub /emotorad/${Environment}/ai/app
      Description: !Sub "EMotorad AI platform startup config for ${Environment}. JSON: API_KEY_CLAUDE, EMOTORAD_OMS_API_KEY, EMOTORAD_AI_PLAYGROUND_USER, EMOTORAD_AI_PLAYGROUND_PASSWORD. Value set by hand; see docs/runbooks/config-store.md."
      Tags:
        - Key: service
          Value: emotorad-ai
        - Key: environment
          Value: !Ref Environment

  ReadPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      ManagedPolicyName: !Sub emotorad-ai-${Environment}-config-store-read
      Description: Read the one app config secret. Attached to the instance role.
      Roles:
        - !Ref InstanceRoleName
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Action: secretsmanager:GetSecretValue
            Resource: !Ref AppConfig

Outputs:
  SecretArn:
    Value: !Ref AppConfig
  SecretName:
    Value: !Sub /emotorad/${Environment}/ai/app
```

Validate: `aws cloudformation validate-template --profile emotorad-staging --region ap-south-1 --template-body file://infra/config-store.yaml --query 'Parameters[].ParameterKey' --output text`
Expected: `Environment InstanceRoleName`.

- [ ] **Step 2: Write the runbook**

```markdown
# Runbook: the app config store

One Secrets Manager secret per environment, `/emotorad/<env>/ai/app`, read by the
container at startup (`docker/start.py` → `src/emotorad_ai/config_store.py`). The value is
a flat JSON object. Field names are the environment variables the code reads:

| Field | Read by |
|---|---|
| `API_KEY_CLAUDE` | exported as `ANTHROPIC_API_KEY` too; the API in `anthropic` mode and the playground |
| `EMOTORAD_OMS_API_KEY` | `tools/oms.py` |
| `EMOTORAD_AI_PLAYGROUND_USER` | `api.py` basic auth on `/playground` |
| `EMOTORAD_AI_PLAYGROUND_PASSWORD` | `api.py` basic auth on `/playground` |

Every command below uses `--profile emotorad-staging --region ap-south-1`. Set them once:

```bash
export AWS_PROFILE=emotorad-staging AWS_REGION=ap-south-1
```

## 1. Create the secret shell and the role permission (once per environment)

```bash
aws cloudformation deploy \
  --stack-name emotorad-ai-stage-config-store \
  --template-file infra/config-store.yaml \
  --parameter-overrides Environment=stage InstanceRoleName=emotorad-ai-stage-ec2-role \
  --capabilities CAPABILITY_NAMED_IAM
```

For prod: `Environment=prod` and the prod instance role name, stack
`emotorad-ai-prod-config-store`.

## 2. Set the value (a person, from a terminal)

Write the JSON to a file outside any repo, set it, then shred the file. Never paste a value
into a chat session, a commit, or a workflow.

```bash
cat > /tmp/app-config.json <<'EOF'
{"API_KEY_CLAUDE":"...","EMOTORAD_OMS_API_KEY":"...","EMOTORAD_AI_PLAYGROUND_USER":"...","EMOTORAD_AI_PLAYGROUND_PASSWORD":"..."}
EOF
aws secretsmanager put-secret-value --secret-id /emotorad/stage/ai/app --secret-string file:///tmp/app-config.json
rm -P /tmp/app-config.json
```

Check the shape without printing values:

```bash
aws secretsmanager get-secret-value --secret-id /emotorad/stage/ai/app --query SecretString --output text | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)))'
```

Expected: the four field names.

## 3. Deploy

Run the "Deploy to staging (EC2)" workflow. It passes `EMOTORAD_AI_SECRET_ID`,
`EMOTORAD_AI_MODE=anthropic` and `EMOTORAD_AI_MODEL`; nothing sensitive. Then:

```bash
curl -s https://ai-release-stage.emotorad.com/health
```

Expected: `{"status":"ok","mode":"anthropic","secrets":"loaded"}`. A container that could
not read the secret does not start; its reason is one line in the CloudWatch log group
`emotorad-ai-stage` beginning `startup config:`.

## 4. After the first successful deploy

Delete the two GitHub Actions secrets the workflow no longer reads:

```bash
gh secret delete PLAYGROUND_BASIC_AUTH_USER --env staging --repo emotorad-tech/emotorad-ai-architecture
gh secret delete PLAYGROUND_BASIC_AUTH_PASSWORD --env staging --repo emotorad-tech/emotorad-ai-architecture
```

## 5. Rotate a value

Repeat step 2 with the new value, then redeploy (step 3). The loader reads the secret only
at container start.

## 6. Switch the model path

`EMOTORAD_AI_MODE` in the workflow's `docker run` line: `anthropic` (default), `bedrock`
(needs `bedrock:InvokeModel` on the inference profile, see the deployment plan §1.1 and
§2.1), or `offline`.
```

- [ ] **Step 3: Change the deploy workflow**

In `.github/workflows/deploy-staging.yml`, replace the comment block above the "Rebuild and restart" step (the one beginning `# PLAYGROUND_BASIC_AUTH_USER/PASSWORD are GitHub Actions repo secrets`) with:

```yaml
        # No secrets in this command. The container reads them at startup from
        # Secrets Manager (EMOTORAD_AI_SECRET_ID) through the instance role —
        # docs/runbooks/config-store.md. Everything passed as -e here is safe to
        # appear in SSM command history.
```

and replace the `docker run` line with:

```
              \"docker run -d --name emotorad-ai --restart unless-stopped --log-driver=awslogs --log-opt awslogs-region=$AWS_REGION --log-opt awslogs-group=emotorad-ai-stage --log-opt awslogs-create-group=true -p 127.0.0.1:8000:8000 -e AWS_REGION=$AWS_REGION -e EMOTORAD_AI_SECRET_ID=/emotorad/stage/ai/app -e EMOTORAD_AI_MODE=anthropic -e EMOTORAD_AI_MODEL=claude-opus-5 -e EMOTORAD_CLOUDINARY_CLOUD=$EMOTORAD_CLOUDINARY_CLOUD emotorad-ai:stage\"
```

Add to the `env:` block a comment line after `EMOTORAD_CLOUDINARY_CLOUD`:

```yaml
  # Secrets (Anthropic key, OMS key, playground auth) are NOT here. See
  # docs/runbooks/config-store.md.
```

In the "Health check" step, replace `curl -sf https://ai-release-stage.emotorad.com/health` with:

```yaml
          curl -sf https://ai-release-stage.emotorad.com/health | tee /dev/stderr | grep -q '"secrets":"loaded"'
```

- [ ] **Step 4: Point the deployment plan at the runbook**

In `docs/Emotorad_AWS_Deployment_Plan.md` §2.4, replace the first two bullets (SSM Parameter Store … `config.py` is already fully env-overridable) with:

```markdown
- **Secrets Manager, one JSON secret per environment** (`/emotorad/<env>/ai/app`), read at
  container start by `docker/start.py` through the instance role and exported as env vars.
  `config.py` stays env-overridable, so no app code reads AWS. Template
  `infra/config-store.yaml`; runbook `docs/runbooks/config-store.md`. Decided 2026-09-21,
  replacing the SSM Parameter Store idea below it.
```

and change the Playground Basic Auth bullet's "Currently sourced as GitHub Actions secrets … if that visibility becomes a problem." to "Sourced from the config store since 2026-09-21; the GitHub Actions secrets are deleted (runbook §4)."

- [ ] **Step 5: Run the suite and a YAML sanity check**

Run: `.venv/bin/python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-staging.yml')); yaml.safe_load(open('infra/config-store.yaml')); print('yaml ok')" && .venv/bin/python3 -m unittest discover -s tests -t . 2>&1 | tail -2`
Expected: `yaml ok`; suite green.

- [ ] **Step 6: Commit**

```bash
git add infra/config-store.yaml docs/runbooks/config-store.md .github/workflows/deploy-staging.yml docs/Emotorad_AWS_Deployment_Plan.md
git commit -m "chore(deploy): config store template and runbook; no secrets in the deploy command

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Apply on staging (controller, not a subagent)

This task runs real AWS and GitHub commands. The controller runs it with the `emotorad-staging` profile after the person has said yes to each outward step; the secret value step is the person's alone.

- [ ] **Step 1: Deploy the stack** — runbook §1. Expected: `Successfully created/updated stack - emotorad-ai-stage-config-store`. Confirm: `aws iam list-attached-role-policies --role-name emotorad-ai-stage-ec2-role --query 'AttachedPolicies[].PolicyName' --output text` lists `emotorad-ai-stage-config-store-read`.
- [ ] **Step 2: The person sets the value** — runbook §2, from their own terminal. The controller then runs only the shape check (field names, never values).
- [ ] **Step 3: Push the branch and open the PR** — only after the person's explicit yes in the session. PR description carries the test evidence and a rollback section: revert the workflow commit and re-add the two Actions secrets; the stack can stay.
- [ ] **Step 4: After merge, run the deploy workflow on `main`** and check `/health` returns `"mode":"anthropic","secrets":"loaded"`. If the container fails to start, `aws logs tail emotorad-ai-stage --since 10m --profile emotorad-staging` shows the `startup config:` line.
- [ ] **Step 5: Delete the two Actions secrets** — runbook §4, after the person's yes.

---

## Self-review

**Spec coverage.** §2 secret and fields → Task 1 (ALIASES, names), Task 6 (template, runbook §2). §3 loader rules (env wins, no values logged, raise on failure, `start.py` entrypoint) → Tasks 1–2. §4 `AnthropicClaude`, `response_to_llm`, mode table, fail-fast, playground hiding → Tasks 3–5. §5 workflow changes and health field → Tasks 4 and 6, Actions secret deletion → Task 7. §6 template and runbook → Task 6, applied in Task 7. §7 tests → each task. §8 out of scope: no rotation, no KMS, Cloudinary untouched.

**Type consistency.** `load_into_environ(secret_id, client, environ)` in Task 1 is what Task 2 patches by name. `select_llm(mode, settings, environ, client)` in Task 3 is what Task 4 calls as `select_llm(MODE, settings)`. `SECRET_ID_ENV` from Task 1 is imported in Task 4. `AnthropicClaude(settings, api_key, model, client)` matches the unmerged bot-builder PR's constructor, so that merge stays a no-op.

**Judgement calls.** `start.py` inserts `src/` on `sys.path` itself so `python start.py` works both in the image (where `PYTHONPATH` is set) and from a checkout. The `anthropic` SDK is 1.2.0 in the venv; `thinking={"type":"adaptive"}` and `output_config` are already what `BedrockClaude` sends, so no new API surface is introduced.
