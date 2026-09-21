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
