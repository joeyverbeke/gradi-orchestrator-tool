# Repository Guidelines

## Project Structure & Module Organization
- `orchestrator/` — Python watchdog that coordinates vLLM, Kokoro, and each `gradi-*` art piece. Key files: `orchestrator.py`, `services.toml`, `requirements.txt`, logs under `logs/`.
- `0_vllm`, `1_tts`, `2_gradi-mediate`, `3_gradi-calibrate`, `4_gradi-predict`, `5_gradi-compress` — standalone projects (Python or Node) with their own virtualenvs and READMEs; the orchestrator shells into these directories instead of duplicating code.
- `.uv-cache/`, `orchestrator/.venv/`, and service logs are ignored; recreate local environments as needed.

## Build, Test, and Development Commands
- `source orchestrator/.venv/bin/activate && python3 orchestrator/orchestrator.py` — run the watchdog with production defaults; add `--testing` to flip Calibrate/Compress into lab mode.
- `uv venv orchestrator/.venv && uv pip install -r orchestrator/requirements.txt` — bootstrap dependencies (primarily `pyudev`).
- Service-specific commands live in each project README (e.g., `uv run scripts/session_controller.py …` for Mediation, `npm start` for Compress).

## Coding Style & Naming Conventions
- Python: follow PEP 8, 4-space indents, use `typing` annotations, prefer `asyncio` primitives already in use. Use `uv` for dependency pinning; keep new modules listed in `requirements.txt`.
- Node (Gradi Compress): ES modules with standard eslint/prettier defaults; respect existing logging tags like `[CTRL]`.
- Config files use TOML (`services.toml`) and should keep snake_case keys.

## Testing Guidelines
- No unified test harness yet; validate orchestrator changes with `python -m py_compile orchestrator/orchestrator.py` plus manual smoke tests (device disconnect/reconnect, `--testing` flag).
- For service repos, follow their documented test commands (e.g., `npm test`, `uv run pytest`). When editing those projects, update their READMEs if commands change.

## Commit & Pull Request Guidelines
- Write descriptive commits in imperative mood (`Add pyudev device watcher`, `Fix Kokoro working_dir`). Group related changes; avoid mixing orchestrator edits with service code unless necessary.
- PRs should summarize the change, list test evidence (`python -m py_compile …`, manual steps), and reference any applicable issues. Include screenshots/log excerpts when touching UI or watchdog output.

## Security & Configuration Tips
- Never commit credentials or `.env` files; copy from the relevant sample templates.
- The orchestrator assumes `/dev/gradi-*` symlinks exist; if hardware names change, update `services.toml` rather than patching code.
