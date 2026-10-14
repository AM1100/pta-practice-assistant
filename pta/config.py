"""配置加载：从 config.json 读取，cookie 可用环境变量 PTA_COOKIE 覆盖。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict

# PROGRAMMING 题按文件后缀推断编译器；CODE_COMPLETION 不需要编译器。
# 这里只是默认猜测，最终会用考试返回的 compilers 列表做校验。
DEFAULT_COMPILER_BY_EXTENSION: Dict[str, str] = {
    ".c": "GCC",
    ".cpp": "GXX",
    ".cc": "GXX",
    ".cxx": "GXX",
    ".py": "PYTHON3",
    ".java": "JAVAC",
}


@dataclass
class Config:
    base_url: str
    problem_set_id: str
    cookie: str
    answers_dir: str = "answers"
    default_compiler: str = "GCC"
    compiler_by_extension: Dict[str, str] = field(default_factory=dict)
    poll_interval_seconds: float = 2.0
    poll_timeout_seconds: float = 120.0
    submit_delay_seconds: float = 1.5
    log_dir: str = "logs"
    problems_dir: str = "problems"
    fetch_delay_seconds: float = 0.5
    extra_headers: Dict[str, str] = field(default_factory=dict)

    def compiler_for_extension(self, ext: str) -> str:
        ext = (ext or "").lower()
        merged = dict(DEFAULT_COMPILER_BY_EXTENSION)
        merged.update({k.lower(): v for k, v in (self.compiler_by_extension or {}).items()})
        return merged.get(ext, self.default_compiler)


def load_config(path: str) -> Config:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"找不到配置文件: {path}\n"
            "请复制 config.example.json 为 config.json 并填写 cookie 与 problemSetId。"
        )
    with open(path, "r", encoding="utf-8") as f:
        raw: Dict[str, Any] = json.load(f)

    # cookie 优先取环境变量，避免明文写进配置文件
    cookie = os.environ.get("PTA_COOKIE") or raw.get("cookie", "")
    cookie = (cookie or "").strip()

    # problemSetId 对发现/列表工具（pta_list.py）不是必需，因此这里不强制；
    # 提交/抓取工具会在各自入口校验它是否存在。
    problem_set_id = str(raw.get("problemSetId", "")).strip()

    base_url = str(raw.get("baseUrl", "https://pintia.cn")).rstrip("/")

    return Config(
        base_url=base_url,
        problem_set_id=problem_set_id,
        cookie=cookie,
        answers_dir=raw.get("answersDir", "answers"),
        default_compiler=raw.get("defaultCompiler", "GCC"),
        compiler_by_extension=raw.get("compilerByExtension", {}) or {},
        poll_interval_seconds=float(raw.get("pollIntervalSeconds", 2.0)),
        poll_timeout_seconds=float(raw.get("pollTimeoutSeconds", 120.0)),
        submit_delay_seconds=float(raw.get("submitDelaySeconds", 1.5)),
        log_dir=raw.get("logDir", "logs"),
        problems_dir=raw.get("problemsDir", "problems"),
        fetch_delay_seconds=float(raw.get("fetchDelaySeconds", 0.5)),
        extra_headers=raw.get("extraHeaders", {}) or {},
    )


def load_config_or_default(path: str) -> Config:
    """加载配置；文件不存在时返回带默认值的空配置（供 WebUI 首次运行）。"""
    try:
        return load_config(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return Config(
            base_url="https://pintia.cn",
            problem_set_id="",
            cookie=(os.environ.get("PTA_COOKIE") or "").strip(),
        )


def update_config_file(path: str, updates: Dict[str, Any]) -> None:
    """把 updates 合并写回 config.json（键用 camelCase，如 cookie/problemSetId）。

    采用「写临时文件 + 原子替换」，避免写入中断导致配置损坏。
    """
    raw: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (ValueError, OSError):
            raw = {}
    raw.update(updates)

    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
