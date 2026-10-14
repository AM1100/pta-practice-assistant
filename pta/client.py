"""PTA Web API 客户端，仅封装本工具实际使用的读取与提交接口。"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from math import ceil
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from .config import Config
from .network import direct_session

# 当前用户信息在 passport 子域，而非主站
PASSPORT_BASE = "https://passport.pintia.cn"
STANDARD_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.7827.115 Safari/537.36"
)
GET_CACHE_SECONDS = 1.0
GET_CACHE_MAX_ENTRIES = 256
PROBLEM_SET_PAGE_LIMIT = 30
_GET_CACHE_LOCK = threading.RLock()
_GET_CACHE: OrderedDict[Tuple[str, str, str], Tuple[float, Dict[str, Any]]] = OrderedDict()


def _safe_request_url(url: Any) -> Optional[str]:
    """Return a bounded URL suitable for logs and diagnostics.

    Response URLs may contain credentials, query parameters, or fragments.  Keep
    only an HTTP(S) origin and escaped path; malformed and non-HTTP URLs are not
    persisted at all.
    """

    try:
        parts = urlsplit(str(url or ""))
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        if scheme not in ("http", "https") or not hostname:
            return None

        host = hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ):
            host = f"{host}:{port}"

        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")[:512]
        return urlunsplit((scheme, host, path, "", ""))
    except (TypeError, ValueError):
        return None


class PTAError(Exception):
    """通用错误。"""

    error_code = "PTA_ERROR"

    def safe_log_fields(self) -> Dict[str, Any]:
        """Return stable metadata that is safe to write to persistent logs."""

        return {
            "errorClass": type(self).__name__,
            "errorCode": self.error_code,
        }


class PTAAuthError(PTAError):
    """鉴权失败（cookie 失效 / 无权限）。"""

    error_code = "PTA_AUTH_ERROR"


class PTAHTTPError(PTAError):
    error_code = "PTA_HTTP_ERROR"

    def __init__(self, status_code: int, body: str, url: str):
        safe_url = _safe_request_url(url)
        body_preview = str(body or "")[:500]
        location = f" @ {safe_url}" if safe_url else ""
        diagnostic = f"\n{body_preview}" if body_preview else ""
        super().__init__(f"HTTP {status_code}{location}{diagnostic}")
        self.status_code = status_code
        # A bounded preview is retained for interactive diagnostics only.  Callers
        # must use safe_log_fields() rather than serializing the exception object.
        self.body = body_preview
        self.url = safe_url

    def safe_log_fields(self) -> Dict[str, Any]:
        fields = super().safe_log_fields()
        fields["httpStatus"] = self.status_code
        if self.url:
            fields["requestUrl"] = self.url
        return fields


def clear_get_cache() -> None:
    with _GET_CACHE_LOCK:
        _GET_CACHE.clear()


def _prune_get_cache(now: float) -> None:
    """Drop expired and least-recently-used entries while holding the cache lock."""

    for key, (expires_at, _value) in list(_GET_CACHE.items()):
        if expires_at <= now:
            _GET_CACHE.pop(key, None)
    while len(_GET_CACHE) > GET_CACHE_MAX_ENTRIES:
        _GET_CACHE.popitem(last=False)


CURRENT_USER_IDENTITY_KEYS = ("id", "nickname", "name", "email", "phone", "realName")
CURRENT_USER_LOGIN_FLAG_KEYS = (
    "emailLogin",
    "phoneLogin",
    "omsLogin",
    "studentUserLogin",
)


def _has_current_user_identity(user: Dict[str, Any]) -> bool:
    return any(user.get(k) not in (None, "", [], {}) for k in CURRENT_USER_IDENTITY_KEYS)


def _has_authenticated_login_flag(user: Dict[str, Any]) -> bool:
    return any(user.get(k) is True for k in CURRENT_USER_LOGIN_FLAG_KEYS)


def extract_current_user(resp: Any) -> Dict[str, Any]:
    """从 /api/u/current 响应中提取已登录用户；未登录状态对象返回空 dict。"""
    if not isinstance(resp, dict):
        return {}
    nested = resp.get("user")
    if isinstance(nested, dict):
        if _has_current_user_identity(nested) or _has_authenticated_login_flag(nested):
            return nested
        return {}
    if _has_current_user_identity(resp) or _has_authenticated_login_flag(resp):
        return resp
    return {}


class PTAClient:
    def __init__(self, config: Config):
        self.cfg = config
        self.session = direct_session()
        headers = {
            "Accept": "application/json;charset=UTF-8",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept-Language": "zh-CN",
            "User-Agent": STANDARD_CHROME_UA,
            "Origin": config.base_url,
            "Referer": f"{config.base_url}/problem-sets/{config.problem_set_id}/overview",
        }
        if config.cookie:
            headers["Cookie"] = config.cookie
        headers.update(config.extra_headers or {})
        self.session.headers.update(headers)

    # ---------- 底层请求 ----------
    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.cfg.base_url + path

    def _handle(self, resp: requests.Response) -> Dict[str, Any]:
        if resp.status_code in (401, 403):
            safe_url = _safe_request_url(resp.url)
            location = f"：{safe_url}" if safe_url else ""
            raise PTAAuthError(
                f"鉴权失败 (HTTP {resp.status_code})，cookie 可能已失效或无权限{location}"
            )
        # PTA 把 412 当作正常业务响应（非错误），照常解析 JSON
        if resp.status_code == 412:
            try:
                return resp.json()
            except ValueError:
                return {}
        if not resp.ok:
            raise PTAHTTPError(resp.status_code, resp.text, resp.url)
        if not resp.text:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise PTAError("响应不是 JSON：cookie 可能失效，或服务端忽略了 Accept 头。") from exc

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        clear_get_cache()
        return self._handle(self.session.post(self._url(path), data=json.dumps(body), timeout=30))

    def _cache_key(self, path: str, params: Optional[Dict[str, Any]]) -> Tuple[str, str, str]:
        url = self._url(path)
        payload = json.dumps(params or {}, ensure_ascii=False, sort_keys=True, default=str)
        # Hash the complete effective session header identity.  ``extraHeaders`` may
        # carry Authorization or tenant/account selectors even when Cookie is equal.
        # Only the full SHA-256 digest enters the process-local cache key.
        normalized_headers = sorted(
            (str(name).strip().lower(), str(value)) for name, value in self.session.headers.items()
        )
        request_identity = json.dumps(
            {
                "baseUrl": self.cfg.base_url,
                "headers": normalized_headers,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        identity_sig = hashlib.sha256(request_identity.encode("utf-8")).hexdigest()
        return (url, payload, identity_sig)

    def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        cache_seconds: float = GET_CACHE_SECONDS,
    ) -> Dict[str, Any]:
        key = self._cache_key(path, params)
        now = time.time()
        if cache_seconds > 0:
            with _GET_CACHE_LOCK:
                hit = _GET_CACHE.get(key)
                if hit and hit[0] > now:
                    _GET_CACHE.move_to_end(key)
                    return copy.deepcopy(hit[1])
                if hit:
                    _GET_CACHE.pop(key, None)

        data = self._handle(self.session.get(self._url(path), params=params, timeout=30))
        if cache_seconds > 0:
            stored_at = time.time()
            with _GET_CACHE_LOCK:
                _prune_get_cache(stored_at)
                _GET_CACHE[key] = (stored_at + cache_seconds, copy.deepcopy(data))
                _GET_CACHE.move_to_end(key)
                _prune_get_cache(stored_at)
        return data

    # ---------- 发现 / 列表接口（用于获取 problemSetId）----------
    def get_current_user(self) -> Dict[str, Any]:
        """当前登录用户（位于 passport 子域）。"""
        return self.get(f"{PASSPORT_BASE}/api/u/current")

    def _get_problem_sets_paginated(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        params = dict(params or {})
        params.setdefault("page", 0)
        params.setdefault("limit", PROBLEM_SET_PAGE_LIMIT)
        first = self.get(path, params=params, cache_seconds=30.0)
        total = first.get("total")
        problem_sets = list(first.get("problemSets") or [])
        if not total or len(problem_sets) >= int(total):
            return first

        merged = dict(first)
        dict_fields: Dict[str, Dict[str, Any]] = {
            k: dict(v) for k, v in first.items() if isinstance(v, dict)
        }
        limit = int(params["limit"])
        pages = ceil(int(total) / limit)
        for page in range(1, pages):
            page_params = dict(params)
            page_params["page"] = page
            data = self.get(path, params=page_params, cache_seconds=30.0)
            problem_sets.extend(data.get("problemSets") or [])
            for k, v in data.items():
                if isinstance(v, dict):
                    dict_fields.setdefault(k, {}).update(v)
        merged["problemSets"] = problem_sets
        merged.update(dict_fields)
        return merged

    def get_my_problem_sets(self, only_active: bool = True) -> Dict[str, Any]:
        """我加入的题集。only_active=True 时只返回未结束的。"""
        if only_active:
            now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            iso = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
            flt = json.dumps({"endAtAfter": iso})
        else:
            flt = "{}"
        return self._get_problem_sets_paginated("/api/problem-sets", params={"filter": flt})

    def get_always_available_problem_sets(self) -> Dict[str, Any]:
        """长期开放 / 公开题集（如 PAT、天梯赛练习集等）。"""
        return self.get(
            "/api/problem-sets/always-available",
            params={"page": 0, "limit": PROBLEM_SET_PAGE_LIMIT},
            cache_seconds=30.0,
        )

    def get_dashboard(self) -> Dict[str, Any]:
        """首页 dashboard 分类内容（content 字段是 JSON 字符串）。"""
        return self.get("/api/content/dashboard")

    # ---------- 只读接口 ----------
    def get_exam(self) -> Dict[str, Any]:
        return self.get(f"/api/problem-sets/{self.cfg.problem_set_id}/exams", cache_seconds=5.0)

    def get_problem_types(self) -> Dict[str, Any]:
        return self.get(
            f"/api/problem-sets/{self.cfg.problem_set_id}/exam-problem-types",
            cache_seconds=30.0,
        )

    def get_problem_status(self, exam_id: str) -> Dict[str, Any]:
        return self.get(
            f"/api/exams/{exam_id}/problem-sets/{self.cfg.problem_set_id}/problem-status"
        )

    def get_exam_problem_status(self) -> Dict[str, Any]:
        return self.get(f"/api/problem-sets/{self.cfg.problem_set_id}/exam-problem-status")

    def get_problem_detail(self, problem_set_problem_id: str) -> Dict[str, Any]:
        return self.get(
            f"/api/problem-sets/{self.cfg.problem_set_id}/exam-problems/{problem_set_problem_id}",
            cache_seconds=300.0,
        )

    # ---------- 写接口（提交）----------
    def submit(self, exam_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.post(f"/api/exams/{exam_id}/submissions", payload)

    def get_submission(self, exam_id: str, submission_id: str) -> Dict[str, Any]:
        return self.get(
            f"/api/exams/{exam_id}/submissions/{submission_id}",
            params={"customTestDataSubmission": "false"},
            cache_seconds=0,
        )
