#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local, read-only Claude Code usage dashboard.

The dashboard reads Claude's native project JSONL files and never writes to
the Claude data directory.  It deliberately uses only the Python standard
library so it can run on a clean macOS or Windows installation.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]


try:
    LOCAL_ZONE = ZoneInfo("Asia/Tokyo") if ZoneInfo else timezone(timedelta(hours=9))
except Exception:  # pragma: no cover - Windows installs without bundled tzdata
    LOCAL_ZONE = timezone(timedelta(hours=9))
TOKEN_FIELDS = (
    "inputTokens",
    "outputTokens",
    "thinkingTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
)
USAGE_FIELDS = (
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("cache_read_input_tokens", "cacheReadInputTokens"),
    ("cache_creation_input_tokens", "cacheCreationInputTokens"),
)

# First-party API list prices, USD per million tokens. Verified against both
# official pages on this date; deliberately local so scanning never needs a key
# or uploads transcripts. Historical usage is valued at this price snapshot.
PRICING_DATE = "2026-10-19"
PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
API_PRICES = {
    # input, output, 5m cache write, 1h cache write, cache read
    "claude-fable-5-1": (10, 50, 12.5, 20, .25),
    "claude-mythos-5-1": (10, 50, 12.5, 20, .25),
    "claude-fable-5": (10, 50, 12.5, 20, 1),
    "claude-mythos-5": (10, 50, 12.5, 20, 1),
    "claude-opus-5-5": (4, 20, 5, 8, .2),
    "claude-opus-5": (5, 25, 6.25, 10, .5),
    "claude-opus-4-8": (5, 25, 6.25, 10, .5),
    "claude-opus-4-7": (5, 25, 6.25, 10, .5),
    "claude-opus-4-6": (5, 25, 6.25, 10, .5),
    "claude-opus-4-5": (5, 25, 6.25, 10, .5),
    "claude-sonnet-5-5": (2, 10, 2.5, 4, .1),
    "claude-sonnet-5": (2, 10, 2.5, 4, .2),
    "claude-sonnet-4-6": (3, 15, 3.75, 6, .3),
    "claude-haiku-5-5": (.1, .5, .125, .2, .01),
    "claude-haiku-4-5": (1, 5, 1.25, 2, .1),
}


