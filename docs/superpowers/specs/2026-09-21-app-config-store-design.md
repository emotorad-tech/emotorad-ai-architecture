# AWS Secrets Manager for application secrets — design

**Date:** 2026-09-21
**Status:** approved design, awaiting implementation plan
**Scope:** every secret the container needs comes from one AWS Secrets Manager secret,
read at startup through the instance role. Nothing sensitive travels through GitHub
Actions, the SSM deploy command, or a `-e` flag. The Anthropic API key
(`API_KEY_CLAUDE`) becomes the default model path for both the playground and the API,
with Bedrock kept as a one-variable switch.

Companion spec: `2026-09-21-media-storage-design.md` (S3 uploads). This one lands first;
it is a day of work and unblocks the playground on staging immediately.

## 1. Why

- The playground asks for an Anthropic key in a text box on every session. On staging
  that means someone pastes a shared key into a browser each time.
- The playground basic-auth user and password, and soon `EMOTORAD_OMS_API_KEY`, are GitHub
  Actions secrets interpolated into the `docker run` line of an SSM command. Anyone with
  `ssm:GetCommandInvocation` on the account can read them from command history. The
  deployment plan (§2.4) called this "acceptable for a first pass" and named the fix.
- Adding a fourth secret the same way would add a fourth `-e` flag and a fourth place to
  leak. One loader, one secret, one IAM permission.

## 2. The secret

One Secrets Manager secret per environment, named `/emotorad/<env>/ai/app`, in
`ap-south-1`, following the account's existing convention (`/emotorad/stage/website/api`,
`/emotorad/prod/website/api`). The value is a flat JSON object. Field names are the environment variable
names the code already reads, except the Anthropic key, which is stored under the name
the team uses for it:

```json
{
  "API_KEY_CLAUDE": "sk-ant-...",
  "EMOTORAD_OMS_API_KEY": "...",
  "EMOTORAD_AI_PLAYGROUND_USER": "...",
  "EMOTORAD_AI_PLAYGROUND_PASSWORD": "..."
}
```

Rules:

- Values are set from a terminal by a person with account access (`aws secretsmanager
  put-secret-value`), never through a chat session, a commit, or a workflow file.
- Rotation is a `put-secret-value` plus a redeploy. No automatic rotation in this pass.
- The secret is created empty-shelled by the CloudFormation template in §6 so the ARN is
  stable and the role policy can name it exactly.

## 3. The loader: `src/emotorad_ai/config_store.py`

One module, one public function, no framework:

```python
def load_into_environ(secret_id: str | None = None) -> list[str]:
    """Fetch the JSON secret and export each field as an env var.

    Returns the field names exported. Does nothing and returns [] when
    secret_id (default: EMOTORAD_AI_SECRET_ID) is unset, so local runs keep
    using the shell environment. Raises SecretsError on any AWS or JSON
    failure: a container that starts without its secrets serves 503s with
    no explanation, which is worse than not starting.
    """
```

- Uses `boto3` (`secretsmanager.get_secret_value`). `boto3` is added to
  `requirements.txt`; `botocore.stub.Stubber` covers the tests.
- **Environment wins over the secret.** A field is exported only if that variable is not
  already set. This keeps `docker run -e` overrides and local `.env` files working, and
  means the loader cannot clobber a deliberate per-run value.
- **Aliases.** `API_KEY_CLAUDE` is additionally exported as `ANTHROPIC_API_KEY`, because
  that is what the `anthropic` SDK, the playground's fallback (`playground.py:1328`), and
  the new client in §4 read. The alias table lives in `config_store.py` and is the only place
  that knows both names.
- Values are never logged. The loader logs the field *names* it exported and the secret id,
  once, to stdout.
- Invoked once, before either process starts. `docker/start.sh` is replaced by
  `docker/start.py`: it calls `load_into_environ()` into its own `os.environ`, then
  `subprocess.Popen`s Streamlit and `os.execvp`s uvicorn with the same arguments the shell
  script uses today. Children inherit the environment. A shell script that printed
  `export NAME=value` lines would put every value into process listings and shell history;
  the Python entrypoint writes nothing anywhere.

## 4. Model path: Anthropic API by default, Bedrock by switch

