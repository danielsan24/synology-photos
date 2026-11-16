"""Offline tests: no network, no Keychain. Run: uv run --with pytest --with requests --with pyyaml --with dnspython
--with keyring --with pillow pytest -q scripts/test_synophotos.py"""

import argparse
import io
import re
import zipfile
from pathlib import Path

import pytest

import synophotos as s

SRC = Path(s.__file__).read_text()
CFG = {"public_host": "nas.test", "lan_host": "", "nas_port": 5001, "account": "acct", "keychain_service": "svc",
       "ca_bundle": "system"}


class _Resp:
    def __init__(self, body, ctype="application/json"):
        self._b, self.headers, self.content = body, {"Content-Type": ctype}, b"bin"

    def raise_for_status(self):
        pass

    def json(self):
        return self._b


def client(monkeypatch, body):
    calls = []
    c = s.SynoClient(CFG, connect=False)
    c.MIN_INTERVAL_S = 0
    monkeypatch.setattr(s, "get_password", lambda cfg: "pw-test-123")
    monkeypatch.setattr(c.session, "post", lambda url, **kw: calls.append((url, kw)) or _Resp(body))
    return c, calls


def test_login_single_attempt_post_only(monkeypatch):
    c, calls = client(monkeypatch, {"success": True, "data": {"sid": "sid-abcdef", "synotoken": "tok-xyz1"}})
    c.login()
    assert len(calls) == 1
    url, kw = calls[0]
    assert "?" not in url and "pw-test-123" not in url
    assert kw["data"]["session"] == "webui" and kw["data"]["passwd"] == "pw-test-123"
    assert kw["headers"]["Host"] == "nas.test:5001"
    assert c.session.verify is True
    assert s.redact("x sid-abcdef tok-xyz1 passwd=pw-test-123") == "x *** *** passwd=***"


def test_login_failure_no_retry(monkeypatch):
    c, calls = client(monkeypatch, {"success": False, "error": {"code": 407}})
    with pytest.raises(SystemExit, match="Auto Block"):
        c.login()
    assert len(calls) == 1


def test_params_json_encoded_and_sid_attached(monkeypatch):
    c, calls = client(monkeypatch, {"success": True, "data": {"list": []}})
    c.sid, c.synotoken = "S1D-123", "T0K-456"
    c.ok("SYNO.FotoTeam.Browse.Item", "list", 7, folder_id=3, additional=["rating"], sort_by="takentime")
    d = calls[0][1]["data"]
    assert d["folder_id"] == "3" and d["additional"] == '["rating"]' and d["sort_by"] == "takentime"
    assert d["_sid"] == "S1D-123" and calls[0][1]["headers"]["X-SYNO-TOKEN"] == "T0K-456"


@pytest.mark.parametrize("api,method", [
    ("SYNO.Foto.Browse.Item", "delete"), ("SYNO.FotoTeam.Browse.Item", "delete"),
    ("SYNO.Foto.Browse.NormalAlbum", "delete"), ("SYNO.Foto.Browse.Item", "move"),
    ("SYNO.Foto.Browse.Item", "copy"), ("SYNO.Foto.Browse.Folder", "delete_folder"),
    ("SYNO.Foto.Browse.Item", "move_to_trash"), ("SYNO.Foto.Browse.Item", "DELETE"),
    ("SYNO.Core.Share", "list"), ("SYNO.FileStation.Delete", "start"),
])
def test_forbidden_requests_never_sent(monkeypatch, api, method):
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    with pytest.raises(s.Refused):
        c.call(api, method, 1)
    assert calls == []


def test_album_membership_face_and_tag_edits_allowed():
    s.SynoClient.check_allowed("SYNO.Foto.Browse.NormalAlbum", "delete_item")
    s.SynoClient.check_allowed("SYNO.FotoTeam.Browse.Person", "delete_face")
    s.SynoClient.check_allowed("SYNO.FotoTeam.Browse.Item", "remove_tag")
    for api in ("SYNO.FotoTeam.Browse.Item", "SYNO.Foto.Browse.Item", "SYNO.Foto.Browse.Album"):
        with pytest.raises(s.Refused):
            s.SynoClient.check_allowed(api, "delete_item")


def test_source_has_no_delete_method_calls():
    """No API call in the script names a delete-like method (string literal as method argument)."""
    calls = re.findall(r"\.(?:ok|call|fetch|pages)\(\s*[^,]+,\s*\"([a-z_]+)\"", SRC)
    assert calls, "regex should find API calls"
    allowed = {m for _, m in s.ALLOWED_DESPITE_RE}
    bad = [m for m in calls if s.FORBIDDEN_METHOD_RE.search(m) and m not in allowed]
    assert bad == []


def ns(**kw):
    base = dict(date_from=None, date_to=None, person=None, tag=None, rating=None, all_people=False)
    return argparse.Namespace(**{**base, **kw})


def test_filter_date_people_tags_rating():
    f = s.build_filter(None, ns(date_from="2024-01-01", date_to="2024-12-31", person=["12", "34"], all_people=True,
                                tag=["5"], rating=4))
    assert f["time"] == [{"start_time": 1704067200, "end_time": 1735689599}]
    assert f["person"] == [12, 34] and f["person_policy"] == "and"
    assert f["general_tag"] == [5] and f["rating"] == [4, 5]
    assert s.build_filter(None, ns()) == {}


def test_resolver_uses_cache_without_network(monkeypatch):
    cache = {"discovered_at": 10**10, "spaces": {"team": {"people": {"Alice Example": {"id": 7, "items": 1}},
                                                          "tags": {"beach": 3}}}, "albums": {"Trash candidate": {"id": 9}}}
    monkeypatch.setattr(s, "cache_fresh", lambda c: True)
    r = s.Resolver(c=None, cfg=CFG, space="team", cache=cache)
    assert r.get("people", "alice example") == 7 and r.get("tags", "beach") == 3
    assert r.get("albums", "Trash candidate") == 9 and r.get("people", "42") == 42 and r.get("people", "#42") == 42


def test_trash_plan_dedups_and_never_deletes():
    new, plan = s.trash_plan({1, 2}, [2, 3, 3, 4], album_exists=True)
    assert new == [3, 4] and "Nothing is deleted" in plan and "1 already there" in plan
    new, plan = s.trash_plan(set(), [5], album_exists=False)
    assert new == [5] and "will be created" in plan


