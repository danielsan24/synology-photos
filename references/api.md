# Synology Photos web API: confirmed calls

All calls are `POST https://<host>:5001/webapi/entry.cgi` (form-encoded), except `SYNO.API.Info` on `query.cgi`.
Non-string parameters are JSON-encoded (`additional=["rating"]`, `id=[1,2]`). After login every request
carries `_sid` in the body and the `X-SYNO-TOKEN` header. `SYNO.FotoTeam.*` = Shared Space, `SYNO.Foto.*` =
the logged-in account's personal space. Albums exist only under `SYNO.Foto.*` and may contain Shared Space items.

**Status** column: `confirmed` = observed working on this NAS (source given); `public` = from third-party code,
not yet seen here; `capture needed` = shape unknown, the script refuses it until a capture confirms it.
A write is enabled in the script by adding its name to `CONFIRMED_WRITES` in `scripts/synophotos.py`.

## Auth and discovery

| Call | Params | Status |
|---|---|---|
| `SYNO.API.Auth` v7 `login` | `account, passwd, session=webui, format=sid, enable_syno_token=yes` -> `data.sid, data.synotoken` | confirmed (DevTools capture 2026-10-30). `session=SynologyPhotos` gives 402 |
| `SYNO.API.Auth` v7 `logout` | `session=webui` | confirmed |
| `SYNO.API.Info` v1 `query` (query.cgi) | `query=all` | confirmed; works unauthenticated too |
| `SYNO.Foto.UserInfo` v1 `me` | none | public |

## Read

| Call | Params | Status |
|---|---|---|
| `*.Browse.Folder` v2 `list` | `id, offset, limit, sort_by=filename, sort_direction=asc` | confirmed (team; root id 1) |
| `*.Browse.Folder` v2 `get` | none = root folder | public |
| `*.Browse.Item` v7 `list` | `folder_id` **required for team** (no folder -> error 120), `offset, limit<=500, additional, sort_by=takentime, sort_direction` | confirmed |
| `*.Browse.Item` v7 `list` | `person_id` | confirmed |
| `SYNO.Foto.Browse.Item` v7 `list` | `album_id` | public |
| `*.Browse.Item` v7 `count` | `folder_id` | confirmed (not recursive) |
| `*.Browse.Item` v7 `list_with_filter` / `count_with_filter` | `rating=[4,5]` | confirmed (live) |
| same, scope | omit `folder` for the whole space; `folder=[id]` limits to items stored directly in that folder (the UI sends it from a folder view) | confirmed (live test 2026-10-31) |
| same, date range | `time=[{"start_time":<epoch>,"end_time":<epoch>}, ...]` (several ranges are OR-ed) | confirmed (capture 1) |
| same, people | `person=[ids], person_policy=or` (`and` assumed for all-of) | confirmed (capture 1) |
| same, tags | `general_tag=[ids], general_tag_policy=or|and` | capture needed |
| `*.Browse.Item` v7 `get` | `id=[ids], additional` | public |
| `*.Browse.Person` v1 `list` | `offset, limit` -> `id, name, item_count` | confirmed (needs person recognition enabled for the space) |
| `*.Browse.GeneralTag` v2 `list` | `offset, limit` -> `id, name` | confirmed |
| `SYNO.Foto.Browse.Album` v4 `list` | `offset, limit, category="normal_share_with_me"` -> `id, name, item_count, passphrase, shared` | confirmed |
| `*.Search.Search` v7 `list_item` | `keyword, offset, limit, additional` (UI adds `start_time, end_time`) | confirmed (capture 2) |
| `*.Search.Search` v7 `suggest` | `keyword` (prefix) | confirmed (capture 2) |
| `*.Thumbnail` v2 `get` | `id, cache_key (additional.thumbnail.cache_key), type=unit, size=sm|m|xl` -> JPEG | confirmed (xl ~ 1920 px) |
| `*.Download` v2 `download` | `unit_id=[id], force_download=true` -> original, zip for live photos | confirmed |

