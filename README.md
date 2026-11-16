# synology-photos (Claude Code skill)

A Claude Code skill and CLI to read and curate a Synology Photos library on a Synology NAS: search, download
(original or compressed), a local mirror for other tools, people and face fixes, tags, ratings, albums (also
from rules), sharing by link or with DSM users, folder rearrangement and uploads.

**It never deletes a photo.** "Delete" becomes the **Trash candidate** album for a human to review. The CLI sends
only an allow-list of API calls; the two that remove something (an empty folder, an album this tool created) are
sent only right after their own checks. Every write is a dry run until `--yes` and is journaled first.

> Not affiliated with or endorsed by Synology. The Synology Photos web API is undocumented: every call here was
> confirmed from captures of the web app (DSM 7 with the Synology Photos version current in October-November 2026) and may change
> with a DSM update. Use at your own risk, on a library you have backed up.

**Rating and tag edits rewrite original files** (Synology Photos writes them into JPEG, PNG and CR2 metadata), so
anything that tracks photos by file hash sees them as changed.

## Contents

- `SKILL.md`: what Claude reads (rules, commands, flows)
- `scripts/synophotos.py`: the CLI (`uv run scripts/synophotos.py --help`), with `synophotos.py.lock` pinning its dependencies
- `scripts/test_synophotos.py`: offline tests
- `references/setup.md`: account, keyring and config setup, error codes; `references/api.md`: the confirmed API calls
- `references/captures/`: the web-app captures behind each call (ids and names replaced)
- `CONTRIBUTING.md`: how a new API call is captured and added (maintainers)

## Requirements

- A Synology NAS with DSM 7 and a current Synology Photos; the Shared Space enabled for `--space team` (the
  default), or use `--space personal`.
- A DSM account **without 2FA** (2FA accounts are not supported), allowed to use Synology Photos only.
- macOS with the Keychain (the password is read through Python `keyring`; other keyring backends may work but
  are untested), [uv](https://docs.astral.sh/uv/) and Python 3.11+.
- HTTPS to the NAS with a certificate that matches `public_host` (a DDNS name, or the certificate name of a
  LAN-only NAS with `ca_bundle` pointing at its exported certificate).

## Install

```bash
git clone <this repository> ~/.claude/skills/synology-photos
cd ~/.claude/skills/synology-photos
mkdir -p ~/.config/synology-photos && cp references/config.example.yaml ~/.config/synology-photos/config.yaml
```

Then follow `references/setup.md` (DSM account, keyring item, config) and run
`uv run scripts/synophotos.py login-test`.

The companion skill `photo-picker` reads the local mirror this skill writes (`sync`); it is optional.

## Licence

MIT, see `LICENSE`.