def merge_pricing_usage(target: dict[str, Any], usage: dict[str, Any]) -> None:
    """Retain billing metadata alongside cumulative streamed token counters."""
    for key in ("speed", "inference_geo", "service_tier"):
        if usage.get(key) is not None:
            target[key] = usage[key]
    for group, keys in (("cache_creation", ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")),
                        ("server_tool_use", ("web_search_requests", "code_execution_requests"))):
        raw = usage.get(group)
        if isinstance(raw, dict):
            for key in keys:
                if is_amount(raw.get(key)):
                    target[key] = max(integer(target.get(key)), integer(raw[key]))


def estimate_api_cost(model: str, tokens: dict[str, int], metadata: dict[str, Any]) -> dict[str, Any]:
    """Price one deduplicated request, not an aggregated session token count."""
    model = re.sub(r"-\d{8}$", "", model)
    has_usage = any(tokens.get(key, 0) for key in TOKEN_FIELDS if key != "thinkingTokens")
    if model == "<synthetic>" and not has_usage:
        return {"cost": 0.0, "complete": True, "notes": []}
    rates = API_PRICES.get(model)
    if rates is None:
        return {"cost": None, "complete": False, "notes": [f"未收录模型价格：{model}"]}
    notes = set()
    input_count = tokens.get("inputTokens", 0)
    output_count = tokens.get("outputTokens", 0)
    read_count = tokens.get("cacheReadInputTokens", 0)
    write_count = tokens.get("cacheCreationInputTokens", 0)
    # The two TTL counters subdivide cache creation; do not charge them again.
    hour_count = min(write_count, integer(metadata.get("ephemeral_1h_input_tokens")))
    short_count = write_count - hour_count
    classified = integer(metadata.get("ephemeral_5m_input_tokens")) + integer(metadata.get("ephemeral_1h_input_tokens"))
    if write_count > classified:
        notes.add("未标明时长的缓存写入按 5 分钟估算")
    if classified > write_count:
        notes.add("缓存时长明细与总量不一致，按缓存总量估算")
    # Haiku's tier is based on *all input tokens of this request*, including cache.
    if model == "claude-haiku-5-5" and input_count + read_count + write_count > 100_000:
        rates = (.5, 2.5, .625, 1, .05)
    multiplier = 1.0
    speed = metadata.get("speed")
    if speed == "fast":
        if model in ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"):
            multiplier *= 2
        else:
            return {"cost": None, "complete": False, "notes": [f"没有该模型的 Fast 价格：{model}"]}
    elif speed not in (None, "standard"):
        notes.add("未知速度档位，按标准速度估算")
    geo = metadata.get("inference_geo")
    if geo == "us":
        multiplier *= 1.1
    elif geo not in (None, "global", "not_available"):
        notes.add("未知推理区域，按全球 API 价格估算")
    tier = metadata.get("service_tier")
    if tier == "batch":
        multiplier *= .5
    elif tier not in (None, "standard"):
        notes.add("未收录服务档位，按标准 API 价格估算")
    token_cost = (input_count * rates[0] + output_count * rates[1] +
                  short_count * rates[2] + hour_count * rates[3] + read_count * rates[4]) / 1_000_000
    cost = token_cost * multiplier + integer(metadata.get("web_search_requests")) * .01
    complete = not metadata.get("code_execution_requests")
    if not complete:
        notes.add("代码执行缺少时长与免费额度信息，未估算其附加费用")
    return {"cost": cost, "complete": complete, "notes": sorted(notes)}


def number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def integer(value: Any) -> int:
    return int(number(value))


def is_amount(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def process_is_running(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless existence probe on Windows.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied: process exists.
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_ZONE)
    except ValueError:
        return None


def project_name(cwd: str) -> str:
    if not cwd:
        return "未标记项目"
    path = cwd.rstrip("/")
    return Path(path).name or path


def clean_title(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:180] if value else None


def empty_tokens() -> dict[str, int]:
    return {key: 0 for key in TOKEN_FIELDS}


def add_tokens(target: dict[str, int], source: dict[str, Any], *, native: bool = False) -> None:
    if native:
        for key in TOKEN_FIELDS:
            target[key] += integer(source.get(key))
    else:
        for source_key, target_key in USAGE_FIELDS:
            target[target_key] += integer(source.get(source_key))
        details = source.get("output_tokens_details")
        if isinstance(details, dict):
            target["thinkingTokens"] += integer(details.get("thinking_tokens"))


def merge_token_snapshot(target: dict[str, int], usage: dict[str, Any]) -> None:
    """A streamed request is cumulative; repeated blocks are not new usage."""
    tokens = empty_tokens()
    add_tokens(tokens, usage)
    for key in TOKEN_FIELDS:
        target[key] = max(target[key], tokens[key])


class UsageIndex:
    def __init__(self, root: Path):
        self.root = root.expanduser()
        self._lock = threading.RLock()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.usage_events: list[dict[str, Any]] = []
        self.cost_events: list[dict[str, Any]] = []
        self.latest_states: dict[str, dict[str, Any]] = {}
        self.accounted_states: dict[str, dict[str, Any]] = {}
        self.cost_records: list[dict[str, Any]] = []
        self.file_histories: dict[str, set[str]] = defaultdict(set)
        self.file_request_times: dict[str, dict[str, datetime | None]] = defaultdict(dict)
        self.first_queue: dict[str, datetime] = {}
        self.scanned_files = 0
        self.scanned_records = 0
        self.shared_request_count = 0
        self.attribution_ambiguous_request_count = 0
        self._scan()

    def snapshot(self) -> dict[str, Any]:
        """Rescan the local Claude archive and return a consistent summary."""
        with self._lock:
            self.sessions = {}
            self.usage_events = []
            self.cost_events = []
            self.latest_states = {}
            self.accounted_states = {}
            self.cost_records = []
            self.file_histories = defaultdict(set)
            self.file_request_times = defaultdict(dict)
            self.first_queue = {}
            self.scanned_files = 0
            self.scanned_records = 0
            self.shared_request_count = 0
            self.attribution_ambiguous_request_count = 0
            self._scan()
            return self.summary()

    def _session(self, sid: str) -> dict[str, Any]:
        if sid not in self.sessions:
            self.sessions[sid] = {
                "id": sid,
                "title": None,
                "cwd": "",
                "project": "未标记项目",
                "branch": "",
                "first": None,
                "last": None,
                "user_messages": 0,
                "assistant_messages": 0,
                "event_count": 0,
                "models": Counter(),
                "primary_models": Counter(),
                "subagent_models": Counter(),
                "advisors": Counter(),
                "advisor_calls": 0,
                "tokens": empty_tokens(),
                "main_tokens": empty_tokens(),
                "advisor_tokens": empty_tokens(),
                "advisor_model_tokens": {},
                "advisor_usage_missing_calls": 0,
                "inherited_tokens": empty_tokens(),
                "inherited_request_count": 0,
                "advisor_cost": None,
                "cost": None,
            }
        return self.sessions[sid]

    def _scan(self) -> None:
        project_root = self.root / "projects"
        if not project_root.exists():
            return
        files = sorted(project_root.rglob("*.jsonl"))
        self.scanned_files = len(files)
        requests: dict[str, dict[str, Any]] = {}
        first_queue = self.first_queue

        for path in files:
            subagent_file = "subagents" in path.relative_to(project_root).parts[:-1]
            # Forks can retain an original sessionId in copied lines. The physical
            # transcript is therefore needed as well as the line's sessionId.
            history: set[str] = set()
            last_event_time: dict[str, datetime] = {}
            try:
                handle = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for line_number, line in enumerate(handle):
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    self.scanned_records += 1
                    sid = obj.get("sessionId")
                    if not isinstance(sid, str) or not sid:
                        continue
                    session = self._session(sid)
                    ts = parse_timestamp(obj.get("timestamp"))
                    if ts:
                        if session["first"] is None or ts < session["first"]:
                            session["first"] = ts
                        if session["last"] is None or ts > session["last"]:
                            session["last"] = ts
                        last_event_time[sid] = ts
                        session["event_count"] += 1
                    cwd = obj.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        session["cwd"] = cwd
                        session["project"] = project_name(cwd)
                    branch = obj.get("gitBranch")
                    if isinstance(branch, str) and branch:
                        session["branch"] = branch

                    kind = obj.get("type")
                    if kind == "queue-operation" and ts:
                        first_queue[sid] = min(first_queue.get(sid, ts), ts)
                    if kind == "custom-title":
                        title = clean_title(obj.get("customTitle"))
                        if title:
                            session["title"] = title
                    elif kind == "user":
                        session["user_messages"] += 1
                    elif kind == "assistant":
                        session["assistant_messages"] += 1
                        message = obj.get("message")
                        usage = message.get("usage") if isinstance(message, dict) else None

                        if not isinstance(message, dict):
                            continue
                        # Request IDs survive history copies into forked conversations.
                        # Message IDs also survive copies and streaming block splits.
                        identity = next((f"{key}:{value}" for key, value in (
                            ("request", obj.get("requestId")), ("message", message.get("id")),
                            ("uuid", obj.get("uuid")),
                        ) if isinstance(value, str) and value), f"line:{path}:{line_number}")
                        history.add(identity)
                        self.file_histories[path.stem].add(identity)
                        self.file_request_times[path.stem][identity] = ts
                        request = requests.setdefault(identity, {
                            "sessions": {}, "model": None, "tokens": empty_tokens(),
                            "session_roles": defaultdict(set),
                            "has_usage": False, "advisor_iterations": {},
                            "pricing": {},
                            "advisor_tools": set(), "advisor_model": None,
                        })
                        if sid not in request["sessions"] or (ts and (request["sessions"][sid] is None or ts < request["sessions"][sid])):
                            request["sessions"][sid] = ts
                        # Agent IDs survive copied/streamed records. A dedicated
                        # subagents transcript also identifies older records that
                        # lack the metadata; isSidechain alone is not sufficient.
                        agent_id = obj.get("agentId")
                        role = "subagent" if subagent_file or (isinstance(agent_id, str) and agent_id.strip()) else "primary"
                        request["session_roles"][sid].add(role)
                        model = message.get("model") or obj.get("requestedModel")
                        if isinstance(model, str) and model:
                            request["model"] = model
                        # Availability of advisorModel alone does not prove intervention.
                        content = message.get("content") if isinstance(message, dict) else None
                        advisor_tools = [
                            item for item in content
                            if isinstance(item, dict)
                            and item.get("type") == "server_tool_use"
                            and item.get("name") == "advisor"
                        ] if isinstance(content, list) else []
                        for index, tool in enumerate(advisor_tools):
                            request["advisor_tools"].add(str(tool.get("id") or f"block:{obj.get('apiBlockIndex', 0)}:{index}"))
                        fallback_model = obj.get("advisorModel")
                        if isinstance(fallback_model, str) and fallback_model:
                            request["advisor_model"] = fallback_model
                        if not isinstance(usage, dict):
                            continue
                        request["has_usage"] = request["has_usage"] or any(is_amount(usage.get(source)) for source, _ in USAGE_FIELDS)
                        merge_token_snapshot(request["tokens"], usage)
                        merge_pricing_usage(request["pricing"], usage)
                        iterations = usage.get("iterations")
                        for index, iteration in enumerate(iterations if isinstance(iterations, list) else []):
                            if not isinstance(iteration, dict) or iteration.get("type") != "advisor_message":
                                continue
                            advisor = request["advisor_iterations"].setdefault(index, {"model": None, "tokens": empty_tokens(), "pricing": {}, "has_usage": False})
                            if isinstance(iteration.get("model"), str):
                                advisor["model"] = iteration["model"]
                            merge_token_snapshot(advisor["tokens"], iteration)
                            merge_pricing_usage(advisor["pricing"], iteration)
                            advisor["has_usage"] = advisor["has_usage"] or any(is_amount(iteration.get(source)) for source, _ in USAGE_FIELDS)

                    elif kind == "cost-state":
                        if not is_amount(obj.get("totalCostUSD")):
                            continue
                        state_time = last_event_time.get(sid) or parse_timestamp(obj.get("timestamp"))
                        if state_time is None:
                            start_ms = number(obj.get("startTime"))
                            state_time = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).astimezone(LOCAL_ZONE) if start_ms else None
                        self.cost_records.append({"session": sid, "state": obj,
                                                  "time": state_time, "history": set(history)})
                        self.latest_states[sid] = obj

        self._finalize_usage(requests, first_queue)
        # Only usage-bearing requests constrain a monetary baseline. Synthetic
        # zero-usage messages can be copied or compacted without changing cost.
        paid = {key for key, request in requests.items() if
                any(request["tokens"].values()) or any(
                    any(advisor["tokens"].values()) for advisor in request["advisor_iterations"].values())}
        for record in self.cost_records:
            record["history"].intersection_update(paid)
        for sid, ids in self.file_histories.items():
            ids.intersection_update(paid)
        self._read_native_project_costs()
        self._reconcile_costs()
        active_sessions = self._active_sessions()
        for sid, session in self.sessions.items():
            native_state = self.latest_states.get(sid, {})
            state = self.accounted_states.get(sid, {})
            raw_cost = state.get("totalCostUSD")
            available = is_amount(raw_cost)
            active = sid in active_sessions
            session["cost_available"] = available
            session["cost"] = number(raw_cost) if available else None
            session["cost_source"] = state.get("_source", "cost-state") if available else None
            session["process_active"] = active
            session["runtime_status"] = active_sessions.get(sid)
            session["native_cost_available"] = is_amount(native_state.get("totalCostUSD"))
            session["native_cost"] = native_state.get("totalCostUSD") if session["native_cost_available"] else None
            session["native_cost_source"] = native_state.get("_source", "cost-state") if session["native_cost_available"] else None
            session["native_tokens"] = self._native_tokens(state)
            session["native_snapshot_tokens"] = self._native_tokens(native_state)
            session["cost_status"] = (("snapshot" if active else "recorded") if available else
                                      "unreconciled" if session["native_cost_available"] else
                                      "pending" if active else "missing")
            model_usage = state.get("modelUsage")
            advisor_amounts_available = bool(session["advisors"]) and isinstance(model_usage, dict) and all(
                isinstance(model_usage.get(advisor), dict) and is_amount(model_usage[advisor].get("costUSD"))
                for advisor in session["advisors"]
            )
            shared_models = set(session["advisors"]) & set(session["models"])
            session["advisor_cost_available"] = advisor_amounts_available and not shared_models
            session["advisor_cost_status"] = "missing" if session["advisors"] else "not_used"
            if shared_models:
                # Native costUSD is per model, not per role. It cannot separate a
                # model used as both the main assistant and Advisor without pricing.
                session["advisor_cost_status"] = "shared_model"
            elif session["advisor_cost_available"]:
                session["advisor_cost"] = sum(
                    number(model_usage[advisor]["costUSD"])
                    for advisor in session["advisors"]
                )
                exact_usage = not session["advisor_usage_missing_calls"] and all(
                    all(integer(model_usage[model].get(key)) == usage[key] for key in TOKEN_FIELDS)
                    for model, usage in session["advisor_model_tokens"].items()
                ) and set(session["advisor_model_tokens"]) == set(session["advisors"])
                session["advisor_cost_status"] = "recorded" if exact_usage else "model_total"
            session["last"] = session["last"] or session["first"]
        self._estimate_missing_costs()

    def _estimate_missing_costs(self) -> None:
        """Add a separate fallback; never replace or add to a usable native cost."""
        for session in self.sessions.values():
            session.update(estimated_cost=None, estimate_complete=False, estimate_notes=[],
                           estimated_models={}, estimated_advisor_cost=None)
        for event in self.usage_events:
            session = self.sessions[event["session"]]
            if session["cost_available"]:
                continue
            parts = event["api_estimates"]
            priced = [part for part in parts if part["cost"] is not None]
            if priced:
                session["estimated_cost"] = number(session["estimated_cost"]) + sum(p["cost"] for p in priced)
            if session["advisors"]:
                advisor_parts = [p for p in priced if p["advisor"]]
                if advisor_parts:
                    session["estimated_advisor_cost"] = number(session["estimated_advisor_cost"]) + sum(p["cost"] for p in advisor_parts)
            for part in priced:
                model = part["model"]
                session["estimated_models"][model] = session["estimated_models"].get(model, 0) + part["cost"]
            session["estimate_notes"].extend(note for p in parts for note in p["notes"])
            if any(not part["complete"] for part in parts):
                session["estimate_notes"].append("部分请求或附加费用无法估算")
            event["estimated_cost"] = sum(p["cost"] for p in priced)
        for session in self.sessions.values():
            if session["advisor_usage_missing_calls"]:
                session["estimate_notes"].append("部分 Advisor 调用未记录 Token，未估算这些调用")
            session["estimate_notes"] = sorted(set(session["estimate_notes"]))
            session["estimate_complete"] = (session["estimated_cost"] is not None and
                not any(note.startswith(("部分请求", "部分 Advisor")) for note in session["estimate_notes"]))
            session["combined_cost"] = (session["cost"] if session["cost_available"] else session["estimated_cost"])

    def _finalize_usage(self, requests: dict[str, dict[str, Any]], first_queue: dict[str, datetime]) -> None:
        self.shared_request_count = 0
        self.attribution_ambiguous_request_count = 0
        for request in requests.values():
            if not request["has_usage"]:
                continue
            candidates = request["sessions"]
            owner = next(iter(candidates))
            if len(candidates) > 1:
                self.shared_request_count += 1
                # Forks copy old assistant messages with rewritten sessionId but retain
                # their request ID and time. Their own queue starts after that history.
                eligible = [sid for sid, when in candidates.items()
                            if when and sid in first_queue and first_queue[sid] <= when]
                if len(eligible) == 1:
                    owner = eligible[0]
                else:
                    # Global totals still count once when ownership is indeterminate.
                    # Expose this limitation rather than claim exact project attribution.
                    self.attribution_ambiguous_request_count += 1
                    owner = min(eligible or list(candidates))
            session = self.sessions[owner]
            request["owner"] = owner
            model = request["model"] or "未知模型"
            session["models"][model] += 1
            # Use the deduplicated owner's roles, not a fork's copied history.
            # If a request is also present in a primary transcript, count its
            # model once as primary rather than manufacturing another agent call.
            roles = request["session_roles"][owner]
            role_models = "primary_models" if "primary" in roles else "subagent_models"
            session[role_models][model] += 1
            main_tokens = request["tokens"]
            main_estimate = (estimate_api_cost(model, main_tokens, request["pricing"]) if request["has_usage"] else
                             {"cost": None, "complete": False, "notes": ["请求未保存 Token 用量"]})
            api_estimates = [dict(main_estimate, model=model, advisor=False)]
            advisor_tokens = empty_tokens()
            advisor_models: dict[str, dict[str, int]] = {}
            for advisor in request["advisor_iterations"].values():
                name = advisor["model"] or request["advisor_model"] or "未知 Advisor 模型"
                advisor_estimate = (estimate_api_cost(name, advisor["tokens"], advisor["pricing"]) if advisor["has_usage"] else
                                    {"cost": None, "complete": False, "notes": ["Advisor 未保存 Token 用量"]})
                api_estimates.append(dict(advisor_estimate, model=name, advisor=True))
                session["advisors"][name] += 1
                row = advisor_models.setdefault(name, empty_tokens())
                model_total = session["advisor_model_tokens"].setdefault(name, empty_tokens())
                for key in TOKEN_FIELDS:
                    row[key] += advisor["tokens"][key]
                    model_total[key] += advisor["tokens"][key]
                    advisor_tokens[key] += advisor["tokens"][key]
            calls = max(len(request["advisor_tools"]), len(request["advisor_iterations"]))
            session["advisor_calls"] += calls
            session["advisor_usage_missing_calls"] += max(0, calls - len(request["advisor_iterations"]))
            if calls and not advisor_models:
                session["advisors"][request["advisor_model"] or "未知 Advisor 模型"] += calls
            combined = empty_tokens()
            for key in TOKEN_FIELDS:
                session["main_tokens"][key] += main_tokens[key]
                session["advisor_tokens"][key] += advisor_tokens[key]
                combined[key] = main_tokens[key] + advisor_tokens[key]
                session["tokens"][key] += combined[key]
            for sid in candidates:
                if sid != owner:
                    self.sessions[sid]["inherited_request_count"] += 1
                    for key in TOKEN_FIELDS:
                        self.sessions[sid]["inherited_tokens"][key] += combined[key]
            self.usage_events.append({
                "session": owner, "model": model, "time": candidates[owner],
                "tokens": combined, "main_tokens": dict(main_tokens),
                "advisor_tokens": advisor_tokens, "advisor_models": advisor_models,
                "api_estimates": api_estimates,
            })

    @staticmethod
    def _native_tokens(state: dict[str, Any]) -> dict[str, int] | None:
        models = state.get("modelUsage")
        required = tuple(target for _, target in USAGE_FIELDS)
        if not isinstance(models, dict) or not models:
            return None
        total = empty_tokens()
        for raw in models.values():
            if not isinstance(raw, dict) or not all(is_amount(raw.get(key)) for key in required):
                return None
            add_tokens(total, raw, native=True)
        return total

    @staticmethod
    def _ledger_contains(later: dict[str, Any], earlier: dict[str, Any]) -> bool:
        """Numeric compatibility is necessary, but never proves lineage alone."""
        if not is_amount(earlier.get("totalCostUSD")) or not is_amount(later.get("totalCostUSD")):
            return False
        if number(earlier["totalCostUSD"]) > number(later["totalCostUSD"]) + 1e-8:
            return False
        before, after = earlier.get("modelUsage"), later.get("modelUsage")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        for model, raw in before.items():
            dest = after.get(model)
            if not isinstance(raw, dict) or not isinstance(dest, dict):
                return False
            for key in (*TOKEN_FIELDS, "costUSD", "webSearchRequests"):
                if key in raw and (not is_amount(raw[key]) or not is_amount(dest.get(key)) or
                                   number(raw[key]) > number(dest[key]) + 1e-8):
                    return False
        return True

    def _reconcile_costs(self) -> None:
        """Subtract only a saved, evidenced ancestor; exclude uncertain ledgers.

        Same price or same startTime alone is insufficient. A baseline must be
        saved before the new session's first queue, its recorded requests must
        appear in the new transcript, and every model counter must be compatible.
        Divergent branches without a common dominating ancestor remain unknown.
        """
        groups: dict[Any, list[str]] = defaultdict(list)
        records: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in self.cost_records:
            records[record["session"]].append(record)
        for sid, state in self.latest_states.items():
            start = state.get("startTime")
            if is_amount(start) and start:
                groups[start].append(sid)

        def birth(sid: str) -> datetime:
            return self.first_queue.get(sid) or self.sessions[sid]["first"] or datetime.max.replace(tzinfo=LOCAL_ZONE)

        for sid, state in self.latest_states.items():
            session = self.sessions[sid]
            session.update(cost_basis="native", inherited_cost=0.0, cost_baseline_session=None,
                           cost_reconciliation_reason=None)
            group = groups.get(state.get("startTime"), [sid])
            root = min(group, key=lambda key: (birth(key), key))
            needs_baseline = sid != root or bool(session["inherited_request_count"])
            baseline = None
            if needs_baseline:
                candidates = []
                inherited_prefix = {key for key, when in self.file_request_times.get(sid, {}).items()
                                    if key in self.file_histories.get(sid, set()) and when and
                                    sid in self.first_queue and when < self.first_queue[sid]}
                for other in self.cost_records:
                    if (other["session"] == sid or other["session"] not in group or
                            not other["history"] or not other["history"] <= self.file_histories.get(sid, set()) or
                            not inherited_prefix or not inherited_prefix <= other["history"] or
                            sid not in self.first_queue or not other["time"] or
                            other["time"] > self.first_queue[sid] or birth(other["session"]) >= birth(sid)):
                        continue
                    if self._ledger_contains(state, other["state"]):
                        candidates.append(other)
                # A later point on one lineage must dominate all matching saved
                # ancestors in both request history and native model counters.
                maxima = [item for item in candidates if all(
                    old["history"] <= item["history"] and self._ledger_contains(item["state"], old["state"])
                    for old in candidates)]
                if maxima:
                    baseline = max(maxima, key=lambda item: (item["time"], item["session"]))
                else:
                    session.update(cost_basis="unreconciled", inherited_cost=None,
                                   cost_reconciliation_reason="继承账本缺少可验证的原生基数" if not candidates else
                                   "存在多条无法确定先后的继承账本")
                    continue

            own_records = records.get(sid, [])
            if any((r["state"].get("startTime") != state.get("startTime") or
                    number(r["state"]["totalCostUSD"]) > number(state["totalCostUSD"]) + 1e-8)
                   for r in own_records):
                session.update(cost_basis="unreconciled", inherited_cost=None,
                               cost_reconciliation_reason="会话内账本发生重置或累计金额下降")
                continue

            base = baseline["state"] if baseline else {}
            base_cost = number(base.get("totalCostUSD"))
            adjusted = dict(state)
            adjusted["totalCostUSD"] = max(0.0, number(state["totalCostUSD"]) - base_cost)
            for duration in ("totalAPIDurationWithoutRetries", "totalToolDuration"):
                if is_amount(state.get(duration)):
                    adjusted[duration] = max(0.0, number(state[duration]) - number(base.get(duration)))
            if isinstance(state.get("modelUsage"), dict):
                adjusted["modelUsage"] = {}
                for model, raw in state["modelUsage"].items():
                    if not isinstance(raw, dict):
                        continue
                    previous = (base.get("modelUsage") or {}).get(model, {})
                    row = dict(raw)
                    for key in (*TOKEN_FIELDS, "costUSD", "webSearchRequests"):
                        if is_amount(raw.get(key)):
                            if model in (base.get("modelUsage") or {}) and not is_amount(previous.get(key)):
                                row.pop(key, None)
                                continue
                            row[key] = max(0, raw[key] - number(previous.get(key)))
                            if key != "costUSD":
                                row[key] = int(row[key])
                    adjusted["modelUsage"][model] = row
            self.accounted_states[sid] = adjusted
            if baseline:
                session.update(cost_basis="incremental", inherited_cost=base_cost,
                               cost_baseline_session=baseline["session"])
            previous = 0.0
            for record in own_records:
                value = max(0.0, number(record["state"]["totalCostUSD"]) - base_cost)
                # A final state can be valid while intermediate snapshots are stale.
                value = min(value, adjusted["totalCostUSD"])
                delta = max(0.0, value - previous)
                previous = max(previous, value)
                if delta:
                    self.cost_events.append({"session": sid, "model": None,
                                             "time": record["time"], "cost": delta})

    def _active_sessions(self) -> dict[str, str]:
        active = {}
        for path in (self.root / "sessions").glob("*.json"):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            sid = metadata.get("sessionId")
            if isinstance(sid, str) and process_is_running(metadata.get("pid")):
                active[sid] = str(metadata.get("status") or "unknown")
        return active

    def _read_native_project_costs(self) -> None:
        # Claude also saves the last session's ledger in its project config.
        # Only an exact session ID match may supply a missing cost-state.
        for path in (self.root / ".claude.json", self.root.parent / ".claude.json"):
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            projects = config.get("projects") if isinstance(config, dict) else None
            if not isinstance(projects, dict):
                continue
            for project in projects.values():
                if not isinstance(project, dict):
                    continue
                sid = project.get("lastSessionId")
                if not isinstance(sid, str) or sid not in self.sessions or sid in self.latest_states:
                    continue
                cost = project.get("lastCost")
                if not is_amount(cost):
                    continue
                state = {"totalCostUSD": cost, "modelUsage": project.get("lastModelUsage"),
                         "startTime": project.get("lastStartTime"), "_source": "project-config"}
                for source, target in (("lastAPIDurationWithoutRetries", "totalAPIDurationWithoutRetries"), ("lastToolDuration", "totalToolDuration")):
                    state[target] = project.get(source)
                self.latest_states[sid] = state
                when = self.sessions[sid]["last"]
                self.cost_records.append({"session": sid, "state": state, "time": when,
                                          "history": set(self.file_histories.get(sid, set()))})

    def _latest_model_totals(self) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        def model_row(model: str) -> dict[str, Any]:
            return output.setdefault(model, {"model": model, "cost": 0.0, "sessions": set(), "tokens": empty_tokens()})

        for event in self.usage_events:
            for model, tokens in [(event["model"], event["main_tokens"]), *event["advisor_models"].items()]:
                row = model_row(model)
                row["sessions"].add(event["session"])
                for key in TOKEN_FIELDS:
                    row["tokens"][key] += tokens[key]
        for sid, state in self.accounted_states.items():
            raw_models = state.get("modelUsage") or {}
            if not isinstance(raw_models, dict):
                continue
            for model, raw in raw_models.items():
                if not isinstance(raw, dict):
                    continue
                row = model_row(model)
                row["cost"] += number(raw.get("costUSD"))
                row["sessions"].add(sid)
            assigned = sum(number(raw.get("costUSD")) for raw in raw_models.values() if isinstance(raw, dict))
            remainder = number(state.get("totalCostUSD")) - assigned
            if remainder > 1e-8:
                row = model_row("未分配模型费用")
                row["cost"] += remainder
                row["sessions"].add(sid)
        for row in output.values():
            row["sessions"] = len(row["sessions"])
            row["estimated_cost"] = 0.0
        for sid, session in self.sessions.items():
            for model, cost in session.get("estimated_models", {}).items():
                row = output.setdefault(model, {"model": model, "cost": 0.0, "sessions": 0,
                                                "tokens": empty_tokens(), "estimated_cost": 0.0})
                row["estimated_cost"] += cost
        for row in output.values():
            row["combined_cost"] = row["cost"] + row["estimated_cost"]
        return output

    @staticmethod
    def _day(dt: datetime | None) -> str:
        return dt.strftime("%Y-%m-%d") if dt else "未知日期"

    def summary(self) -> dict[str, Any]:
        tokens = empty_tokens()
        main_tokens = empty_tokens()
        advisor_tokens = empty_tokens()
        for event in self.usage_events:
            for key in TOKEN_FIELDS:
                tokens[key] += event["tokens"][key]
                main_tokens[key] += event["main_tokens"][key]
                advisor_tokens[key] += event["advisor_tokens"][key]
        total_cost = sum(number(session["cost"]) for session in self.sessions.values())
        estimated_cost = sum(number(session.get("estimated_cost")) for session in self.sessions.values())
        cost_available_sessions = sum(bool(session.get("cost_available")) for session in self.sessions.values())
        cost_pending_sessions = sum(session.get("cost_status") == "pending" for session in self.sessions.values())
        cost_active_snapshots = sum(session.get("cost_status") == "snapshot" for session in self.sessions.values())
        cost_unreconciled_sessions = sum(session.get("cost_status") == "unreconciled" for session in self.sessions.values())
        native_tokens = empty_tokens()
        native_token_sessions = 0
        for session in self.sessions.values():
            if session.get("native_tokens") is not None:
                add_tokens(native_tokens, session["native_tokens"], native=True)
                native_token_sessions += 1
        active_duration_ms = sum(
            number(state.get("totalAPIDurationWithoutRetries")) + number(state.get("totalToolDuration"))
            for state in self.accounted_states.values()
        )
        models = self._latest_model_totals()

        daily: dict[str, dict[str, Any]] = {}
        for event in self.usage_events:
            day = self._day(event["time"])
            row = daily.setdefault(day, {"date": day, "cost": 0.0, "sessions": set(), "tokens": empty_tokens()})
            row["sessions"].add(event["session"])
            row["estimated_cost"] = row.get("estimated_cost", 0.0) + number(event.get("estimated_cost"))
            for key in TOKEN_FIELDS:
                row["tokens"][key] += event["tokens"][key]
        for event in self.cost_events:
            day = self._day(event["time"])
            row = daily.setdefault(day, {"date": day, "cost": 0.0, "sessions": set(), "tokens": empty_tokens()})
            row["cost"] += event["cost"]
            row["sessions"].add(event["session"])
        daily_rows = []
        for row in sorted(daily.values(), key=lambda item: item["date"]):
            row["sessions"] = len(row["sessions"])
            row.setdefault("estimated_cost", 0.0)
            row["combined_cost"] = row["cost"] + row["estimated_cost"]
            daily_rows.append(row)

        reference = datetime.now(LOCAL_ZONE).date()
        latest_day = reference.isoformat()
        periods = {}
        for label, span in (("day", 1), ("week", 7), ("month", 30)):
            period_tokens = empty_tokens()
            period_cost = 0.0
            period_estimated_cost = 0.0
            active_days = 0
            if reference:
                cutoff = reference - timedelta(days=span - 1)
                for row in daily_rows:
                    try:
                        day = datetime.strptime(row["date"], "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if cutoff <= day <= reference:
                        period_cost += row["cost"]
                        period_estimated_cost += row["estimated_cost"]
                        active_days += bool(row["cost"] or any(row["tokens"].values()))
                        for key in TOKEN_FIELDS:
                            period_tokens[key] += row["tokens"][key]
            periods[label] = {"cost": period_cost, "estimated_cost": period_estimated_cost,
                              "combined_cost": period_cost + period_estimated_cost,
                              "tokens": period_tokens, "active_days": active_days, "reference": latest_day}

        projects: dict[str, dict[str, Any]] = {}
        for session in self.sessions.values():
            project = session["project"]
            row = projects.setdefault(project, {"project": project, "sessions": 0, "cost": 0.0,
                                                "estimated_cost": 0.0, "tokens": empty_tokens()})
            row["sessions"] += 1
            row["cost"] += number(session["cost"])
            row["estimated_cost"] += number(session.get("estimated_cost"))
            row["combined_cost"] = row["cost"] + row["estimated_cost"]
            for key in TOKEN_FIELDS:
                row["tokens"][key] += session["tokens"][key]

        hours = Counter()
        weekdays = Counter()
        heatmap = Counter()
        for event in self.usage_events:
            if event["time"]:
                weekday = event["time"].weekday()
                hour = event["time"].hour
                hours[hour] += 1
                weekdays[weekday] += 1
                heatmap[(weekday, hour)] += 1

        sessions = []
        for session in self.sessions.values():
            row = dict(session)
            row["models"] = dict(session["models"])
            row["primary_models"] = dict(session["primary_models"])
            row["subagent_models"] = dict(session["subagent_models"])
            row["advisors"] = dict(session["advisors"])
            row["tokens"] = dict(session["tokens"])
            row["main_tokens"] = dict(session["main_tokens"])
            row["advisor_tokens"] = dict(session["advisor_tokens"])
            row["first"] = session["first"].isoformat() if session["first"] else None
            row["last"] = session["last"].isoformat() if session["last"] else None
            sessions.append(row)
        sessions.sort(key=lambda item: item["last"] or "", reverse=True)

        def serial_row(row: dict[str, Any]) -> dict[str, Any]:
            out = dict(row)
            out["tokens"] = dict(row["tokens"])
            return out

        return {
            "generated_at": datetime.now(LOCAL_ZONE).isoformat(),
            "data_root": str(self.root),
            "scanned_files": self.scanned_files,
            "scanned_records": self.scanned_records,
            "session_count": len(self.sessions),
            "cost_available_session_count": cost_available_sessions,
            "cost_missing_session_count": sum(not session.get("native_cost_available") for session in self.sessions.values()),
            "cost_pending_session_count": cost_pending_sessions,
            "cost_active_snapshot_session_count": cost_active_snapshots,
            "cost_unreconciled_session_count": cost_unreconciled_sessions,
            "cost_native_session_count": sum(bool(s.get("native_cost_available")) for s in self.sessions.values()),
            "cost_estimated_session_count": sum(s.get("estimated_cost") is not None for s in self.sessions.values()),
            "cost_estimate_partial_session_count": sum(s.get("estimated_cost") is not None and not s["estimate_complete"] for s in self.sessions.values()),
            "pricing": {"verified_at": PRICING_DATE, "source": PRICING_URL, "currency": "USD",
                        "basis": "当前第一方 API 公开价；未标明档位时按标准速度、全球区域估算；不含订阅费、税费及账户折扣",
                        "models": {model: dict(zip(("input", "output", "cache_5m", "cache_1h", "cache_read"), rates))
                                   for model, rates in API_PRICES.items()}},
            "cost_inherited_deduction": sum(number(s.get("inherited_cost")) for s in self.sessions.values()),
            "cost_unreconciled_native_sum": sum(number(s.get("native_cost")) for s in self.sessions.values()
                                                 if s.get("cost_status") == "unreconciled"),
            "cost_native_snapshot_sum": sum(number(s.get("native_cost")) for s in self.sessions.values()),
            "native_token_session_count": native_token_sessions,
            "usage_event_count": len(self.usage_events),
            "shared_request_count": self.shared_request_count,
            "attribution_ambiguous_request_count": self.attribution_ambiguous_request_count,
            "cost_state_count": len(self.latest_states),
            "totals": {"cost": total_cost, "estimated_cost": estimated_cost,
                       "combined_cost": total_cost + estimated_cost, "tokens": tokens, "main_tokens": main_tokens,
                       "advisor_tokens": advisor_tokens, "native_tokens": native_tokens},
            "active_duration_ms": active_duration_ms,
            "periods": periods,
            "models": [serial_row(v) for v in sorted(models.values(), key=lambda item: item["combined_cost"], reverse=True)],
            "projects": [serial_row(v) for v in sorted(projects.values(), key=lambda item: item["combined_cost"], reverse=True)],
            "daily": daily_rows,
            "active_hours": [{"hour": hour, "events": hours[hour]} for hour in range(24)],
            "active_weekdays": [{"weekday": day, "events": weekdays[day]} for day in range(7)],
            "active_heat": [
                {"weekday": day, "hours": [heatmap[(day, hour)] for hour in range(24)]}
                for day in range(7)
            ],
            "sessions": sessions,
            "notes": [
                "已对账费用以原生 cost-state 为依据；只有原生基数、复制请求、时间先后和模型计数共同支持继承关系时才扣除历史账本。无法确认基数的会话保留待对账状态，其原生累计不进入汇总；已归属的唯一日志请求可单独按 API 价格估算。原生累计金额保留在会话明细中。Claude 金额是使用费用估算。",
                "API 估算采用 2026-10-19 核对的第一方公开价：输入、输出、缓存读、5 分钟/1 小时缓存写分别计费；Thinking 不另计；已知 Fast、US 区域和搜索附加费纳入。未注明缓存时长时按 5 分钟估算，未知模型不套用其他模型价格。",
                "每日原生费用为去除继承后的快照增量，按快照附近的事件时间归属，跨午夜为近似分配；今日与近 7/30 天按 Asia/Tokyo 的真实当天计算。",
                "Token 按唯一 API 请求统计：合并所有流式累计 usage，保留各项已记录的最大值；跨会话复制的相同 requestId 只计一次，缺失时使用 message.id / uuid。",
                f"发现 {self.shared_request_count} 个跨会话历史副本请求；依据请求时间与各会话首次队列时间归属，副本不计作新消耗。归属不确定的请求有 {self.attribution_ambiguous_request_count} 个，其总量仍只计一次。",
                "仍打开的会话可能只在 Claude 进程内保存当前金额；本地没有金额时按官方 API 价格估算，已有可对账金额时使用已保存快照；不在旧原生快照上追加整段日志估算。原生记录可用后替换估算。",
                "顶部费用合计 = 已对账原生费用 + 缺口会话 API 估算，明细分别显示；缺少模型价格或调用用量时标记部分估算。原始待写入/待对账状态保留，API 估算不是订阅账单。",
                "日志 Token 总量 = 输入 + 输出 + 缓存读 + 缓存写，包含主模型和 Advisor；Thinking 已包含在输出中，不重复相加。会话 Token 列也是主模型 + Advisor；Advisor 列仅展示其中的组成部分，不再重复相加。每日、项目和模型的 Token 使用同一去重口径。",
                "会话模型列根据 agentId 或 subagents 日志目录识别子代理，子代理模型名整体放入括号并以灰色显示；主对话实际使用的多个模型保留多模型展示。同一模型兼任两种角色时分别展示，Token 与费用仍按唯一请求统计一次。",
                "Token 统计范围为本地 assistant usage 与 advisor_message；未写入这些日志的内部调用无法还原。模型费用优先使用原生快照，缺口按 API 价格估算；原生模型用量可能大于日志可还原用量。",
                "Advisor 介入以 server_tool_use/advisor 或 advisor_message 为证；其独立 Token 已包含在会话合计中。只有工具记录而没有对应 usage 的调用标记为用量未记录或部分记录。",
                "JSONL 的 cost-state.modelUsage 保存按模型累计的 Token 和金额。原生 Token 对账汇总使用已确认账本增量；仅覆盖有完整计数字段且已对账的快照，会话/调用范围及保存时间可能与逐请求日志不同，不能要求两个数强行一致。",
                "会话新增费用和 Advisor 模型费用共同扣除已确认的继承基数。Advisor 已包含在会话金额中，不能重复相加；同模型兼任两种角色时无法拆分。",
            ],
        }


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Cost Lens</title>
<style>
:root{color-scheme:dark;--bg:#0e1117;--panel:#171b24;--line:#2a3140;--muted:#8e99ab;--text:#eef2f8;--accent:#7c9cff;--green:#53d39a;--orange:#ffb86b;--pink:#e58cff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 8% 0%,#263b72 0,#111522 25%,transparent 48%),radial-gradient(circle at 92% 18%,#40205d 0,#10131d 28%,transparent 52%),#0b0e14;color:var(--text);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif}
.wrap{max-width:1500px;margin:0 auto;padding:34px 28px}.head{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:24px;padding:20px 22px;border:1px solid #33405b;border-radius:18px;background:linear-gradient(110deg,#19233a99,#17152577);box-shadow:0 16px 50px #0005;position:relative;overflow:hidden}.head:after{content:"";position:absolute;width:260px;height:260px;right:8%;top:-180px;border-radius:50%;background:#7c9cff55;filter:blur(28px)}.eyebrow{display:inline-flex;color:#b9c8ff;font-weight:750;letter-spacing:.1em;text-transform:uppercase;font-size:10px;background:#273558aa;border:1px solid #415584;padding:4px 8px;border-radius:99px}.title{font-size:32px;letter-spacing:-.03em;font-weight:800;margin:7px 0}.sub{color:var(--muted)}button{border:1px solid #5269a1;background:linear-gradient(135deg,#4d67bd,#8756bc);color:white;border-radius:10px;padding:10px 15px;cursor:pointer;box-shadow:0 7px 18px #4d67bd55;position:relative;z-index:1}button:hover{filter:brightness(1.15);transform:translateY(-1px)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px;margin-bottom:14px}.card,.panel{background:linear-gradient(145deg,rgba(25,31,45,.96),rgba(15,18,27,.96));border:1px solid #293246;border-radius:16px;box-shadow:0 12px 34px #0004}.card{padding:17px;position:relative;overflow:hidden}.card.tip{overflow:visible;z-index:5}.card:after{content:"";position:absolute;inset:auto -20px -45px auto;width:130px;height:90px;background:#788cff22;filter:blur(20px);transform:rotate(-18deg)}.card:hover,.panel:hover{border-color:#46557a}.label{color:#a5b0c4;font-size:12px}.value{font-size:26px;font-weight:800;letter-spacing:-.03em;margin-top:5px;color:#f6f8ff}.small{font-size:12px;color:var(--muted)}.layout{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}.panel{padding:18px;margin-bottom:14px}.panel h2{font-size:16px;margin:0 0 14px;letter-spacing:-.01em}.panel h2:before{content:"";display:inline-block;width:4px;height:15px;border-radius:4px;background:linear-gradient(#7c9cff,#e58cff);margin-right:8px;vertical-align:-2px}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:14px}.panel-head h2{margin:0}.session-filter{display:flex;align-items:center;justify-content:flex-end;gap:6px;flex-wrap:wrap}.session-filter label,.session-filter span{font-size:11px;color:#aeb9cf}.session-filter input{height:30px;padding:0 8px;border:1px solid #3c4967;border-radius:8px;background:#101623;color:#eef2f8;color-scheme:dark;font:11px inherit}.session-filter button{height:30px;padding:5px 10px;font-size:11px}.session-filter .filter-meta{flex-basis:100%;text-align:right;color:var(--muted);font-size:10px}.wide{grid-column:1/-1}.bars{display:flex;align-items:flex-end;gap:5px;height:175px;padding:5px 0}.barcol{flex:1;min-width:4px;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:5px}.bar{width:100%;max-width:25px;background:linear-gradient(180deg,#9da9ff,#5268e0 62%,#9b5cdb);border-radius:7px 7px 3px 3px;min-height:2px;box-shadow:0 0 15px #687cff55}.barlabel{font-size:10px;color:var(--muted);white-space:nowrap;transform:rotate(-45deg);transform-origin:top left;margin-top:12px}.legend{display:flex;gap:12px;color:var(--muted);font-size:12px}.legend i{display:inline-block;width:8px;height:8px;border-radius:50%;background:#8497ff;margin-right:4px;box-shadow:0 0 8px #8497ff}.tablewrap{overflow:auto;max-height:720px;max-height:min(76vh,800px)}.tablewrap thead th{position:sticky;top:0;z-index:2;background:#151b27}table{width:100%;min-width:1040px;table-layout:fixed;border-collapse:collapse}th,td{text-align:left;padding:12px 10px;border-bottom:1px solid #252c39;white-space:nowrap;vertical-align:top}th{color:#99a6bd;font-size:11px;font-weight:650}td{font-size:13px}th:nth-child(1),td:nth-child(1){width:20%;min-width:200px}th:nth-child(2),td:nth-child(2){width:15%;min-width:150px}th:nth-child(3),td:nth-child(3){width:18%;min-width:175px}th:nth-child(4),td:nth-child(4){width:10%;min-width:100px}th:nth-child(5),td:nth-child(5){width:9%;min-width:90px}th:nth-child(6),td:nth-child(6){width:13%;min-width:140px}th:nth-child(7),td:nth-child(7){width:15%;min-width:145px}td:nth-child(-n+3){overflow:hidden;text-overflow:ellipsis}td.session-models{white-space:normal;line-height:1.5;overflow-wrap:anywhere}.subagent-model{color:var(--muted);display:inline-block}td .small{display:block;margin-top:5px;line-height:1.35}.right{text-align:right}.money{display:inline-block!important;font-variant-numeric:tabular-nums;font-weight:800;color:#ffd58e;text-shadow:0 0 14px #ffb86b55;letter-spacing:.01em}.pill{display:inline-block;padding:3px 7px;border-radius:20px;background:#222b3b;color:#b9c8ff;font-size:11px}.rank{display:grid;grid-template-columns:170px 1fr 85px;align-items:center;gap:10px;margin:11px 0}.track{height:8px;background:#252c39;border-radius:10px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,#7890ff,#d86cff);border-radius:10px;box-shadow:0 0 10px #8b73ff66}.range-panel{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px;padding:12px 15px;border:1px solid #293246;border-radius:13px;background:linear-gradient(120deg,#171e30dd,#151622dd)}.range-copy{display:flex;flex-direction:column;gap:2px}.range-title{font-size:13px;font-weight:750;color:#dbe3f5}.range-help{font-size:11px;color:var(--muted)}.range-form{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.range-form label{font-size:11px;color:#aeb9cf}.range-form input{height:32px;padding:0 9px;border:1px solid #3c4967;border-radius:8px;background:#101623;color:#eef2f8;color-scheme:dark;font:12px inherit}.range-form button{height:32px;padding:6px 12px;font-size:12px}.heat-scroll{overflow-x:auto;padding-bottom:4px}.heatmap{display:grid;grid-template-columns:42px repeat(24,minmax(18px,1fr));gap:4px;min-width:760px}.heat-corner{height:16px}.heat-hour{height:16px;color:#8190ab;font-size:9px;text-align:center}.heat-weekday{height:23px;display:flex;align-items:center;color:#aeb9cf;font-size:10px}.cell{height:23px;border-radius:5px;background:#222a38;display:flex;align-items:center;justify-content:center;color:#d9e1f4;font-size:9px;border:1px solid #34405c55}.tip{position:relative;cursor:help}.tip-pop{position:fixed;z-index:1000;left:0;top:0;bottom:auto;display:flex;flex-direction:column;gap:5px;min-width:250px;max-width:380px;padding:11px 13px;border:1px solid #52638e;border-radius:10px;background:#111827f7;color:#eef2f8;box-shadow:0 14px 30px #0009;font-size:12px;line-height:1.4;white-space:normal;text-align:left;opacity:0;visibility:hidden;pointer-events:none;transform:translateY(4px);transition:opacity .16s ease,transform .16s ease}.tip-pop span{display:flex;justify-content:space-between;gap:18px;white-space:nowrap}.tip-pop b{color:#9eacc8;font-weight:550}.tip-caption{display:block!important;padding-bottom:5px;margin-bottom:2px;border-bottom:1px solid #3a4662;color:#cbd6ef}.tip:hover>.tip-pop,.tip:focus-visible>.tip-pop{opacity:1;visibility:visible;transform:translateY(0)}.cost-pending{color:var(--orange)}.error{background:#46252d;border:1px solid #874454;padding:14px;border-radius:10px;color:#ffc5d1}
@media(max-width:950px){.grid{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.wide{grid-column:auto}}@media(max-width:700px){.panel-head{display:block}.session-filter{justify-content:flex-start;margin-top:10px}.session-filter .filter-meta{text-align:left}}@media(max-width:560px){.wrap{padding:16px}.grid{grid-template-columns:1fr}.head{display:block}.range-panel{align-items:stretch;flex-direction:column}.range-form{align-items:stretch}.range-form input,.range-form button{flex:1}.session-filter{align-items:stretch}.session-filter input,.session-filter button{flex:1}.rank{grid-template-columns:120px 1fr 65px}}
.cost-value{display:inline-flex;flex-direction:column;align-items:flex-end;gap:2px;vertical-align:top}.card .value .cost-value{align-items:flex-start}.cost-estimate{font-size:11px;line-height:1.25;color:var(--muted);font-weight:400;white-space:nowrap;letter-spacing:0;text-shadow:none}
</style></head><body><main class="wrap"><header class="head"><div><div class="eyebrow">LOCAL / READ ONLY</div><div class="title">Claude Cost Lens</div><div class="sub" id="meta">正在读取 Claude 数据…</div></div><button onclick="loadData()">刷新数据</button></header><div id="app"></div></main>
<script>
const fmt=n=>new Intl.NumberFormat('zh-CN',{maximumFractionDigits:0}).format(n||0), money=n=>'$'+Number(n||0).toFixed(2), feeFmt=n=>`<span class="money">${money(n)}</span>`, esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function tokenTotal(t){return (t.inputTokens||0)+(t.outputTokens||0)+(t.cacheReadInputTokens||0)+(t.cacheCreationInputTokens||0)}
function tokenFmt(n){n=Number(n||0);if(n>=999995000)return (n/1e9).toFixed(2)+'B';if(n>=999995)return (n/1e6).toFixed(2)+'M';if(n>=1000)return (n/1e3).toFixed(2)+'K';return fmt(n)}
function tokenTipHtml(t,prefix=''){const rows=[['输入',tokenFmt(t.inputTokens)],['输出',tokenFmt(t.outputTokens)],['缓存读',tokenFmt(t.cacheReadInputTokens)],['缓存写',tokenFmt(t.cacheCreationInputTokens)],['其中 Thinking',tokenFmt(t.thinkingTokens)],['合计',tokenFmt(tokenTotal(t))]];return `${prefix?`<span class=\"tip-caption\">${esc(prefix)}</span>`:''}${rows.map(([label,value])=>`<span><b>${label}</b><span>${value}</span></span>`).join('')}`}
function tokenSourceTipHtml(main,advisor){return `<span class="tip-caption">来源拆分（已包含在合计中）</span><span><b>主模型</b><span>${tokenFmt(tokenTotal(main||{}))}</span></span><span><b>Advisor</b><span>${tokenFmt(tokenTotal(advisor||{}))}</span></span>`}
function totalTokenTipHtml(totals){return tokenTipHtml(totals.tokens,'日志 Token 总量')+tokenSourceTipHtml(totals.main_tokens,totals.advisor_tokens)}
function sessionTokenTipHtml(x){return tokenTipHtml(x.tokens,'逐请求日志合计（含 Advisor）')+tokenSourceTipHtml(x.main_tokens,x.advisor_tokens)+(x.native_snapshot_tokens?`<span class="tip-caption">原生 cost-state 模型汇总</span><span><b>原生累计（可能含继承）</b><span>${tokenFmt(tokenTotal(x.native_snapshot_tokens))}</span></span>${x.native_tokens?`<span><b>已对账新增范围</b><span>${tokenFmt(tokenTotal(x.native_tokens))}</span></span>`:'<span>继承基数待对账</span>'}`:'<span class="tip-caption">原生 Token 快照未保存或字段不完整</span>')+(x.advisor_usage_missing_calls?`<span><b>Advisor 用量未记录</b><span>${fmt(x.advisor_usage_missing_calls)} 次</span></span>`:'')+(x.inherited_request_count?`<span class="tip-caption">已排除历史副本</span><span><b>重复请求</b><span>${fmt(x.inherited_request_count)}</span></span><span><b>副本 Token</b><span>${tokenFmt(tokenTotal(x.inherited_tokens||{}))}</span></span>`:'')}
function advisorTokenTipHtml(x){const missing=x.advisor_usage_missing_calls||0;return (missing&&!tokenTotal(x.advisor_tokens||{})?'<span class="tip-caption">Advisor Token</span><span><b>用量</b><span>未记录</span></span>':tokenTipHtml(x.advisor_tokens||{},missing?'Advisor Token（已记录部分）':'Advisor Token（已含在会话合计）'))+(missing?`<span><b>用量未记录</b><span>${fmt(missing)} 次</span></span>`:'')}
function advisorTokenHtml(x){const n=tokenTotal(x.advisor_tokens||{});if(x.advisor_usage_missing_calls)return n?`${tokenFmt(n)}（部分记录）`:'用量未记录';return tokenFmt(n)}
function sessionFeeHtml(x,advisor=false){
 const estimate=advisor?x.estimated_advisor_cost:x.estimated_cost;
 if(!x.cost_available&&estimate!=null){const details=(x.estimate_notes||[]).join('；');return `<span title="${esc(details||'按官方 API 公开价与去重后的日志用量估算')}；价格核对 2026-10-19">${feeFmt(estimate)}<span class="small">API 估算${x.estimate_complete?'':'（部分）'}</span></span>`}
 if(x.cost_status==='unreconciled')return `<span class="small cost-pending" title="${esc(x.cost_reconciliation_reason||'继承基数无法确认')}；原生累计 ${money(x.native_cost)}，未计入已对账汇总">待对账</span>`;
 if(advisor&&x.advisor_cost_status==='shared_model')return '<span class="small" title="同一模型同时用于主模型和 Advisor，原生费用没有按角色拆分；会话金额仍使用已对账增量">无法拆分</span>';
 const available=advisor?x.advisor_cost_available:x.cost_available;
 if(!available){const pending=x.cost_status==='pending';return `<span class="small${pending?' cost-pending':''}" title="${pending?'Claude 会话仍打开，原生费用尚未保存到本地文件':'本地文件尚无对应的原生费用记录'}">${pending?'待写入':'未记录'}</span>`}
 return feeFmt(advisor?x.advisor_cost:x.cost)+(!advisor&&x.cost_basis==='incremental'?`<span class="small" title="原生累计 ${money(x.native_cost)}；扣除继承 ${money(x.inherited_cost)}">已扣继承</span>`:'')+(advisor&&x.advisor_cost_status==='model_total'?'<span class="small" title="原生模型累计费用的范围与去重后的 Advisor 日志不一致，可能包含历史或日志未记录的调用；该费用已在会话原生总额中">模型累计</span>':'')+(x.cost_status==='snapshot'?'<span class="small">已保存快照</span>':'');
}
function modelLabel(m){if(!m)return '未知模型';if(m==='<synthetic>')return 'Advisor / Synthetic';return m.replace(/^claude-/i,'Claude ').replace(/-(\d+)-(\d+)$/,' $1.$2').replace(/(^|\s)([a-z])/g,(a,b,c)=>b+c.toUpperCase())}
function sessionModelLabels(x){const primary=x.primary_models??x.models??{};return [...Object.keys(primary).map(modelLabel),...Object.keys(x.subagent_models||{}).map(m=>`(${modelLabel(m)})`)]}
function sessionModelsHtml(x){const primary=x.primary_models??x.models??{};return [...Object.keys(primary).map(m=>esc(modelLabel(m))),...Object.keys(x.subagent_models||{}).map(m=>`<span class="subagent-model">(${esc(modelLabel(m))})</span>`)].join(', ')||'—'}
function combinedFeeHtml(row){return `<span class="cost-value">${feeFmt(row?.combined_cost??row?.cost??0)}${row?.estimated_cost?`<span class="cost-estimate" title="其中按官方 API 价格估算的部分">估算 ${money(row.estimated_cost)}</span>`:''}</span>`}
function durationFmt(ms){let min=Math.round(Number(ms||0)/60000);if(min<60)return `${min} 分钟`;let h=Math.floor(min/60),m=min%60;return `${h} 小时 ${m} 分钟`}
function shiftDate(date,days){const dt=new Date(`${date}T00:00:00Z`);dt.setUTCDate(dt.getUTCDate()+days);return dt.toISOString().slice(0,10)}
let rangeData=null;
function updateCustomRange(){
 let start=document.getElementById('rangeStart')?.value||'';
 let end=document.getElementById('rangeEnd')?.value||'';
 if(start&&end&&start>end){[start,end]=[end,start];const startEl=document.getElementById('rangeStart');const endEl=document.getElementById('rangeEnd');if(startEl)startEl.value=start;if(endEl)endEl.value=end}
 const rows=(rangeData?.daily||[]).filter(row=>(!start||row.date>=start)&&(!end||row.date<=end));
 const tokens=emptyTokenView();
 let cost=0,estimated=0,activeDays=0;
 for(const row of rows){cost+=Number(row.combined_cost??row.cost??0);estimated+=Number(row.estimated_cost||0);if(Number(row.cost||0)||tokenTotal(row.tokens))activeDays++;for(const key of Object.keys(tokens))tokens[key]+=Number(row.tokens?.[key]||0)}
 const costEl=document.getElementById('customRangeCost');
 const metaEl=document.getElementById('customRangeMeta');
 if(costEl)costEl.innerHTML=combinedFeeHtml({combined_cost:cost,estimated_cost:estimated});
 if(metaEl)metaEl.textContent=`${start||'起始'} 至 ${end||'结束'} · ${activeDays} 个活跃日 · ${tokenFmt(tokenTotal(tokens))} tokens`;
}
function emptyTokenView(){return {inputTokens:0,outputTokens:0,thinkingTokens:0,cacheReadInputTokens:0,cacheCreationInputTokens:0}}
function sessionDateValue(value){if(!value)return '';const dt=new Date(value);return Number.isNaN(dt.getTime())?'':new Date(dt.getTime()+9*3600000).toISOString().slice(0,10)}
function updateSessionRange(){
 let start=document.getElementById('sessionStart')?.value||'';
 let end=document.getElementById('sessionEnd')?.value||'';
 if(start&&end&&start>end){[start,end]=[end,start];const startEl=document.getElementById('sessionStart');const endEl=document.getElementById('sessionEnd');if(startEl)startEl.value=start;if(endEl)endEl.value=end}
 const rows=[...document.querySelectorAll('#sessionBody tr[data-session-row]')];
 let visible=0;
 rows.forEach(row=>{const date=row.dataset.sessionDate||'';const show=(!start&&!end)||Boolean(date)&&(!start||date>=start)&&(!end||date<=end);row.hidden=!show;if(show)visible++});
 const meta=document.getElementById('sessionFilterMeta');
 if(meta)meta.textContent=`显示 ${visible} / ${rows.length} 个会话`;
}
function render(d){
 document.getElementById('meta').textContent=`${d.data_root} · ${d.scanned_files} 个文件 · ${d.session_count} 个会话 · 生成于 ${new Date(d.generated_at).toLocaleString()}`;
 const t=d.totals.tokens, p=d.periods||{}, days=d.daily.slice(-30), max=Math.max(...days.map(x=>tokenTotal(x.tokens)),1), models=d.models, projects=d.projects;
 const modelMax=Math.max(...models.map(x=>x.combined_cost??x.cost),1), projectMax=Math.max(...projects.map(x=>x.combined_cost??x.cost),1), activeMax=Math.max(...d.active_hours.map(x=>x.events),1);
 const dailyBars=days.map(x=>`<div class="barcol tip" tabindex="0"><div class="bar" style="height:${Math.max(2,tokenTotal(x.tokens)/max*100)}%"></div><div class="barlabel">${x.date.slice(5)}</div><span class="tip-pop"><span class="tip-caption">${x.date}</span>${tokenTipHtml(x.tokens)}<span><b>费用合计</b>${combinedFeeHtml(x)}</span><span><b>会话</b><span>${x.sessions}</span></span></span></div>`).join('');
 const modelRows=models.map(x=>`<div class="rank tip" tabindex="0"><span>${esc(modelLabel(x.model))}</span><div class="track"><div class="fill" style="width:${(x.combined_cost??x.cost)/modelMax*100}%"></div></div><span class="right">${combinedFeeHtml(x)}</span><span class="tip-pop">${tokenTipHtml(x.tokens)}<span><b>会话</b><span>${x.sessions}</span></span></span></div>`).join('')||'<div class="small">暂无模型数据</div>';
 const advisorCell=x=>{const names=Object.keys(x.advisors||{});if(!names.length)return '<span class="small">未介入</span>';const at=x.advisor_tokens||{};return `<span class="pill">已介入</span><div class="small">${names.map(modelLabel).join(', ')}</div><div class="small">${fmt(x.advisor_calls||0)} 次介入</div><div class="small">Advisor Token ${advisorTokenHtml(x)}</div><div class="small">Advisor 费用 ${sessionFeeHtml(x,true)}</div>`};
 const projectRows=projects.slice(0,12).map(x=>`<div class="rank"><span title="${esc(x.project)}">${esc(x.project).slice(0,24)}</span><div class="track"><div class="fill" style="width:${(x.combined_cost??x.cost)/projectMax*100}%"></div></div><span class="right">${combinedFeeHtml(x)}</span></div>`).join('')||'<div class="small">暂无项目数据</div>';
 const weekdayLabels=['周一','周二','周三','周四','周五','周六','周日'];
 const heatRows=d.active_heat||weekdayLabels.map((_,weekday)=>({weekday,hours:Array.from({length:24},()=>0)}));
 const heatMax=Math.max(...heatRows.flatMap(row=>row.hours),1);
 const heatHeader=`<div class="heat-corner"></div>${Array.from({length:24},(_,hour)=>`<div class="heat-hour">${hour}</div>`).join('')}`;
 const heat=heatHeader+heatRows.map(row=>`<div class="heat-weekday">${weekdayLabels[row.weekday]||row.weekday}</div>${row.hours.map((events,hour)=>`<div class="cell" style="background:rgba(124,156,255,${.08+.84*events/heatMax})" title="${weekdayLabels[row.weekday]||row.weekday} ${hour}:00 · ${events} 个请求">${events||''}</div>`).join('')}`).join('');
 const latestDate=d.daily.length?d.daily[d.daily.length-1].date:'';
 const defaultStart=latestDate?shiftDate(latestDate,-6):'';
 const sessionDates=d.sessions.map(x=>sessionDateValue(x.last)).filter(Boolean).sort();
 const sessionEndDate=sessionDates.length?sessionDates[sessionDates.length-1]:latestDate;
 const sessionStartDate=sessionEndDate?shiftDate(sessionEndDate,-29):'';
 const sessionRows=d.sessions.map(x=>{const names=Object.keys(x.advisors||{});const at=x.advisor_tokens||{};const advisorTip=names.length?`<span class="tip-pop"><span class="tip-caption">Advisor 模型 · ${fmt(x.advisor_calls||0)} 次介入</span>${names.map(n=>`<span><b>模型</b><span>${esc(modelLabel(n))}</span></span>`).join('')}${advisorTokenTipHtml(x)}<span><b>费用（会话内）</b>${sessionFeeHtml(x,true)}</span></span>`:`<span class="tip-pop"><span>未介入</span></span>`;const sessionCost=sessionFeeHtml(x);return `<tr data-session-row data-session-date="${sessionDateValue(x.last)}"><td>${esc(x.title||'未命名对话')}</td><td>${esc(x.project)}</td><td class="session-models" title="${esc(sessionModelLabels(x).join(', '))}">${sessionModelsHtml(x)}</td><td class="right tip" tabindex="0">${tokenFmt(tokenTotal(x.tokens))}<span class="tip-pop">${sessionTokenTipHtml(x)}</span></td><td class="right">${sessionCost}</td><td class="tip" tabindex="0">${advisorCell(x)}${advisorTip}</td><td>${x.last?new Date(x.last).toLocaleString():''}</td></tr>`}).join('');
 rangeData=d;
 document.getElementById('app').innerHTML=`<div class="range-panel"><div class="range-copy"><div class="range-title">费用时间范围</div><div class="range-help">原生费用优先，缺口按 API 公开价估算；原生费用日期按快照近似归属</div></div><div class="range-form"><label for="rangeStart">开始</label><input id="rangeStart" type="date" value="${defaultStart}"><label for="rangeEnd">结束</label><input id="rangeEnd" type="date" value="${latestDate}"><button id="applyRange">应用范围</button></div></div><section class="grid"><div class="card"><div class="label">费用合计（原生 + API 估算）</div><div class="value">${feeFmt(d.totals.combined_cost??d.totals.cost)}</div><div class="small">原生 ${money(d.totals.cost)} + API 估算 ${money(d.totals.estimated_cost)}<br>${d.cost_available_session_count} 个会话原生已对账 · ${d.cost_estimated_session_count||0} 个使用估算 · ${d.cost_pending_session_count} 个待写入 · ${d.cost_unreconciled_session_count||0} 个待对账${d.cost_active_snapshot_session_count?` · ${d.cost_active_snapshot_session_count} 个活动快照`:''}</div></div><div class="card"><div class="label">自定义范围费用合计</div><div class="value" id="customRangeCost">$0.00</div><div class="small" id="customRangeMeta">正在计算…</div></div><div class="card"><div class="label">今日费用合计</div><div class="value">${combinedFeeHtml(p.day)}</div><div class="small">${tokenFmt(tokenTotal(p.day?.tokens||{}))} tokens</div></div><div class="card"><div class="label">近 7 天费用合计</div><div class="value">${combinedFeeHtml(p.week)}</div><div class="small">${p.week?.active_days||0} 个活跃日</div></div><div class="card"><div class="label">近 30 天费用合计</div><div class="value">${combinedFeeHtml(p.month)}</div><div class="small">${p.month?.active_days||0} 个活跃日</div></div><div class="card tip" tabindex="0"><div class="label">Token 总量</div><div class="value">${tokenFmt(tokenTotal(t))}</div><div class="small">按日志统计 · 含 Advisor · 历史副本已去重</div><span class="tip-pop">${totalTokenTipHtml(d.totals)}</span></div><div class="card"><div class="label">已对账范围活跃时长</div><div class="value">${durationFmt(d.active_duration_ms)}</div><div class="small">API + 工具耗时</div></div><div class="card"><div class="label">活跃 API 请求</div><div class="value">${fmt(d.usage_event_count)}</div><div class="small">跨会话去重 · 合并流式用量</div></div></section>
 <section class="layout"><div class="panel wide"><h2>原生快照 Token 对账</h2><div class="small">API 估算价表核对：${esc(d.pricing?.verified_at||'')} · <a href="https://platform.claude.com/docs/en/about-claude/pricing" target="_blank" rel="noreferrer" style="color:#b9c8ff">Claude 官方价格</a><br>${esc(d.pricing?.basis||'')}。未收录模型或缺少 Advisor 用量时仅估算已知部分。</div><div class="value">${tokenFmt(tokenTotal(d.totals.native_tokens||{}))}</div><div class="small">JSONL cost-state.modelUsage · ${d.native_token_session_count||0} 个会话有完整计数字段且已对账 · 已扣除确认的继承基数。此范围可能包含日志未记录的调用，也可能尚未保存最新用量。</div><div class="small">费用已扣除确认继承 ${feeFmt(d.cost_inherited_deduction||0)} · ${d.cost_unreconciled_session_count||0} 个会话继承基数待对账 · 顶部金额包含已对账原生费用和日志 API 估算。</div><div class="tip" tabindex="0">查看原生 Token 明细<span class="tip-pop">${tokenTipHtml(d.totals.native_tokens||{},'已对账原生快照范围')}</span></div></div><div class="panel"><h2>每日消耗（最近 30 天）</h2><div class="bars">${dailyBars||'<div class="small">暂无数据</div>'}</div><div class="legend"><span><i></i>高度代表 Token 数，悬停查看费用</span></div></div><div class="panel"><h2>整体模型偏好 / 费用</h2>${modelRows}</div><div class="panel"><h2>项目分布 / 费用</h2>${projectRows}</div><div class="panel"><h2>活跃时间 / 周 × 小时</h2><div class="heat-scroll"><div class="heatmap">${heat}</div></div><div class="small" style="margin-top:12px">按本地时区统计唯一 API 请求；每格为一个星期几和小时</div></div><div class="panel wide"><div class="panel-head"><div><h2>会话消耗</h2><div class="small" style="margin-top:7px">Token 合计含 Advisor · 原生费用优先 · 无可用原生金额时标注 API 估算</div></div><div class="session-filter"><label for="sessionStart">日期</label><input id="sessionStart" type="date" value="${sessionStartDate}"><span>至</span><input id="sessionEnd" type="date" value="${sessionEndDate}"><button id="applySessionRange">应用</button><span class="filter-meta" id="sessionFilterMeta">正在筛选…</span></div></div><div class="tablewrap"><table><thead><tr><th>对话</th><th>项目</th><th>模型</th><th class="right">Token</th><th class="right">费用</th><th>Advisor 介入</th><th>最后活动</th></tr></thead><tbody id="sessionBody">${sessionRows||'<tr><td colspan="7">暂无数据</td></tr>'}</tbody></table></div></div><div class="panel wide"><h2>口径说明</h2><div class="small">${d.notes.map(x=>`<div>• ${esc(x)}</div>`).join('')}</div></div></section>`;
 document.getElementById('applyRange')?.addEventListener('click',updateCustomRange);
 document.getElementById('rangeStart')?.addEventListener('change',updateCustomRange);
 document.getElementById('rangeEnd')?.addEventListener('change',updateCustomRange);
 document.getElementById('applySessionRange')?.addEventListener('click',updateSessionRange);
 document.getElementById('sessionStart')?.addEventListener('change',updateSessionRange);
 document.getElementById('sessionEnd')?.addEventListener('change',updateSessionRange);
 updateCustomRange();
 updateSessionRange();
 bindTipPositions();
}
let activeTip=null;
function positionTip(el){
 const pop=el.querySelector(':scope > .tip-pop');
 if(!pop)return;
 const r=el.getBoundingClientRect();
 pop.style.left='0px';
 pop.style.top='0px';
 const box=pop.getBoundingClientRect();
 const gap=10;
 const vw=window.innerWidth,vh=window.innerHeight;
 const clampTop=value=>Math.min(Math.max(8,value),Math.max(8,vh-box.height-8));
 const wrap=el.closest('.tablewrap');
 const wr=wrap?.getBoundingClientRect();
 let left,top;
 if(wr && wr.right+gap+box.width<=vw-8){
  left=wr.right+gap;
  top=clampTop(r.top);
 }else if(wr && wr.left-gap-box.width>=8){
  left=wr.left-gap-box.width;
  top=clampTop(r.top);
 }else if(r.right+gap+box.width<=vw-8){
  left=r.right+gap;
  top=clampTop(r.top);
 }else if(r.left-gap-box.width>=8){
  left=r.left-gap-box.width;
  top=clampTop(r.top);
 }else{
  const maxLeft=Math.max(8,vw-box.width-8);
  left=Math.min(Math.max(8,r.left),maxLeft);
  top=r.top-box.height-gap;
  if(top<8)top=r.bottom+gap;
  top=clampTop(top);
 }
 pop.style.left=`${Math.round(left)}px`;
 pop.style.top=`${Math.round(top)}px`;
}
function bindTipPositions(){
 document.querySelectorAll('.tip').forEach(el=>{
  if(el.dataset.tipBound)return;
  el.dataset.tipBound='1';
  el.addEventListener('mouseenter',()=>{activeTip=el;positionTip(el)});
  el.addEventListener('focusin',()=>{activeTip=el;positionTip(el)});
  el.addEventListener('mouseleave',()=>{if(activeTip===el)activeTip=null});
  el.addEventListener('focusout',()=>{if(activeTip===el)activeTip=null});
 });
}
window.addEventListener('resize',()=>{if(activeTip)positionTip(activeTip)});
window.addEventListener('scroll',()=>{if(activeTip)positionTip(activeTip)},true);
async function loadData(){try{const r=await fetch('/api/summary?ts='+Date.now());if(!r.ok)throw new Error(await r.text());render(await r.json())}catch(e){document.getElementById('app').innerHTML=`<div class="error">读取失败：${esc(e.message)}<br><span class="small">请确认 Claude 数据目录存在，并从项目根目录启动 app.py。</span></div>`}}
loadData();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    index: UsageIndex | None = None
    page = PAGE

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/summary":
            assert self.index is not None
            payload = json.dumps(self.index.snapshot(), ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path in ("/", "/index.html"):
            payload = self.page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Cost Lens: read-only local Claude usage dashboard", epilog="Run claude-cost-lens without arguments for the macOS menu bar or Windows tray; use --web for only the Web dashboard.")
    parser.add_argument("--data-root", default=os.path.expanduser("~/.claude"), help="Claude data directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser")
    parser.add_argument("--once", action="store_true", help="Print the parsed summary as JSON and exit")
    args = parser.parse_args()
    index = UsageIndex(Path(args.data_root))
    if args.once:
        print(json.dumps(index.summary(), ensure_ascii=False, indent=2, default=str))
        return
    Handler.index = index
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{server.server_port}/"
    print(f"Claude Cost Lens: {url}", flush=True)
    print(f"Data root: {index.root}")
    print(f"Scanned {index.scanned_files} files / {index.scanned_records} records")
    if args.open:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
