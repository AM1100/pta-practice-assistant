"""批量提交编排：扫描答案目录、筛选目标、构造提交体、轮询判题结果。"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .client import PTAClient, PTAError
from .config import Config

ACCEPTED_PROBLEM_STATUS = "PROBLEM_ACCEPTED"

# 判题进行中的状态集合：命中这些（或 queued=true）就继续轮询。
PENDING_SUBMISSION_STATES = {
    "",
    "WAITING",
    "JUDGING",
    "QUEUED",
    "QUEUEING",
    "COMPILING",
    "PENDING",
    "RUNNING",
    "SUBMITTED",
}

PROBLEM_STATUS_TO_SUBMISSION_STATUS = {
    "PROBLEM_ACCEPTED": "ACCEPTED",
    "PROBLEM_NO_ANSWER": "NO_ANSWER",
    "PROBLEM_SUBMITTED": "SUBMITTED",
}

ACCEPTED_JUDGE_STATES = {"AC", "OK", "PASS", "PASSED", "SUCCESS", "ACCEPTED"}
JUDGE_STATUS_KEYS = {"status", "result", "judgeResult", "verdict"}

# 判题结果状态 -> 中文，仅用于展示。
STATUS_TEXT = {
    "ACCEPTED": "通过",
    "WRONG_ANSWER": "答案错误",
    "PRESENTATION_ERROR": "格式错误",
    "MULTIPLE_ERROR": "多种错误",
    "COMPILE_ERROR": "编译错误",
    "RUNTIME_ERROR": "运行时错误",
    "TIME_LIMIT_EXCEEDED": "运行超时",
    "MEMORY_LIMIT_EXCEEDED": "内存超限",
    "OUTPUT_LIMIT_EXCEEDED": "输出超限",
    "NON_ZERO_EXIT_CODE": "非零退出",
    "SEGMENTATION_FAULT": "段错误",
    "FLOAT_POINT_EXCEPTION": "浮点异常",
    "INTERNAL_ERROR": "内部错误",
    "NO_ANSWER": "未作答",
    "PROBLEM_ACCEPTED": "通过",
    "PROBLEM_NO_ANSWER": "未作答",
    "PROBLEM_SUBMITTED": "已提交",
    "SUBMITTED": "已提交（未轮询）",
    "TIMEOUT": "判题轮询超时",
}


def status_text(s: Optional[str]) -> str:
    return STATUS_TEXT.get((s or "").upper(), s or "未知")


def normalize_status(s: Any) -> str:
    return str(s or "").strip().upper()


def is_pending_submission_status(s: Any) -> bool:
    return normalize_status(s) in PENDING_SUBMISSION_STATES


def find_problem_status(
    status_resp: Dict[str, Any], problem_set_problem_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    if not problem_set_problem_id:
        return None
    needle = str(problem_set_problem_id)
    for st in status_resp.get("problemStatus") or []:
        if str(st.get("id")) == needle:
            return st
    return None


def _canonical_judge_status(s: Any) -> str:
    status = normalize_status(s)
    if status in ACCEPTED_JUDGE_STATES:
        return "ACCEPTED"
    return status


def _walk_judge_statuses(value: Any) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, val in value.items():
            if key in JUDGE_STATUS_KEYS and val not in (None, ""):
                status = _canonical_judge_status(val)
                if status:
                    found.append(status)
            elif isinstance(val, (dict, list)):
                found.extend(_walk_judge_statuses(val))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_judge_statuses(item))
    return found


def _status_from_judge_contents(submission: Dict[str, Any]) -> Optional[str]:
    statuses = _walk_judge_statuses(submission.get("judgeResponseContents"))
    if not statuses or any(is_pending_submission_status(s) for s in statuses):
        return None
    non_accepted = [s for s in statuses if s != "ACCEPTED"]
    if not non_accepted:
        return "ACCEPTED"
    unique = sorted(set(non_accepted))
    return unique[0] if len(unique) == 1 else "MULTIPLE_ERROR"


def problem_status_to_submission_status(problem_status: Any) -> str:
    status = normalize_status(problem_status)
    return PROBLEM_STATUS_TO_SUBMISSION_STATUS.get(status, status)


def summarize_submission_result(
    data: Dict[str, Any],
    problem_status_row: Optional[Dict[str, Any]] = None,
    previous_problem_status: Optional[str] = None,
) -> Dict[str, Any]:
    """把 PTA 的提交详情归一化为 WebUI/CLI 可直接判断的结果。

    submission 详情有时仍显示 SUBMITTED/queued，但题目状态已经刷新为 AC。
    只有提交前不是已通过时，才使用 problem-status 的 PROBLEM_ACCEPTED 作为兜底，
    避免把历史 AC 误判成当前这次提交 AC。
    """
    sub = data.get("submission") or {}
    status = normalize_status(sub.get("status"))
    queued = bool(data.get("queued"))
    source = "submission"
    score = sub.get("score")

    if status and not is_pending_submission_status(status):
        queued = False
    else:
        derived = _status_from_judge_contents(sub)
        if derived:
            status = derived
            queued = False
            source = "judgeResponseContents"

    problem_status = normalize_status((problem_status_row or {}).get("problemSubmissionStatus"))
    previous = normalize_status(previous_problem_status)
    can_use_problem_status = bool(previous) and previous != ACCEPTED_PROBLEM_STATUS

    if (queued or not status or is_pending_submission_status(status)) and can_use_problem_status:
        mapped = problem_status_to_submission_status(problem_status)
        if mapped == "ACCEPTED":
            status = mapped
            queued = False
            source = "problemStatus"
            if score is None and problem_status_row:
                score = problem_status_row.get("score")

    pending = queued or not status or is_pending_submission_status(status)
    return {
        "queued": queued,
        "pending": pending,
        "status": status,
        "statusText": status_text(status),
        "score": score,
        "time": sub.get("time"),
        "memory": sub.get("memory"),
        "compiler": sub.get("compiler"),
        "cause": sub.get("cause"),
        "hints": sub.get("hints"),
        "publicCases": data.get("publicCases"),
        "problemStatus": problem_status or None,
        "source": source,
    }


@dataclass
class ProblemMeta:
    pspid: str
    title: str
    ptype: str  # CODE_COMPLETION / PROGRAMMING
    score: Any
    index_in_pool: Any
    status: Optional[str] = None  # problemSubmissionStatus
    label: str = ""


@dataclass
class AnswerFile:
    pspid: str
    path: str
    ext: str

    def read(self) -> str:
        for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
            try:
                with open(self.path, "r", encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        with open(self.path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")


@dataclass
class SubmitResult:
    pspid: str
    title: str
    ptype: str
    ok: bool
    status: str
    score: Any = None
    submission_id: Optional[str] = None
    compiler: Optional[str] = None
    message: str = ""


# ---------- 元信息装配 ----------
def validate_exam(exam_resp: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[bool]]:
    exam = exam_resp.get("exam") or {}
    exam_id = exam.get("id")
    if not exam_id:
        raise PTAError(
            "没有拿到 exam.id：可能尚未开始/进入该题集。\n"
            "本工具不会自动创建或开始考试（高风险），请先在网页端进入题集后重试。"
        )
    return str(exam_id), exam.get("status"), exam.get("ended")


def available_compilers(exam_resp: Dict[str, Any]) -> List[str]:
    ps = exam_resp.get("problemSet") or {}
    cfg = ps.get("problemSetConfig") or {}
    comps = cfg.get("compilers") or []
    result: List[str] = []
    for c in comps:
        if isinstance(c, str):
            result.append(c)
        elif isinstance(c, dict):
            for key in ("compiler", "value", "name", "id"):
                if c.get(key):
                    result.append(str(c[key]))
                    break
    return result


def build_catalog(types_resp: Dict[str, Any]) -> Dict[str, ProblemMeta]:
    catalog: Dict[str, ProblemMeta] = {}
    for lb in types_resp.get("labels") or []:
        pspid = str(lb.get("id"))
        catalog[pspid] = ProblemMeta(
            pspid=pspid,
            title=lb.get("title") or lb.get("label") or pspid,
            ptype=lb.get("type") or "",
            score=lb.get("score"),
            index_in_pool=lb.get("indexInProblemPool", 0),
            label=lb.get("label") or "",
        )
    return catalog


def apply_status(catalog: Dict[str, ProblemMeta], status_resp: Dict[str, Any]) -> None:
    for st in status_resp.get("problemStatus") or []:
        pspid = str(st.get("id"))
        if pspid in catalog:
            catalog[pspid].status = st.get("problemSubmissionStatus")


# ---------- 答案扫描与筛选 ----------
def scan_answers(answers_dir: str) -> Dict[str, AnswerFile]:
    answers: Dict[str, AnswerFile] = {}
    root = Path(answers_dir).expanduser().resolve()
    if not root.is_dir():
        return answers
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        try:
            resolved = entry.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if not resolved.is_file():
            continue
        stem, ext = os.path.splitext(entry.name)
        stem = stem.strip()
        # 仅识别以题目 id（ASCII 纯数字）命名且仍位于 answersDir 的文件。
        if not stem.isascii() or not stem.isdigit():
            continue
        if stem in answers:
            continue
        answers[stem] = AnswerFile(pspid=stem, path=str(resolved), ext=ext.lower())
    return answers


def select_targets(
    catalog: Dict[str, ProblemMeta],
    answers: Dict[str, AnswerFile],
    include_accepted: bool,
    only_ids: Optional[set],
) -> Tuple[List[Tuple[ProblemMeta, AnswerFile]], List[str], List[ProblemMeta]]:
    targets: List[Tuple[ProblemMeta, AnswerFile]] = []
    unknown: List[str] = []
    skipped_accepted: List[ProblemMeta] = []
    for pspid, ans in answers.items():
        if only_ids and pspid not in only_ids:
            continue
        meta = catalog.get(pspid)
        if not meta:
            unknown.append(pspid)
            continue
        if not include_accepted and meta.status == ACCEPTED_PROBLEM_STATUS:
            skipped_accepted.append(meta)
            continue
        targets.append((meta, ans))
    targets.sort(key=lambda t: t[0].pspid)
    return targets, unknown, skipped_accepted


# ---------- 提交体构造与结果解析 ----------
def build_payload(meta: ProblemMeta, code: str, compiler: Optional[str]) -> Dict[str, Any]:
    # details[].problemId 是题目在题池内的序号（单题池一般为 "0"），
    # 与 problemSetProblemId（题集题目 id）不同。
    index = meta.index_in_pool if meta.index_in_pool is not None else 0
    detail: Dict[str, Any] = {
        "problemId": str(index),
        "problemSetProblemId": str(meta.pspid),
    }
    if meta.ptype == "CODE_COMPLETION":
        detail["codeCompletionSubmissionDetail"] = {"program": code}
    elif meta.ptype == "PROGRAMMING":
        detail["programmingSubmissionDetail"] = {
            "program": code,
            "compiler": compiler or "GCC",
        }
    else:
        raise PTAError(f"暂不支持的题型: {meta.ptype}（题目 {meta.pspid}）")
    return {"problemType": meta.ptype, "details": [detail]}


def extract_submission_id(resp: Dict[str, Any]) -> Optional[str]:
    sid = resp.get("submissionId")
    if sid:
        return str(sid)
    sub = resp.get("submission") or {}
    if sub.get("id"):
        return str(sub["id"])
    return None


def wait_for_submission_result(
    client: PTAClient,
    exam_id: str,
    submission_id: str,
    cfg: Config,
    problem_set_problem_id: Optional[str] = None,
    previous_problem_status: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    deadline = time.time() + cfg.poll_timeout_seconds
    last: Dict[str, Any] = {}
    while True:
        last = client.get_submission(exam_id, submission_id)
        problem_status_row = None
        quick = summarize_submission_result(last)
        previous = normalize_status(previous_problem_status)
        if (
            quick["pending"]
            and problem_set_problem_id
            and previous
            and previous != ACCEPTED_PROBLEM_STATUS
        ):
            try:
                problem_status_row = find_problem_status(
                    client.get_problem_status(exam_id), problem_set_problem_id
                )
            except PTAError:
                problem_status_row = None
        result = summarize_submission_result(
            last,
            problem_status_row=problem_status_row,
            previous_problem_status=previous_problem_status,
        )
        if not result["pending"]:
            return result, last
        if time.time() >= deadline:
            result["queued"] = False
            result["pending"] = False
            result["status"] = "TIMEOUT"
            result["statusText"] = status_text("TIMEOUT")
            return result, last
        time.sleep(cfg.poll_interval_seconds)


def poll_result(
    client: PTAClient,
    exam_id: str,
    submission_id: str,
    cfg: Config,
    problem_set_problem_id: Optional[str] = None,
    previous_problem_status: Optional[str] = None,
) -> Tuple[str, Any, Dict[str, Any]]:
    result, raw = wait_for_submission_result(
        client,
        exam_id,
        submission_id,
        cfg,
        problem_set_problem_id=problem_set_problem_id,
        previous_problem_status=previous_problem_status,
    )
    return result["status"], result["score"], raw
