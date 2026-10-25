"""Convert a private browser export into the site-neutral local asset format.

Only this acquisition-side tool reads a signed media URL. Its output contains
landed media files and material metadata, never a URL, cookie or auth token.
"""

from __future__ import annotations

from datetime import date
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import ssl
import tempfile
import time
import urllib.parse
import zipfile

import certifi

MAX_SOURCE_FILES = 10_000
MAX_SOURCE_UNCOMPRESSED = 512 * 1024**2
MAX_IMAGE = 32 * 1024**2
MAX_MEDIA = 7 * 1024**3
MAX_META = 1024 * 1024
MAX_PLAYLIST = 2 * 1024 * 1024
MAX_SEGMENTS = 10_000
CHUNK = 1024 * 1024
# Earliest ZIP timestamp; used for every member so packaging is deterministic.
PACKAGE_DATE_TIME = (1980, 1, 1, 0, 0, 0)


class CollectorError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while block := source.read(CHUNK):
                digest.update(block)
    except OSError as exc:
        raise CollectorError("浏览器资产包不可读取") from exc
    return digest.hexdigest()


def _sha256_stream(stream) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    while block := stream.read(CHUNK):
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SENSITIVE_TITLE = re.compile(
    r"(?i)(?:https?://|www\.|auth[_-]?key|cookie|token|signature|sign\s*=|x-amz-|access[_-]?key)"
)


def _safe_title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "智云课程"
    title = " ".join(value.split())[:200]
    if _SENSITIVE_TITLE.search(title) or any(ord(character) < 32 for character in title):
        return "智云课程"
    return title


def _normalized_host(host: str) -> str:
    normalized = host.encode("idna").decode("ascii").lower()
    if len(normalized) > 253 or any(not _HOST_LABEL.fullmatch(label)
                                    for label in normalized.rstrip(".").split(".")):
        raise ValueError("invalid host")
    return normalized


def inspect_export(export_zip: str | Path, *, require_https: bool = False) -> dict:
    """Return only safe display metadata and exact media host names."""
    source = Path(export_zip).expanduser()
    if source.is_symlink() or not source.is_file():
        raise CollectorError("输入必须是普通浏览器资产包")
    try:
        with source.open("rb") as source_stream:
            digest = _sha256_stream(source_stream)
            with zipfile.ZipFile(source_stream) as archive:
                private = _small(archive, "private/source.json")
                course = _small(archive, "course_info.json")
                if not isinstance(private, dict) or not isinstance(course, dict):
                    raise CollectorError("浏览器资产包元数据格式无效")
                fingerprint = _browser_fingerprint(archive)
            if _sha256_stream(source_stream) != digest:
                raise CollectorError("浏览器资产包在预检期间已更改")
    except (OSError, zipfile.BadZipFile) as exc:
        raise CollectorError("浏览器资产包无效") from exc
    if not isinstance(private, dict) or not isinstance(course, dict):
        raise CollectorError("浏览器资产包元数据格式无效")
    raw_url = private.get("videoUrl")
    if not isinstance(raw_url, str) or len(raw_url) > 8192:
        raise CollectorError("浏览器资产包缺少有效媒体信息")
    parts = urllib.parse.urlsplit(raw_url)
    host = parts.hostname
    if (not host or parts.username or parts.password or parts.fragment or
            parts.scheme not in ({"https"} if require_https else {"https", "http"})):
        raise CollectorError("浏览器资产包媒体信息无效")
    try:
        host = _normalized_host(host)
        # Validate host syntax and port without returning either URL component.
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
    except (UnicodeError, ValueError) as exc:
        raise CollectorError("浏览器资产包媒体域名无效") from exc
    title = _safe_title(course.get("courseName"))
    day = course.get("lectureDate")
    if day is not None:
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise CollectorError("课程日期格式无效")
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise CollectorError("课程日期格式无效") from exc
    return {"course_title": title, "lecture_date": day,
            "media_hosts": [host], "source_sha256": digest,
            "source_id": _source_id(private, course, source.name), "source_fingerprint": fingerprint}


