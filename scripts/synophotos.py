#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "urllib3>=2", "keyring>=25", "pyyaml>=6", "dnspython>=2.6", "pillow>=10"]
# ///
"""Synology Photos CLI for the synology-photos skill.

Reads freely; writes need --yes. Deleting photos is not implemented and is refused at the transport
layer: "delete" requests go to the `Trash candidate` album for a human to review.
Credentials come only from the system keyring (macOS Keychain) and travel only in POST bodies; they are never printed.
"""


from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import secrets as pysecrets
import socket
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import requests

__version__ = "0.1.0"
log = logging.getLogger("synophotos")

CONFIG_PATH = Path(os.environ.get("SYNOPHOTOS_CONFIG", "~/.config/synology-photos/config.yaml")).expanduser()
CACHE_PATH = Path(os.environ.get("SYNOPHOTOS_CACHE", "~/.cache/synology-photos/discovery.json")).expanduser()
# The undo journal and the list of albums this tool created are state, not cache: kept where cache cleaners do not look.
STATE_DIR = Path(os.environ.get("SYNOPHOTOS_STATE", "~/.local/state/synology-photos")).expanduser()
CACHE_MAX_AGE_S = 7 * 24 * 3600
TRASH_ALBUM = "Trash candidate"
PAGE = 500
AUTH_SESSION = "webui"  # session=SynologyPhotos returned DSM 402
ITEM_V, FOLDER_V, PERSON_V, THUMB_V, DOWNLOAD_V = 7, 2, 1, 2, 2

# ---------------------------------------------------------------------------------------------
# Safety: methods this tool must never send. Checked on every request, before anything leaves the machine.
FORBIDDEN_METHOD_RE = re.compile(r"(?:^|_)(delete|purge|empty|move|copy|destroy|wipe|trash)(?:_|$)", re.I)
# Exact exceptions, each confirmed by a web-app capture (2026-10-31) to leave every photo in the library:
# - NormalAlbum.delete_item removes photos from an album only;
# - Person.delete_face unlinks a person from one face of a photo (the photo is untouched).
ALLOWED_DESPITE_RE = {
    ("SYNO.Foto.Browse.NormalAlbum", "delete_item"),
    # Captures 12 and 13 (2026-11-16), Shared Space only. File.move relocates photos (or folders) between folders.
    # File.delete deletes photos too when item_id is filled: it passes only with item_id [] and one folder id
    # (check_params), and `folder-remove` checks right before that the folder holds no photos and no subfolders.
    ("SYNO.FotoTeam.BackgroundTask.File", "move"),
    ("SYNO.FotoTeam.BackgroundTask.File", "delete"),
    # Capture 18: Album.delete removes one album; Photos keeps its photos. Sent only for one album the skill's
    # account owns (check_params, remove_album).
    ("SYNO.Foto.Browse.Album", "delete"),
    ("SYNO.FotoTeam.Browse.Person", "delete_face"),
    ("SYNO.Foto.Browse.Person", "delete_face"),
}
FORBIDDEN_API_PREFIXES = ("SYNO.Core.", "SYNO.FileStation.", "SYNO.Foto.Setting.", "SYNO.FotoTeam.Sharing.Folder")

# Allow-list: the only (api, method) pairs this tool sends. "SYNO.FotoTeam." (Shared Space) is checked as "SYNO.Foto.".
# Anything else is refused before it leaves the machine, whatever its name. Never widen it for a delete, trash or
# overwrite action: deleting photos is reserved to a human.
ALLOWED_CALLS: dict[str, frozenset[str]] = {
    "SYNO.API.Auth": frozenset({"login", "logout"}),
    "SYNO.API.Info": frozenset({"query"}),
    "SYNO.Foto.Index": frozenset({"get"}),
    "SYNO.Foto.UserInfo": frozenset({"me"}),
    "SYNO.Foto.Browse.Item": frozenset({"list", "list_with_filter", "count", "count_with_filter", "get", "list_face",
                                        "set", "add_tag", "remove_tag"}),
    "SYNO.Foto.Browse.Folder": frozenset({"list", "get", "create", "rename"}),
    "SYNO.Foto.Browse.Person": frozenset({"list", "set", "merge", "add_face", "delete_face"}),
    "SYNO.Foto.Browse.GeneralTag": frozenset({"list", "create"}),
    "SYNO.Foto.Browse.Album": frozenset({"list", "get", "set_name", "delete"}),
    "SYNO.Foto.Browse.NormalAlbum": frozenset({"create", "add_item", "delete_item"}),
    "SYNO.Foto.Browse.ConditionAlbum": frozenset({"create", "peek_item_count"}),
    "SYNO.Foto.Search.Search": frozenset({"list_item"}),
    "SYNO.Foto.Thumbnail": frozenset({"get"}),
    "SYNO.Foto.Download": frozenset({"download"}),
    "SYNO.Foto.Upload.Item": frozenset({"upload_to_folder"}),
    "SYNO.Foto.BackgroundTask.Info": frozenset({"get_status"}),
    "SYNO.Foto.BackgroundTask.File": frozenset({"move", "delete"}),
    "SYNO.Foto.Sharing.Passphrase": frozenset({"set_shared", "update"}),
    "SYNO.Foto.Sharing.Misc": frozenset({"list_user_group"}),
}
# Personal-space-only and Shared-Space-only pairs (the folder tasks were captured in the Shared Space only).
TEAM_ONLY = {("SYNO.Foto.BackgroundTask.File", "move"), ("SYNO.Foto.BackgroundTask.File", "delete")}
# Calls that remove something (an empty folder, one album): the transport sends one only when the function that
# checked it armed exactly that id a moment before (SynoClient.arm). A direct call is refused.
ARMED_CALLS = {("SYNO.FotoTeam.BackgroundTask.File", "delete"): "folder_id", ("SYNO.Foto.Browse.Album", "delete"): "id"}
# The exact parameters each armed call is sent with (captures 13 and 18): nothing else may ride along.
ARMED_PARAMS = {("SYNO.FotoTeam.BackgroundTask.File", "delete"): {"item_id", "folder_id"},
                ("SYNO.Foto.Browse.Album", "delete"): {"id"}}
ARM_TTL_S = 30
ROOT_FOLDER_ID = 1  # the Shared Space root; never removed, renamed or moved

LOGIN_ERRORS = {
    400: "wrong username or password",
    401: "account disabled",
    402: "permission denied (is the account allowed to use Synology Photos?)",
    403: "2FA code required: this tool supports accounts without 2FA only",
    404: "2FA code rejected",
    406: "2FA enforced for this account",
    407: "IP blocked by DSM Auto Block: stop, wait or unblock in DSM before retrying",
    408: "password expired; change it in DSM",
    409: "password expired; change it in DSM",
    410: "password must be changed in DSM",
}
COMMON_ERRORS = {
    100: "unknown error", 101: "bad parameter / no such API", 102: "API does not exist", 103: "method does not exist",
    104: "version not supported", 105: "permission denied (account lacks rights for this action)",
    106: "session timeout", 107: "session interrupted by duplicate login", 119: "invalid session / SID not found",
    120: "invalid parameter", 801: "item or album not found", 803: "no permission", 805: "already exists",
}


class ApiError(RuntimeError):
    def __init__(self, api: str, method: str, code: Any, detail: Any = None):
        self.code, self.detail = code, detail
        hint = COMMON_ERRORS.get(code, "") if isinstance(code, int) else ""
        super().__init__(f"{api}.{method} failed: DSM error {code}" + (f" ({hint})" if hint else ""))


class Refused(RuntimeError):
    pass


# --- redaction -------------------------------------------------------------------------------
_secrets: set[str] = set()
_KEY_RE = re.compile(r"(passwd|password|passphrase|_sid|sid|synotoken|SynoToken|did|device_id|otp_code|sharing_link)"
                     r"([\"']?\s*[:=]\s*[\\\"']*)[^\"'&\s,;}\\]+")


def register_secret(v: str | None) -> None:
    if v and len(v) >= 3:  # shorter values are masked by their key name only (_KEY_RE), not everywhere
        _secrets.add(v)


def redact(text: str) -> str:
    text = _KEY_RE.sub(r"\1\2***", text)
    for s in sorted(_secrets, key=len, reverse=True):
        text = text.replace(s, "***")
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), None
        return True


def setup_logging(verbose: bool) -> None:
    h = logging.StreamHandler(sys.stderr)
    h.addFilter(_RedactFilter())
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[h])
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --- config ----------------------------------------------------------------------------------
def load_config() -> dict:
    import yaml

    if not CONFIG_PATH.is_file():
        raise SystemExit(f"missing config {CONFIG_PATH}: see references/setup.md")
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    for k in ("public_host", "nas_port", "account", "keychain_service"):
        if not cfg.get(k):
            raise SystemExit(f"config {CONFIG_PATH}: '{k}' is required")
    cfg.setdefault("lan_host", "")
    cfg.setdefault("ca_bundle", "system")
    cfg.setdefault("default_space", "team")
    return cfg


def get_password(cfg: dict) -> str:
    import keyring

    v = keyring.get_password(cfg["keychain_service"], cfg["account"])
    if not v:
        raise SystemExit(
            f"no Keychain item '{cfg['keychain_service']}' for account '{cfg['account']}'. The owner runs, in Terminal:\n"
            f"  security add-generic-password -s {cfg['keychain_service']} -a {cfg['account']} -w"
        )
    register_secret(v)
    return v


# --- endpoint selection ----------------------------------------------------------------------
def tcp_ok(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_public(name: str) -> str:
    """Resolve through public DNS so an /etc/hosts LAN override does not break access from outside."""
    import dns.resolver

    r = dns.resolver.Resolver(configure=False)
    r.nameservers = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
    r.lifetime = 4
    try:
        return r.resolve(name, "A")[0].to_text()
    except Exception:
        return socket.gethostbyname(name)  # system resolver as a last resort


def choose_endpoint(cfg: dict, force: str | None = None) -> tuple[str, str]:
    """Returns (kind, ip). The TLS certificate is always validated against public_host."""
    port = int(cfg["nas_port"])
    lan = cfg.get("lan_host")
    if force in (None, "auto", "lan") and lan and tcp_ok(lan, port):
        return "lan", lan
    if force == "lan":
        raise SystemExit(f"LAN address {lan}:{port} not reachable")
    ip = resolve_public(cfg["public_host"])
    if not tcp_ok(ip, port, timeout=5):
        raise SystemExit(f"NAS not reachable: LAN {lan or '-'} and public {cfg['public_host']} ({ip}):{port}")
    return "public", ip


# --- client ----------------------------------------------------------------------------------
class PinnedHostAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, hostname: str, **kw: Any):
        self.hostname = hostname
        super().__init__(**kw)

    def init_poolmanager(self, *a: Any, **kw: Any) -> None:
        kw["server_hostname"] = self.hostname
        kw["assert_hostname"] = self.hostname
        super().init_poolmanager(*a, **kw)