def test_unconfirmed_write_refused_and_dry_run_default():
    with pytest.raises(SystemExit, match="not confirmed"):
        s.require_write("not-a-write", argparse.Namespace(yes=True, unconfirmed=True), "plan")
    assert s.require_write("rate", argparse.Namespace(yes=False), "plan") is False
    with pytest.raises(SystemExit):  # no hidden flag skips the capture gate
        s.parser().parse_args(["rate", "1", "--stars", "3", "--unconfirmed"])


def test_unzip_live_photo_keeps_still():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("IMG_1.MOV", b"video")
        z.writestr("IMG_1.HEIC", b"still")
    assert s.unzip_still(buf.getvalue()) == (b"still", "IMG_1.HEIC")
    assert s.unzip_still(b"\xff\xd8jpeg") == (b"\xff\xd8jpeg", None)


def test_download_names_and_resume(monkeypatch, tmp_path):
    c, calls = client(monkeypatch, {})
    monkeypatch.setattr(c, "fetch", lambda *a, **kw: b"\xff\xd8data")
    it = {"id": 11, "time": 1548936000, "filename": "FOTO 27.JPG", "additional": {"thumbnail": {"cache_key": "11_1"}}}
    p, new = s.download_one(c, "team", it, "xl", tmp_path)
    assert new and p.name == "2019-01-31_11_FOTO_27_xl.jpg"
    p2, new2 = s.download_one(c, "team", it, "original", tmp_path)
    assert new2 and p2.name == "2019-01-31_11_FOTO_27.JPG"
    assert s.download_one(c, "team", it, "original", tmp_path)[1] is False
    assert s.download_one(c, "team", it, "xl", tmp_path)[1] is False


class Recorder:
    """Fake client: records (api, method, version, params) and answers from a table."""

    arm = s.SynoClient.arm
    check_armed = s.SynoClient.check_armed

    def __init__(self, answers, index="idle"):
        self.calls, self.answers, self._armed, self.index = [], answers, {}, index

    def index_status(self, api="SYNO.FotoTeam"):
        return self.index

    def ok(self, api, method, version, **params):
        s.SynoClient.check_allowed(api, method)
        s.SynoClient.check_params(api, method, params)
        self.check_armed(api, method, params)
        self.calls.append((api, method, version, params))
        a = self.answers.get((api, method), {})
        return a(params) if callable(a) else a

    def pages(self, api, method, version, limit=None, **params):
        yield from self.ok(api, method, version, **params).get("list", [])


def test_create_album_unnamed_then_named():
    rec = Recorder({("SYNO.Foto.Browse.NormalAlbum", "create"): {"album": {"id": 28}}})
    assert s.create_album(rec, "Alice 2024", [1, 2]) == {"id": 28, "name": "Alice 2024"}
    assert rec.calls[0] == ("SYNO.Foto.Browse.NormalAlbum", "create", 1, {"name": '""', "item": [1, 2]})
    assert rec.calls[1] == ("SYNO.Foto.Browse.Album", "set_name", 1, {"id": 28, "name": '"Alice 2024"'})


def test_create_album_finds_id_when_response_lacks_it():
    rec = Recorder({("SYNO.Foto.Browse.NormalAlbum", "create"): {"list": []},
                    ("SYNO.Foto.Browse.Album", "list"): {"list": [{"id": 31, "name": ""}, {"id": 30, "name": "x"}]}})
    assert s.create_album(rec, "n", [5])["id"] == 31


def test_share_album_public_download_with_password():
    album = {"list": [{"id": 29, "passphrase": "PPabc123", "additional": {"sharing_info": {
        "sharing_link": "https://nas.test:5001/mo/sharing/PPabc123", "privacy_type": "public-download",
        "enable_password": True}}}]}
    rec = Recorder({("SYNO.Foto.Browse.Album", "get"): album})
    res = s.share_album(rec, 29, "pw-xyz", download=True)
    assert res["link"].endswith("/mo/sharing/PPabc123") and res["privacy"] == "public-download"
    names = [(a, m) for a, m, _, _ in rec.calls]
    assert names[0] == ("SYNO.Foto.Sharing.Passphrase", "set_shared")
    assert rec.calls[0][3] == {"policy": '"album"', "album_id": 29, "enabled": True}
    upd = [p for a, m, _, p in rec.calls if m == "update"][0]
    assert upd["passphrase"] == '"PPabc123"' and upd["password"] == '"pw-xyz"' and upd["expiration"] == 0
    assert upd["permission"] == [{"action": "update", "role": "download", "member": {"type": "public"}}]


def test_pick_face():
    faces = [{"face_id": 1, "person_id": 10}, {"face_id": 2, "person_id": 20}, {"face_id": 3, "person_id": 20}]
    assert s.pick_face(faces, None, 10)["face_id"] == 1
    assert s.pick_face(faces, 3, None)["face_id"] == 3
    with pytest.raises(SystemExit, match="several"):
        s.pick_face(faces, None, 20)
    with pytest.raises(SystemExit, match="not on this photo"):
        s.pick_face(faces, 9, None)


def test_all_writes_confirmed_and_needs_checked(monkeypatch):
    assert {"album-add", "tag-remove", "trash-candidate"} <= s.CONFIRMED_WRITES
    monkeypatch.setattr(s, "CONFIRMED_WRITES", s.CONFIRMED_WRITES - {"album-add"})
    with pytest.raises(SystemExit, match="album-add"):
        s.require_write("trash-candidate", argparse.Namespace(yes=True), "p", needs=("album-add",))


def test_group_faces_only_this_person():
    items = {"list": [{"id": 1, "time": 0, "filename": "a.jpg", "additional": {"thumbnail": {"cache_key": "k1"}}},
                      {"id": 2, "time": 0, "filename": "b.jpg"}]}
    faces = {1: [{"face_id": 11, "person_id": 7, "face_bounding_box": {"b": 1}}, {"face_id": 12, "person_id": 8,
                                                                                 "face_bounding_box": {}}],
             2: [{"face_id": 21, "person_id": 7, "face_bounding_box": {"b": 2}}]}
    rec = Recorder({("SYNO.FotoTeam.Browse.Item", "list"): items,
                    ("SYNO.FotoTeam.Browse.Item", "list_face"): lambda p: {"list": faces[p["id_item"]]}})
    rows = s.group_faces(rec, "team", 7)
    assert [(r["tile"], r["item"], r["face_id"]) for r in rows] == [(1, 1, 11), (2, 2, 21)]
    assert rec.calls[0][3]["person_id"] == 7


