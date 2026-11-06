"""Small, site-neutral machine interface for the separately installed collector."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

from . import __version__
from .collector import CollectorError, convert, inspect_export

TOOL_ID = "handouter-zhiyun-collector"
PROTOCOL_VERSION = 1
# ``progress_bytes`` is a feature flag, not a method: convert/collect accept an
# optional ``"progress_bytes": true`` and then add byte counts to downloading frames.
CAPABILITIES = ["inspect", "convert", "progress_bytes"]
MAX_REQUEST_BYTES = 64 * 1024
# Non-progress frames (identity, inspection, result, error).
MAX_RESPONSE_LINES = 8
# Hosts accept up to 1000 progress frames; stay well inside that.
MAX_PROGRESS_FRAMES = 500
# Stage frames that may still follow byte frames (packaging, publishing, slack).
_RESERVED_STAGE_FRAMES = 4
BYTE_FRAME_INTERVAL = 1.0
ERROR_MESSAGE = "采集失败；原文件已保留，请检查工具、授权和媒体域名后重试"
_frames_sent = 0
_progress_sent = 0


class ProtocolError(ValueError):
    pass


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label}必须是JSON对象")
    return value


def _request(raw: bytes) -> tuple[str, dict]:
    if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise ProtocolError("请求为空、过大或未以换行结束")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("请求JSON无效") from exc
    data = _object(data, "请求")
    if set(data) != {"protocol_version", "method", "params"}:
        raise ProtocolError("请求字段不受支持")
    if type(data["protocol_version"]) is not int or data["protocol_version"] not in {1, 2}:
        raise ProtocolError("协议版本不受支持")
    method = data["method"]
    if not isinstance(method, str):
        raise ProtocolError("方法不受支持")
    params = _object(data["params"], "params")
    # The only optional parameter: an explicit opt-in to byte progress frames.
    optional = {"progress_bytes"} if method in {"convert", "collect"} else set()
    if "progress_bytes" in params and type(params["progress_bytes"]) is not bool:
        raise ProtocolError("progress_bytes必须是布尔值")
    if data["protocol_version"] == 2:
        expected = {"identity": set(), "auth_status": set(), "auth_login": set(), "auth_logout": set(),
                    "catalog": {"parent_id", "cursor", "query"}, "plan": {"resource_id"},
                    "collect": {"plan_id", "allowed_media_hosts", "output_path"}}
        if method not in expected or not expected[method] <= set(params) <= expected[method] | optional:
            raise ProtocolError("方法或参数不受支持")
        return "v2:" + method, params
    expected = {"identity": set(), "inspect": {"source_path"},
                "convert": {"source_path", "source_sha256", "allowed_media_hosts", "output_path"}}
    if method not in expected or not expected[method] <= set(params) <= expected[method] | optional:
        raise ProtocolError("方法或参数不受支持")
    return method, params


def _emit(frame: dict) -> None:
    global _frames_sent, _progress_sent
    is_progress = frame.get("type") == "progress"
    if (_progress_sent >= MAX_PROGRESS_FRAMES) if is_progress else (_frames_sent >= MAX_RESPONSE_LINES):
        raise ProtocolError("响应帧数超过限制")
    payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > 16 * 1024:
        raise ProtocolError("响应超过限制")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    if is_progress:
        _progress_sent += 1
    else:
        _frames_sent += 1


class ByteProgress:
    """Throttle ``downloading`` byte frames for hosts that asked for them.

    Consecutive byte frames are at least ``BYTE_FRAME_INTERVAL`` seconds apart
    and, when the total is known, at least 1% of it apart. With an unknown total
    (HLS) the interval doubles every 60 frames. Frames stop before the shared
    progress budget could be exhausted, so later stage frames always fit.
    """

    def __init__(self, emit=None, clock=None):
        self._emit = emit or _emit
        self._clock = clock or time.monotonic
        self._last_time = None
        self._last_bytes = 0
        self._frames = 0

    def __call__(self, completed: int, total: int | None) -> None:
        if type(completed) is not int or completed < 0:
            return
        if total is not None and (type(total) is not int or total <= 0 or completed > total):
            total = None
        now = self._clock()
        if self._last_time is None:
            # Stage baseline; the plain ``downloading`` stage frame was already sent.
            self._last_time = now
            return
        interval = BYTE_FRAME_INTERVAL * (1 if total is not None else 2 ** min(self._frames // 60, 10))
        if now - self._last_time < interval or completed <= self._last_bytes:
            return
        if total is not None and (completed - self._last_bytes) * 100 < total:
            return
        if _progress_sent >= MAX_PROGRESS_FRAMES - _RESERVED_STAGE_FRAMES:
            return
        self._emit({"type": "progress", "stage": "downloading",
                    "completed_bytes": completed, "total_bytes": total})
        self._last_time = now
        self._last_bytes = completed
        self._frames += 1


def _string(params: dict, key: str) -> str:
    value = params[key]
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ProtocolError(f"{key}无效")
    return value


def _convert(params: dict) -> None:
    source_path = _string(params, "source_path")
    output_path = _string(params, "output_path")
    expected_hash = params["source_sha256"]
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ProtocolError("source_sha256无效")
    allowed = params["allowed_media_hosts"]
    if (not isinstance(allowed, list) or not allowed or len(allowed) > 8 or
            any(not isinstance(host, str) or not host or len(host) > 253 for host in allowed) or
            len(set(allowed)) != len(allowed)):
        raise ProtocolError("allowed_media_hosts无效")
    # inspect reports exactly one media host. convert() checks the source digest
    # against the confirmation on the stream it then reads, and the media URL's
    # host against this one confirmed host, so no separate re-inspect pass.
    if len(allowed) != 1:
        raise CollectorError("确认信息与预检资产不匹配")
    source = Path(source_path).expanduser().resolve()
    target = Path(output_path).expanduser().absolute()
    if target.resolve(strict=False) == source:
        raise ProtocolError("输出路径必须是新的独立路径")
    if not target.parent.is_dir() or target.exists() or target.is_symlink():
        raise CollectorError("输出路径必须位于已存在目录且尚不存在")
    def progress(stage: str) -> None:
        _emit({"type": "progress", "stage": stage})

    convert(source_path, output_path, allowed_media_host=allowed[0],
            expected_source_sha256=expected_hash, progress=progress,
            on_bytes=ByteProgress() if params.get("progress_bytes") is True else None)
    _emit({"type": "result", "output_path": os.fspath(target)})


def _interrupted(signum, _frame):
    # Unwind instead of dying in place, so staging directories are removed.
    raise SystemExit(128 + signum)


def _install_interrupt_handlers() -> list:
    """Turn the host's cancel request (SIGTERM; CTRL_BREAK on Windows) into cleanup.

    A hard kill (Windows job close, SIGKILL) cannot be intercepted; leftover
    staging is then removed by the host's temporary directory or by the next
    run in the same output directory (``collector.remove_stale_staging``).
    """
    if threading.current_thread() is not threading.main_thread():
        return []
    previous = []
    for name in ("SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous.append((number, signal.signal(number, _interrupted)))
        except (OSError, ValueError):
            continue
    return previous


def run() -> int:
    previous = _install_interrupt_handlers()
    try:
        return _run()
    finally:
        for number, handler in previous:
            signal.signal(number, handler)


def _run() -> int:
    global _frames_sent, _progress_sent
    _frames_sent = 0
    _progress_sent = 0
    try:
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES or sys.stdin.buffer.read(1):
            raise ProtocolError("仅接受单个有界JSON请求")
        method, params = _request(raw)
        if method.startswith("v2:"):
            _v2(method[3:], params)
        elif method == "identity":
            _emit({"type": "identity", "tool_id": TOOL_ID,
                   "protocol_version": PROTOCOL_VERSION, "tool_version": __version__,
                   "capabilities": CAPABILITIES})
        elif method == "inspect":
            source_path = _string(params, "source_path")
            details = inspect_export(source_path, require_https=True)
            _emit({"type": "inspection", "tool_id": TOOL_ID,
                   "protocol_version": PROTOCOL_VERSION, "tool_version": __version__,
                   "capabilities": CAPABILITIES, **details})
        else:
            _convert(params)
        return 0
    except (ProtocolError, CollectorError, OSError, ValueError) as exc:
        from .direct import DirectError
        code = exc.code if isinstance(exc, (DirectError, CollectorError)) else "COLLECTOR_FAILED"
        _emit({"type": "error", "code": code, "message": ERROR_MESSAGE})
        return 1
    except Exception:
        # Never print exception strings or tracebacks: they may contain signed URLs.
        _emit({"type": "error", "code": "COLLECTOR_FAILED", "message": ERROR_MESSAGE})
        return 1


def _v2(method, params):
    from .direct import Adapter, CAPABILITIES
    if method == "identity":
        _emit({"type": "identity", "tool_id": TOOL_ID, "protocol_version": 2,
               "tool_version": __version__, "capabilities": CAPABILITIES})
        return
    if method == "catalog":
        for key in ("parent_id", "cursor"):
            value = params[key]
            if value is not None and (not isinstance(value, str) or not value or len(value) > 256):
                raise ProtocolError("目录参数无效")
        query = params["query"]
        if not isinstance(query, str) or len(query) > 200 or any(ord(c) < 32 for c in query):
            raise ProtocolError("搜索参数无效")
    elif method == "plan":
        _string(params, "resource_id")
    elif method == "collect":
        _string(params, "plan_id")
        _string(params, "output_path")
        allowed = params["allowed_media_hosts"]
        if (not isinstance(allowed, list) or not 1 <= len(allowed) <= 8 or
                any(not isinstance(h, str) or not h or len(h) > 253 for h in allowed) or
                len(set(allowed)) != len(allowed)):
            raise ProtocolError("媒体域名无效")
    adapter = Adapter()
    if method == "collect":
        params = dict(params)
        byte_progress = ByteProgress() if params.pop("progress_bytes", False) is True else None
        _emit(adapter.collect(**params, progress=lambda stage: _emit({"type": "progress", "stage": stage}),
                              byte_progress=byte_progress))
    else:
        _emit(getattr(adapter, method)(**params))
