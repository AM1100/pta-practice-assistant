"""项目级网络策略：PTA 工具默认直连，不继承系统代理。"""

from __future__ import annotations

import os
from typing import Iterable

import requests

PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

USE_SYSTEM_PROXY_ENV = "PTA_USE_SYSTEM_PROXY"


def use_system_proxy() -> bool:
    """Whether PTA HTTP and browser traffic should honor system proxy settings."""

    return os.environ.get(USE_SYSTEM_PROXY_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def disable_project_proxy_env() -> None:
    """只清理当前 Python 进程的代理环境变量，不修改系统设置。"""
    for name in PROXY_ENV_VARS:
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = use_system_proxy()
    return session


def chromium_direct_args() -> Iterable[str]:
    return () if use_system_proxy() else ("--no-proxy-server",)