def test_unlink_faces_journals_before_each_call(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "JOURNAL_PATH", tmp_path / "j.jsonl")
    rec = Recorder({})
    rows = [{"item": 1, "face_id": 11, "box": {"x": 1}}, {"item": 2, "face_id": 21, "box": {"x": 2}}]
    s.unlink_faces(rec, "team", 7, rows, "test")
    assert [c[3] for c in rec.calls] == [{"person_id": 7, "face_id": [11]}, {"person_id": 7, "face_id": [21]}]
    lines = (tmp_path / "j.jsonl").read_text().splitlines()
    assert len(lines) == 2 and '"box": {"x": 1}' in lines[0]


def test_resolve_tiles():
    idx = [{"tile": 1, "face_id": 5}, {"tile": 2, "face_id": 6}]
    assert [r["face_id"] for r in s.resolve_tiles(idx, [2, 2])] == [6]
    with pytest.raises(SystemExit, match="not in the sheet"):
        s.resolve_tiles(idx, [3])


def test_person_name_and_group_clean_confirmed():
    assert {"person-name", "group-clean"} <= s.CONFIRMED_WRITES


def test_filter_has_no_default_folder_scope():
    """Live test: folder=[1] restricts list_with_filter to the root folder itself."""
    rec = Recorder({("SYNO.FotoTeam.Browse.Item", "list_with_filter"): {"list": []}})
    args = argparse.Namespace(space="team", album=None, keyword=None, folder=None, date_from="2024-07-01",
                              date_to="2024-07-31", person=None, tag=None, rating=None, all_people=False)
    s.select_items(rec, None, args, 10)
    assert "folder" not in rec.calls[0][3] and "time" in rec.calls[0][3]
    args.folder = 609
    s.select_items(rec, None, args, 10)
    assert rec.calls[1][3]["folder"] == [609]


def test_face_crop_inside_image():
    from PIL import Image
    im = Image.new("RGB", (1000, 500))
    box = {"top_left": {"x": 0.9, "y": 0.8}, "bottom_right": {"x": 1.0, "y": 1.0}}
    c = s.face_crop(im, box, size=160)
    assert 0 < c.width <= 160 and 0 < c.height <= 160


def test_801_retried_for_reads_not_writes(monkeypatch):
    seq = [{"success": False, "error": {"code": 801}}, {"success": True, "data": {"count": 5}}]
    c, calls = client(monkeypatch, None)
    c.RETRY_WAITS = (0, 0, 0)
    answers = iter(seq)
    monkeypatch.setattr(c, "call", lambda api, method, v, **p: next(answers) if method != "get" else {"success": True, "data": {}})
    assert c.ok("SYNO.FotoTeam.Browse.Item", "count_with_filter", 7) == {"count": 5}
    monkeypatch.setattr(c, "call", lambda api, method, v, **p: {"success": False, "error": {"code": 801}})
    with pytest.raises(s.ApiError):
        c.ok("SYNO.FotoTeam.Browse.Item", "set", 7, id=[1], rating=2)


# --- sync ------------------------------------------------------------------------------------
def _item(i, rating=0, name="a.jpg", people=(), t=1700000000):
    return {"id": i, "type": "photo", "filename": name, "folder_id": 3, "time": t, "filesize": 10, "indexed_time": 5,
            "additional": {"rating": rating, "resolution": {"width": 4000, "height": 3000},
                           "thumbnail": {"cache_key": f"{i}_k"}, "person": [{"id": k, "name": n} for k, n in people]}}


def test_sync_action(tmp_path):
    (tmp_path / "thumbs").mkdir()
    (tmp_path / "thumbs" / "t1.jpg").write_bytes(b"x")
    e = {"file": "thumbs/t1.jpg"}
    assert s.sync_action(_item(1), None, tmp_path, 4) == "thumb"
    assert s.sync_action(_item(1, 5), None, tmp_path, 4) == "original"
    assert s.sync_action(_item(1, 5, "a.NEF"), None, tmp_path, 4) == "thumb+original"
    assert s.sync_action(_item(1), e, tmp_path, 4) == "skip"
    assert s.sync_action(_item(1, 4), e, tmp_path, 4) == "upgrade"
    assert s.sync_action(_item(1, 4), e, tmp_path, 6) == "skip"
    assert s.sync_action(_item(1), {"file": "thumbs/gone.jpg"}, tmp_path, 4) == "thumb"


def test_sync_rows_and_signature():
    it = _item(7, 4, people=[(2, "Bella Example"), (1, "Alice Example"), (9, "")])
    r = s.library_row(it, "team", "run")
    assert r["photo_id"] == "t7" and r["people"] == ["Alice Example", "Bella Example"] and r["rating"] == 4
    assert r["taken_at"] == "2023-11-14T22:13:20" and (r["width"], r["height"]) == (4000, 3000)
    assert s.library_row(it, "personal", "run")["photo_id"] == "p7"
    assert s.people_sig(it) == "1=Alice Example;2=Bella Example" and s.people_sig(_item(8)) == ""
    f = s.face_rows([{"face_id": 5, "person_id": 1, "name": "Alice Example",
                      "face_bounding_box": {"top_left": {"x": 0.1, "y": 0.2}, "bottom_right": {"x": 0.3, "y": 0.5}}}])
    assert f == [{"face_id": 5, "person_id": 1, "name": "Alice Example", "box": [0.1, 0.2, 0.3, 0.5]}]


def test_sync_library_merge_never_drops():
    old = {1: {"id": 1, "taken_at": "2024-01-01T00:00:00"}, 2: {"id": 2, "taken_at": "2024-02-01T00:00:00"},
           3: {"id": 3, "taken_at": "2020-02-01T00:00:00"}}
    rows = s.merge_library(old, [{"id": 2, "taken_at": "2024-02-01T00:00:00", "rating": 5}], "2023-01-01", "2025-01-01", "r2")
    by = {r["id"]: r for r in rows}
    assert by[1]["missing_since"] == "r2" and by[2]["rating"] == 5 and "missing_since" not in by[3] and len(rows) == 3


