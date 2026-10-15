# agos-backend

FastAPI + SQLAlchemy (async) + PostgreSQL + Alembic. Serves the responder PWA and the native responder app. This is the team's own fork (`PSergio984/agos-backend-v2`); upstream `RusselxD/agos-backend` is read-only reference — never push fork-specific changes upstream.

## Commands

```bash
# venv (Windows)
.venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt

# tests
pytest                          # full suite (asyncio_mode = auto)
pytest app/tests/endpoints/test_claim_alert.py   # single file

# lint
ruff check .                    # 205 pre-existing violations — informational in CI until cleaned

# migrations
alembic upgrade head
alembic revision --autogenerate -m "..."   # then verify the revision id doesn't collide with existing versions

# run locally
uvicorn app.main:app --reload   # needs .env (copy .env.example; tests only need the keys present)
```

## Test conventions

- `asyncio_mode = auto` in `pytest.ini` — plain `async def` tests, no marker needed.
- Endpoint tests override `get_db` + auth dependencies and use `TestClient(app)` **without** a context manager — a `with` block runs app lifespan, which connects to Postgres.
- `Settings()` requires every `.env.example` key present — tests satisfy it with `ENV_STATE=test` + dummy values, no real services needed.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues on `PSergio984/agos-backend-v2`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at repo root. See `docs/agents/domain.md`.