`additional` keys confirmed: `thumbnail, resolution, orientation, exif, gps, person, rating, tag`
(`favorite` accepted but returns nothing). Item fields: `id, filename, filesize, folder_id, time
(local wall clock as epoch), indexed_time, owner_user_id, type (photo|live|video)`.

| `*.Index` v1 `get` | none -> queue sizes `basic, metadata, metadata_updating, thumbnail, face_extraction, person_clustering, concept_detection, geo_coding` (all 0 = idle) | confirmed |

Live tests 2026-10-31: face boxes are fractions of the **upright** image (checked on EXIF orientation 1, 6 and 8:
boxes drawn on the XL preview sit on the faces). Filters right after bulk edits / a merge returned intermittent
801 that disappeared moments later; the script retries reads on 801 (2, 5, 10 s) and reports `Index.get`.

## Write

Captured 2026-10-31 (`captures/03-04-album.txt`, `captures/05-07-share-rate-tag-faces.txt`). Free-text
parameters (names, passwords, passphrase, policy) are JSON-quoted, as the web app sends them.

| Command | Calls | Status |
|---|---|---|
| `rate` | `*.Browse.Item` v7 `set` `id=[ids], rating=N` | confirmed |
| `tag-add` | `*.Browse.GeneralTag` v2 `create` `name` (new tag); `*.Browse.Item` v7 `add_tag` `id=[ids], tag=[tag ids]` | confirmed |
| `tag-remove` | `*.Browse.Item` v7 `remove_tag` `id=[ids], tag=[tag ids]` | confirmed (capture 8) |
| `faces` (read) | `*.Browse.Item` v7 `list_face` `id_item` -> `face_id, person_id, name, face_bounding_box` | confirmed |
| `person-remove` | `*.Browse.Person` v1 `delete_face` `person_id, face_id=[ids]` (unlinks; photo untouched) | confirmed |
| `person-add` | `*.Browse.Person` v3 `add_face` `id_item, face=[{face_bounding_box, face_id_temp:"<item>-0", person_id}]`; to reassign an existing face, `delete_face` first and reuse its box | confirmed |
| `group-clean` | `delete_face` once per face (list form with several ids not seen) | confirmed |
| `person-name` (new name) | `*.Browse.Person` v1 `set` `id, name` -> `id, name, item_count` | confirmed (capture 9) |
| `person-name` (existing name = merge) | `*.Browse.Person` v2 `merge` `target_id, merged_id=[ids], name` (target's name) -> merged person | confirmed (capture 9) |
| `album-create` | `SYNO.Foto.Browse.NormalAlbum` v1 `create` `name="", item=[ids]`, then `SYNO.Foto.Browse.Album` v1 `set_name` `id, name` | confirmed (create response shape not seen; the script falls back to the newest unnamed album) |
| `album-add` | `SYNO.Foto.Browse.NormalAlbum` v1 `add_item` `id, item=[ids]` | confirmed (capture 8) |
| `album-remove-item` | `SYNO.Foto.Browse.NormalAlbum` v1 `delete_item` `id, item=[ids]` (album membership only) | confirmed |
| `album-rename` | `SYNO.Foto.Browse.Album` v1 `set_name` `id, name` | confirmed |
| `album-share` | `SYNO.Foto.Sharing.Passphrase` v1 `set_shared` `policy="album", album_id, enabled=true`; `SYNO.Foto.Browse.Album` v4 `get` `id=[id], additional=["sharing_info"]` -> `passphrase`; `Passphrase` v1 `update` `passphrase, password, expiration=0, permission=[{"action":"update","role":"download"|"view","member":{"type":"public"}}]`; link = `sharing_info.sharing_link` | confirmed |
| `album-unshare` | `SYNO.Foto.Sharing.Passphrase` v1 `set_shared` `policy="album", album_id, enabled=false` | confirmed |
| `trash-candidate` | `album-create` when the album is missing, else `album-add` | confirmed |

Other reads seen: `SYNO.Foto.Browse.Album` v4 `list` `category="normal_share_with_me", sort_by="create_time", sort_direction="desc"`;
`*.Browse.GeneralTag` v2 `list` / `suggest(keyword)`; `SYNO.Foto.Sharing.Misc` v1 `list_user_group`.

## Never used (refused at the transport layer, see `FORBIDDEN_METHOD_RE`)

Any method whose name has a part delete / purge / empty / move / copy / destroy / wipe / trash, any `SYNO.Core.*`,
`SYNO.FileStation.*`, `SYNO.Foto.Setting.*` and folder-permission API. Three exact exceptions, each confirmed to leave every photo in the library: `SYNO.Foto.Browse.NormalAlbum.delete_item`
(album membership) and `SYNO.Foto(Team).Browse.Person.delete_face` (person-to-face link). `remove_tag` is not delete-like.

## Capture checklist

Each capture is saved as `captures/<n>-<action>.json` (output of `scripts/capture_logger.js`, secrets removed).

1. Filter by date range **and** a person in the Photos web app.
2. Search a keyword.
3. Create an empty album; add 2 photos; remove 1; rename it.
4. Share that album: public link, password, download allowed. Then stop sharing.
5. Set 3 stars on a test photo, then back to its old rating.
6. Add a new tag to a test photo, remove it.
7. On a test photo: remove a wrongly recognised person from a face; assign a face to a person.
8. Optional: create a conditional album (date + person).


## Folders and uploads (captures 10 and 11, 2026-11-16)

| Call | Parameters | Status |
|---|---|---|
| `*.Browse.Folder` v1 `create` | `target_id` (parent folder id; Shared Space root 1), `name` JSON-quoted (`"Name"`). Answer: `folder {id, name: "/Name", parent, ...}` | confirmed (team) |
| `*.Upload.Item` v8 `upload_to_folder` | multipart: `file` (the JPEG), `thumb_xl`, `thumb_m`, `thumb_sm` (JPEG previews the browser makes: short edge 1280 / 320 / 240 px; the NAS then serves exactly those bytes), `name` JSON-quoted, `mtime` (file mtime, seconds), `target_folder_id`, `duplicate` `"ignore"` (the web app's default; the transport sends nothing else; its effect on a same-name file was not tested live, so `upload` also skips files whose name is already in the folder). Answer: `{action: "new", id, unit_id}` | confirmed (team) |

A JPEG without an EXIF capture date is dated by the upload: a test file without EXIF became an item dated by the upload day. Files meant to keep their date need EXIF DateTimeOriginal and a matching mtime.

## Moving photos and removing empty folders (captures 12 and 13, 2026-11-16)

| Call | Parameters | Status |
|---|---|---|
| `SYNO.FotoTeam.BackgroundTask.File` v1 `move` | `target_folder_id`, `item_id` [ids], `folder_id` [] (folder ids to move whole folders: not captured with a value), `action` `"skip"` (name clash), `extra_info` JSON string `{"version":2,"source_library":"shared_space","source_folder_ids":[...]}`. Answer: `task_info {id, status: waiting, total}` | confirmed (team); the transport sends only `"skip"` |
| `SYNO.Foto.BackgroundTask.Info` v1 `get_status` | `id` [task ids]. Answer: `list [{id, status: done, completion, skip, error}]` | confirmed |
| `SYNO.FotoTeam.BackgroundTask.File` v1 `delete` | `item_id` [], `folder_id` [one id] | confirmed (team), **guarded**: the same call deletes photos when `item_id` is filled. The transport sends it only with `item_id` [] and one folder (never the root), and only for the folder `folder-remove` has just found empty while Photos was idle (armed call) |

The first folder delete (601) was made in a tab without the recorder and was not captured; 603 was captured.

## Renaming and moving folders (captures 14 and 15, 2026-11-16)

| Call | Parameters | Status |
|---|---|---|
| `SYNO.FotoTeam.Browse.Folder` v1 `rename` | `id`, `name` JSON-quoted. Synchronous; answer `folder {id, name: "/New", parent}` | confirmed (team) |
| `SYNO.FotoTeam.BackgroundTask.File` v1 `move` (a folder) | `target_folder_id`, `folder_id` [the folder], `item_id` [], `action` `"skip"`, `extra_info` with `source_folder_ids` [the folder's parent]. Background task | confirmed (team) |

Live test 2026-11-16 (synophotos, test folders without photos): `folder-rename` ok; `folder-move` first failed with
DSM 120 because `extra_info` was encoded once; it must be a JSON string of compact JSON (encoded twice), as in the
captures. Fixed (`move_extra`, also used by `item-move`), then `folder-move` ok (task 29), `folder-remove` of two
empty folders ok (tasks 30, 31). Writes are journaled before they are sent, so a failed call leaves a journal line.
`item-move` uses the same corrected encoding; not yet run live.

Live 2026-11-16, a batch upload: the first multipart upload failed with DSM 119 (SID not found) with
`_sid` only in the body; with the session also sent as the `id` cookie of the upload request, every file uploaded.
JPEGs with EXIF DateTimeOriginal are dated by it (checked on the uploaded items), camera and GPS read from EXIF.

## Sharing with users, expiry, rule albums (captures 16 and 17, 2026-11-16)

| Call | Parameters | Status |
|---|---|---|
| `SYNO.Foto.Sharing.Misc` v1 `list_user_group` | `team_space_sharable_list=false` -> `list [{id, name, type: user|group}]` | confirmed |
| `SYNO.Foto.Sharing.Passphrase` v1 `update` (users, expiry) | `permission` holds the public member and/or `{"role": "view"|"download", "action": "update", "member": {"type": "user", "id": uid}}`; `expiration` epoch seconds of 23:59:59 local on the last day (0 = never) | confirmed; a user has only view or download |
| `SYNO.Foto.Browse.ConditionAlbum` v4 `peek_item_count` / `create` | `condition` JSON: `user_id` 0 (Shared Space), `item_type` [], `folder_filter` [ids], `time` [{start_time, end_time}] (local day bounds as UTC, like the item filter), `favorite` [true], `person` [ids], `person_policy` "or"|"and"; `create` adds `name`. Answer `album {id, type: "condition", create_time}` | confirmed |
| `SYNO.Foto.Browse.ConditionAlbum` v4 `suggest` | `keyword`, `user_id`, `condition` [kinds] -> values per kind (person, general_tag, camera, lens, geocoding, concept, iso, aperture, exposure, focal length) | read, not used yet |
| Album fields for cleanup | `Browse.Album get additional=["sharing_info"]`: `create_time`, `owner_user_id`, `shared`; `sharing_info.expiration`, `is_expired`, `owner` | confirmed |

Live 2026-11-16: a test album re-shared as public link (download, password) plus one user (download), expiring
in 30 days; `album-cleanup` lists it (age 0, not due). Rule albums and removing albums: not yet run live / captured.

## Removing albums (capture 18, 2026-11-16)

| Call | Parameters | Status |
|---|---|---|
| `SYNO.Foto.Sharing.Passphrase` v1 `set_shared` `enabled=false` | stop sharing (link and users) | confirmed (= album-unshare) |
| `SYNO.Foto.Browse.Album` v1 `delete` | `id` [one album id]. Removes the album; Photos keeps its photos | confirmed on a rule album; the transport sends it for exactly one id, armed by `remove_album`, which accepts only an album this tool created (local registry) and this account owns |

Live 2026-11-16: `album-remove` on a normal test album with 1 photo: unshared, removed, the photo stays in its
folder. The automatic share of a new album with users only (`set_shared enabled=false`, then
`update` with user members and an expiration) failed with DSM 120: not captured; refused until it is. That album was
created unshared and removed.

Live 2026-11-16 (capture 19 applied): a new test album auto-shared with one user as download, public link off:
that user sees it under "Shared with me". The NAS keeps no expiry for a users-only share (expiration reads back 0):
expiry applies to the public link only, so album-cleanup ends users-only albums after album_max_age_days. The test album
was then removed; its photo stays.
