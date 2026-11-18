# Project Instructions

Prefer simple, direct solutions. Do not over-engineer.

## Scope

* Make only changes required by the current task.
* Do not refactor, rename, reformat, or clean up unrelated code.
* Follow the existing project structure and conventions.
* Stop and report when the requested change becomes ambiguous, risky, or materially expands in scope.

## Architecture and Ownership

This is an in-process AstrBot platform adapter, loaded as a Python package. The external `agent-wechat-native` v2.7.1 daemon owns the WeChat protocol and login state. This plugin uses its `/v1` HTTP/SSE API. Keep package-relative imports and use AstrBot's existing lifecycle, provider, Plugin KV, and message APIs.

Read the relevant modules before changing behavior:

| Area | Owning modules |
| --- | --- |
| Plugin registration, commands, LLM tool declarations, web route wiring | `main.py` |
| SSE consumption, admission rules, media tasks, reply orchestration, shutdown | `adapter.py` |
| AstrBot message conversion, session IDs, component serialization | `event.py` |
| Native HTTP/SSE, media validation/download, operation and send receipts | `native_client.py` |
| Configuration defaults, form metadata, validation, credential loading | `config.py` |
| SQLite journal, cursor, recalls, send/action/sticker intents, statistics | `state.py` |
| Trusted tool context and safe JSON results | `tool_support.py` |
| Chat reads, mutations, explicit mentions | `history_tools.py`, `wechat_actions.py`, `mention_tools.py` |
| Sticker lookup, acquisition, sending, admin sync commands | `sticker_service.py`, `sticker_tools.py`, `sticker_commands.py` |
| Summary snapshots, daily ranking, loop protection | `summary.py`, `group_ranking.py`, `loop_guard.py` |
| Connection checks, login sessions, monitoring, alert binding/delivery | `connection_probe.py`, `login_session.py`, `connection_monitor.py`, `alert_binding.py`, `alert_sender.py` |
| Dashboard backend and static Plugin View | `connection_view.py`, `views/connection/` |

Keep orchestration in the entry points and put protocol, persistence, and feature logic in their existing owners. Avoid adding more business logic to `main.py` or `adapter.py` when a feature module already owns it.

For flow and operational context, consult `docs/architecture.md` and `docs/operations.md`; for user-visible behavior, consult `README.md` and `docs/features.md`. Confirm details against current source and tests: parts of the architecture document predate image/voice sending, stickers, and panel login. Consult `GLOSSARY.md` for established terms and the relevant `docs/SPEC-*` for feature requirements. Existing `docs/superpowers/` files are historical context, not workflow instructions; place new task documents under `docs/`.

## Behavioral Constraints

* Preserve the journal's account/server/instance isolation, monotonic cursor, transactional message acceptance, recall tombstones, and crash recovery. A changed server identity must not silently reset the cursor.
* Record mutation/send intent before submission. For an uncertain result, inspect the original idempotency key and receipt; preserve `needs_review` or `partial` instead of creating a fresh submission. Native `accepted` confirms acceptance, not final delivery to WeChat.
* Recheck source and quoted-message identity and recall status at the send boundary, including after asynchronous media work. Propagate recalls to active events, pending media tasks, and summary snapshots.
* Distinguish real mentions from AstrBot wake markers. A real @ requires raw Native `mentions.state == "known"` and `mentions.self is True`; an `At` component or typed `@name` is insufficient. Preserve whitelist, reply-mode, and session-isolation rules.
* Keep `client_self_id` for AstrBot's platform instance/task identity and `bot_self_id` for the WeChat account identity.
* Derive tool account, thread, sender, and authorization from the trusted event via `tool_support.context`. Keep tool parameters scoped to the current chat, preserve ownership checks across plugin reloads, and return the existing safe JSON result contract. Group administration uses the separate admin client; normal read/send credentials must not substitute for it.
* Connection checks and panel statistics must stay read-only. Use `probe_connection` and `read_statistics`; never instantiate an adapter or `StateStore` for inspection, because construction runs crash recovery. Keep login behind verified Dashboard administrator authentication and alert binding behind administrator private-chat checks. Login/alert state uses Plugin KV, separate from the message journal.
* Route chat sends through the existing loop gate, send lock, journal, and receipt flow. Keep loop cooldown silent. Use `track_send_task` from `adapter.py` for send/tool task ownership across nested calls; feature modules must not directly modify `_send_tasks`. Cancel/await background tasks before closing HTTP clients or SQLite during shutdown.
* Keep credentials, chat bodies, raw upstream responses, and sensitive URLs out of logs and the message journal. Use existing sanitized error codes/results. Preserve media size/type checks, public-download address restrictions, and Native HTTP redirect/proxy protections.

