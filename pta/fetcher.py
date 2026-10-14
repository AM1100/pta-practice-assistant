"""把题目详情抓取并格式化成本地可读的 Markdown。"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# Windows 文件名非法字符
ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_filename(name: str, maxlen: int = 80) -> str:
    cleaned = ILLEGAL_FILENAME.sub("_", name or "").strip().strip(".")
    cleaned = cleaned[:maxlen].strip()
    return cleaned or "untitled"


def _first(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict):
            v = d.get(k)
            if v not in (None, "", [], {}):
                return v
    return default


def _problem_type_config(problem_config: Dict[str, Any]) -> Dict[str, Any]:
    """题型相关配置位于 problemConfig 下不同子键，取其中存在的一个。"""
    for key in (
        "codeCompletionProblemConfig",
        "programmingProblemConfig",
        "multipleFileProblemConfig",
    ):
        cfg = problem_config.get(key)
        if isinstance(cfg, dict):
            return cfg
    return {}


def extract_problem(detail_resp: Dict[str, Any]) -> Dict[str, Any]:
    p = detail_resp.get("problemSetProblem") or {}
    problem_config = p.get("problemConfig") or {}
    type_cfg = _problem_type_config(problem_config)
    return {
        "id": str(p.get("id") or ""),
        "title": p.get("title") or p.get("label") or "",
        "label": p.get("label") or "",
        "type": p.get("type") or "",
        "score": p.get("score"),
        "difficulty": p.get("difficulty"),
        "author": p.get("author"),
        "compiler": p.get("compiler"),
        "content": p.get("content") or "",
        "description": p.get("description") or "",
        "timeLimit": type_cfg.get("timeLimit"),
        "memoryLimit": type_cfg.get("memoryLimit"),
        "codeSizeLimit": type_cfg.get("codeSizeLimit"),
        "examples": type_cfg.get("exampleTestDatas") or [],
    }


def format_examples(examples: List[Any]) -> str:
    lines: List[str] = []
    for i, ex in enumerate(examples, 1):
        if isinstance(ex, dict):
            inp = _first(ex, "input", "in", "testInput", "stdin", default="")
            out = _first(ex, "output", "out", "testOutput", "expectedOutput", "stdout", default="")
        else:
            inp, out = str(ex), ""
        lines.append(f"--- 样例 {i} 输入 ---")
        lines.append(str(inp).rstrip("\n"))
        lines.append(f"--- 样例 {i} 输出 ---")
        lines.append(str(out).rstrip("\n"))
        lines.append("")
    return "\n".join(lines).rstrip()


def format_problem(detail_resp: Dict[str, Any]) -> str:
    p = extract_problem(detail_resp)
    sep = "=" * 70

    head = [sep, f"题目: {p['title']}", f"题目ID: {p['id']}", f"题型: {p['type']}"]
    if p["score"] is not None:
        head.append(f"分数: {p['score']}")
    if p.get("difficulty"):
        head.append(f"难度: {p['difficulty']}")
    if p.get("compiler"):
        head.append(f"默认编译器: {p['compiler']}")

    limits = []
    if p["timeLimit"] is not None:
        limits.append(f"时间 {p['timeLimit']} ms")
    if p["memoryLimit"] is not None:
        limits.append(f"内存 {p['memoryLimit']} KB")
    if p["codeSizeLimit"] is not None:
        limits.append(f"代码长度 {p['codeSizeLimit']} B")
    if limits:
        head.append("限制: " + " / ".join(limits))
    head.append(sep)

    content = (p["content"] or "").strip()
    description = (p["description"] or "").strip()
    main = content or description

    body = ["", "【题面】", "", main or "(接口未返回题面文本)"]
    if content and description and description != content:
        body += ["", "【补充描述】", "", description]

    ex_text = format_examples(p["examples"])
    if ex_text:
        body += ["", "【样例】", "", ex_text]

    return "\n".join(head + body) + "\n"