def _source_id(private: dict, course: dict, filename: str) -> str:
    """The stable legacy identifier is public metadata; page URLs stay private."""
    page = private.get("pageUrl") or course.get("pageUrl") or ""
    page = page.strip() if isinstance(page, str) else ""
    split = urllib.parse.urlsplit(page)
    query = split.query or (split.fragment.split("?", 1)[1] if "?" in split.fragment else "")
    fields = urllib.parse.parse_qs(query)
    values = [(fields.get(name) or [""])[0] for name in ("tenant_code", "course_id", "sub_id")]
    parts = [value for value in values if re.fullmatch(r"[A-Za-z0-9_-]+", value)]
    identifier = "zhiyun-" + "-".join(parts)
    if values[1] and values[2] and len(parts) >= 2 and len(identifier) <= 120:
        return identifier
    return "zhiyun-" + hashlib.sha256((page or filename).encode()).hexdigest()[:12]


def _browser_fingerprint(archive: zipfile.ZipFile) -> str:
    """Preserve old task/cache identity without exposing signed media locations."""
    digest = hashlib.sha256()
    infos = archive.infolist()
    if len(infos) > 10000 or sum(info.file_size for info in infos) > MAX_SOURCE_UNCOMPRESSED:
        raise CollectorError("浏览器资产包超过安全大小限制")
    names = set()
    for info in sorted(infos, key=lambda value: value.filename):
        if info.is_dir():
            continue
        name = _name(info.filename)
        if name in names:
            raise CollectorError("浏览器资产包包含重复成员")
        names.add(name)
        if "private" in name.split("/"):
            if name.endswith("private/source.json") and info.file_size <= 1024 * 1024:
                private = _small(archive, info.filename)
                for key in ("videoUrl", "mediaUrl"):
                    if isinstance(private.get(key), str):
                        parts = urllib.parse.urlsplit(private[key])
                        transient = {"auth_key", "token", "access_token", "signature", "sig", "expires", "expiration", "api_key", "cookie"}
                        query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                                 if key.lower() not in transient and not key.lower().startswith("x-amz-")]
                        location = parts._replace(query=urllib.parse.urlencode(query), fragment="").geturl()
                        digest.update(location.encode())
            continue
        digest.update(name.encode() + b"\0")
        with archive.open(info) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _small(archive: zipfile.ZipFile, name: str) -> object:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise CollectorError("浏览器资产包缺少必要的采集元数据") from exc
    if info.file_size > MAX_META or info.flag_bits & 1:
        raise CollectorError("浏览器元数据过大或已加密")
    with archive.open(info) as stream:
        data = stream.read(MAX_META + 1)
    if len(data) != info.file_size:
        raise CollectorError("浏览器元数据大小不一致")
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CollectorError("浏览器元数据损坏") from exc


def _resolved_host(host: str) -> str:
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CollectorError("媒体域名无法解析") from exc
    if not addresses:
        raise CollectorError("媒体域名没有地址")
    return addresses[0][4][0]


