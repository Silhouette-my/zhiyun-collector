"""Zhiyun-only authenticated API adapter. No credentials cross its protocol.

API shapes and playback signing adapted from zju-learning-cli and
zju-learning-assistant; see THIRD_PARTY_NOTICES.txt in this distribution.
"""

from __future__ import annotations

from datetime import date
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import secrets
import ssl
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit, unquote, parse_qsl
import zipfile

import certifi

from .collector import (CollectorError, MAX_IMAGE, MAX_META, MAX_SOURCE_UNCOMPRESSED, _media_request,
                        _normalized_host, _safe_title, _slide_header, _convert_stream)

CLASSROOM = "classroom.zju.edu.cn"
YJAPI = "yjapi.cmc.zju.edu.cn"
SERVICE = "org.handouter.zhiyun.collector.v2"
CAPABILITIES = ["auth_status", "auth_login", "auth_logout", "catalog", "plan", "collect"]
MAX_ITEMS = 1000
PLAN_SECONDS = 900


class DirectError(CollectorError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _vault():
    # Select only a native OS vault, never keyring's optional plaintext backend.
    try:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
            return Keyring()
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring
            return WinVaultKeyring()
    except ImportError as exc:
        raise DirectError("SECURE_STORAGE_UNAVAILABLE") from exc
    raise DirectError("SECURE_STORAGE_UNAVAILABLE")


class State:
    def __init__(self):
        if sys.platform == "win32":
            base = Path(os.environ["LOCALAPPDATA"])
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            raise DirectError("UNSUPPORTED_PLATFORM")
        self.root = base / "Handouter" / "plugins" / "zhiyun" / "v2"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.vault = _vault()

    def session(self):
        value = self.vault.get_password(SERVICE, "session")
        return json.loads(value) if value else None

    def set_session(self, token):
        value = {"token": token, "id": secrets.token_hex(16)}
        self.vault.set_password(SERVICE, "session", json.dumps(value))
        return value

    def logout(self):
        if self.vault.get_password(SERVICE, "session"):
            self.vault.delete_password(SERVICE, "session")

    def put(self, kind, data):
        identifier = secrets.token_hex(24)
        path = self.root / (kind + "-" + identifier + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        return identifier

    def get(self, kind, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{48}", identifier):
            raise DirectError("INVALID_RESOURCE")
        path = self.root / (kind + "-" + identifier + ".json")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_META:
            raise DirectError("INVALID_RESOURCE")
        return json.loads(path.read_text(encoding="utf-8"))


def _api(host, path, token, params=None):
    if host not in {CLASSROOM, YJAPI}:
        raise DirectError("API_FAILED")
    target = path + ("?" + urlencode(params) if params else "")
    connection = http.client.HTTPSConnection(host, timeout=30,
        context=ssl.create_default_context(cafile=certifi.where()))
    try:
        connection.request("GET", target, headers={"Authorization": "Bearer " + token,
                           "Accept": "application/json", "User-Agent": "Handouter-collector/2"})
        response = connection.getresponse()
        if response.status == 401:
            raise DirectError("AUTH_REQUIRED")
        if response.status == 403:
            raise DirectError("ACCESS_DENIED")
        if response.status != 200:
            raise DirectError("API_FAILED")
        raw = response.read(2 * MAX_META + 1)
        if len(raw) > 2 * MAX_META:
            raise DirectError("API_RESPONSE_INVALID")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise DirectError("API_RESPONSE_INVALID")
        if result.get("code") not in {None, 0}:
            raise DirectError("RESOURCE_UNAVAILABLE")
        return result
    finally:
        connection.close()


def _token(cookies):
    for cookie in cookies:
        for morsel in cookie.values():
            if morsel["domain"].lstrip(".") not in {CLASSROOM, "zju.edu.cn"}:
                continue
            match = re.search(r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"([^"\r\n]+)";\}', unquote(morsel.value))
            if match and len(match[1]) <= 1500:
                return match[1]
    return None


def login(state):
    # Native WebViews keep passwords out of Python and all protocol frames.
    # An ephemeral profile prevents a second, unencrypted session cache.
    import webview
    logging.getLogger("pywebview").disabled = True
    result = []
    errors = []
    done = threading.Event()
    window = webview.create_window("智云课堂 · 登录", "https://tgmedia.cmc.zju.edu.cn/index.php?"
        "r=auth/login&auType=cmc&tenant_code=112&forward=https%3A%2F%2Fclassroom.zju.edu.cn%2F",
        width=1000, height=760)

    def monitor():
        try:
            # pywebview can otherwise fall back to legacy MSHTML on Windows.
            expected = "edgechromium" if sys.platform == "win32" else "wkwebview"
            if window.gui.renderer != expected:
                raise DirectError("LOGIN_ENGINE_UNAVAILABLE")
            deadline = time.monotonic() + 300
            while not done.wait(1) and time.monotonic() < deadline:
                url = window.get_current_url()
                if url and urlsplit(url).hostname == CLASSROOM:
                    token = _token(window.get_cookies())
                    if token:
                        _api(CLASSROOM, "/userapi/v1/infosimple", token)["params"]
                        state.set_session(token)
                        result.append(True)
                        window.destroy()
                        return
        except Exception:
            errors.append(True)
        finally:
            if not done.is_set():
                window.destroy()

    window.events.closed += done.set
    # pywebview's private mode uses an in-memory cookie store on both targets.
    webview.start(monitor, private_mode=True, debug=False,
                  gui="edgechromium" if sys.platform == "win32" else "cocoa")
    if errors:
        raise DirectError("AUTH_FAILED")
    if not result:
        raise DirectError("AUTH_CANCELLED")
    return {"type": "auth", "status": "signed_in"}


class Adapter:
    def __init__(self, state=None):
        self.state = state or State()

    def _session(self):
        session = self.state.session()
        if not session:
            raise DirectError("AUTH_REQUIRED")
        return session

    def _info(self, session):
        return _api(CLASSROOM, "/userapi/v1/infosimple", session["token"])["params"]

    def auth_status(self):
        session = self.state.session()
        if not session:
            return {"type": "auth", "status": "signed_out"}
        try:
            self._info(session)
        except DirectError as exc:
            if exc.code != "AUTH_REQUIRED":
                raise
            self.state.logout()
            return {"type": "auth", "status": "signed_out"}
        return {"type": "auth", "status": "signed_in"}

    def auth_login(self):
        return login(self.state)

    def auth_logout(self):
        self.state.logout()
        return {"type": "auth", "status": "signed_out"}

    def catalog(self, parent_id, cursor, query):
        session = self._session()
        if parent_id is None:
            if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", cursor)):
                raise DirectError("INVALID_RESOURCE")
            page = int(cursor or "1")
            info = self._info(session)
            data = _api(CLASSROOM, "/pptnote/v1/searchlist", session["token"], {
                "tenant_id": 112, "user_id": info["id"], "user_name": info["account"],
                "page": page, "per_page": 16, "title": query, "realname": "",
                "trans": "", "tenant_code": 112})["total"]
            rows = data["list"]
            if not isinstance(rows, list) or len(rows) > 16:
                raise DirectError("API_RESPONSE_INVALID")
            items = []
            for row in rows:
                title = _safe_title(row["title"])
                resource = {"course_id": int(row["course_id"]), "title": title, "course_title": title, "session": session["id"]}
                items.append({"id": self.state.put("resource", resource), "title": resource["title"], "kind": "collection"})
            if len(json.dumps(items, ensure_ascii=False).encode()) > 15 * 1024:
                raise DirectError("API_RESPONSE_INVALID")
            return {"type": "catalog", "items": items,
                    "next_cursor": str(page + 1) if page * 16 < int(data["total"]) else None}
        if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", cursor)):
            raise DirectError("INVALID_RESOURCE")
        offset = int(cursor or "1") - 1
        parent = self._resource(parent_id, session)
        if "sub_id" in parent:
            raise DirectError("INVALID_RESOURCE")
        info = self._info(session)
        data = _api(YJAPI, "/courseapi/v3/multi-search/get-course-detail", session["token"],
                    {"course_id": parent["course_id"], "student": info["account"]})["data"]
        resources = []
        for year in (data.get("sub_list") or {}).values():
            for month in year.values():
                for week in month.values():
                    for row in week:
                        if len(resources) >= MAX_ITEMS:
                            raise DirectError("CATALOG_TOO_LARGE")
                        title = _safe_title(row["sub_title"])
                        if query.casefold() not in title.casefold():
                            continue
                        begin = int(row.get("class_begin") or 0)
                        resource = {**parent, "sub_id": int(row["id"]), "title": title,
                                    "lecture_date": date.fromtimestamp(begin).isoformat() if begin else None}
                        resources.append(resource)
        resources.sort(key=lambda resource: resource["sub_id"])
        items = []
        for resource in resources[offset:offset + 50]:
            item = {"id": self.state.put("resource", resource), "title": resource["title"], "kind": "resource"}
            if len(json.dumps(items + [item], ensure_ascii=False).encode()) > 15 * 1024:
                break
            items.append(item)
        next_offset = offset + len(items)
        return {"type": "catalog", "items": items,
                "next_cursor": str(next_offset + 1) if next_offset < len(resources) else None}

    def _resource(self, identifier, session):
        resource = self.state.get("resource", identifier)
        if resource["session"] != session["id"]:
            raise DirectError("AUTH_REQUIRED")
        return resource

    def _assets(self, resource, session):
        token = session["token"]
        params = {"course_id": resource["course_id"], "sub_id": resource["sub_id"]}
        data = _api(CLASSROOM, "/courseapi/v3/portal-home-setting/get-sub-info", token, params)["data"]
        raw = data["content"]["save_playback"]["contents"]
        _host(raw)
        info = self._info(session)
        timestamp = int(time.time())
        # Platform-defined request authentication, not a cryptographic integrity check.
        content = urlsplit(raw).path + str(info["id"]) + str(info["tenant_id"]) + str(info["phone"])[::-1] + str(timestamp)
        signature = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
        video = raw + ("&" if "?" in raw else "?") + urlencode({"t": f'{info["id"]}-{timestamp}-{signature}'})
        slides, seen = [], set()
        for page in range(1, 51):
            response = _api(CLASSROOM, "/pptnote/v1/schedule/search-ppt", token,
                            {**params, "page": page, "per_page": 100})
            rows = response.get("list") or []
            added = 0
            for row in rows:
                # Preserve repeated occurrences; only discard duplicated API records.
                marker = json.dumps(row, sort_keys=True)
                if marker in seen:
                    continue
                seen.add(marker)
                content = json.loads(row["content"])
                url = content["pptimgurl"]
                parts = urlsplit(url)
                if parts.scheme == "http" and parts.hostname and parts.hostname.endswith(".zju.edu.cn"):
                    url = "https:" + url[5:]
                _host(url)
                slides.append({"url": url, "seconds": None, "index": len(slides) + 1})
                added += 1
                if len(slides) > 10000:
                    raise DirectError("API_RESPONSE_INVALID")
            if len(slides) >= int(response.get("total") or 0):
                break
            if not added or page == 50:
                raise DirectError("ASSETS_INCOMPLETE")
        return video, slides

    def plan(self, resource_id):
        session = self._session()
        resource = self._resource(resource_id, session)
        if "sub_id" not in resource:
            raise DirectError("INVALID_RESOURCE")
        video, slides = self._assets(resource, session)
        hosts = sorted({_host(video), *(_host(slide["url"]) for slide in slides)})
        if len(hosts) > 8:
            raise DirectError("HOST_NOT_ALLOWED")
        plan = {"resource": resource, "media_hosts": hosts, "revision": _revision(video, slides),
                "expires_at": int(time.time()) + PLAN_SECONDS}
        plan_id = self.state.put("plan", plan)
        return {"type": "plan", "plan_id": plan_id, "title": resource["title"],
                "lecture_date": resource["lecture_date"], "media_hosts": hosts, "expires_at": plan["expires_at"]}

    def collect(self, plan_id, allowed_media_hosts, output_path, progress):
        session = self._session()
        plan = self.state.get("plan", plan_id)
        if plan["expires_at"] <= int(time.time()):
            raise DirectError("PLAN_EXPIRED")
        if plan["resource"]["session"] != session["id"]:
            raise DirectError("AUTH_REQUIRED")
        if set(plan["media_hosts"]) != set(allowed_media_hosts):
            raise DirectError("HOST_NOT_ALLOWED")
        target = Path(output_path).expanduser().absolute()
        if not target.parent.is_dir() or target.exists() or target.is_symlink():
            raise DirectError("INVALID_OUTPUT")
        video, slides = self._assets(plan["resource"], session)
        hosts = {_host(video), *(_host(slide["url"]) for slide in slides)}
        if hosts != set(allowed_media_hosts):
            raise DirectError("HOST_NOT_ALLOWED")
        if plan["revision"] != _revision(video, slides):
            raise DirectError("PLAN_EXPIRED")
        # Spill only public asset bytes to disk, avoiding a second in-memory copy
        # of a large slide ZIP. TemporaryFile is unlinked on POSIX and opened
        # delete-on-close on Windows; a killed collector does not retain it.
        # The signed media URL goes to the conversion core only as an argument.
        with tempfile.TemporaryFile(mode="w+b", prefix=".collector-assets-", dir=target.parent) as source:
            progress("preparing")
            with zipfile.ZipFile(source, "x") as archive:
                resource = plan["resource"]
                archive.writestr("course_info.json", json.dumps({"courseName": resource["course_title"], "lectureDate": resource["lecture_date"]}))
                metadata = []
                total_images = 0
                progress("downloading")
                for index, slide in enumerate(slides, 1):
                    with _media_request(slide["url"], _host(slide["url"]), False) as response:
                        data = response.read(MAX_IMAGE + 1)
                    if len(data) > MAX_IMAGE or not _slide_header(data[:12]):
                        raise DirectError("ASSET_DOWNLOAD_FAILED")
                    total_images += len(data)
                    if total_images > MAX_SOURCE_UNCOMPRESSED - 2 * MAX_META:
                        raise DirectError("ASSETS_TOO_LARGE")
                    filename = f"slide_{index:04d}"
                    archive.writestr("slides/" + filename, data)
                    metadata.append({"filename": filename, "seconds": slide["seconds"], "index": slide["index"]})
                archive.writestr("slides_meta.json", json.dumps(metadata))
            source.seek(0)
            _convert_stream(source, target, allowed_media_host=_host(video), media_url=video,
                    progress=lambda stage: progress(stage) if stage in {"packaging", "publishing"} else None)
        return {"type": "result", "output_path": os.fspath(target)}


def _host(url):
    if not isinstance(url, str) or len(url) > 8192:
        raise DirectError("API_RESPONSE_INVALID")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise DirectError("INSECURE_RESOURCE")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise DirectError("INSECURE_RESOURCE")
    return _normalized_host(parts.hostname)


def _revision(video, slides):
    # Bind the confirmed asset list without persisting private URLs. The platform's
    # expiring authentication parameters may refresh; resource references may not.
    def reference(url):
        parsed = urlsplit(url)
        query = [(key, value) for key, value in parse_qsl(parsed.query)
                 if key.lower() not in {"t", "auth_key", "token", "signature", "expires"}]
        return [parsed.scheme, parsed.netloc, parsed.path, sorted(query)]
    references = [reference(video), *[reference(slide["url"]) for slide in slides]]
    return hashlib.sha256(json.dumps(references, separators=(",", ":")).encode()).hexdigest()
