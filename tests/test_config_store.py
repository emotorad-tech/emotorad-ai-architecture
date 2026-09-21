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