def test_sync_download_writes_files_and_index(monkeypatch, tmp_path):
    c, calls = client(monkeypatch, {})
    monkeypatch.setattr(c.session, "post", lambda url, **kw: calls.append((url, kw)) or _Resp({}, "image/jpeg"))
    (tmp_path / "thumbs" / "xl").mkdir(parents=True)
    e, n = s.sync_download(c, "team", _item(11), "thumb", None, tmp_path, "run")
    assert e["file"] == "thumbs/xl/t11.jpg" and (tmp_path / e["file"]).read_bytes() == b"bin" and n == 3
    assert e["photo_id"] == "t11" and e["cache_key"] == "11_k" and e["versions"][0]["seen_run"] == "run"
    e2, _ = s.sync_download(c, "team", _item(11, 5), "upgrade", e, tmp_path, "run2")
    assert e2["file"] == e["file"] and e2["original_file"].startswith("originals/2023/2023-11-14_t11_")
    assert (tmp_path / e2["original_file"]).exists()
    assert all(kw["data"]["method"] in ("get", "download") for _, kw in calls)


def test_sync_refuses_git_repository(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(SystemExit, match="git repository"):
        s.cmd_sync(CFG, argparse.Namespace(root=str(tmp_path)))


# --- folder-create and upload (captures 10 and 11, 2026-11-16) ------------------------------------
def test_create_folder_shape_and_reuse():
    rec = Recorder({("SYNO.FotoTeam.Browse.Folder", "create"): {"folder": {"id": 601, "name": "/New"}}})
    assert s.create_folder(rec, "team", 1, "New") == {"id": 601, "name": "/New", "existed": False}
    assert rec.calls[-1] == ("SYNO.FotoTeam.Browse.Folder", "create", 1, {"target_id": 1, "name": '"New"'})
    rec2 = Recorder({})
    rec2.pages = lambda *a, **kw: iter([{"id": 7, "name": "/New"}])
    assert s.create_folder(rec2, "team", 1, "New")["existed"] is True and rec2.calls == []
    with pytest.raises(SystemExit):
        s.create_folder(rec, "team", 1, "a/b")


def test_upload_multipart_fields_and_thumbs(monkeypatch, tmp_path):
    from PIL import Image
    f = tmp_path / "01_x.jpg"
    Image.new("RGB", (4080, 3060), (200, 100, 50)).save(f, "JPEG")
    c, calls = client(monkeypatch, {"success": True, "data": {"action": "new", "id": 7001, "unit_id": 7001}})
    c.sid = "S1D"
    assert s.upload_file(c, "team", 601, f) == {"file": "01_x.jpg", "id": 7001, "action": "new"}
    kw = calls[0][1]
    d, files = kw["data"], kw["files"]
    assert (d["api"], d["method"], d["version"]) == ("SYNO.FotoTeam.Upload.Item", "upload_to_folder", "8")
    assert d["duplicate"] == '"ignore"' and d["name"] == '"01_x.jpg"' and d["target_folder_id"] == "601"
    assert d["mtime"].isdigit() and d["_sid"] == "S1D"
    assert kw["headers"]["Cookie"] == "id=S1D" and "S1D" not in calls[0][0]
    assert files["file"][0] == "01_x.jpg" and files["file"][2] == "image/jpeg"
    import io as _io
    sizes = {k: Image.open(_io.BytesIO(files[k][1])).size for k in ("thumb_xl", "thumb_m", "thumb_sm")}
    assert sizes == {"thumb_xl": (1707, 1280), "thumb_m": (427, 320), "thumb_sm": (320, 240)}


def test_upload_refuses_non_jpeg(monkeypatch, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    with pytest.raises(SystemExit):
        s.upload_file(c, "team", 1, f)
    assert calls == []


def test_folder_create_and_upload_confirmed():
    assert {"folder-create", "upload"} <= s.CONFIRMED_WRITES


def test_excluded_folders_include_subfolders():
    tree = {5: [{"id": 6}], 6: [{"id": 7}], 7: [], 9: []}
    rec = Recorder({})
    rec.pages = lambda api, method, v, limit=None, **p: iter(tree.get(p["id"], []))
    assert s.excluded_folders(rec, "team", [5, 9]) == {5, 6, 7, 9}
    assert s.excluded_folders(rec, "team", []) == set()



# --- item-move and folder-remove (captures 12 and 13, 2026-11-16) ------------------------------------
F = "SYNO.FotoTeam.BackgroundTask.File"


def _tasks(rec, status="done"):
    rec.answers[("SYNO.Foto.BackgroundTask.Info", "get_status")] = {"list": [{"id": 24, "status": status, "skip": 0, "error": 0}]}


def test_photo_delete_refused_at_transport(monkeypatch):
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    for params in ({"item_id": [7001], "folder_id": []}, {"item_id": [1], "folder_id": [603]},
                   {"item_id": [], "folder_id": [1, 2]}, {"folder_id": [603]}, {"item_id": [], "folder_id": []}):
        with pytest.raises(s.Refused):
            c.ok(F, "delete", 1, **params)
    assert calls == []
    for api, method in (("SYNO.FotoTeam.Browse.Item", "delete"), ("SYNO.Foto.BackgroundTask.File", "delete"),
                        ("SYNO.FotoTeam.Browse.Folder", "delete"), ("SYNO.Foto.Browse.Item", "move")):
        with pytest.raises(s.Refused):
            c.ok(api, method, 1, item_id=[], folder_id=[5])


def test_move_items_shape_journal_and_wait(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "JOURNAL_PATH", tmp_path / "j.jsonl")
    monkeypatch.setattr(s.time, "sleep", lambda t: None)
    rec = Recorder({("SYNO.FotoTeam.Browse.Item", "get"): {"list": [{"id": 7001, "folder_id": 601}]},
                    (F, "move"): {"task_info": {"id": 24, "status": "waiting"}}})
    _tasks(rec)
    out = s.move_items(rec, "team", [7001], 602)
    assert out == [{"items": 1, "from": [601], "to": 602, "task": 24, "status": "done", "skipped": 0, "error": 0}]
    mv = [x for x in rec.calls if x[1] == "move"][0]
    assert mv[3] == {"target_folder_id": 602, "item_id": [7001], "folder_id": [], "action": '"skip"',
                     "extra_info": '"{\\"version\\":2,\\"source_library\\":\\"shared_space\\",\\"source_folder_ids\\":[601]}"'}
    j = (tmp_path / "j.jsonl").read_text()
    assert '"move"' in j and "[7001, 601]" in j
    with pytest.raises(SystemExit):
        s.move_items(rec, "personal", [7001], 602)


def test_remove_folder_only_when_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "JOURNAL_PATH", tmp_path / "j.jsonl")
    monkeypatch.setattr(s.time, "sleep", lambda t: None)
    folder = {("SYNO.FotoTeam.Browse.Folder", "get"): {"folder": {"id": 603, "name": "/X", "parent": 1}}}
    rec = Recorder({("SYNO.FotoTeam.Browse.Item", "count"): {"count": 0},
                    (F, "delete"): {"task_info": {"id": 26, "status": "waiting"}}, **folder})
    rec.pages = lambda *a, **kw: iter([])
    _tasks(rec)
    assert s.remove_empty_folder(rec, "team", 603)["status"] == "done"
    assert [x for x in rec.calls if x[1] == "delete"][0][3] == {"item_id": [], "folder_id": [603]}
    assert len([x for x in rec.calls if x[1] == "count"]) == 2  # counted twice, while the index is idle
    assert '"name": "/X"' in (tmp_path / "j.jsonl").read_text()
    rec2 = Recorder({("SYNO.FotoTeam.Browse.Item", "count"): {"count": 3}, **folder})
    rec2.pages = lambda *a, **kw: iter([])
    with pytest.raises(SystemExit):
        s.remove_empty_folder(rec2, "team", 603)
    rec3 = Recorder({("SYNO.FotoTeam.Browse.Item", "count"): {"count": 0}, **folder})
    rec3.pages = lambda *a, **kw: iter([{"id": 9}])
    with pytest.raises(SystemExit):
        s.remove_empty_folder(rec3, "team", 603)
    rec4 = Recorder({("SYNO.FotoTeam.Browse.Item", "count"): {"count": 0}, **folder}, index={"basic": 12})
    rec4.pages = lambda *a, **kw: iter([])
    with pytest.raises(SystemExit, match="indexing"):  # files not indexed yet would not be counted
        s.remove_empty_folder(rec4, "team", 603)
    for r in (rec2, rec3, rec4):
        assert not [x for x in r.calls if x[1] == "delete"]
    with pytest.raises(SystemExit):
        s.remove_empty_folder(rec, "team", 1)


# --- folder-rename and folder-move (captures 14 and 15, 2026-11-16) ---------------------------------
def test_rename_folder_shape_and_guards(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "JOURNAL_PATH", tmp_path / "j.jsonl")
    rec = Recorder({("SYNO.FotoTeam.Browse.Folder", "get"): {"folder": {"id": 602, "name": "/A", "parent": 1}},
                    ("SYNO.FotoTeam.Browse.Folder", "rename"): {"folder": {"id": 602, "name": "/B"}}})
    rec.pages = lambda *a, **kw: iter([{"id": 602, "name": "/A"}, {"id": 7, "name": "/C"}])
    assert s.rename_folder(rec, "team", 602, "B") == {"folder": 602, "old": "/A", "name": "/B"}
    assert rec.calls[-1] == ("SYNO.FotoTeam.Browse.Folder", "rename", 1, {"id": 602, "name": '"B"'})
    for fid, name in ((602, "C"), (602, "x/y"), (1, "Z")):
        with pytest.raises(SystemExit):
            s.rename_folder(rec, "team", fid, name)
    assert "rename_folder" in (tmp_path / "j.jsonl").read_text()


def test_move_folder_shape_and_guards(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "JOURNAL_PATH", tmp_path / "j.jsonl")
    monkeypatch.setattr(s.time, "sleep", lambda t: None)
    info = {602: {"id": 602, "name": "/A", "parent": 1}, 604: {"id": 604, "name": "/P", "parent": 1},
            630: {"id": 630, "name": "/A/sub", "parent": 602}}
    rec = Recorder({("SYNO.FotoTeam.Browse.Folder", "get"): lambda p: {"folder": info.get(p["id"], {})},
                    (F, "move"): {"task_info": {"id": 25, "status": "waiting"}}})
    tree = {602: [{"id": 630}], 630: [], 604: []}
    rec.pages = lambda api, method, v, limit=None, **p: iter(tree.get(p["id"], []))
    _tasks(rec)
    out = s.move_folder(rec, "team", 602, 604)
    assert out["status"] == "done" and out["from"] == 1 and out["to"] == 604
    mv = [x for x in rec.calls if x[1] == "move"][-1]
    assert mv[3] == {"target_folder_id": 604, "item_id": [], "folder_id": [602], "action": '"skip"',
                     "extra_info": '"{\\"version\\":2,\\"source_library\\":\\"shared_space\\",\\"source_folder_ids\\":[1]}"'}
    n = len([x for x in rec.calls if x[1] == "move"])
    for fid, target in ((602, 630), (602, 602), (602, 1), (1, 604)):
        with pytest.raises(SystemExit):
            s.move_folder(rec, "team", fid, target)
    assert len([x for x in rec.calls if x[1] == "move"]) == n


# --- members, expiry, rule albums, cleanup (captures 16 and 17, 2026-11-16) --------------------------
def test_share_with_members_and_expiry():
    rec = Recorder({("SYNO.Foto.Browse.Album", "get"): {"list": [{"id": 31, "passphrase": "PP", "additional": {"sharing_info": {}}}]}})
    members = [{"name": "u1", "id": 5001, "role": "view"}]
    s.share_album(rec, 31, "pw", True, members, 1797461999, public=True)
    upd = [x for x in rec.calls if x[1] == "update"][0][3]
    assert upd["expiration"] == 1797461999 and upd["password"] == '"pw"'
    assert upd["permission"] == [{"action": "update", "role": "download", "member": {"type": "public"}},
                                 {"role": "view", "action": "update", "member": {"type": "user", "id": 5001}}]
    rec.calls.clear()
    s.share_album(rec, 31, None, True, members, 1797461999, public=False)  # users only (capture 19)
    assert rec.calls[0][1] == "set_shared" and rec.calls[0][3]["enabled"] is True
    upd = [x for x in rec.calls if x[1] == "update"][0][3]
    assert upd["expiration"] == 0  # users-only: the NAS keeps no expiry; album-cleanup ends the album
    assert "password" not in upd and upd["permission"] == [
        {"role": "view", "action": "update", "member": {"type": "user", "id": 5001}}]


def test_share_expiration_default_and_bounds():
    from datetime import date, datetime as dt, timedelta
    e = s.share_expiration({})
    d = dt.fromtimestamp(e)
    assert d.date() == date.today() + timedelta(days=30) and (d.hour, d.minute, d.second) == (23, 59, 59)
    assert dt.fromtimestamp(s.share_expiration({"share_expiry_days": 7})).date() == date.today() + timedelta(days=7)
    with pytest.raises(SystemExit):
        s.share_expiration({}, on=date.today().isoformat())


def test_parse_members_roles_and_unknown():
    users = {"user.one": 5001}
    assert s.parse_members(["user.one"], users) == [{"name": "user.one", "id": 5001, "role": "download"}]
    assert s.parse_members([{"user": "user.one", "role": "view"}], users)[0]["role"] == "view"
    for bad in (["user.one:manage"], ["nobody"]):
        with pytest.raises(SystemExit):
            s.parse_members(bad, users)


def test_album_condition_shape():
    class R:
        def get(self, what, key, required=True):
            return {"Alice": 5100}[key]
    args = argparse.Namespace(folder=[605], date_from="2025-11-01", date_to="2026-11-01", favorite=True,
                              person=["Alice"], all_people=False)
    assert s.album_condition(R(), args) == {"user_id": 0, "item_type": [], "folder_filter": [605],
                                            "time": [{"start_time": 1761955200, "end_time": 1793577599}],
                                            "favorite": [True], "person": [5100], "person_policy": "or"}
    with pytest.raises(SystemExit):
        s.album_condition(R(), argparse.Namespace(folder=None, date_from=None, date_to=None, favorite=False,
                                                  person=None, all_people=False))


def test_album_status_eligibility():
    now, day = 2_000_000_000, 86400
    old = {"id": 1, "create_time": now - 40 * day, "owner_user_id": 3, "shared": True,
           "additional": {"sharing_info": {"expiration": now + day}}}
    young = {"id": 2, "create_time": now - 2 * day, "owner_user_id": 3, "shared": True,
             "additional": {"sharing_info": {"expiration": now + day}}}
    expired = {**young, "id": 3, "additional": {"sharing_info": {"expiration": now - day, "is_expired": True}}}
    unshared = {"id": 4, "create_time": now - 2 * day, "owner_user_id": 3, "shared": False, "additional": {}}
    other = {**old, "id": 5, "owner_user_id": 1}
    st = {a["id"]: s.album_status(a, 3, now, 30, {1, 2, 3, 4, 5}) for a in (old, young, expired, unshared, other)}
    assert st[1]["eligible"] and st[3]["eligible"] and st[4]["eligible"]
    assert not st[2]["eligible"] and not st[5]["eligible"]
    assert not s.album_status(old, 3, now, 30, {2})["eligible"]  # owned, due, but not created by this tool
    assert not s.album_status(old, 3, now, 30)["eligible"]
    assert st[3]["reasons"] == ["share expired"] and st[4]["reasons"] == ["not shared"]


# --- album removal (capture 18) ------------------------------------------------------------------
def test_album_delete_only_one_id(monkeypatch):
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    for params in ({"id": [31, 32]}, {"id": []}, {"id": "[31]"}, {}):
        with pytest.raises(s.Refused):
            c.ok("SYNO.Foto.Browse.Album", "delete", 1, **params)
    assert calls == []
    with pytest.raises(s.Refused):
        c.ok("SYNO.Foto.Browse.NormalAlbum", "delete", 1, id=[31])


def test_remove_album_owner_trash_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "JOURNAL_PATH", tmp_path / "j.jsonl")
    monkeypatch.setattr(s, "CREATED_ALBUMS_PATH", tmp_path / "created.jsonl")
    for aid in (31, 40, 41):
        s.record_created_album(aid, "x", "normal")
    albums = {31: {"id": 31, "name": "Cal", "owner_user_id": 3, "type": "normal", "shared": True, "create_time": 1},
              40: {"id": 40, "name": "Theirs", "owner_user_id": 1, "type": "normal"},
              41: {"id": 41, "name": s.TRASH_ALBUM, "owner_user_id": 3, "type": "normal"}}
    rec = Recorder({("SYNO.Foto.Browse.Album", "get"): lambda p: {"list": [albums[p["id"][0]]]}})
    rec.pages = lambda *a, **kw: iter([{"id": 7}, {"id": 8}])
    assert s.remove_album(rec, 31, 3) == {"album_id": 31, "name": "Cal", "removed": True, "photos_kept": 2}
    names = [x[1] for x in rec.calls]
    assert names.index("set_shared") < names.index("delete") and rec.calls[-1][3] == {"id": [31]}
    assert '"items": [7, 8]' in (tmp_path / "j.jsonl").read_text()
    for aid in (40, 41):
        with pytest.raises(SystemExit):
            s.remove_album(rec, aid, 3)
    assert names.count("delete") == 1
    albums[42] = {"id": 42, "name": "Own, made by hand", "owner_user_id": 3, "type": "normal"}
    with pytest.raises(SystemExit, match="not created by this tool"):
        s.remove_album(rec, 42, 3)
    assert [x[1] for x in rec.calls].count("delete") == 1


