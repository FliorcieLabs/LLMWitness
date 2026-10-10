# Development

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
pytest -q
ruff check llmwitness tests
mypy --config-file pyproject.toml llmwitness
python -m build
```

Run Community services only on loopback:

```bash
python -m uvicorn llmwitness.ingest:app --host 127.0.0.1 --port 8000
python -m uvicorn llmwitness.gateway:app --host 127.0.0.1 --port 8011
```

Configure development keys and shared tokens through environment variables; never commit them. The consolidated development branch persists generated signing/HMAC keys under `.llmwitness/keys` by default. `LLMWITNESS_EPHEMERAL_KEYS=1` opts into process-local test keys. Local key files are not a managed identity or secure key-storage service; protect and back them up separately.

See [Community architecture](docs/ARCHITECTURE.md) for the distinction between best-effort telemetry, local session persistence, and consequential-effect journals. Keep regression fixtures synthetic and do not include credentials, receipt state, private planning folders, or other product repositories in a contribution.