`llm.py` gains `AnthropicClaude`, identical in interface to `BedrockClaude`
(`create(system, messages, tools) -> LLMResponse`), backed by
`anthropic.Anthropic(api_key=...)`, same `thinking={"type": "adaptive"}` and
`output_config={"effort": ...}` request shape. The response mapping is shared with
`BedrockClaude` in one `response_to_llm()` function so the two cannot drift. (The unmerged
bot-builder PR #1 carries the same class; this is a subset, and the merge is a no-op.)

`api.py`'s `EMOTORAD_AI_MODE` grows one value:

| `EMOTORAD_AI_MODE` | LLM | Needs |
|---|---|---|
| `offline` (today's default) | `OfflinePlanner` | nothing |
| `anthropic` (**new deploy default**) | `AnthropicClaude` | `ANTHROPIC_API_KEY` in env |
| `bedrock` | `BedrockClaude` | instance role with `bedrock:InvokeModel` |

`EMOTORAD_AI_MODEL` keeps meaning "the model id for the active path"; the deploy sets
`claude-opus-5` for `anthropic`. Missing key in `anthropic` mode fails at startup with a
named error, not on the first customer message.

**Stated trade-off.** The architecture doc chose Bedrock so LLM traffic stays inside the
AWS boundary. Defaulting to the Anthropic API reverses that for now, by decision on
2026-09-21. Switching back is `EMOTORAD_AI_MODE=bedrock` plus the inference-profile IAM
line the deployment plan already documents (§1.1, §2.1). Nothing else changes.

The playground: when `ANTHROPIC_API_KEY` is present the key field is hidden and a caption
says "Key from environment". The text box stays for local use with no key set.

## 5. Deploy workflow changes

`.github/workflows/deploy-staging.yml`:

- Remove `-e EMOTORAD_AI_PLAYGROUND_USER=...` and `-e EMOTORAD_AI_PLAYGROUND_PASSWORD=...`.
- Add `-e EMOTORAD_AI_SECRET_ID=/emotorad/stage/ai/app -e EMOTORAD_AI_MODE=anthropic
  -e EMOTORAD_AI_MODEL=claude-opus-5`. Neither value is sensitive.
- Delete the GitHub Actions secrets `PLAYGROUND_BASIC_AUTH_USER` and
  `PLAYGROUND_BASIC_AUTH_PASSWORD` once the first deploy with the loader succeeds (manual
  step in the runbook). `AWS_DEPLOY_ROLE_ARN` remains the only Actions secret.
- The health check gains `"secrets": "loaded" | "not configured"` so a deploy that started
  without its secret is visible from the workflow log.

## 6. Infrastructure: `infra/config-store.yaml` and a runbook

Account facts, read on 2026-09-21 with the `emotorad-staging` CLI profile (account
`851725486214`): the instance role is `emotorad-ai-stage-ec2-role` (inline policy
`emotorad-ai-stage-inline`: S3 on the deploy bucket, CloudWatch logs; no Secrets Manager,
no Bedrock). The GitHub deploy role `emotorad-ai-github-deploy` needs no change. The change
ships a CloudFormation template, applied with `aws cloudformation deploy` from the CLI for
staging now and re-run with one parameter for prod:

- `infra/config-store.yaml`: the `AWS::SecretsManager::Secret` shell (no value), and an
  `AWS::IAM::ManagedPolicy` granting `secretsmanager:GetSecretValue` on that one ARN,
  attached to the existing instance role by name (parameter, default
  `emotorad-ai-stage-ec2-role`).
- `docs/runbooks/config-store.md`: deploy the stack, set the value with `put-secret-value`
  from a JSON file that is then shredded, redeploy, confirm `/health` reports
  `secrets: loaded`, delete the two Actions secrets. Rotation is the same page.

## 7. Testing

- `tests/test_config_store.py` with `Stubber`: exports every field; leaves a pre-set variable
  alone; adds the `ANTHROPIC_API_KEY` alias; returns `[]` with no secret id; raises
  `SecretsError` on `ResourceNotFoundException`, on a non-JSON value, and on a non-object
  JSON value; never includes a value in the raised message.
- `tests/test_llm_anthropic.py`: `AnthropicClaude` with a fake client sends the same
  request shape as `BedrockClaude` and maps text, tool use and usage identically.
- `tests/test_api_mode.py`: `anthropic` mode without a key fails at import with the named
  error; `offline` unchanged.
- The deploy workflow's test step stays the gate.

## 8. Out of scope

- Automatic rotation, cross-region replication, and per-secret KMS keys.
- Moving `EMOTORAD_CLOUDINARY_CLOUD` (not a secret; it appears in every delivery URL).
- Migrating the playground off the Anthropic SDK direct call; it already reads
  `ANTHROPIC_API_KEY`, which the loader provides.
- S3 and media (companion spec).