# --- publishing review: guards are pinned, allow-list, armed removals, overwrite never ---------------
def test_guard_sets_are_pinned():
    """Changing a guard is a deliberate, reviewed change to this test too: never widen it for a delete."""
    assert s.FORBIDDEN_METHOD_RE.pattern == r"(?:^|_)(delete|purge|empty|move|copy|destroy|wipe|trash)(?:_|$)"
    assert s.ALLOWED_DESPITE_RE == {
        ("SYNO.Foto.Browse.NormalAlbum", "delete_item"), ("SYNO.FotoTeam.BackgroundTask.File", "move"),
        ("SYNO.FotoTeam.BackgroundTask.File", "delete"), ("SYNO.Foto.Browse.Album", "delete"),
        ("SYNO.FotoTeam.Browse.Person", "delete_face"), ("SYNO.Foto.Browse.Person", "delete_face")}
    assert s.ARMED_CALLS == {("SYNO.FotoTeam.BackgroundTask.File", "delete"): "folder_id",
                             ("SYNO.Foto.Browse.Album", "delete"): "id"}
    deleting = {(a, m) for a, ms in s.ALLOWED_CALLS.items() for m in ms if s.FORBIDDEN_METHOD_RE.search(m)}
    assert deleting == {("SYNO.Foto.Browse.NormalAlbum", "delete_item"), ("SYNO.Foto.BackgroundTask.File", "move"),
                        ("SYNO.Foto.BackgroundTask.File", "delete"), ("SYNO.Foto.Browse.Album", "delete"),
                        ("SYNO.Foto.Browse.Person", "delete_face")}


