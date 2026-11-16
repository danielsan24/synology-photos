# Contributing: adding an API call

Synology does not document the Photos API. A call is added only after it was seen in the web app; a guessed write
is never sent to a NAS "to see what happens". This is maintainer work on a test library, never part of a user's
task (SKILL.md rule 3).

Never add a call that deletes, trashes or overwrites photos. The guards (`ALLOWED_CALLS`, `FORBIDDEN_METHOD_RE`,
`ALLOWED_DESPITE_RE`, `ARMED_CALLS`, `check_params`) are pinned by `test_guard_sets_are_pinned`: changing one is a
deliberate change to that test too, and needs a reviewer.

1. Open Synology Photos in Chrome on a **test** library and keep the tab open.
2. Inject `scripts/capture_logger.js` into that tab (DevTools console, or a browser automation tool). It records
   every webapi request with session ids, tokens, passwords, share passphrases and links removed from requests and
   responses, and does not record logins.
3. Perform exactly one action in the UI (for example "set 3 stars on photo X").
4. Read `window.__synoCaptures` (the read-back line is at the top of the script). **Review every line before
   saving it**: replace user names, user ids, real names of people, album and folder names, file names and item
   ids with neutral placeholders. Save it as `references/captures/<n>-<action>.json`.
5. Update `references/api.md`, implement the call in `scripts/synophotos.py`, add its (api, method) to
   `ALLOWED_CALLS` and its command to `CONFIRMED_WRITES`, add a test, and run the tests:

```bash
cd scripts
uv run --with pytest --with requests --with pyyaml --with dnspython --with keyring --with pillow pytest -q test_synophotos.py
```

6. Before a release, run `scripts/live_tests.sh` on a test library (it reverts its changes and writes its log to
   `$TMPDIR`, outside the repository).

Commits run gitleaks and a 2 MB file limit as pre-commit hooks (`pre-commit install`).
