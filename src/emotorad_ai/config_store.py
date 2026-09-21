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

    # Validate every field before exporting any of it, so a bad field later in
    # the dict never leaves earlier fields already exported — a partial export
    # is worse than none, because the caller has no way to tell it happened.
    for name, value in fields.items():
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ConfigStoreError("secret %r: field %r must be a scalar" % (secret_id, name))

    exported: List[str] = []
    for name, value in fields.items():
        if value is None:
            # A JSON null means "not set". str(None) would export the literal
            # string "None", which passes a truthiness/presence check further
            # down the line and becomes a guessable credential (e.g. a
            # playground password of "None").
            continue
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
        # A botocore ClientError carries a service error code (metadata, not a
        # value) in exc.response["Error"]["Code"] — surface it so "the secret
        # doesn't exist" and "the role can't read it" don't look identical.
        code = None
        response_attr = getattr(exc, "response", None)
        if isinstance(response_attr, dict):
            code = response_attr.get("Error", {}).get("Code")
        label = "%s (%s)" % (type(exc).__name__, code) if code else type(exc).__name__
        raise ConfigStoreError("could not read secret %r: %s" % (secret_id, label)) from None

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