def test_every_call_in_source_is_on_the_allow_list():
    for api, method in re.findall(r"\.(?:ok|call|fetch|pages)\(\s*\"([A-Za-z.]+)\",\s*\"([a-z_]+)\"", SRC):
        s.SynoClient.check_allowed(api, method)


@pytest.mark.parametrize("api,method", [
    ("SYNO.FotoTeam.Browse.Item", "remove"), ("SYNO.Foto.Browse.Item", "deleteItem"), ("SYNO.Foto.Browse.Item", "delete2"),
    ("syno.filestation.delete", "start"), ("SYNO.Foto.Browse.Folder", "remove"), ("SYNO.Foto.Browse.Unknown", "list"),
    ("SYNO.Foto.BackgroundTask.File", "delete"), ("SYNO.Foto.BackgroundTask.File", "move"),
])
def test_allow_list_refuses_anything_else(monkeypatch, api, method):
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    with pytest.raises(s.Refused):
        c.call(api, method, 1, item_id=[], folder_id=[5], action='"skip"')
    assert calls == []


def test_removals_need_an_armed_target(monkeypatch):
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    with pytest.raises(s.Refused, match="right after its own checks"):
        c.ok(F, "delete", 1, item_id=[], folder_id=[555])
    with pytest.raises(s.Refused):
        c.ok("SYNO.Foto.Browse.Album", "delete", 1, id=[31])
    c.arm(F, "delete", 556)
    with pytest.raises(s.Refused):  # armed for another folder
        c.ok(F, "delete", 1, item_id=[], folder_id=[555])
    c.arm(F, "delete", 1)
    with pytest.raises(s.Refused, match="root"):
        c.ok(F, "delete", 1, item_id=[], folder_id=[1])
    assert calls == []
    c.arm(F, "delete", 555)
    c.ok(F, "delete", 1, item_id=[], folder_id=[555])
    with pytest.raises(s.Refused):  # one call per arm
        c.ok(F, "delete", 1, item_id=[], folder_id=[555])
    assert len(calls) == 1