## Skills

Use [mattpocock/skills](https://github.com/mattpocock/skills) as this project's engineering workflow, replacing Superpowers. Do not invoke `superpowers:*` skills for this project.

Read the relevant installed `mattpocock-skills:*` skill before applying it. Select skills by the current task:

* `diagnosing-bugs`: diagnose bugs, failures, and performance regressions.
* `tdd`: implement features or fixes with a test-first workflow.
* `codebase-design`: design module interfaces and boundaries.
* `domain-modeling`: clarify terminology or update `GLOSSARY.md` and ADRs.
* `prototype`: explore a design question with a throwaway prototype.
* `code-review`: review changes against project standards and requirements.
* `research`: investigate technical questions using primary sources.
* `pr`: write pull request descriptions.
* `grilling`: stress-test a plan or decision when the user requests an interview.
* `writing-for-agents`: create or update agent instructions and skills.

Apply only the skills relevant to the task. If a required skill is unavailable, report the missing skill instead of silently substituting Superpowers.

## Verification

Run the narrowest meaningful verification available.

Use the project's Python environment with dependencies from `requirements-dev.txt`; tests use the real pinned AstrBot SDK and mock Native HTTP/SSE, without WeChat credentials. `tests/conftest.py` loads modules as the synthetic `wechat_plugin` package; reuse its `module()` helper and `serve` fixture.

* Run `python -m pytest tests/test_<area>.py -q` for the changed behavior and `python -m ruff check <changed_python_files>` for Python edits. Settings live in `pyproject.toml`.
* For send, journal, or admission changes, include the relevant cases in `tests/test_adapter.py`, `tests/test_config_state.py`, `tests/test_native_client.py`, `tests/test_quotes.py`, and `tests/test_loop_guard.py`; add the affected media/sticker tests when applicable.
* For tool changes, include `tests/test_tool_contract.py` and the affected tool tests. Preserve registry schemas, current-chat scope, disabled-feature behavior, and permission checks.
* For panel/login/alert changes, select the relevant `tests/test_connection_*.py` and `tests/test_panel_statistics.py`. Static panel behavior is exercised by `tests/test_connection_ui.py` in Playwright Chromium; this is an AstrBot Plugin View, without a separate frontend build.
* For cross-cutting changes, use the checks in `.github/workflows/tests.yml`: `python -m ruff check .` and `python -m pytest --cov=. --cov-report=term-missing`, plus the workflow's module compilation check. Chromium must be installed for browser tests.
* For documentation-only changes, check referenced paths and the diff; application tests are unnecessary. Distinguish mock-based verification from real-account acceptance, and report missing dependencies or browsers explicitly.

Do not claim that a change works unless it was verified. Clearly state when verification could not be performed.

## Code Discovery

Use `codebase-memory-mcp` when it is already available.

Preferred order:

1. `search_graph`
2. `trace_path`
3. `get_code_snippet`
4. `query_graph`
5. `get_architecture`

Use normal repository tools when:

* `codebase-memory-mcp` is unavailable
* Searching configs, scripts, documentation, strings, or error messages
* Graph results are insufficient

Do not install, initialize, rebuild, or configure `codebase-memory-mcp`.
