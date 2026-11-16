# Setup (once per NAS)

Requirements: DSM 7 with Synology Photos; the Shared Space enabled if you use `--space team` (the default).
**Accounts with 2FA are not supported**: the tool logs in with a password only and stops on any 2FA prompt.

## 1. DSM account

Use a **dedicated account** for the skill, not your own: the albums it creates belong to that account, and
`album-cleanup` only ever touches albums the tool recorded when creating them.

1. DSM > Control Panel > User: create `photos-agent`, not in `administrators`, without 2FA, strong password.
2. Applications: allow Synology Photos only.
3. Synology Photos > Settings > Shared Space > permissions: give `photos-agent` "Full access" (needed to
   edit tags, ratings and people on Shared Space photos). Do not grant Photos admin rights.
4. Albums are created in the account's personal space. To see them in your own Photos, list your own DSM user in
   `album_members` in the config (every new album is shared with it, view or download) or open the share link.

## 2. Keychain (in a terminal, never from Claude)

```bash
security add-generic-password -s synology-photos -a photos-agent -w
```

Then set `account: photos-agent` and `keychain_service: synology-photos` in
`~/.config/synology-photos/config.yaml`.

## 3. Config `~/.config/synology-photos/config.yaml`

Copy `references/config.example.yaml` there and edit it:

```yaml
public_host: mynas.synology.me   # DDNS / public name; must match the DSM certificate
lan_host: 192.168.1.10           # optional, tried first (1.5 s TCP probe)
nas_port: 5001
ca_bundle: system                # or the path to an exported DSM certificate (.pem)
account: photos-agent
keychain_service: synology-photos
default_space: team
album_members: [{user: <your DSM user>, role: download}]
```

The other keys (`sync_exclude_folders`, `share_expiry_days`, `share_max_days`, `album_max_age_days`) are in
`references/config.example.yaml`. Environment overrides: `SYNOPHOTOS_CONFIG` (config file), `SYNOPHOTOS_CACHE`
(discovery cache file), `SYNOPHOTOS_STATE` (journal and album registry folder, default
`~/.local/state/synology-photos`).

**A NAS without a DDNS name** (LAN only): set `public_host` to the name in the NAS certificate, `lan_host` to its
IP, and `ca_bundle` to the certificate exported from DSM (Control Panel > Security > Certificate > Export) as a
`.pem` file. The public fallback then simply fails when away from home.

## 4. Network

- The client connects to the LAN IP when it answers, otherwise to the public IP resolved through public DNS
  (1.1.1.1, 8.8.8.8). Both ways the TLS certificate is checked against `public_host`, so no `/etc/hosts`
  entry is needed (an `/etc/hosts` override for the public name is ignored).
- From inside the LAN the public IP does not answer (no NAT hairpin), so `--endpoint public` only works away from home.
- DSM Auto Block bans an IP after repeated failed logins. The script logs in once per command and never
  retries; on error 407 stop and unblock in DSM > Security > Protection.

## 5. Running from Claude Code

The Claude Code auto-mode classifier may block a script that reads the NAS password from the Keychain.
Either allow it once when prompted, add a permission rule for
`Bash(uv run <skill dir>/scripts/synophotos.py:*)`, or run the commands in a terminal and paste the output.

## Error codes

| Code | Meaning |
|---|---|
| 105 | account lacks the right for this action (e.g. writes with a read-only account) |
| 119 | session invalid: log in again |
| 120 | invalid parameter (e.g. Shared Space `list` without `folder_id`) |
| 400 / 402 / 407 | login: wrong password / Photos not allowed or wrong session / Auto Block |
| 801 / 803 | not found / no permission; 801 also comes and goes while Photos is indexing (reads retry) |