def test_overwrite_never_sent(monkeypatch):
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    for action in ('"overwrite"', '"rename"', None):
        with pytest.raises(s.Refused):
            c.ok(F, "move", 1, target_folder_id=2, item_id=[5], folder_id=[], action=action)
    with pytest.raises(s.Refused):
        c.ok_upload("SYNO.FotoTeam.Upload.Item", "upload_to_folder", 8, {"file": ("a.jpg", b"x", "image/jpeg")},
                    duplicate='"overwrite"')
    assert calls == []


def test_redirect_not_followed(monkeypatch):
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    r = _Resp({})
    r.status_code = 307
    monkeypatch.setattr(c.session, "post", lambda url, **kw: calls.append(kw) or r)
    with pytest.raises(s.Refused, match="redirect"):
        c.ok("SYNO.FotoTeam.Browse.Item", "list", 7)
    assert calls[0]["allow_redirects"] is False


def test_resolver_prefers_names_over_ids(monkeypatch):
    cache = {"discovered_at": 10**10, "spaces": {"team": {"people": {}, "tags": {}}},
             "albums": {"2024": {"id": 7}, "Other": {"id": 2024}}}
    monkeypatch.setattr(s, "cache_fresh", lambda c: True)
    r = s.Resolver(c=None, cfg=CFG, space="team", cache=cache)
    assert r.get("albums", "2024") == 7 and r.get("albums", "#2024") == 2024
    assert r.name_of("albums", 2024) == "Other"


def test_share_refuses_to_drop_a_public_link_expiry():
    album = {"list": [{"id": 31, "passphrase": "PP", "additional": {"sharing_info": {
        "permission": [{"role": "view", "member": {"type": "public"}}]}}}]}
    rec = Recorder({("SYNO.Foto.Browse.Album", "get"): album})
    with pytest.raises(SystemExit, match="public link"):
        s.share_album(rec, 31, None, True, [{"name": "u1", "id": 5001, "role": "view"}], 0, public=False)
    assert not [x for x in rec.calls if x[1] == "update"]


def test_share_expiry_is_bounded():
    from datetime import date, timedelta
    with pytest.raises(SystemExit, match="within 90 days"):
        s.share_expiration({}, days=36500)
    with pytest.raises(SystemExit):
        s.share_expiration({}, on=(date.today() + timedelta(days=91)).isoformat())
    s.share_expiration({}, days=90)


def test_redaction_covers_share_keys():
    assert "PPsecret9" not in s.redact('{"passphrase": "PPsecret9", "password": "pw1"}')
    assert "pw1" not in s.redact("password=pw1&x=1")
    assert "XYZ12" not in s.redact('"sharing_link": "https://nas.test:5001/mo/sharing/XYZ12"')