class SynoClient:
    MIN_INTERVAL_S = 0.2  # about 5 requests per second

    def __init__(self, cfg: dict, endpoint: str | None = None, connect: bool = True):
        self.cfg = cfg
        self.session = requests.Session()
        # Connect to an IP but use SNI and hostname check = public_host, so the certificate is validated
        # against the DDNS name on both the LAN and the public address.
        self.session.mount("https://", PinnedHostAdapter(cfg["public_host"]))
        ca = cfg.get("ca_bundle", "system")
        self.session.verify = True if ca == "system" else str(Path(ca).expanduser())
        if self.session.verify is not True and not Path(self.session.verify).is_file():
            raise SystemExit("ca_bundle in config does not point to a file")
        self.sid: str | None = None
        self.synotoken: str | None = None
        self._armed: dict[tuple[str, str], tuple[int, float]] = {}
        self._last = 0.0
        self.kind, self.ip = choose_endpoint(cfg, endpoint) if connect else ("test", "127.0.0.1")
        self.base = f"https://{self.ip}:{cfg['nas_port']}/webapi"
        self.host_header = f"{cfg['public_host']}:{cfg['nas_port']}"

    # transport
    def _throttle(self) -> None:
        wait = self.MIN_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    @staticmethod
    def check_allowed(api: str, method: str) -> None:
        if api.startswith(FORBIDDEN_API_PREFIXES):
            raise Refused(f"{api} is outside this tool's scope")
        if FORBIDDEN_METHOD_RE.search(method) and (api, method) not in ALLOWED_DESPITE_RE:
            raise Refused(f"{api}.{method} is refused: this tool never deletes, moves or copies photos. "
                          f"Use `trash-candidate` to queue items for a human.")
        key = api.replace("SYNO.FotoTeam.", "SYNO.Foto.", 1)
        if method not in ALLOWED_CALLS.get(key, ()):
            raise Refused(f"{api}.{method} is not on this tool's allow-list")
        if (key, method) in TEAM_ONLY and not api.startswith("SYNO.FotoTeam."):
            raise Refused(f"{api}.{method}: confirmed for the Shared Space only")

    @staticmethod
    def check_params(api: str, method: str, params: dict) -> None:
        """Deleting photos is reserved to a human: the only delete call sent is the removal of one folder."""
        if api == "SYNO.Foto.Browse.Album" and method == "delete":
            ids = params.get("id")
            if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], int):
                raise Refused("Album.delete is sent for exactly one album id")
        if api.endswith("BackgroundTask.File") and method == "delete":
            items, folders = params.get("item_id"), params.get("folder_id")
            if items not in ([], "[]") or not isinstance(folders, list) or len(folders) != 1:
                raise Refused("BackgroundTask.File.delete is sent only as item_id [] with one folder id: "
                              "this tool never deletes photos")
            if not isinstance(folders[0], int) or folders[0] <= ROOT_FOLDER_ID:
                raise Refused("the root folder is never removed")
        if api.endswith("BackgroundTask.File") and method == "move":
            if params.get("action") != '"skip"':
                raise Refused('BackgroundTask.File.move is sent only with action "skip": a name clash never overwrites')
            fids = params.get("folder_id")
            if not isinstance(fids, list) or not all(type(f) is int for f in fids) or ROOT_FOLDER_ID in fids:
                raise Refused("BackgroundTask.File.move takes a list of integer folder ids, never the root")
        if api.endswith("Browse.Folder") and method == "rename" and (type(params.get("id")) is not int
                                                                     or params.get("id") <= ROOT_FOLDER_ID):
            raise Refused("Browse.Folder.rename takes one integer folder id, never the root")
        if api.endswith("Upload.Item") and params.get("duplicate") != '"ignore"':
            raise Refused('uploads are sent only with duplicate "ignore": an existing photo is never overwritten')

    def arm(self, api: str, method: str, target: int) -> None:
        """Allow one removal call (ARMED_CALLS) for exactly `target`, within ARM_TTL_S. Called by the function that
        has just checked the target (an empty folder, an album this tool created)."""
        self._armed[(api, method)] = (int(target), time.monotonic() + ARM_TTL_S)

    def check_armed(self, api: str, method: str, params: dict) -> None:
        key = ARMED_CALLS.get((api, method))
        if key is None:
            return
        tok = self._armed.pop((api, method), None)
        if set(params) != ARMED_PARAMS[(api, method)]:
            raise Refused(f"{api}.{method} is sent only with {sorted(ARMED_PARAMS[(api, method)])}")
        ids = params.get(key)
        target = ids[0] if isinstance(ids, list) and len(ids) == 1 else None
        if tok is None or tok[0] != target or time.monotonic() > tok[1]:
            raise Refused(f"{api}.{method} is sent only right after its own checks (folder-remove, album-remove)")

    def _post(self, api: str, method: str, version: int, params: dict, cgi: str = "entry.cgi",
              timeout: int = 60, files: dict | None = None) -> requests.Response:
        self.check_allowed(api, method)
        self.check_params(api, method, params)
        self.check_armed(api, method, params)
        self._throttle()
        data = {"api": api, "method": method, "version": str(version)}
        for k, v in params.items():
            if v is None:
                continue
            data[k] = v if isinstance(v, str) else json.dumps(v)
        if self.sid:
            data["_sid"] = self.sid
        headers = {"Host": self.host_header}
        if self.synotoken:
            headers["X-SYNO-TOKEN"] = self.synotoken
        log.debug("POST %s %s.%s v%s %s", cgi, api, method, version,
                  {k: v for k, v in data.items()
                   if k not in ("passwd", "password", "passphrase", "_sid", "api", "method", "version")})
        if files:  # multipart upload: the same fields, plus file parts (name, bytes, type), as the web app sends
            # DSM does not read _sid from a multipart body (error 119, live 2026-11-16): like the browser, the
            # session travels as the `id` cookie of this one request. Never in the URL, never printed.
            if self.sid:
                headers["Cookie"] = f"id={self.sid}"
            r = self.session.post(f"{self.base}/{cgi}", data=data, files=files, headers=headers, timeout=timeout,
                                  allow_redirects=False)
        else:
            r = self.session.post(f"{self.base}/{cgi}", data=data, headers=headers, timeout=timeout,
                                  allow_redirects=False)
        if 300 <= getattr(r, "status_code", 200) < 400:  # DSM's webapi never redirects; the session stays here
            raise Refused(f"{api}.{method}: the NAS answered with a redirect ({r.status_code}); not followed")
        r.raise_for_status()
        return r

    def ok_upload(self, api: str, method: str, version: int, files: dict, **params: Any) -> Any:
        """A multipart write (never retried)."""
        body = self._post(api, method, version, params, timeout=600, files=files).json()
        if body.get("success"):
            return body.get("data", {})
        err = body.get("error", {})
        raise ApiError(api, method, err.get("code"), err.get("errors"))

    def call(self, api: str, method: str, version: int, cgi: str = "entry.cgi", **params: Any) -> dict:
        return self._post(api, method, version, params, cgi).json()

    READ_RETRY = {"list", "list_with_filter", "count", "count_with_filter", "get", "list_face", "list_item"}
    RETRY_WAITS = (2, 5, 10)

    def ok(self, api: str, method: str, version: int, **params: Any) -> Any:
        """Reads that fail with 801 are retried with backoff: live test 2026-10-31 saw intermittent 801 on
        filters right after bulk edits and a merge (index busy); the same calls succeeded moments later.
        Writes are never retried."""
        for wait in (*self.RETRY_WAITS, None):
            body = self.call(api, method, version, **params)
            if body.get("success"):
                return body.get("data", {})
            err = body.get("error", {})
            if err.get("code") == 801 and method in self.READ_RETRY and wait is not None:
                log.warning("%s.%s: DSM 801, retrying in %ss (index: %s)", api, method, wait, self.index_status(api))
                time.sleep(wait)
                continue
            raise ApiError(api, method, err.get("code"), err.get("errors"))
        raise AssertionError("unreachable")

    def index_status(self, api: str = "SYNO.FotoTeam") -> Any:
        """Photos' indexing queues (basic, metadata, face_extraction, person_clustering, ...); all 0 = idle."""
        idx = "SYNO.FotoTeam.Index" if api.startswith("SYNO.FotoTeam") else "SYNO.Foto.Index"
        try:
            b = self.call(idx, "get", 1)
        except requests.RequestException:
            return "unknown"
        data = b.get("data") if b.get("success") else None
        if not isinstance(data, dict) or not data:  # no answer is not "idle": checks that need idle then refuse
            return "unknown"
        return {k: v for k, v in data.items() if v} or "idle"

    def fetch(self, api: str, method: str, version: int, **params: Any) -> bytes:
        """Binary endpoints (thumbnail, download). A JSON body means DSM returned an error."""
        r = self._post(api, method, version, params, timeout=600)
        if r.headers.get("Content-Type", "").startswith("application/json"):
            raise ApiError(api, method, r.json().get("error", {}).get("code"))
        return r.content

    def pages(self, api: str, method: str, version: int, limit: int | None = None, **params: Any) -> Iterator[dict]:
        offset, n = 0, 0
        while True:
            size = PAGE if limit is None else min(PAGE, limit - n)
            if size <= 0:
                return
            rows = self.ok(api, method, version, offset=offset, limit=size, **params).get("list", [])
            for r in rows:
                yield r
                n += 1
            if len(rows) < size:
                return
            offset += size

    # auth
    def login(self) -> None:
        """Exactly one attempt; DSM Auto Block punishes retries."""
        pw = get_password(self.cfg)
        params = {"account": self.cfg["account"], "passwd": pw, "session": AUTH_SESSION, "format": "sid",
                  "enable_syno_token": "yes"}
        try:
            body = self.call("SYNO.API.Auth", "login", 7, **params)
        except requests.exceptions.SSLError:
            raise SystemExit("TLS verification failed: public_host must match the DSM certificate")
        except requests.RequestException as e:
            raise SystemExit(f"NAS unreachable ({type(e).__name__})")
        finally:
            del pw, params
        if not body.get("success"):
            code = body.get("error", {}).get("code")
            raise SystemExit(f"login failed: {LOGIN_ERRORS.get(code, f'DSM error {code}')}")
        d = body["data"]
        self.sid, self.synotoken = d["sid"], d.get("synotoken")
        register_secret(self.sid)
        register_secret(self.synotoken)

    def logout(self) -> None:
        if self.sid:
            try:
                self.call("SYNO.API.Auth", "logout", 7, session=AUTH_SESSION)
            except requests.RequestException:
                pass
            finally:
                self.sid = self.synotoken = None

    def __enter__(self) -> "SynoClient":
        self.login()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.logout()


# --- api names per space ---------------------------------------------------------------------
def apis(space: str) -> dict[str, str]:
    p = "SYNO.FotoTeam" if space == "team" else "SYNO.Foto"
    return {
        "item": f"{p}.Browse.Item", "folder": f"{p}.Browse.Folder", "person": f"{p}.Browse.Person",
        "tag": f"{p}.Browse.GeneralTag", "thumb": f"{p}.Thumbnail", "download": f"{p}.Download",
        "search": f"{p}.Search.Search", "timeline": f"{p}.Browse.Timeline", "upload": f"{p}.Upload.Item",
        # Albums exist only in the personal API; they may contain Shared Space items.
        "album": "SYNO.Foto.Browse.Album", "normal_album": "SYNO.Foto.Browse.NormalAlbum",
        "album_item": "SYNO.Foto.Browse.Item",
        "passphrase": "SYNO.Foto.Sharing.Passphrase", "sharing": "SYNO.Foto.Sharing.Misc",
    }


ADDITIONAL = ["thumbnail", "resolution", "orientation", "exif", "gps", "person", "rating", "tag"]