def _validate_media_url(value: object, *, allowed_host: str, development_loopback: bool) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise CollectorError("浏览器资产包没有可用媒体直链")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment or not parts.hostname:
        if not (development_loopback and parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"}):
            raise CollectorError("只接受已确认域名的 HTTPS 媒体直链")
    try:
        url_host = _normalized_host(parts.hostname)
        confirmed_host = _normalized_host(allowed_host)
    except (UnicodeError, ValueError) as exc:
        raise CollectorError("媒体地址的域名无效") from exc
    if url_host != confirmed_host:
        raise CollectorError("媒体地址的域名不等于操作者明确选择的域名")
    _resolved_host(parts.hostname)
    return value


def _name(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name or any(part in {"", ".", ".."} for part in name.split("/")):
        raise CollectorError("浏览器资产包路径不安全")
    return PurePosixPath(name).as_posix()


def _slide_header(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n") or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")


def _slide_extension(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise CollectorError("课件图片类型无效")


class _PinnedResponse:
    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse, url: str):
        self.connection = connection
        self.response = response
        self.url = url
        self.headers = response.headers

    def geturl(self) -> str:
        return self.url

    def read(self, amount: int) -> bytes:
        return self.response.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.response.close()
        self.connection.close()


def _media_request(url: str, allowed_host: str, development_loopback: bool):
    _validate_media_url(url, allowed_host=allowed_host, development_loopback=development_loopback)
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    # Connect to the IP we checked, not a second DNS answer. HTTPS keeps the
    # original host for SNI and certificate verification; no TLS bypass exists.
    pinned_ip = _resolved_host(host)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(host, port, timeout=60,
            context=ssl.create_default_context(cafile=certifi.where()))
    else:
        connection = http.client.HTTPConnection(host, port, timeout=60)
    connection._create_connection = lambda _address, timeout, source_address=None: socket.create_connection(
        (pinned_ip, port), timeout, source_address)
    target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    try:
        connection.request("GET", target, headers={"Accept-Encoding": "identity", "User-Agent": "Handouter-collector/1"})
        response = connection.getresponse()
        if response.status != 200:
            response.close()
            connection.close()
            raise CollectorError("媒体地址不可用或发生重定向")
        return _PinnedResponse(connection, response, url)
    except BaseException:
        connection.close()
        raise


def _playlist_urls(text: str, base: str, allowed_host: str, development_loopback: bool) -> tuple[str, list[str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise CollectorError("HLS playlist格式无效")
    if any(line.startswith(("#EXT-X-KEY", "#EXT-X-SESSION-KEY", "#EXT-X-MAP", "#EXT-X-BYTERANGE")) for line in lines):
        raise CollectorError("加密、分段范围或fMP4 HLS需采集侧专用下载器")
    if any(line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line for line in lines):
        raise CollectorError("多音轨HLS需采集侧专用下载器")
    urls = [urllib.parse.urljoin(base, line) for line in lines if not line.startswith("#")]
    if not urls or len(urls) > MAX_SEGMENTS:
        raise CollectorError("HLS分段数无效或超限")
    for item in urls:
        _validate_media_url(item, allowed_host=allowed_host, development_loopback=development_loopback)
    kind = "master" if any(line.startswith("#EXT-X-STREAM-INF") for line in lines) else "media"
    if kind == "media" and "#EXT-X-ENDLIST" not in lines:
        raise CollectorError("仅接受已结束的点播HLS，不采集无限直播流")
    return kind, urls


def _download_media(url: str, temporary_root: Path, allowed_host: str,
                    development_loopback: bool) -> Path:
    current = url
    for depth in range(2):
        try:
            response = _media_request(current, allowed_host, development_loopback)
            with response:
                if response.geturl() != current:
                    raise CollectorError("媒体重定向已拒绝")
                content_type = str(response.headers.get("Content-Type", "")).lower()
                is_hls = ("mpegurl" in content_type or urllib.parse.urlsplit(current).path.lower().endswith(".m3u8"))
                if is_hls:
                    body = response.read(MAX_PLAYLIST + 1)
                    if len(body) > MAX_PLAYLIST:
                        raise CollectorError("HLS清单超过限制")
                    kind, urls = _playlist_urls(body.decode("utf-8-sig"), current,
                                                allowed_host, development_loopback)
                else:
                    if not content_type.startswith(("video/", "application/octet-stream", "application/mp4")):
                        raise CollectorError("媒体响应不是直接视频或HLS")
                    output = temporary_root / "lecture.mp4"
                    total = 0
                    with output.open("xb") as target:
                        while block := response.read(CHUNK):
                            total += len(block)
                            if total > MAX_MEDIA:
                                raise CollectorError("媒体文件超过本地长课预算")
                            target.write(block)
                    with output.open("rb") as stream:
                        header = stream.read(12)
                    if len(header) < 8 or header[4:8] != b"ftyp":
                        raise CollectorError("媒体文件不是可接受的MP4/MOV容器")
                    return output
        except (UnicodeError, OSError) as exc:
            raise CollectorError("媒体获取失败，请检查授权会话和直链有效性") from exc
        if kind == "master":
            if depth or len(urls) != 1:
                raise CollectorError("多变体HLS需要采集侧显式选择并验收")
            current = urls[0]
            continue
        output = temporary_root / "lecture.ts"
        total = 0
        with output.open("xb") as target:
            for segment in urls:
                try:
                    with _media_request(segment, allowed_host, development_loopback) as response:
                        if response.geturl() != segment:
                            raise CollectorError("HLS分段重定向已拒绝")
                        segment_bytes = 0
                        first = True
                        while block := response.read(CHUNK):
                            segment_bytes += len(block)
                            total += len(block)
                            if segment_bytes > 512 * 1024**2 or total > MAX_MEDIA:
                                raise CollectorError("HLS分段或总媒体超限")
                            if first and block[:1] != b"\x47":
                                raise CollectorError("HLS分段不是MPEG-TS")
                            first = False
                            target.write(block)
                        if not segment_bytes:
                            raise CollectorError("HLS分段为空")
                except OSError as exc:
                    raise CollectorError("HLS分段获取失败") from exc
        return output
    raise CollectorError("HLS层级超过限制")


def convert(export_zip: str | Path, output_zip: str | Path, *, allowed_media_host: str,
            development_loopback: bool = False, expected_source_sha256: str | None = None,
            progress=None) -> dict:
    source = Path(export_zip).expanduser()
    target = Path(output_zip).expanduser()
    if source.is_symlink() or not source.is_file() or target.exists() or target.is_symlink():
        raise CollectorError("采集输入必须是普通文件，输出必须是新文件")
    if expected_source_sha256 and _sha256(source) != expected_source_sha256:
        raise CollectorError("浏览器资产包在预检后已更改")
    with source.open("rb") as source_stream:
        return _convert_stream(source_stream, target, allowed_media_host=allowed_media_host,
            development_loopback=development_loopback, expected_source_sha256=expected_source_sha256,
            progress=progress)


def _convert_stream(source_stream, target: Path, *, allowed_media_host: str,
                    development_loopback: bool = False, expected_source_sha256: str | None = None,
                    media_url: str | None = None, progress=None) -> dict:
    """Shared conversion core; direct acquisition keeps private metadata in memory."""
    if target.exists() or target.is_symlink():
        raise CollectorError("输出必须是新文件")
    if progress:
        progress("preparing")
    if expected_source_sha256 and _sha256_stream(source_stream) != expected_source_sha256:
          raise CollectorError("浏览器资产包在预检后已更改")
    with zipfile.ZipFile(source_stream) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_SOURCE_FILES or sum(info.file_size for info in infos) > MAX_SOURCE_UNCOMPRESSED:
            raise CollectorError("浏览器资产包过大")
        names = set()
        archive_names = {info.filename for info in infos}
        for info in infos:
            name = _name(info.filename[:-1] if info.is_dir() else info.filename)
            if name.casefold() in names or info.flag_bits & 1:
                raise CollectorError("浏览器资产包含重名或加密文件")
            names.add(name.casefold())
        private = _small(archive, "private/source.json") if media_url is None else {"videoUrl": media_url}
        course = _small(archive, "course_info.json")
        slides_meta = _small(archive, "slides_meta.json")
        if not isinstance(private, dict) or not isinstance(course, dict) or not isinstance(slides_meta, list):
            raise CollectorError("浏览器资产元数据格式无效")
        url = _validate_media_url(private.get("videoUrl"), allowed_host=allowed_media_host,
                                  development_loopback=development_loopback)
        day = course.get("lectureDate")
        if day is not None:
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise CollectorError("课程日期格式无效")
            date.fromisoformat(day)
        title = _safe_title(course.get("courseName"))
        images: dict[str, bytes] = {}
        image_names: dict[str, str] = {}
        events: list[dict] = []
        if len(slides_meta) > 10000:
            raise CollectorError("课件事件数量超过限制")
        for item in slides_meta:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                raise CollectorError("课件事件格式无效")
            filename = PurePosixPath(_name(item["filename"])).name
            if filename != item["filename"]:
                raise CollectorError("课件名称不能带路径")
            browser_name = "slides/" + filename
            if browser_name not in archive_names:
                raise CollectorError("课件图缺失；请在浏览器采集器补全后重新导出")
            if filename not in image_names:
                info = archive.getinfo(browser_name)
                if info.file_size > MAX_IMAGE:
                    raise CollectorError("单张课件图过大")
                data = archive.read(info)
                if len(data) != info.file_size or not _slide_header(data[:12]):
                    raise CollectorError("课件图片类型或大小无效")
                output_name = f"slide_{len(images) + 1:04d}{_slide_extension(data[:12])}"
                images[output_name] = data
                image_names[filename] = output_name
            seconds = item.get("seconds")
            start = None
            if seconds is not None:
                from decimal import Decimal
                value = Decimal(str(seconds)) * 1000
                if not value.is_finite() or value < 0 or value != value.to_integral_value():
                    raise CollectorError("课件时间不是整数毫秒")
                start = int(value)
            index = item.get("index")
            if index is not None and (type(index) is not int or index < 1):
                raise CollectorError("课件原始序号无效")
            events.append({"filename": image_names[filename], "source_index": index,
                           "start_ms": start, "end_ms": None})
        if expected_source_sha256 and _sha256_stream(source_stream) != expected_source_sha256:
            raise CollectorError("浏览器资产包在预检后已更改")
    with tempfile.TemporaryDirectory(prefix=".handouter-collector-", dir=target.parent) as temporary:
        temporary_root = Path(temporary)
        if progress:
            progress("downloading")
        media = _download_media(url, temporary_root, allowed_media_host, development_loopback)
        media_name = "media/lecture" + media.suffix
        manifest = {"schema_version": 1, "lecture": {"course_title": title.strip(), "lecture_date": day},
                    "media": {"path": media_name}}
        if images:
            manifest["slides"] = {"images_dir": "slides/images", "metadata_path": "slides/slides_meta.json"}
        staged_zip = temporary_root / "standard.zip"
        if progress:
            progress("packaging")
        # Fixed member timestamps: the same lecture always packs to the same bytes,
        # so re-collecting it does not look like new material downstream.
        fixed = time.mktime(PACKAGE_DATE_TIME + (0, 0, -1))
        os.utime(media, (fixed, fixed))

        def member(name: str) -> zipfile.ZipInfo:
            info = zipfile.ZipInfo(name, date_time=PACKAGE_DATE_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            return info

        with zipfile.ZipFile(staged_zip, "x", compression=zipfile.ZIP_STORED) as output:
            output.writestr(member("handouter-input.json"), json.dumps(manifest, ensure_ascii=False))
            output.write(media, media_name)
            if images:
                output.writestr(member("slides/slides_meta.json"), json.dumps(events, ensure_ascii=False))
            for name, data in sorted(images.items()):
                output.writestr(member("slides/images/" + name), data)
        # Exclusive publish; no signed URL or private/ member enters the result.
        if progress:
            progress("publishing")
        os.link(staged_zip, target)
        return {"output": str(target), "media_bytes": media.stat().st_size,
                "slide_images": len(images), "slide_events": len(events), "schema_version": 1}