def test_read_jsonl_tolerates_torn_last_line(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text('{"a": 1}\n{"a": 2}\n{"a": ')
    assert list(s.read_jsonl(f)) == [{"a": 1}, {"a": 2}]
    f.write_text('{"a": 1}\n{"a"\n{"a": 3}\n')
    with pytest.raises(ValueError):  # damage in the middle is not hidden
        list(s.read_jsonl(f))


def test_download_is_atomic(monkeypatch, tmp_path):
    c, _ = client(monkeypatch, {})
    it = {"id": 11, "time": 1548936000, "filename": "a.jpg", "additional": {}}
    def boom(*a, **kw):
        raise OSError("killed")
    monkeypatch.setattr(s, "write_atomic", lambda path, data: (path.with_name(path.name + ".part").write_bytes(b"half"), boom()))
    monkeypatch.setattr(c, "fetch", lambda *a, **kw: b"\xff\xd8data")
    with pytest.raises(OSError):
        s.download_one(c, "team", it, "original", tmp_path)
    monkeypatch.undo()
    c, _ = client(monkeypatch, {})
    monkeypatch.setattr(c, "fetch", lambda *a, **kw: b"\xff\xd8data")
    p, new = s.download_one(c, "team", it, "original", tmp_path)  # the .part file is not taken for a finished one
    assert new and p.read_bytes() == b"\xff\xd8data" and not p.name.endswith(".part")


def test_private_files(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "JOURNAL_PATH", tmp_path / "state" / "j.jsonl")
    s.journal("x", a=1)
    assert (tmp_path / "state" / "j.jsonl").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state").stat().st_mode & 0o777 == 0o700
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    monkeypatch.setattr(s, "JOURNAL_PATH", shared / "j.jsonl")
    s.journal("x", a=1)
    assert shared.stat().st_mode & 0o777 == 0o755  # a folder the user chose is never re-permissioned
    monkeypatch.setattr(s, "CACHE_PATH", tmp_path / "cache" / "discovery.json")
    s.save_cache({"albums": {}})
    assert (tmp_path / "cache" / "discovery.json").stat().st_mode & 0o777 == 0o600


def test_move_stops_after_a_failed_batch(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "JOURNAL_PATH", tmp_path / "j.jsonl")
    monkeypatch.setattr(s.time, "sleep", lambda t: None)
    rec = Recorder({("SYNO.FotoTeam.Browse.Item", "get"): {"list": [{"id": i, "folder_id": 601} for i in (1, 2, 3)]},
                    (F, "move"): {"task_info": {"id": 24, "status": "waiting"}}})
    _tasks(rec, status="error")
    out = s.move_items(rec, "team", [1, 2, 3], 602, batch=1)
    assert len(out) == 1 and out[0]["status"] == "error"
    assert len([x for x in rec.calls if x[1] == "move"]) == 1


# --- round 2 of the publishing review ---------------------------------------------------------------
def test_index_unknown_is_not_idle(monkeypatch):
    c, _ = client(monkeypatch, {"success": False, "error": {"code": 105}})
    assert c.index_status() == "unknown"
    c, _ = client(monkeypatch, {"success": True, "data": {}})
    assert c.index_status() == "unknown"
    c, _ = client(monkeypatch, {"success": True, "data": {"basic": 0, "metadata": 0}})
    assert c.index_status() == "idle"
    folder = {("SYNO.FotoTeam.Browse.Folder", "get"): {"folder": {"id": 626, "name": "/X", "parent": 1}},
              ("SYNO.FotoTeam.Browse.Item", "count"): {"count": 0}}
    rec = Recorder(folder, index="unknown")
    rec.pages = lambda *a, **kw: iter([])
    with pytest.raises(SystemExit, match="indexing"):
        s.remove_empty_folder(rec, "team", 626)


def test_armed_calls_take_exactly_their_parameters(monkeypatch):
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    c.arm(F, "delete", 555)
    with pytest.raises(s.Refused):
        c.ok(F, "delete", 1, item_id=[], folder_id=[555], unit_id=[9])
    c.arm("SYNO.Foto.Browse.Album", "delete", 31)
    with pytest.raises(s.Refused):
        c.ok("SYNO.Foto.Browse.Album", "delete", 1, id=[31], item=[5])
    assert calls == []


def test_root_never_moved_or_renamed_at_transport(monkeypatch):
    c, calls = client(monkeypatch, {"success": True, "data": {}})
    with pytest.raises(s.Refused, match="root"):
        c.ok(F, "move", 1, target_folder_id=5, item_id=[], folder_id=[1], action='"skip"')
    with pytest.raises(s.Refused, match="root"):
        c.ok("SYNO.FotoTeam.Browse.Folder", "rename", 1, id=1, name='"x"')
    for fids in (["1"], "[1]", None):
        with pytest.raises(s.Refused):
            c.ok(F, "move", 1, target_folder_id=5, item_id=[], folder_id=fids, action='"skip"')
    for fid in ("1", [1], True):
        with pytest.raises(s.Refused):
            c.ok("SYNO.FotoTeam.Browse.Folder", "rename", 1, id=fid, name='"x"')
    assert calls == []


def test_migration_copies_and_skips_overridden_paths(monkeypatch, tmp_path):
    cache, state = tmp_path / "cache", tmp_path / "state"
    cache.mkdir()
    (cache / "journal.jsonl").write_text('{"a": 1}\n')
    monkeypatch.setattr(s, "CACHE_PATH", cache / "discovery.json")
    monkeypatch.setattr(s, "STATE_DIR", state)
    monkeypatch.setenv("SYNOPHOTOS_STATE", str(state))
    s.migrate_state()
    assert not (state / "journal.jsonl").exists()  # an overridden state folder never receives the user's journal
    monkeypatch.delenv("SYNOPHOTOS_STATE")
    monkeypatch.delenv("SYNOPHOTOS_CACHE", raising=False)
    s.migrate_state()
    assert (state / "journal.jsonl").read_text() == '{"a": 1}\n' and (cache / "journal.jsonl").exists()


def test_captures_hold_no_live_secrets():
    """Captures are scrubbed by hand before they are saved: fail on anything that looks like a real share key,
    link code, session id or token."""
    caps = Path(s.__file__).resolve().parent.parent / "references" / "captures"
    bad = []
    for f in caps.iterdir():
        t = f.read_text()
        for m in re.finditer(r'(passphrase|sharing_link|_sid|sid|synotoken|password|passwd)\\?"?\s*[:=]\s*\\?"*([^"\\,}\s]*)', t):
            v = m.group(2)
            if v and v not in ("***", "<album", "<link>", "<album passphrase>", "''", "false", "true", "null") \
                    and not v.startswith("<") and not v.startswith("*") \
                    and not (v.startswith("https://mynas.synology.me") and "/mo/sharing/<" in t[m.start():m.end() + 20]):
                bad.append((f.name, m.group(0)[:60]))
        bad += [(f.name, x) for x in re.findall(r"/mo/sharing/[A-Za-z0-9]{5,}", t)]
    assert bad == []