# --- discovery cache -------------------------------------------------------------------------
def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(c: dict) -> None:
    private_dir(CACHE_PATH.parent)
    fd = os.open(CACHE_PATH.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:  # it holds album passphrases: readable by this user only
        f.write(json.dumps(c, indent=1, sort_keys=True, ensure_ascii=False))
    os.replace(CACHE_PATH.with_suffix(".tmp"), CACHE_PATH)


def _try(fn, *a, **kw) -> Any:
    try:
        return fn(*a, **kw)
    except (ApiError, requests.RequestException, KeyError) as e:
        return {"error": str(e)}


def _people_table(rows: list[dict]) -> dict:
    return {p.get("name") or f"#{p['id']}": {"id": p["id"], "items": p.get("item_count")} for p in rows}


def list_albums(c: "SynoClient") -> list[dict]:
    """Albums the account owns or that are shared with it (capture 2026-10-31: Album.list v4)."""
    return list(c.pages("SYNO.Foto.Browse.Album", "list", 4, category=js("normal_share_with_me"),
                        sort_by=js("create_time"), sort_direction=js("desc")))


def js(v: Any) -> str:
    """JSON-encode a value the way the Photos web app does (strings become \"quoted\"); keeps user text unambiguous."""
    return json.dumps(v, ensure_ascii=False)


def _album_table(rows: list[dict]) -> dict:
    return {a["name"]: {"id": a["id"], "items": a.get("item_count"), "shared": a.get("shared"),
                        "passphrase": a.get("passphrase") or None} for a in rows}


def discover(c: SynoClient, cfg: dict) -> dict:
    info = c.call("SYNO.API.Info", "query", 1, cgi="query.cgi", query="all").get("data", {})
    catalogue = {k: {f: v[f] for f in ("minVersion", "maxVersion", "requestFormat") if f in v}
                 for k, v in sorted(info.items()) if k.startswith(("SYNO.API.Auth", "SYNO.Foto.", "SYNO.FotoTeam."))}
    out: dict[str, Any] = {
        "discovered_at": int(time.time()), "account": cfg["account"], "endpoint": c.kind,
        "lan_host": cfg.get("lan_host"), "public_host": cfg["public_host"], "apis": catalogue,
        "whoami": _try(c.ok, "SYNO.Foto.UserInfo", "me", 1),
    }
    out["spaces"] = {}
    for space in ("team", "personal"):
        a = apis(space)
        s: dict[str, Any] = {"root_folder": _try(c.ok, a["folder"], "get", FOLDER_V)}
        ppl = _try(lambda: list(c.pages(a["person"], "list", PERSON_V)))
        s["people"] = _people_table(ppl) if isinstance(ppl, list) else ppl
        tags = _try(lambda: list(c.pages(a["tag"], "list", 2)))
        s["tags"] = {t["name"]: t["id"] for t in tags} if isinstance(tags, list) else tags
        out["spaces"][space] = s
    albums = _try(lambda: list_albums(c))
    out["albums"] = _album_table(albums) if isinstance(albums, list) else albums
    return out


def cache_fresh(cache: dict) -> bool:
    return bool(cache) and time.time() - cache.get("discovered_at", 0) < CACHE_MAX_AGE_S


class Resolver:
    """Name -> id lookups through the discovery cache; one refresh of a list on a miss."""

    def __init__(self, c: SynoClient, cfg: dict, space: str, cache: dict | None = None):
        self.c, self.cfg, self.space = c, cfg, space
        self.cache = cache if cache is not None else load_cache()
        if not cache_fresh(self.cache):
            log.info("discovery cache missing or stale: running discover")
            self.cache = discover(c, cfg)
            save_cache(self.cache)

    def _refresh(self, what: str) -> None:
        a = apis(self.space)
        if what == "people":
            self.cache["spaces"][self.space]["people"] = _people_table(list(self.c.pages(a["person"], "list", PERSON_V)))
        elif what == "tags":
            self.cache["spaces"][self.space]["tags"] = {t["name"]: t["id"] for t in self.c.pages(a["tag"], "list", 2)}
        elif what == "albums":
            self.cache["albums"] = _album_table(list_albums(self.c))
        save_cache(self.cache)

    def _table(self, what: str) -> dict:
        t = self.cache.get("albums") if what == "albums" else self.cache.get("spaces", {}).get(self.space, {}).get(what)
        return t if isinstance(t, dict) and "error" not in t else {}

    @staticmethod
    def _lookup(table: dict, key: str) -> int | None:
        """A name first (exact, then case-insensitive), so an album called "2024" is that album; then an id,
        written N or #N."""
        key = str(key)
        if key in table:
            v = table[key]
            return v["id"] if isinstance(v, dict) else v
        low = {k.lower(): k for k in table}
        if key.lower() in low:
            return Resolver._lookup(table, low[key.lower()])
        bare = key[1:] if key.startswith("#") else key
        return int(bare) if bare.isdigit() else None

    def name_of(self, what: str, vid: int) -> str:
        for name, v in self._table(what).items():
            if (v["id"] if isinstance(v, dict) else v) == vid:
                return name
        return "?"

    def get(self, what: str, key: str, required: bool = True) -> int | None:
        v = self._lookup(self._table(what), key)
        if v is None:
            self._refresh(what)
            v = self._lookup(self._table(what), key)
        if v is None and required:
            raise SystemExit(f"no {what[:-1]} named '{key}' in the {self.space} space")
        return v


# --- selection -------------------------------------------------------------------------------
def to_epoch(d: str, end: bool = False) -> int:
    """Synology stores capture time as local wall-clock seconds treated as UTC."""
    dt = datetime.fromisoformat(d)
    if end and len(d) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def item_date(it: dict) -> str:
    return datetime.fromtimestamp(int(it.get("time", 0)), tz=timezone.utc).strftime("%Y-%m-%d")


def build_filter(r: Resolver | None, args: argparse.Namespace) -> dict:
    """Parameters for Browse.Item list_with_filter / count_with_filter (shape in references/api.md)."""
    f: dict[str, Any] = {}
    if args.date_from or args.date_to:
        f["time"] = [{"start_time": to_epoch(args.date_from or "1970-01-01"),
                      "end_time": to_epoch(args.date_to or "2100-01-01", end=True)}]
    if args.person:
        f["person"] = [r.get("people", p) for p in args.person] if r else [int(p) for p in args.person]
        f["person_policy"] = "and" if getattr(args, "all_people", False) else "or"
    if args.tag:
        f["general_tag"] = [r.get("tags", t) for t in args.tag] if r else [int(t) for t in args.tag]
        f["general_tag_policy"] = "or"
    if args.rating is not None:
        f["rating"] = list(range(args.rating, 6))
    return f


def select_items(c: SynoClient, r: Resolver, args: argparse.Namespace, limit: int | None) -> list[dict]:
    a = apis(args.space)
    extra = {"additional": ADDITIONAL, "sort_by": "takentime", "sort_direction": "asc"}
    if getattr(args, "album", None):
        aid = r.get("albums", args.album)
        return list(c.pages(a["album_item"], "list", ITEM_V, limit, album_id=aid, **extra))
    if getattr(args, "keyword", None):
        return list(c.pages(a["search"], "list_item", 7, limit, keyword=args.keyword, additional=ADDITIONAL))
    f = build_filter(r, args)
    if getattr(args, "folder", None) is not None and not f:
        return list(c.pages(a["item"], "list", ITEM_V, limit, folder_id=args.folder, **extra))
    if not f:
        raise SystemExit("give a selection: --album, --folder, --keyword, --person, --from/--to, --tag or --rating")
    if getattr(args, "folder", None) is not None:
        f["folder"] = [args.folder]  # folder=[id] limits to items stored directly in that folder (live test)
    return list(c.pages(a["item"], "list_with_filter", ITEM_V, limit, **f, **extra))


def root_folder(r: Resolver, space: str) -> int:
    """Root folder id of a space (1 in the Shared Space)."""
    rf = (r.cache.get("spaces", {}).get(space, {}).get("root_folder") or {}) if r else {}
    return int((rf.get("folder") or {}).get("id", 1)) if isinstance(rf, dict) else 1


def get_items(c: SynoClient, space: str, ids: list[int]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(ids), 100):
        out += c.ok(apis(space)["item"], "get", ITEM_V, id=ids[i:i + 100], additional=ADDITIONAL).get("list", [])
    return out


# --- output ----------------------------------------------------------------------------------
def summarize(it: dict) -> dict:
    ad = it.get("additional") or {}
    res = ad.get("resolution") or {}
    tags = ad.get("tag")
    return {
        "id": it["id"], "date": item_date(it), "filename": it.get("filename"), "type": it.get("type"),
        "folder_id": it.get("folder_id"), "size": f"{res.get('width', '?')}x{res.get('height', '?')}",
        "rating": ad.get("rating"), "orientation": ad.get("orientation"),
        "orientation_original": ad.get("orientation_original"),
        "people": [p.get("name") or f"#{p['id']}" for p in ad.get("person") or []],
        "tags": [t.get("name") for t in tags] if isinstance(tags, list) else [],
    }


def emit(rows: Any, as_json: bool) -> None:
    if as_json or not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        print(json.dumps(rows, indent=1, ensure_ascii=False, default=str))
        return
    keys = list(rows[0])
    print("\t".join(keys))
    for r in rows:
        print("\t".join(", ".join(map(str, v)) if isinstance(v, list) else str(v) for v in (r.get(k) for k in keys)))


# --- downloads -------------------------------------------------------------------------------
def safe_name(n: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", n)[:100]


def write_atomic(path: Path, data: bytes) -> None:
    """Write next to the target, then rename: an interrupted download never leaves a truncated file behind."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def private_dir(d: Path) -> Path:
    """Create the folder readable by this user only. An existing folder keeps its permissions (it may be a folder
    the user pointed SYNOPHOTOS_CACHE or SYNOPHOTOS_STATE into); the files inside are 0600 either way."""
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            d.chmod(0o700)
        except OSError:
            pass
    return d


def append_private(path: Path, line: str) -> None:
    """Append one line to a file only this user can read (journal, album registry, trash log)."""
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line.rstrip("\n") + "\n")


MAX_ZIP_MEMBER = 4 << 30  # a still image above 4 GB is not a photo


def unzip_still(data: bytes) -> tuple[bytes, str | None]:
    """Live photos and multi-file units arrive as a zip; keep the still image."""
    if data[:4] != b"PK\x03\x04":
        return data, None
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.lower().endswith((".mov", ".mp4"))] or z.namelist()
        n = sorted(names)[0]
        if z.getinfo(n).file_size > MAX_ZIP_MEMBER:
            raise ApiError("download", "unzip", "member too large")
        return z.read(n), Path(n).name


def download_one(c: SynoClient, space: str, it: dict, size: str, out: Path) -> tuple[Path, bool]:
    """size=original: the full-size file. xl/m/sm: Synology's compressed JPEG previews (xl ~ 1920 px)."""
    a = apis(space)
    prefix = f"{item_date(it)}_{int(it['id'])}_"
    name = safe_name(it.get("filename") or str(it["id"]))
    if size == "original":
        existing = [p for p in out.glob(prefix + "*")
                    if not re.search(r"_(xl|m|sm)\.jpg$", p.name) and not p.name.endswith(".part")]
        if existing:
            return existing[0], False
        data, inner = unzip_still(c.fetch(a["download"], "download", DOWNLOAD_V, unit_id=[it["id"]],
                                          force_download="true"))
        path = out / (prefix + (safe_name(inner) if inner else name))
    else:
        path = out / f"{prefix}{Path(name).stem}_{size}.jpg"
        if path.exists():
            return path, False
        ck = ((it.get("additional") or {}).get("thumbnail") or {}).get("cache_key", "")
        data = c.fetch(a["thumb"], "get", THUMB_V, id=str(it["id"]), cache_key=str(ck), type="unit", size=size)
    write_atomic(path, data)
    return path, True


# --- sync: incremental local mirror of a space -----------------------------------------------
# Layout under --root (read by the photo-picker skill; nothing here depends on it):
#   thumbs/xl/<photo_id>.jpg            XL preview of every photo below the originals rating
#   originals/<year>/<date>_<photo_id>_<name>   original of every photo rated >= --originals-rating
#   cache/ingest_index.jsonl            append-only: item id -> local files (last line per item wins)
#   cache/library.jsonl                 one row per item: date, size, rating, people, tags (rewritten each sync)
#   cache/syno_faces.jsonl              append-only: item id -> faces with person and box (last line wins)
# photo_id is t<item id> in the Shared Space and p<item id> in a personal space.
SPACE_PREFIX = {"team": "t", "personal": "p"}
INDEX_NAME, LIBRARY_NAME, FACES_NAME = "ingest_index.jsonl", "library.jsonl", "syno_faces.jsonl"
SYNC_ADDITIONAL = ["thumbnail", "resolution", "orientation", "person", "rating", "tag"]
RAW_EXT = (".nef", ".cr2", ".cr3", ".arw", ".dng", ".raf", ".orf", ".rw2")


def read_jsonl(path: Path) -> Iterator[dict]:
    """Rows of an append-only file. A torn last line (the process was killed mid-write) is skipped with a warning."""
    if path.exists():
        with open(path) as f:
            lines = f.readlines()
        for k, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError:
                if k == len(lines) - 1 and not line.endswith("\n"):
                    log.warning("%s: ignoring an unfinished last line", path.name)
                    return
                raise


def dumps(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def append_jsonl(path: Path, row: dict) -> None:
    with open(path, "a") as f:
        f.write(dumps(row) + "\n")


def item_time_iso(it: dict) -> str:
    return datetime.fromtimestamp(int(it.get("time", 0)), tz=timezone.utc).replace(tzinfo=None).isoformat()


def exif_time_iso(data: bytes) -> str | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            v = im.getexif().get_ifd(0x8769).get(36867)  # DateTimeOriginal
        return datetime.strptime(v, "%Y:%m:%d %H:%M:%S").isoformat() if v else None
    except Exception:
        return None


def listing_fields(it: dict) -> dict:
    """Fields that change when Photos rewrites a file (rating, tag edits): the local copy is then flagged stale."""
    th = (it.get("additional") or {}).get("thumbnail") or {}
    return {"filesize": int(it["filesize"]) if it.get("filesize") is not None else None,
            "indexed_time": int(it["indexed_time"]) if it.get("indexed_time") is not None else None,
            "cache_key": str(th["cache_key"]) if th.get("cache_key") else None}


def people_sig(it: dict) -> str:
    """Named people of an item, as stored with its faces: a change means the faces must be read again."""
    ps = [p for p in (it.get("additional") or {}).get("person") or [] if p.get("name")]
    return ";".join(f"{p['id']}={p['name']}" for p in sorted(ps, key=lambda p: p["id"]))


def library_row(it: dict, space: str, run_id: str) -> dict:
    ad = it.get("additional") or {}
    res = ad.get("resolution") or {}
    tags = ad.get("tag")
    return {"id": it["id"], "photo_id": f"{SPACE_PREFIX[space]}{it['id']}", "space": space, "type": it.get("type"),
            "filename": it.get("filename"), "folder_id": it.get("folder_id"), "taken_at": item_time_iso(it),
            "width": int(res.get("width") or 0), "height": int(res.get("height") or 0),
            "rating": int(ad.get("rating") or 0),
            "people": sorted(p["name"] for p in ad.get("person") or [] if p.get("name")),
            "tags": sorted(t["name"] for t in tags if t.get("name")) if isinstance(tags, list) else [],
            **listing_fields(it), "seen": run_id}


def sync_action(it: dict, entry: dict | None, root: Path, originals_rating: int) -> str:
    """skip | thumb | original | upgrade (thumbnail on disk, now rated: add the original)."""
    rated = int((it.get("additional") or {}).get("rating") or 0) >= originals_rating
    raw = str(it.get("filename") or "").lower().endswith(RAW_EXT)
    if entry and (root / entry["file"]).exists():
        has_orig = entry["file"].startswith("originals") or (
            entry.get("original_file") and (root / entry["original_file"]).exists())
        return "upgrade" if rated and not has_orig else "skip"
    return "original" if rated and not raw else ("thumb+original" if rated else "thumb")


def sync_download(c: "SynoClient", space: str, it: dict, action: str, entry: dict | None, root: Path,
                  run_id: str) -> tuple[dict, int]:
    """Download what `action` asks and return the new index entry and the bytes written."""
    a = apis(space)
    sid = str(int(it["id"]))
    pid = (entry or {}).get("photo_id") or f"{SPACE_PREFIX[space]}{sid}"
    fields = listing_fields(it)
    n = 0
    e = dict(entry or {"syno_id": sid, "photo_id": pid})

    def original() -> tuple[bytes, str]:
        data, inner = unzip_still(c.fetch(a["download"], "download", DOWNLOAD_V, unit_id=[it["id"]],
                                          force_download="true"))
        taken = e.get("taken_at") or exif_time_iso(data) or item_time_iso(it)
        name = safe_name(inner or it.get("filename") or sid)[:80]
        rel = f"originals/{taken[:4]}/{taken[:10]}_{pid}_{name}"
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        write_atomic(root / rel, data)
        e["taken_at"] = taken
        return data, rel

    if action in ("thumb", "thumb+original"):
        ck = fields["cache_key"] or ""
        data = c.fetch(a["thumb"], "get", THUMB_V, id=sid, cache_key=ck, type="unit", size="xl")
        rel = f"thumbs/xl/{pid}.jpg"
        write_atomic(root / rel, data)
        n += len(data)
        e |= {"file": rel, "taken_at": item_time_iso(it), "sha16": sha16(data), **fields,
              "versions": [dict(fields, sha16=sha16(data), seen_run=run_id)]}
    if action == "original":
        data, rel = original()
        n += len(data)
        e |= {"file": rel, "sha16": sha16(data), **fields, "versions": [dict(fields, sha16=sha16(data), seen_run=run_id)]}
    if action in ("upgrade", "thumb+original"):
        data, rel = original()
        n += len(data)
        e |= {"original_file": rel, "original_sha16": sha16(data)}
    return e, n


def sha16(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()[:16]


def face_rows(faces: list[dict]) -> list[dict]:
    out = []
    for f in faces:
        b = f.get("face_bounding_box") or {}
        tl, br = b.get("top_left") or {}, b.get("bottom_right") or {}
        out.append({"face_id": f.get("face_id"), "person_id": f.get("person_id"), "name": f.get("name") or "",
                    "box": [round(float(v), 5) for v in (tl.get("x", 0), tl.get("y", 0), br.get("x", 0), br.get("y", 0))]})
    return out


def merge_library(old: dict[int, dict], rows: list[dict], lo: str, hi: str, run_id: str) -> list[dict]:
    """Rows of this listing replace the stored ones; a stored row inside the listed period that is no longer
    listed keeps its data and gets `missing_since` (nothing is ever dropped)."""
    cur = dict(old)
    listed = {r["id"] for r in rows}
    for r in rows:
        cur[r["id"]] = r
    for i, r in cur.items():
        if i not in listed and lo <= r["taken_at"][:10] <= hi and not r.get("missing_since"):
            cur[i] = dict(r, missing_since=run_id)
    return [cur[i] for i in sorted(cur)]


def _stamp(msg: str) -> None:
    print(f"[{time.strftime('%H:%M')}] {msg}", file=sys.stderr, flush=True)


def excluded_folders(c: SynoClient, space: str, ids: list[int]) -> set[int]:
    """The folders `sync` leaves out, with all their subfolders: photos the library itself produced (exports
    uploaded for sharing) must not come back as new photos."""
    out, queue = set(), [int(i) for i in ids]
    while queue:
        fid = queue.pop()
        if fid in out:
            continue
        out.add(fid)
        queue += [f["id"] for f in child_folders(c, space, fid)]
    return out


def cmd_sync(cfg: dict, args: argparse.Namespace) -> None:
    root = Path(args.root).expanduser()
    if (root / ".git").exists():
        raise SystemExit(f"{root} is a git repository: photos never go into one")
    for d in ("originals", "thumbs/xl", "cache"):
        (root / d).mkdir(parents=True, exist_ok=True)
    space, run_id = args.space, args.run_id or time.strftime("%Y-%m-%d-sync")
    index_path, lib_path, faces_path = (root / "cache" / n for n in (INDEX_NAME, LIBRARY_NAME, FACES_NAME))
    index = {str(r["syno_id"]): r for r in read_jsonl(index_path)}
    lo, hi = args.date_from or "1970-01-01", args.date_to or "2100-01-01"
    rep: dict[str, Any] = {"root": str(root), "space": space, "period": [lo, hi], "run_id": run_id}
    c = SynoClient(cfg, args.endpoint)
    log.info("endpoint: %s %s", c.kind, c.ip)
    c.login()
    try:
        t0 = time.monotonic()
        a = apis(space)
        listed = list(c.pages(a["item"], "list_with_filter", ITEM_V, None, additional=SYNC_ADDITIONAL,
                              time=[{"start_time": to_epoch(lo), "end_time": to_epoch(hi, end=True)}],
                              sort_by="takentime", sort_direction="asc"))
        items = [i for i in listed if i.get("type", "photo") in ("photo", "live")]
        skip_folders = excluded_folders(c, space, list(cfg.get("sync_exclude_folders") or []) + list(args.exclude_folder or []))
        if skip_folders:
            n0 = len(items)
            items = [i for i in items if i.get("folder_id") not in skip_folders]
            rep["excluded_items"] = n0 - len(items)
            _stamp(f"left out {n0 - len(items)} photos in {len(skip_folders)} excluded folder(s)")
        plan = [(it, sync_action(it, index.get(str(it["id"])), root, args.originals_rating)) for it in items]
        todo = [(it, act) for it, act in plan if act != "skip"]
        rep |= {"listed": len(listed), "photos": len(items), "list_s": round(time.monotonic() - t0),
                "to_download": {k: sum(1 for _, x in todo if x == k) for k in sorted({x for _, x in todo})}}
        _stamp(f"listed {len(listed)} items ({len(items)} photos) in {rep['list_s']}s; to download: {rep['to_download']}")
        if args.limit:
            todo = todo[:args.limit]
        t1, n_err, n_bytes, n_stale = time.monotonic(), 0, 0, 0
        for k, (it, act) in enumerate(todo, 1):
            sid = str(it["id"])
            try:
                entry, n = sync_download(c, space, it, act, index.get(sid), root, run_id)
            except (ApiError, OSError, requests.RequestException) as e:
                n_err += 1
                print(f"item {sid}: {redact(str(e))}", file=sys.stderr)
                if n_err >= 20 and n_err == k:
                    raise SystemExit("the first 20 downloads all failed; stopping")
                continue
            n_bytes += n
            index[sid] = entry
            append_jsonl(index_path, entry)
            if k % 200 == 0:
                rate = k / (time.monotonic() - t1)
                _stamp(f"download {k}/{len(todo)} at {rate:.1f}/s, about {round((len(todo) - k) / rate / 60)} min left")
        for it, act in plan:  # files rewritten on the NAS since the download: note the version, keep the local copy
            e = index.get(str(it["id"]))
            f = listing_fields(it)
            if act == "skip" and e and any(e.get(x) is not None and f[x] is not None and e[x] != f[x] for x in f):
                n_stale += 1
                e = dict(e, stale=True, **f)
                e["versions"] = (e.get("versions") or []) + [dict(f, seen_run=run_id)]
                index[str(it["id"])] = e
                append_jsonl(index_path, e)
        rep |= {"downloaded": len(todo) - n_err, "download_errors": n_err, "download_mb": round(n_bytes / 1e6, 1),
                "download_s": round(time.monotonic() - t1), "rewritten_on_nas": n_stale}
        if not args.limit:
            rows = merge_library({r["id"]: r for r in read_jsonl(lib_path)}, [library_row(i, space, run_id) for i in items],
                                 lo, hi, run_id)
            tmp = lib_path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(dumps(r) + "\n" for r in rows))
            tmp.replace(lib_path)
            rep |= {"library_rows": len(rows), "missing": sum(1 for r in rows if r.get("missing_since"))}
        if args.faces:
            have = {r["id"]: r.get("sig") for r in read_jsonl(faces_path)}
            want = [(it, people_sig(it)) for it in items]
            want = [(it, sig) for it, sig in want if (sig and have.get(it["id"]) != sig) or (not sig and have.get(it["id"]))]
            rep["faces_to_read"] = len(want)
            if args.limit:
                want = want[:args.limit]
            t2, f_err = time.monotonic(), 0
            for k, (it, sig) in enumerate(want, 1):
                try:
                    faces = face_rows(list_faces(c, space, it["id"])) if sig else []
                except (ApiError, requests.RequestException) as e:
                    f_err += 1
                    print(f"faces {it['id']}: {redact(str(e))}", file=sys.stderr)
                    if f_err >= 20 and f_err == k:
                        raise SystemExit("the first 20 face reads all failed; stopping")
                    continue
                append_jsonl(faces_path, {"id": it["id"], "sig": sig, "faces": faces})
                if k % 500 == 0:
                    rate = k / (time.monotonic() - t2)
                    _stamp(f"faces {k}/{len(want)} at {rate:.1f}/s, about {round((len(want) - k) / rate / 60)} min left")
            rep |= {"faces_read": len(want) - f_err, "face_errors": f_err, "faces_s": round(time.monotonic() - t2)}
        if args.limit:
            rep["trial"] = f"limit {args.limit}: library.jsonl not written"
    finally:
        c.logout()
    print(json.dumps(rep))



# --- writes ----------------------------------------------------------------------------------
# A write runs only after its request shape was confirmed by a capture of the Photos web app
# (references/captures/, summarised in references/api.md). Add the name here when confirmed.
CONFIRMED_WRITES: set[str] = {
    "rate", "tag-add", "tag-remove", "person-add", "person-remove", "album-add", "group-clean", "person-name",
    "album-create", "album-remove-item", "album-rename", "album-share", "album-unshare", "trash-candidate",
    "folder-create", "upload",  # captures 10 and 11, 2026-11-16
    "item-move", "folder-remove",  # captures 12 and 13, 2026-11-16 (Shared Space)
    "folder-rename", "folder-move",  # captures 14 and 15, 2026-11-16 (Shared Space)
    "album-rules",  # capture 17, 2026-11-16
    "album-remove",  # capture 18, 2026-11-16
}


def require_write(name: str, args: argparse.Namespace, plan: str, needs: tuple[str, ...] = ()) -> bool:
    print(f"plan: {plan}")
    missing = [n for n in (name, *needs) if n not in CONFIRMED_WRITES]
    if missing:
        raise SystemExit(f"{', '.join(missing)} not confirmed by a web-app capture yet (references/api.md); refusing")
    if not args.yes:
        print("dry run: add --yes to apply")
        return False
    return True


def album_items(c: SynoClient, aid: int) -> list[dict]:
    return list(c.pages(apis("personal")["album_item"], "list", ITEM_V, album_id=aid, additional=["thumbnail"]))


def album_get(c: SynoClient, aid: int) -> dict:
    rows = c.ok("SYNO.Foto.Browse.Album", "get", 4, id=[aid], additional=["sharing_info"]).get("list", [])
    if not rows:
        raise SystemExit(f"album {aid} not found")
    return rows[0]


def create_album(c: SynoClient, name: str, ids: list[int]) -> dict:
    """The web app creates an unnamed album with its first items, then names it (capture 2026-10-31)."""
    if not ids:
        raise SystemExit("an album needs at least one photo")
    data = c.ok("SYNO.Foto.Browse.NormalAlbum", "create", 1, name=js(""), item=ids[:100])
    aid = (data.get("album") or {}).get("id")
    if aid is None:  # the create response shape was not captured: find the newest unnamed album
        cand = [a for a in list_albums(c)[:10] if not a.get("name")]
        if not cand:
            raise SystemExit("album created but its id could not be found; check Synology Photos")
        aid = cand[0]["id"]
    c.ok("SYNO.Foto.Browse.Album", "set_name", 1, id=aid, name=js(name))
    if ids[100:]:
        add_to_album(c, aid, ids[100:])
    return {"id": aid, "name": name}


def add_to_album(c: SynoClient, aid: int, ids: list[int]) -> None:
    for i in range(0, len(ids), 100):
        c.ok("SYNO.Foto.Browse.NormalAlbum", "add_item", 1, id=aid, item=ids[i:i + 100])


def child_folders(c: SynoClient, space: str, parent: int) -> list[dict]:
    return list(c.pages(apis(space)["folder"], "list", FOLDER_V, id=parent, sort_by="filename", sort_direction="asc"))


def create_folder(c: SynoClient, space: str, parent: int, name: str) -> dict:
    """Browse.Folder create v1 {target_id, name: "quoted"}, as the web app sends it (capture 10, 2026-11-16).
    An existing folder of that name under the parent is returned instead of creating a second one."""
    if "/" in name or not name.strip():
        raise SystemExit("folder name: one non-empty path segment, no '/'")
    for f in child_folders(c, space, parent):
        if (f.get("name") or "").rstrip("/").split("/")[-1] == name:
            return {"id": f["id"], "name": f.get("name"), "existed": True}
    f = c.ok(apis(space)["folder"], "create", 1, target_id=parent, name=js(name)).get("folder") or {}
    return {"id": f.get("id"), "name": f.get("name"), "existed": False}


TASK_API = "SYNO.Foto.BackgroundTask.Info"
FILE_TASK_API = "SYNO.FotoTeam.BackgroundTask.File"


def wait_task(c: SynoClient, task: dict, timeout_s: int = 3600) -> dict:
    """Poll a background task (move, delete) until done, as the web app does (get_status, capture 12)."""
    tid, t0, info = task.get("id"), time.monotonic(), task
    while info.get("status") not in ("done", "error", "failed") and time.monotonic() - t0 < timeout_s:
        time.sleep(1 if time.monotonic() - t0 < 10 else 5)
        rows = c.ok(TASK_API, "get_status", 1, id=[tid]).get("list") or []
        info = rows[0] if rows else {"id": tid, "status": "unknown"}
    return info


def move_extra(source_folders: list[int]) -> str:
    """extra_info of a move as the web app sends it (captures 12, 15): a JSON string whose content is compact JSON,
    i.e. encoded twice. Encoded once, DSM answers 120 (live test 2026-11-16)."""
    inner = json.dumps({"version": 2, "source_library": "shared_space", "source_folder_ids": source_folders},
                       separators=(",", ":"))
    return js(inner)


def require_team(space: str, what: str) -> None:
    if space != "team":
        raise SystemExit(f"{what}: confirmed for the Shared Space only (captures 12, 13); use --space team")


def move_items(c: SynoClient, space: str, ids: list[int], target: int, batch: int = 500) -> list[dict]:
    """BackgroundTask.File move v1 (capture 12): item ids, their source folders, target folder, name clash = skip.
    Every batch is journaled before it is sent, with each item's source folder, so it can be moved back."""
    require_team(space, "item-move")
    src = {i["id"]: i.get("folder_id") for i in get_items(c, space, ids)}
    unknown = [i for i in ids if i not in src]
    if unknown:
        raise SystemExit(f"unknown item ids: {unknown[:10]}")
    out = []
    for k in range(0, len(ids), batch):
        part = ids[k:k + batch]
        folders = sorted({src[i] for i in part})
        journal("move", space=space, target_folder=target, items=[[i, src[i]] for i in part])
        extra = move_extra(folders)
        task = c.ok(FILE_TASK_API, "move", 1, target_folder_id=target, item_id=part, folder_id=[],
                    action=js("skip"), extra_info=extra).get("task_info") or {}
        info = wait_task(c, task)
        res = {"items": len(part), "from": folders, "to": target, "task": info.get("id"),
               "status": info.get("status"), "skipped": info.get("skip"), "error": info.get("error")}
        journal("move_result", space=space, **res)
        out.append(res)
        if info.get("status") != "done":
            log.warning("move task %s ended %s: the remaining %d item(s) were not sent",
                        info.get("id"), info.get("status"), len(ids) - k - len(part))
            break
    return out


def folder_info(c: SynoClient, space: str, fid: int) -> dict:
    f = c.ok(apis(space)["folder"], "get", FOLDER_V, id=fid).get("folder") or {}
    if not f:
        raise SystemExit(f"folder {fid} not found")
    return f


def rename_folder(c: SynoClient, space: str, fid: int, name: str, root: int = 1) -> dict:
    """Browse.Folder rename v1 {id, name: "quoted"} (capture 14). Refuses the root, a '/' in the name and a name
    another folder under the same parent already has."""
    require_team(space, "folder-rename")
    if fid == root:
        raise SystemExit("the root folder is never renamed")
    if "/" in name or not name.strip():
        raise SystemExit("folder name: one non-empty path segment, no '/'")
    f = folder_info(c, space, fid)
    clash = [x["id"] for x in child_folders(c, space, f.get("parent", root))
             if x["id"] != fid and (x.get("name") or "").rstrip("/").split("/")[-1] == name]
    if clash:
        raise SystemExit(f"a folder named '{name}' already exists next to it ({clash[0]})")
    journal("rename_folder", space=space, folder=fid, old=f.get("name"), new=name)
    g = c.ok(apis(space)["folder"], "rename", 1, id=fid, name=js(name)).get("folder") or {}
    return {"folder": fid, "old": f.get("name"), "name": g.get("name")}


def move_folder(c: SynoClient, space: str, fid: int, target: int, root: int = 1) -> dict:
    """BackgroundTask.File move v1 with folder_id [one] and item_id [] (capture 15): the folder with everything
    in it. Refuses the root, a target inside the moved folder, and a move into the folder's own parent."""
    require_team(space, "folder-move")
    if fid == root:
        raise SystemExit("the root folder is never moved")
    f = folder_info(c, space, fid)
    parent = f.get("parent")
    if target == parent:
        raise SystemExit(f"folder {fid} is already in folder {target}")
    if target in excluded_folders(c, space, [fid]):  # the folder itself and every folder below it
        raise SystemExit(f"folder {target} is inside folder {fid}: a folder cannot move into itself")
    folder_info(c, space, target)
    journal("move_folder", space=space, folder=fid, name=f.get("name"), from_parent=parent, to=target)
    extra = move_extra([parent])
    task = c.ok(FILE_TASK_API, "move", 1, target_folder_id=target, item_id=[], folder_id=[fid],
                action=js("skip"), extra_info=extra).get("task_info") or {}
    info = wait_task(c, task)
    return {"folder": fid, "from": parent, "to": target, "task": info.get("id"), "status": info.get("status"),
            "skipped": info.get("skip"), "error": info.get("error")}


def folder_is_empty(c: SynoClient, space: str, fid: int) -> tuple[int, int]:
    """(photos directly in it, subfolders). Both must be 0 before a folder is removed."""
    n_items = int(c.ok(apis(space)["item"], "count", ITEM_V, folder_id=fid).get("count") or 0)
    return n_items, len(child_folders(c, space, fid))


def require_index_idle(c: SynoClient, what: str) -> None:
    st = c.index_status("SYNO.FotoTeam")
    if st != "idle":
        raise SystemExit(f"{what}: Photos is still indexing ({st}); files it has not indexed yet would not be seen. "
                         "Try again when `index` reports idle")


def remove_empty_folder(c: SynoClient, space: str, fid: int, root: int = 1) -> dict:
    """BackgroundTask.File delete v1 with item_id [] and one folder (capture 13). The emptiness check goes through
    the Photos index, so it runs only while indexing is idle, twice, and again right before the call; the transport
    then sends the delete for this folder id only (arm). Never the root."""
    require_team(space, "folder-remove")
    if fid in (root, ROOT_FOLDER_ID):
        raise SystemExit("the root folder is never removed")
    f = folder_info(c, space, fid)
    require_index_idle(c, "folder-remove")
    for _ in range(2):
        n_items, n_sub = folder_is_empty(c, space, fid)
        if n_items or n_sub:
            raise SystemExit(f"folder {fid} is not empty ({n_items} photos, {n_sub} subfolders): not removed. "
                             "Move its photos first; photos are deleted only by a human")
    require_index_idle(c, "folder-remove")
    journal("remove_folder", space=space, folder=fid, name=f.get("name"), parent=f.get("parent"))
    c.arm(FILE_TASK_API, "delete", fid)
    task = c.ok(FILE_TASK_API, "delete", 1, item_id=[], folder_id=[fid]).get("task_info") or {}
    info = wait_task(c, task)
    return {"folder": fid, "task": info.get("id"), "status": info.get("status"), "error": info.get("error")}


# Previews the web app makes in the browser and sends with the file: short edge 1280 / 320 / 240 px
# (measured 2026-11-16: the NAS then serves exactly those bytes as xl / m / sm).
UPLOAD_THUMBS = {"thumb_xl": 1280, "thumb_m": 320, "thumb_sm": 240}
UPLOAD_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def upload_thumbs(path: Path) -> dict[str, bytes]:
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        out = {}
        for key, short in UPLOAD_THUMBS.items():
            w, h = im.size
            k = min(1.0, short / min(w, h))
            t = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS) if k < 1 else im
            buf = io.BytesIO()
            t.save(buf, "JPEG", quality=90)
            out[key] = buf.getvalue()
    return out


def upload_file(c: SynoClient, space: str, folder_id: int, path: Path) -> dict:
    """Upload.Item upload_to_folder v8, multipart (capture 11, 2026-11-16): file, the three previews,
    name, mtime (the file's, in seconds), target_folder_id, duplicate "ignore"."""
    ctype = UPLOAD_TYPES.get(path.suffix.lower())
    if not ctype:
        raise SystemExit(f"{path.name}: only JPEG uploads are confirmed by a capture")
    thumbs = upload_thumbs(path)
    files = {"file": (path.name, path.read_bytes(), ctype)}
    files |= {k: ("blob", v, "image/jpeg") for k, v in thumbs.items()}
    data = c.ok_upload(apis(space)["upload"], "upload_to_folder", 8, files, duplicate=js("ignore"),
                       name=js(path.name), mtime=str(int(path.stat().st_mtime)), target_folder_id=str(folder_id))
    return {"file": path.name, "id": data.get("id"), "action": data.get("action")}


SHARE_EXPIRY_DAYS = 30  # every share expires; config `share_expiry_days` overrides
SHARE_MAX_DAYS = 90  # the longest expiry accepted; config `share_max_days` overrides
SHARE_ROLES = ("view", "download")  # the only roles a shared album offers a user (capture 16)


def share_expiration(cfg: dict, days: int | None = None, on: str | None = None) -> int:
    """Epoch seconds of the end (23:59:59, local time) of the expiry day, as the web app sends it (capture 17:
    1797461999 = 2026-12-16 23:59:59 CET). Default: `share_expiry_days` (30) from today."""
    from datetime import date, timedelta

    if on:
        day = date.fromisoformat(on)
    else:
        day = date.today() + timedelta(days=int(days if days is not None else cfg.get("share_expiry_days", SHARE_EXPIRY_DAYS)))
    if day <= date.today():
        raise SystemExit("a share must expire after today")
    longest = int(cfg.get("share_max_days", SHARE_MAX_DAYS))
    if day > date.today() + timedelta(days=longest):
        raise SystemExit(f"a share expires within {longest} days (config share_max_days)")
    return int(datetime(day.year, day.month, day.day, 23, 59, 59).timestamp())


def list_users(c: SynoClient) -> dict[str, int]:
    """DSM users Photos can share with, name -> id (Sharing.Misc list_user_group, capture 16)."""
    rows = c.ok("SYNO.Foto.Sharing.Misc", "list_user_group", 1, team_space_sharable_list=False).get("list", [])
    return {r["name"]: r["id"] for r in rows if r.get("type") == "user"}


def parse_members(specs: list, users: dict[str, int]) -> list[dict]:
    """`name` or `name:role` strings, or {user, role} dicts (config `album_members`) -> [{name, id, role}]."""
    out = []
    for m in specs or []:
        name, role = (m.get("user"), m.get("role", "download")) if isinstance(m, dict) else (str(m).split(":", 1) + ["download"])[:2]
        if role not in SHARE_ROLES:
            raise SystemExit(f"member {name}: role is {' or '.join(SHARE_ROLES)} (a shared album has no manager role)")
        if name not in users:
            raise SystemExit(f"member {name}: no such DSM user Photos can share with")
        out.append({"name": name, "id": users[name], "role": role})
    return out


def share_album(c: SynoClient, aid: int, password: str | None, download: bool, members: list[dict] | None = None,
                expiration: int = 0, public: bool = True) -> dict:
    """Share an album (captures 2026-10-31 and 17): set_shared -> album passphrase -> update(password, expiration,
    permission: the public link and/or DSM users, each with its role). One expiration covers the whole share."""
    # Sharing is enabled for a users-only share too; it simply has no public member (capture 19). With
    # enabled=false the update is refused (DSM 120, live 2026-11-16).
    c.ok("SYNO.Foto.Sharing.Passphrase", "set_shared", 1, policy=js("album"), album_id=aid, enabled=True)
    album = album_get(c, aid)
    pp = album.get("passphrase") or ((album.get("additional") or {}).get("sharing_info") or {}).get("passphrase")
    if not pp:
        raise SystemExit("sharing enabled but no passphrase returned; check the album in Synology Photos")
    register_secret(password)
    current = ((album.get("additional") or {}).get("sharing_info") or {}).get("permission") or []
    if not public and any((m.get("member") or {}).get("type") == "public" for m in current if isinstance(m, dict)):
        raise SystemExit(f"album {aid} has a public link: share again with --public (it keeps an expiry) or run "
                         "album-unshare first; a users-only update would leave the link without an expiry")
    perm: list[dict] = []
    if public:
        perm.append({"action": "update", "role": "download" if download else "view", "member": {"type": "public"}})
    perm += [{"role": m["role"], "action": "update", "member": {"type": "user", "id": m["id"]}} for m in members or []]
    # An expiry applies to the public link only: for a users-only share the NAS keeps none (live 2026-11-16,
    # a test album); such albums end through album-cleanup (album_max_age_days after creation).
    expiration = int(expiration) if public else 0
    params: dict[str, Any] = {"passphrase": js(pp), "expiration": expiration, "permission": perm}
    if password and public:
        params["password"] = js(password)
    c.ok("SYNO.Foto.Sharing.Passphrase", "update", 1, **params)
    info = (album_get(c, aid).get("additional") or {}).get("sharing_info") or {}
    return {"album_id": aid, "link": info.get("sharing_link") if public else None, "privacy": info.get("privacy_type"),
            "password_enabled": info.get("enable_password"), "members": [f"{m['name']}:{m['role']}" for m in members or []],
            "expires": datetime.fromtimestamp(expiration).isoformat(timespec="minutes") if expiration else None,
            "ends": None if public else "album-cleanup, album_max_age_days after the album was created"}


def share_members_or_report(c: SynoClient, aid: int, members: list[dict], exp: int) -> dict:
    """The automatic share of a new album with `album_members`. If it cannot be made, the album stays (unshared)
    and the result says so, so the owner can share it or remove it."""
    try:
        return share_album(c, aid, None, True, members, exp, public=False)
    except (SystemExit, ApiError) as e:
        msg = f"album {aid} created but NOT shared with {', '.join(m['name'] for m in members)}: {e}"
        print(msg, file=sys.stderr)
        return {"shared": False, "error": str(e)}


# --- rule albums (capture 17) ---------------------------------------------------------------
def album_condition(r: "Resolver", args: argparse.Namespace) -> dict:
    """The rules of an album as the web app sends them: Shared Space (user_id 0), all item types, and any of
    folders, a capture period, favourites, people (any or all of them). Other rule kinds are not captured yet."""
    cond: dict[str, Any] = {"user_id": 0, "item_type": []}
    if args.folder:
        cond["folder_filter"] = [int(f) for f in args.folder]
    if args.date_from or args.date_to:
        cond["time"] = [{"start_time": to_epoch(args.date_from or "1970-01-01"),
                         "end_time": to_epoch(args.date_to or "2100-01-01", end=True)}]
    if args.favorite:
        cond["favorite"] = [True]
    if args.person:
        cond["person"] = [r.get("people", p) for p in args.person]
        cond["person_policy"] = "and" if args.all_people else "or"
    if len(cond) == 2:
        raise SystemExit("give at least one rule: --folder, --from/--to, --favorite or --person")
    return cond


def create_rule_album(c: SynoClient, name: str, cond: dict) -> dict:
    a = c.ok("SYNO.Foto.Browse.ConditionAlbum", "create", 4, name=js(name), condition=js(cond)).get("album") or {}
    return {"album_id": a.get("id"), "name": a.get("name"), "type": a.get("type")}


# --- cleanup of the skill's own albums ------------------------------------------------------
ALBUM_MAX_AGE_DAYS = 30  # albums the skill created may be removed after this (config `album_max_age_days`)


def remove_album(c: SynoClient, aid: int, me: int) -> dict:
    """Unshare, then Album.delete one album (capture 18). Only an album this account owns, never the trash
    album. The photos stay in the library; the journal keeps the album's name, type, rules and photo ids."""
    a = album_get(c, aid)
    if a.get("owner_user_id") != me:
        raise SystemExit(f"album {aid} belongs to another account: only albums the skill created are removed")
    if aid not in created_albums():
        raise SystemExit(f"album {aid} ({a.get('name')!r}) was not created by this tool ({CREATED_ALBUMS_PATH}): "
                         "not removed; a human removes it in Synology Photos")
    if a.get("name") == TRASH_ALBUM:
        raise SystemExit("the Trash candidate album is never removed by the tool")
    ids = [i["id"] for i in album_items(c, aid)] if a.get("type") != "condition" else []
    journal("remove_album", album=aid, name=a.get("name"), album_type=a.get("type"), condition=a.get("condition"),
            items=ids, create_time=a.get("create_time"))
    if a.get("shared"):
        c.ok("SYNO.Foto.Sharing.Passphrase", "set_shared", 1, policy=js("album"), album_id=aid, enabled=False)
    c.arm("SYNO.Foto.Browse.Album", "delete", aid)
    c.ok("SYNO.Foto.Browse.Album", "delete", 1, id=[aid])
    return {"album_id": aid, "name": a.get("name"), "removed": True, "photos_kept": len(ids)}


def album_status(a: dict, me: int, now: float, max_age_days: int, created: set[int] | None = None) -> dict:
    """Why an album this tool created is (or is not yet) eligible for removal. Albums the account owns but this
    tool did not create (not in `created`) are never eligible."""
    sh = (a.get("additional") or {}).get("sharing_info") or {}
    age = (now - (a.get("create_time") or now)) / 86400
    exp = sh.get("expiration") or 0
    reasons = []
    if age >= max_age_days:
        reasons.append(f"created {age:.0f} days ago")
    if sh.get("is_expired") or (exp and exp < now):
        reasons.append("share expired")
    elif not a.get("shared") and not sh.get("permission") and age >= 1:
        reasons.append("not shared")
    return {"album_id": a["id"], "name": a.get("name"), "type": a.get("type"), "items": a.get("item_count"),
            "created": datetime.fromtimestamp(a.get("create_time") or now).date().isoformat(), "age_days": round(age),
            "shared": bool(a.get("shared")), "expires": datetime.fromtimestamp(exp).date().isoformat() if exp else None,
            "mine": a.get("owner_user_id") == me,
            "created_by_tool": created is not None and a["id"] in created,
            "eligible": bool(reasons) and a.get("owner_user_id") == me and created is not None and a["id"] in created,
            "reasons": reasons}


def album_link(cfg: dict, passphrase: str | None) -> str | None:
    return f"https://{cfg['public_host']}:{cfg['nas_port']}/mo/sharing/{passphrase}" if passphrase else None


def list_faces(c: SynoClient, space: str, item: int) -> list[dict]:
    """[{face_id, person_id, name, face_bounding_box}] for one photo (capture 2026-10-31)."""
    return c.ok(apis(space)["item"], "list_face", ITEM_V, id_item=item).get("list", [])


def pick_face(faces: list[dict], face: int | None, person_id: int | None) -> dict:
    if face is not None:
        hit = [f for f in faces if f.get("face_id") == face]
        if not hit:
            raise SystemExit(f"face {face} not on this photo; faces: {[f.get('face_id') for f in faces]}")
        return hit[0]
    hit = [f for f in faces if person_id is not None and f.get("person_id") == person_id]
    if len(hit) != 1:
        raise SystemExit(f"{'no' if not hit else 'several'} matching faces; pass --face (see `faces ITEM`)")
    return hit[0]


def draw_faces(c: SynoClient, space: str, it: dict, faces: list[dict], out_dir: Path) -> Path:
    """Verification aid: the XL preview with each face box, face id and person drawn on it."""
    from PIL import Image, ImageDraw

    ck = ((it.get("additional") or {}).get("thumbnail") or {}).get("cache_key", "")
    data = c.fetch(apis(space)["thumb"], "get", THUMB_V, id=str(it["id"]), cache_key=str(ck), type="unit", size="xl")
    im = Image.open(io.BytesIO(data)).convert("RGB")
    d = ImageDraw.Draw(im)
    w, h = im.size
    for f in faces:
        b = f["face_bounding_box"]
        box = (b["top_left"]["x"] * w, b["top_left"]["y"] * h, b["bottom_right"]["x"] * w, b["bottom_right"]["y"] * h)
        d.rectangle(box, outline=(255, 0, 0), width=max(2, w // 400))
        d.text((box[0], box[3] + 4), f"{f['face_id']} {f.get('name') or '#' + str(f.get('person_id'))}", fill=(255, 0, 0))
    ad = it.get("additional") or {}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"faces_{it['id']}_orient{ad.get('orientation')}-{ad.get('orientation_original')}.jpg"
    im.save(path, quality=88)
    return path


# --- person groups: review, clean, name ------------------------------------------------------
JOURNAL_PATH = STATE_DIR / "journal.jsonl"
CREATED_ALBUMS_PATH = STATE_DIR / "created_albums.jsonl"  # albums this tool created: the only ones it may remove
TRASH_LOG_PATH = STATE_DIR / "trash_candidates.jsonl"


def migrate_state() -> None:
    """Earlier versions kept the journal and the trash log next to the discovery cache: copy them to the default
    STATE_DIR once, before the first write. The old files stay where they were. Nothing is copied when
    SYNOPHOTOS_STATE or SYNOPHOTOS_CACHE point elsewhere (tests, a temporary folder)."""
    import shutil

    if os.environ.get("SYNOPHOTOS_STATE") or os.environ.get("SYNOPHOTOS_CACHE"):
        return
    for name in ("journal.jsonl", "trash_candidates.jsonl"):
        old, new = CACHE_PATH.parent / name, STATE_DIR / name
        if old.is_file() and not new.exists():
            private_dir(STATE_DIR)
            shutil.copy2(old, new)
            new.chmod(0o600)
            old.chmod(0o600)  # the old copy stays (nothing is deleted) but readable by this user only


def journal(action: str, **data: Any) -> None:
    """Undo log: every face unlink is recorded with its box so `person-add --box` can restore it."""
    append_private(JOURNAL_PATH, json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "action": action,
                                             **data}, ensure_ascii=False))


def record_created_album(aid: Any, name: str, kind: str) -> None:
    if aid is not None:
        append_private(CREATED_ALBUMS_PATH, json.dumps({"album_id": int(aid), "name": name, "type": kind,
                                                        "at": datetime.now().isoformat(timespec="seconds")},
                                                       ensure_ascii=False))


def created_albums() -> set[int]:
    return {int(r["album_id"]) for r in read_jsonl(CREATED_ALBUMS_PATH) if r.get("album_id") is not None}


def group_faces(c: SynoClient, space: str, pid: int, limit: int | None = None) -> list[dict]:
    """Every face linked to person `pid`: one row per (item, face). Items via Browse.Item list(person_id)."""
    rows: list[dict] = []
    for it in c.pages(apis(space)["item"], "list", ITEM_V, limit, person_id=pid, additional=["thumbnail"],
                      sort_by="takentime", sort_direction="asc"):
        for f in list_faces(c, space, it["id"]):
            if f.get("person_id") == pid:
                rows.append({"tile": len(rows) + 1, "item": it["id"], "face_id": f["face_id"], "date": item_date(it),
                             "filename": it.get("filename"), "box": f["face_bounding_box"],
                             "cache_key": ((it.get("additional") or {}).get("thumbnail") or {}).get("cache_key", "")})
    return rows


def face_crop(img: Any, box: dict, margin: float = 0.35, size: int = 160) -> Any:
    w, h = img.size
    x1, y1 = box["top_left"]["x"] * w, box["top_left"]["y"] * h
    x2, y2 = box["bottom_right"]["x"] * w, box["bottom_right"]["y"] * h
    mx, my = (x2 - x1) * margin, (y2 - y1) * margin
    crop = img.crop((max(0, x1 - mx), max(0, y1 - my), min(w, x2 + mx), min(h, y2 + my)))
    crop.thumbnail((size, size))
    return crop


def make_sheets(c: SynoClient, space: str, pid: int, name: str, rows: list[dict], out: Path,
                cols: int = 8, per_sheet: int = 64, tile: int = 160) -> list[Path]:
    """Contact sheets of face crops, each tile labelled with its tile number, for review."""
    from PIL import Image, ImageDraw

    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    cache: dict[int, Any] = {}
    for s0 in range(0, len(rows), per_sheet):
        chunk = rows[s0:s0 + per_sheet]
        n_rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tile, n_rows * (tile + 18)), (255, 255, 255))
        d = ImageDraw.Draw(sheet)
        for k, r in enumerate(chunk):
            if r["item"] not in cache:
                cache.clear()  # one preview in memory at a time is enough: rows are grouped by item
                data = c.fetch(apis(space)["thumb"], "get", THUMB_V, id=str(r["item"]), cache_key=str(r["cache_key"]),
                               type="unit", size="xl")
                cache[r["item"]] = Image.open(io.BytesIO(data)).convert("RGB")
            crop = face_crop(cache[r["item"]], r["box"], size=tile)
            x, y = (k % cols) * tile, (k // cols) * (tile + 18)
            sheet.paste(crop, (x + (tile - crop.width) // 2, y + (tile - crop.height) // 2))
            d.text((x + 4, y + tile + 2), f"{r['tile']}  {r['date']}", fill=(0, 0, 0))
        p = out / f"group_{pid}_{safe_name(name or 'unnamed')}_{s0 // per_sheet + 1:02d}.jpg"
        sheet.save(p, quality=85)
        paths.append(p)
    return paths


def resolve_tiles(index: list[dict], tiles: list[int]) -> list[dict]:
    by = {r["tile"]: r for r in index}
    missing = [t for t in tiles if t not in by]
    if missing:
        raise SystemExit(f"tiles not in the sheet index: {missing}")
    return [by[t] for t in dict.fromkeys(tiles)]


def unlink_faces(c: SynoClient, space: str, pid: int, rows: list[dict], reason: str) -> None:
    """One Person.delete_face call per face, the shape seen in the capture; each face is journaled before its call."""
    for r in rows:
        journal("unlink_face", space=space, person_id=pid, item=r["item"], face_id=r["face_id"], box=r["box"],
                reason=reason)
        c.ok(apis(space)["person"], "delete_face", 1, person_id=pid, face_id=[r["face_id"]])


def person_info(r: "Resolver", pid: int) -> tuple[str, int | None]:
    for name, v in r._table("people").items():
        if v.get("id") == pid:
            return ("" if name.startswith("#") else name), v.get("items")
    return "", None


def trash_plan(existing: set[int], ids: list[int], album_exists: bool) -> tuple[list[int], str]:
    new = [i for i in dict.fromkeys(ids) if i not in existing]
    plan = (f"add {len(new)} item(s) to album '{TRASH_ALBUM}'" + ("" if album_exists else " (album will be created)")
            + f"; {len(set(ids)) - len(new)} already there. Nothing is deleted; a human reviews the album.")
    return new, plan


# --- commands --------------------------------------------------------------------------------
def cmd_login_test(cfg: dict, args: argparse.Namespace) -> None:
    c = SynoClient(cfg, args.endpoint)
    print(f"endpoint: {c.kind} {c.ip}")
    with c:
        pass
    print("login ok, logged out")


def _count(t: Any) -> Any:
    return len(t) if isinstance(t, dict) and "error" not in t else t


def cmd_discover(cfg: dict, args: argparse.Namespace) -> None:
    cache = load_cache()
    if cache_fresh(cache) and not args.refresh:
        print(f"cache fresh ({CACHE_PATH}); use --refresh to rediscover", file=sys.stderr)
    else:
        c = SynoClient(cfg, args.endpoint)
        print(f"endpoint: {c.kind} {c.ip}", file=sys.stderr)
        with c:
            cache = discover(c, cfg)
        save_cache(cache)
    if args.save_api_info:
        Path(args.save_api_info).write_text(json.dumps(cache.get("apis", {}), indent=2, sort_keys=True) + "\n")
    emit({
        "endpoint": cache.get("endpoint"), "account": cache.get("account"),
        "discovered": datetime.fromtimestamp(cache.get("discovered_at", 0)).isoformat(timespec="seconds"),
        "apis": len(cache.get("apis", {})), "whoami": cache.get("whoami"),
        "spaces": {s: {"root_folder": v.get("root_folder"), "people": _count(v.get("people")), "tags": _count(v.get("tags"))}
                   for s, v in cache.get("spaces", {}).items()},
        "albums": _count(cache.get("albums")), "cache": str(CACHE_PATH),
    }, True)


def session(cfg: dict, args: argparse.Namespace) -> tuple[SynoClient, Resolver]:
    c = SynoClient(cfg, args.endpoint)
    log.info("endpoint: %s %s", c.kind, c.ip)
    c.login()
    try:
        return c, Resolver(c, cfg, args.space)
    except BaseException:
        c.logout()
        raise


def cmd_read(cfg: dict, args: argparse.Namespace) -> None:
    c, r = session(cfg, args)
    try:
        a = apis(args.space)
        if args.cmd == "whoami":
            emit(c.ok("SYNO.Foto.UserInfo", "me", 1), True)
        elif args.cmd == "folders":
            rf = r.cache["spaces"][args.space].get("root_folder") or {}
            root = args.parent if args.parent is not None else (rf.get("folder") or {}).get("id", 1)
            rows, queue = [], [root]
            while queue:
                fid = queue.pop(0)
                for f in c.pages(a["folder"], "list", FOLDER_V, id=fid, sort_by="filename", sort_direction="asc"):
                    rows.append({"id": f["id"], "name": f.get("name"), "parent": fid})
                    if args.recursive:
                        queue.append(f["id"])
            emit(rows, args.json)
        elif args.cmd == "people":
            r._refresh("people")
            rows = [{"name": k, **v} for k, v in r._table("people").items()]
            emit(sorted(rows, key=lambda x: -(x.get("items") or 0)), args.json)
        elif args.cmd == "tags":
            r._refresh("tags")
            emit([{"name": k, "id": v} for k, v in r._table("tags").items()], args.json)
        elif args.cmd == "albums":
            r._refresh("albums")
            emit([{"name": k, **{x: y for x, y in v.items() if x != "passphrase"},
                   "link": album_link(cfg, v.get("passphrase")) if v.get("shared") else None}
                  for k, v in r._table("albums").items()], args.json)
        elif args.cmd == "items":
            items = select_items(c, r, args, args.limit)
            emit([summarize(i) for i in items], args.json)
        elif args.cmd == "count":
            f = build_filter(r, args)
            if f:
                if args.folder is not None:
                    f["folder"] = [args.folder]
                print(c.ok(a["item"], "count_with_filter", ITEM_V, **f).get("count"))
            else:
                if args.folder is None:
                    raise SystemExit("give a filter (--person/--from/--to/--tag/--rating) or --folder")
                print(c.ok(a["item"], "count", ITEM_V, folder_id=args.folder).get("count"))
        elif args.cmd == "index":
            emit({"team": c.index_status("SYNO.FotoTeam"), "personal": c.index_status("SYNO.Foto")}, True)
        elif args.cmd == "group-sheet":
            pid = r.get("people", args.person)
            name, n = person_info(r, pid)
            rows = group_faces(c, args.space, pid, args.limit)
            out = Path(args.out).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            sheets = make_sheets(c, args.space, pid, name, rows, out) if rows else []
            index = out / f"group_{pid}_index.json"
            index.write_text(json.dumps({"person_id": pid, "name": name, "space": args.space, "faces": rows},
                                        indent=1, ensure_ascii=False))
            emit({"person_id": pid, "name": name or None, "photos_reported": n, "faces": len(rows),
                  "sheets": [str(x) for x in sheets], "index": str(index)}, True)
        elif args.cmd == "faces":
            faces = list_faces(c, args.space, args.item)
            emit(faces, True)
            if args.draw:
                it = get_items(c, args.space, [args.item])[0]
                out = draw_faces(c, args.space, it, faces, Path(args.draw).expanduser())
                print(f"drawn: {out}", file=sys.stderr)
        elif args.cmd == "item":
            rows = get_items(c, args.space, args.ids)
            emit(rows if args.raw else [summarize(i) for i in rows], True)
        elif args.cmd == "download":
            out = Path(args.out).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            items = get_items(c, args.space, args.ids) if args.ids else select_items(c, r, args, args.limit)
            n_new = n_skip = n_err = 0
            for k, it in enumerate(items, 1):
                try:
                    _, new = download_one(c, args.space, it, args.size, out)
                except (ApiError, OSError) as e:
                    n_err += 1
                    print(f"item {it['id']}: {e}", file=sys.stderr)
                    continue
                n_new += new
                n_skip += not new
                if k % 50 == 0:
                    print(f"{k}/{len(items)}", file=sys.stderr)
            print(json.dumps({"out": str(out), "size": args.size, "selected": len(items), "downloaded": n_new,
                              "skipped_existing": n_skip, "errors": n_err}))
    finally:
        c.logout()


def cmd_write(cfg: dict, args: argparse.Namespace) -> None:
    migrate_state()
    c, r = session(cfg, args)
    try:
        a = apis(args.space)
        cmd = args.cmd
        if cmd == "rate":
            before = {i["id"]: (i.get("additional") or {}).get("rating") for i in get_items(c, args.space, args.ids)}
            unknown = [i for i in args.ids if i not in before]
            if unknown:
                raise SystemExit(f"unknown item ids: {unknown[:10]}")
            if require_write("rate", args, f"set rating {args.stars} on {len(args.ids)} item(s) {args.ids[:10]} "
                             f"(Photos rewrites the original files' metadata; old ratings are journaled)"):
                journal("rate", space=args.space, stars=args.stars, before=[[i, before[i]] for i in args.ids])
                c.ok(a["item"], "set", ITEM_V, id=args.ids, rating=args.stars)
                print("done")
        elif cmd == "tag-add":
            tid = r.get("tags", args.tag, required=False)
            plan = f"tag '{args.tag}'" + ("" if tid else " (new tag)") + f" on {len(args.ids)} item(s)"
            if require_write("tag-add", args, plan):
                journal("tag_add", space=args.space, tag=args.tag, tag_id=tid, items=args.ids)
                if tid is None:
                    data = c.ok(a["tag"], "create", 2, name=js(args.tag))
                    tid = (data.get("tag") or {}).get("id") or r.get("tags", args.tag)
                c.ok(a["item"], "add_tag", ITEM_V, id=args.ids, tag=[tid])
                r._refresh("tags")
                print(f"done (tag id {tid})")
        elif cmd == "tag-remove":
            tid = r.get("tags", args.tag)
            if require_write("tag-remove", args, f"remove tag '{r.name_of('tags', tid)}' (id {tid}) from "
                             f"{len(args.ids)} item(s)"):
                journal("tag_remove", space=args.space, tag=r.name_of("tags", tid), tag_id=tid, items=args.ids)
                c.ok(a["item"], "remove_tag", ITEM_V, id=args.ids, tag=[tid])
                print("done")
        elif cmd == "person-remove":
            pid = r.get("people", args.person)
            f = pick_face(list_faces(c, args.space, args.item), args.face, pid)
            if f.get("person_id") != pid:
                raise SystemExit(f"face {f['face_id']} belongs to person {f.get('person_id')} ({f.get('name') or 'unnamed'}), not {args.person}")
            if require_write("person-remove", args, f"unlink '{args.person}' from face {f['face_id']} on item {args.item} "
                             f"(the photo is not touched)"):
                journal("unlink_face", space=args.space, person_id=pid, item=args.item, face_id=f["face_id"],
                        box=f["face_bounding_box"], reason="person-remove")
                c.ok(a["person"], "delete_face", 1, person_id=pid, face_id=[f["face_id"]])
                print("done (journal: restore with person-add --box)")
        elif cmd == "person-add":
            pid = r.get("people", args.person)
            faces = list_faces(c, args.space, args.item)
            if args.box:
                x1, y1, x2, y2 = (float(v) for v in args.box.split(","))
                box, old = {"top_left": {"x": x1, "y": y1}, "bottom_right": {"x": x2, "y": y2}}, None
            else:
                if args.face is None:
                    raise SystemExit("pass --face FACE_ID (see `faces ITEM`) or --box x1,y1,x2,y2 (0..1)")
                old = pick_face(faces, args.face, None)
                box = old["face_bounding_box"]
                if old.get("person_id") == pid:
                    print(f"face {old['face_id']} is already {args.person}; nothing to do")
                    return
            reassign = f" (unlinking it from {old.get('name') or 'unnamed person'} {old.get('person_id')} first)" if old else ""
            if require_write("person-add", args, f"assign a face on item {args.item} to '{args.person}' (id {pid}){reassign}",
                             needs=("person-remove",) if old else ()):
                if old:
                    journal("unlink_face", space=args.space, person_id=old["person_id"], item=args.item,
                            face_id=old["face_id"], box=old["face_bounding_box"], reason=f"reassign to {pid}")
                    c.ok(a["person"], "delete_face", 1, person_id=old["person_id"], face_id=[old["face_id"]])
                c.ok(a["person"], "add_face", 3, id_item=args.item,
                     face=[{"face_bounding_box": box, "face_id_temp": f"{args.item}-0", "person_id": pid}])
                emit(list_faces(c, args.space, args.item), True)
        elif cmd == "group-clean":
            pid = r.get("people", args.person)
            index = json.loads(Path(args.index).expanduser().read_text())
            if index.get("person_id") != pid:
                raise SystemExit(f"index {args.index} is for person {index.get('person_id')}, not {pid}")
            chosen = resolve_tiles(index["faces"], args.tiles)
            # The sheet may be stale: only unlink faces that are still linked to this person.
            live = {(f["face_id"]) for x in {r_["item"] for r_ in chosen}
                    for f in list_faces(c, args.space, x) if f.get("person_id") == pid}
            todo = [x for x in chosen if x["face_id"] in live]
            stale = [x["tile"] for x in chosen if x["face_id"] not in live]
            name, n = person_info(r, pid)
            plan = (f"unlink {len(todo)} face(s) from person {name or 'unnamed'} ({pid}, {n} photos): tiles "
                    f"{[x['tile'] for x in todo]}" + (f"; skipped, no longer linked: {stale}" if stale else "")
                    + ". Photos are not touched; each face is journaled with its box.")
            if require_write("group-clean", args, plan) and todo:
                unlink_faces(c, args.space, pid, todo, args.reason or "group-clean")
                print(f"done; journal: {JOURNAL_PATH}")
        elif cmd == "person-name":
            pid = r.get("people", args.person)
            name, n = person_info(r, pid)
            target = r.get("people", args.name, required=False) if not args.name.isdigit() else None
            if target == pid:
                print("already has that name")
                return
            if target:
                tname, tn = person_info(r, target)
                plan = (f"MERGE person {name or 'unnamed'} ({pid}, {n} photos) INTO existing '{tname}' ({target}, {tn} "
                        f"photos). Every face of {pid} becomes {tname}. Hard to undo: clean the group first "
                        f"(group-sheet / group-clean).")
            else:
                plan = f"name person {pid} ({n} photos, currently '{name or 'unnamed'}') as '{args.name}' (new person)"
            if require_write("person-name", args, plan):
                if target:
                    # Merge is hard to undo: record every face of the merged group first (face ids, boxes, items).
                    rows = group_faces(c, args.space, pid)
                    journal("merge", space=args.space, merged_id=pid, merged_name=name, target_id=target,
                            target_name=tname, faces=[{k: x[k] for k in ("item", "face_id", "box")} for x in rows])
                    res = c.ok(a["person"], "merge", 2, target_id=target, merged_id=[pid], name=js(tname))
                else:
                    res = c.ok(a["person"], "set", 1, id=pid, name=js(args.name))
                r._refresh("people")
                emit({k: res.get(k) for k in ("id", "name", "item_count")}, True)
        elif cmd == "folder-create":
            parent = args.parent if args.parent is not None else root_folder(r, args.space)
            if require_write("folder-create", args, f"create folder '{args.name}' under folder {parent} ({args.space} space)"):
                emit(create_folder(c, args.space, parent, args.name), True)
        elif cmd == "upload":
            paths = sorted(Path(p).expanduser() for p in args.files)
            missing = [str(p) for p in paths if not p.is_file()]
            if missing:
                raise SystemExit(f"not files: {missing[:5]}")
            there = {i.get("filename") for i in c.pages(a["item"], "list", ITEM_V, folder_id=args.folder)}
            todo = [p for p in paths if p.name not in there]
            mb = sum(p.stat().st_size for p in todo) / 1e6
            plan = (f"upload {len(todo)} file(s), {mb:.0f} MB, into folder {args.folder} ({args.space} space); "
                    f"{len(paths) - len(todo)} already there by name")
            if require_write("upload", args, plan):
                done = []
                for p in todo:
                    res = upload_file(c, args.space, args.folder, p)
                    journal("upload", space=args.space, folder=args.folder, file=res["file"], item=res["id"], result=res["action"])
                    done.append(res)
                    print(f"uploaded {len(done)}/{len(todo)} {p.name} -> {res['id']} ({res['action']})", file=sys.stderr)
                emit({"folder": args.folder, "uploaded": len(done), "items": done}, True)
        elif cmd == "item-move":
            ids = list(dict.fromkeys(args.ids))
            if require_write("item-move", args, f"move {len(ids)} photo(s) into folder {args.to} ({args.space} space); "
                             "photos already there by name are skipped"):
                emit(move_items(c, args.space, ids, args.to), True)
        elif cmd == "folder-rename":
            if require_write("folder-rename", args, f"rename folder {args.folder_id} to '{args.name}'"):
                emit(rename_folder(c, args.space, args.folder_id, args.name, root_folder(r, args.space)), True)
        elif cmd == "folder-move":
            n_items, n_sub = folder_is_empty(c, args.space, args.folder_id)
            if require_write("folder-move", args, f"move folder {args.folder_id} ({n_items} photos directly in it, "
                             f"{n_sub} subfolders) with all its content into folder {args.to}"):
                emit(move_folder(c, args.space, args.folder_id, args.to, root_folder(r, args.space)), True)
        elif cmd == "folder-remove":
            n_items, n_sub = folder_is_empty(c, args.space, args.folder_id)
            state = "empty" if not (n_items or n_sub) else f"NOT empty: {n_items} photos, {n_sub} subfolders (will refuse)"
            if require_write("folder-remove", args, f"remove folder {args.folder_id} ({state}). Only photos Photos has "
                             "indexed are counted: any other file in the folder on disk (sidecars such as .xmp/.aae, "
                             "formats Photos ignores) is deleted with it"):
                emit(remove_empty_folder(c, args.space, args.folder_id, root_folder(r, args.space)), True)
        elif cmd == "album-create":
            ids = list(args.items or [])
            if args.date_from or args.date_to or args.person or args.tag or args.rating is not None or args.folder is not None:
                ids += [i["id"] for i in select_items(c, r, args, None)]
            ids = list(dict.fromkeys(ids))
            needs = ("album-add",) if len(ids) > 100 else ()
            members = parse_members(cfg["album_members"], list_users(c)) if cfg.get("album_members") else []
            exp = share_expiration(cfg) if members else 0
            plan = (f"create album '{args.name}' with {len(ids)} item(s)"
                    + (f"; share with {', '.join(m['name'] + ':' + m['role'] for m in members)} (no NAS expiry for "
                       f"users: album-cleanup ends it after {cfg.get('album_max_age_days', ALBUM_MAX_AGE_DAYS)} days)"
                       if members else ""))
            if require_write("album-create", args, plan, needs):
                album = create_album(c, args.name, ids)
                record_created_album(album["id"], args.name, "normal")
                out = {"album_id": album["id"], "name": args.name, "items": len(ids)}
                if members:
                    out["share"] = share_members_or_report(c, album["id"], members, exp)
                r._refresh("albums")
                emit(out, True)
        elif cmd == "album-add":
            aid = r.get("albums", args.album)
            if require_write("album-add", args, f"add {len(args.ids)} item(s) to album '{r.name_of('albums', aid)}' ({aid})"):
                add_to_album(c, aid, args.ids)
                print("done")
        elif cmd == "album-remove-item":
            aid = r.get("albums", args.album)
            if require_write("album-remove-item", args,
                             f"remove {len(args.ids)} item(s) from album '{r.name_of('albums', aid)}' ({aid}) "
                             "(photos stay in the library)"):
                c.ok("SYNO.Foto.Browse.NormalAlbum", "delete_item", 1, id=aid, item=args.ids)
                print("done")
        elif cmd == "album-rename":
            aid = r.get("albums", args.album)
            if require_write("album-rename", args, f"rename album '{r.name_of('albums', aid)}' ({aid}) to '{args.name}'"):
                c.ok("SYNO.Foto.Browse.Album", "set_name", 1, id=aid, name=js(args.name))
                r._refresh("albums")
                print("done")
        elif cmd == "album-share":
            aid = r.get("albums", args.album)
            members = parse_members(list(cfg.get("album_members") or []) + list(args.member or []), list_users(c)) \
                if (cfg.get("album_members") or args.member) else []
            if not args.public and not members:
                raise SystemExit("share with --public and/or --member NAME[:view|download] (or album_members in the config)")
            pw = None
            if args.public and not args.no_password:
                pw = sys.stdin.readline().strip() if args.password_stdin else pysecrets.token_urlsafe(9)
                if not pw:
                    raise SystemExit("empty link password: pass --no-password to share a link without one")
            exp = share_expiration(cfg, args.expires_days, args.expires_on)
            perm = "download" if args.download else "view"
            plan = (f"share album '{r.name_of('albums', aid)}' ({aid})"
                    + (f" by public link, password={'yes' if pw else 'NO'}, permission={perm}" if args.public else "")
                    + (f"; with {', '.join(m['name'] + ':' + m['role'] for m in members)}" if members else "")
                    + (f"; link expires {datetime.fromtimestamp(exp).date()}" if args.public else
                       f"; users only: no NAS expiry, album-cleanup ends it after "
                       f"{cfg.get('album_max_age_days', ALBUM_MAX_AGE_DAYS)} days"))
            if require_write("album-share", args, plan):
                res = share_album(c, aid, pw or None, args.download, members, exp, public=args.public)
                # The album password is meant to be handed to the recipients, so it is printed here once.
                print(json.dumps({**res, "password": pw or None}, ensure_ascii=False))
                r._refresh("albums")
        elif cmd == "album-rules":
            cond = album_condition(r, args)
            n = c.ok("SYNO.Foto.Browse.ConditionAlbum", "peek_item_count", 4, condition=js(cond)).get("count")
            members = parse_members(cfg.get("album_members") or [], list_users(c)) if cfg.get("album_members") else []
            exp = share_expiration(cfg) if members else 0
            plan = (f"create album '{args.name}' from rules {json.dumps(cond)}: {n} photo(s) match now"
                    + (f"; share with {', '.join(m['name'] + ':' + m['role'] for m in members)} (no NAS expiry for "
                       f"users: album-cleanup ends it after {cfg.get('album_max_age_days', ALBUM_MAX_AGE_DAYS)} days)"
                       if members else ""))
            if require_write("album-rules", args, plan, ("album-share",) if members else ()):
                res = create_rule_album(c, args.name, cond)
                record_created_album(res.get("album_id"), args.name, "condition")
                if members:
                    res["share"] = share_members_or_report(c, res["album_id"], members, exp)
                r._refresh("albums")
                emit(res, True)
        elif cmd == "album-cleanup":
            me = int(c.ok("SYNO.Foto.UserInfo", "me", 1).get("id"))
            now = time.time()
            mine = created_albums()
            rows = []
            for a in list_albums(c):
                if a.get("owner_user_id") != me or a.get("name") == TRASH_ALBUM or a["id"] not in mine:
                    continue
                full = album_get(c, a["id"])
                rows.append(album_status(full, me, now, int(cfg.get("album_max_age_days", ALBUM_MAX_AGE_DAYS)), mine))
            emit(rows, True)
            due = [x for x in rows if x["eligible"]]
            plan = (f"{len(due)} of {len(rows)} album(s) this tool created are due for removal"
                    + (f": {', '.join(str(x['album_id']) + ' ' + repr(x['name']) for x in due)}" if due else "")
                    + "; the photos stay in the library")
            if not args.remove:
                print(plan + (f". To remove them: album-cleanup --remove --expect {len(due)} (dry run first)."
                              if due else "."), file=sys.stderr)
            elif due and args.expect != len(due):
                raise SystemExit(f"{len(due)} album(s) are due now, not --expect {args.expect}: run the dry run again")
            elif due and require_write("album-remove", args, plan):
                for x in due:
                    emit(remove_album(c, x["album_id"], me), True)
                r._refresh("albums")
                print("Uploaded copies (folders in sync_exclude_folders) are photos: queue them with "
                      "`trash-candidate` for a human to delete if they are no longer needed.", file=sys.stderr)
        elif cmd == "album-remove":
            aid = r.get("albums", args.album)
            me = int(c.ok("SYNO.Foto.UserInfo", "me", 1).get("id"))
            if require_write("album-remove", args, f"unshare and remove album '{r.name_of('albums', aid)}' ({aid}); "
                             "the photos stay in the library"):
                emit(remove_album(c, aid, me), True)
                r._refresh("albums")
        elif cmd == "album-unshare":
            aid = r.get("albums", args.album)
            if require_write("album-unshare", args, f"stop sharing album '{r.name_of('albums', aid)}' ({aid}): "
                             "public link and users"):
                c.ok("SYNO.Foto.Sharing.Passphrase", "set_shared", 1, policy=js("album"), album_id=aid, enabled=False)
                r._refresh("albums")
                print("done")
        elif cmd == "trash-candidate":
            aid = r.get("albums", TRASH_ALBUM, required=False)
            existing = {i["id"] for i in album_items(c, aid)} if aid else set()
            new, plan = trash_plan(existing, args.ids, bool(aid))
            needs = ("album-add",) if aid or len(new) > 100 else ("album-create",)
            if require_write("trash-candidate", args, plan, needs):
                if not new:
                    print("nothing to add")
                elif not aid:
                    aid = create_album(c, TRASH_ALBUM, new)["id"]  # never removed by the tool (remove_album)
                else:
                    add_to_album(c, aid, new)
                r._refresh("albums")
                log_path = TRASH_LOG_PATH
                for i in new:
                    append_private(log_path, json.dumps({"item": i, "space": args.space, "reason": args.reason,
                                                         "at": datetime.now().isoformat(timespec="seconds")}))
                emit({"album": TRASH_ALBUM, "album_id": aid, "added": len(new), "reason_log": str(log_path)}, True)
    finally:
        c.logout()


# --- argument parsing ------------------------------------------------------------------------
def add_selection(p: argparse.ArgumentParser, with_album: bool = True) -> None:
    if with_album:
        p.add_argument("--album", help="album name or id")
        p.add_argument("--keyword", help="Photos search keyword")
    p.add_argument("--folder", type=int, help="folder id")
    p.add_argument("--person", action="append", help="person name or id (repeatable)")
    p.add_argument("--all-people", action="store_true", help="with several --person: items showing all of them")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD (capture time, inclusive)")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD (inclusive)")
    p.add_argument("--tag", action="append", help="tag name or id (repeatable)")
    p.add_argument("--rating", type=int, choices=range(0, 6), help="minimum stars")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="synophotos", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"synophotos {__version__}")
    ap.add_argument("--endpoint", choices=["auto", "lan", "public"], default="auto")
    ap.add_argument("--space", choices=["team", "personal"], help="default from config (team = Shared Space)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("-v", "--verbose", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("login-test")
    p = sp.add_parser("discover")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--save-api-info", metavar="PATH")
    for name in ("whoami", "people", "tags", "albums", "index"):
        sp.add_parser(name)
    p = sp.add_parser("folders")
    p.add_argument("--parent", type=int)
    p.add_argument("-r", "--recursive", action="store_true")
    p = sp.add_parser("items")
    add_selection(p)
    p.add_argument("--limit", type=int, default=100)
    p = sp.add_parser("count")
    add_selection(p, with_album=False)
    p = sp.add_parser("faces")
    p.add_argument("item", type=int)
    p.add_argument("--draw", metavar="DIR", help="save the XL preview with the face boxes drawn on it")
    p = sp.add_parser("item")
    p.add_argument("ids", type=int, nargs="+")
    p.add_argument("--raw", action="store_true")
    p = sp.add_parser("download")
    p.add_argument("ids", type=int, nargs="*")
    add_selection(p)
    p.add_argument("--size", choices=["original", "xl", "m", "sm"], default="original")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int)
    p = sp.add_parser("sync", help="incremental local mirror: previews, originals of rated photos, listing, faces")
    p.add_argument("--root", required=True, help="library folder (outside any git repository)")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD capture date, inclusive (default: everything)")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD, inclusive")
    p.add_argument("--originals-rating", type=int, default=4, choices=range(0, 7),
                   help="download the original of photos with at least this many stars (0 = all, 6 = none); default 4")
    p.add_argument("--faces", action=argparse.BooleanOptionalAction, default=True,
                   help="read the faces (person, box) of photos whose named people changed; one request per photo")
    p.add_argument("--limit", type=int, help="trial: at most N downloads and N face reads; listing file not written")
    p.add_argument("--run-id", help="label stored with new rows (default <date>-sync)")
    p.add_argument("--exclude-folder", type=int, action="append",
                   help="folder id to leave out, with its subfolders (repeatable; config: sync_exclude_folders)")

    def w(name: str) -> argparse.ArgumentParser:
        q = sp.add_parser(name)
        q.add_argument("--yes", action="store_true", help="apply (otherwise dry run)")
        return q

    p = w("rate")
    p.add_argument("ids", type=int, nargs="+")
    p.add_argument("--stars", type=int, choices=range(0, 6), required=True)
    for name in ("tag-add", "tag-remove"):
        p = w(name)
        p.add_argument("ids", type=int, nargs="+")
        p.add_argument("--tag", required=True)
    p = sp.add_parser("group-sheet")
    p.add_argument("person", help="person name or id (unnamed groups: the id, see `people`)")
    p.add_argument("--out", required=True, help="folder for contact sheets and the tile index")
    p.add_argument("--limit", type=int, help="max photos to scan")
    p = w("group-clean")
    p.add_argument("person")
    p.add_argument("--index", required=True, help="group_<id>_index.json written by group-sheet")
    p.add_argument("--tiles", type=int, nargs="+", required=True, help="tile numbers of the WRONG faces")
    p.add_argument("--reason", default="")
    p = w("person-name")
    p.add_argument("person", help="person id or name to (re)name")
    p.add_argument("--name", required=True, help="new name; an existing name merges the two people")
    p = w("person-remove")
    p.add_argument("item", type=int)
    p.add_argument("--person", required=True, help="the person to unlink")
    p.add_argument("--face", type=int, help="face id (see `faces ITEM`); needed when the person has several faces")
    p = w("person-add")
    p.add_argument("item", type=int)
    p.add_argument("--person", required=True, help="the person the face really shows")
    p.add_argument("--face", type=int, help="existing face id to (re)assign (see `faces ITEM`)")
    p.add_argument("--box", help="new face box x1,y1,x2,y2 as fractions 0..1 (when Photos did not detect the face)")
    p = w("folder-create")
    p.add_argument("--name", required=True, help="new folder name (an existing one under the parent is reused)")
    p.add_argument("--parent", type=int, help="parent folder id (default: the space's root)")
    p = w("upload")
    p.add_argument("files", nargs="+", help="local JPEG files")
    p.add_argument("--folder", type=int, required=True, help="target folder id (see `folders`)")
    p = w("item-move")
    p.add_argument("ids", type=int, nargs="+", help="photo ids")
    p.add_argument("--to", type=int, required=True, help="target folder id")
    p = w("folder-rename")
    p.add_argument("folder_id", type=int)
    p.add_argument("--name", required=True)
    p = w("folder-move")
    p.add_argument("folder_id", type=int, help="the folder to move, with everything in it")
    p.add_argument("--to", type=int, required=True, help="new parent folder id")
    p = w("folder-remove")
    p.add_argument("folder_id", type=int, help="an EMPTY folder (no photos, no subfolders)")
    p = w("album-create")
    p.add_argument("--name", required=True)
    p.add_argument("--items", type=int, nargs="*")
    add_selection(p, with_album=False)
    for name in ("album-add", "album-remove-item"):
        p = w(name)
        p.add_argument("album")
        p.add_argument("ids", type=int, nargs="+")
    p = w("album-rename")
    p.add_argument("album")
    p.add_argument("--name", required=True)
    p = w("album-share")
    p.add_argument("album")
    p.add_argument("--public", action="store_true", help="anyone with the link")
    p.add_argument("--password-stdin", action="store_true",
                   help="read the link password from the first line of stdin (default: a generated one)")
    p.add_argument("--no-password", action="store_true", help="a public link without a password")
    p.add_argument("--download", action="store_true", help="allow downloading")
    p.add_argument("--member", action="append", help="DSM user NAME or NAME:view|download (repeatable; plus album_members)")
    p.add_argument("--expires-days", type=int, help="days until the share expires (default share_expiry_days, 30; "
                   "at most share_max_days, 90)")
    p.add_argument("--expires-on", help="YYYY-MM-DD the share expires (end of that day)")
    p = w("album-rules")
    p.add_argument("--name", required=True)
    p.add_argument("--folder", type=int, action="append", help="folder id (repeatable)")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD capture date, inclusive")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD, inclusive")
    p.add_argument("--person", action="append", help="person name or id (repeatable)")
    p.add_argument("--all-people", action="store_true", help="photos showing all of --person (default: any)")
    p.add_argument("--favorite", action="store_true", help="only favourites")
    p = w("album-cleanup")
    p.add_argument("--remove", action="store_true", help="remove the due albums (with --yes; the photos stay)")
    p.add_argument("--expect", type=int, help="with --remove: the number of due albums the dry run showed")
    p = w("album-remove")
    p.add_argument("album", help="an album this account created")
    p = w("album-unshare")
    p.add_argument("album")
    p = w("trash-candidate")
    p.add_argument("ids", type=int, nargs="+")
    p.add_argument("--reason", default="")
    return ap


READ_CMDS = {"index", "group-sheet", "faces", "whoami", "folders", "people", "tags", "albums", "items", "count", "item", "download"}


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config()
    args.space = args.space or cfg.get("default_space", "team")
    if not hasattr(args, "folder"):
        args.folder = None
    try:
        if args.cmd == "login-test":
            cmd_login_test(cfg, args)
        elif args.cmd == "discover":
            cmd_discover(cfg, args)
        elif args.cmd == "sync":
            cmd_sync(cfg, args)
        elif args.cmd in READ_CMDS:
            cmd_read(cfg, args)
        else:
            cmd_write(cfg, args)
    except (ApiError, Refused) as e:
        raise SystemExit(redact(str(e)))


if __name__ == "__main__":
    main()
