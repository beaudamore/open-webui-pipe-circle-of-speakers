"""
title: Circle of Speakers
author: beaudamore
author_url: https://github.com/beaudamore/circle-of-speakers-pipeline
version: 4.0
license: MIT
description: Multi-speaker conversations from tagged Open WebUI models. One Pipe Function = one circle, its cast chosen by the SPEAKER_TAGS valve; install it again under another id for another circle. Replaces the Pipelines-server blueprints (streaming blueprint + circle blueprint + per-circle shim) with a single file. Runs in-process: no HTTP loopback, no API key.
"""

# Port history (2026-11-08): the Pipelines server (github.com/open-webui/pipelines) is archived, so the
# three-file layout (openwebui_streaming_blueprint.py -> circle_of_speakers_blueprint.py -> <circle>.py)
# is merged here into one Open WebUI Pipe Function. Differences from the Pipelines version:
#   - class is `Pipe`, `pipe()` is an async generator taking Open WebUI's body/metadata parameters;
#   - one Function = one circle, as one shim file was one circle. The cast is the SPEAKER_TAGS valve
#     (empty = every speaker, the old Dynamic Circle). Another circle = this same file installed again
#     under its own Function id and title with its own valves, which is what the shim files did;
#   - no HTTP at all. The speaker registry is read from Open WebUI's model store (Models) and the
#     orderer/speaker calls go through Open WebUI's own generate_chat_completion, as the chatting user,
#     on a fresh Request over the caller's scope (so nested calls do not share request.state). That is
#     what the old /api/v1/chat/completions loopback did for us, minus the route's chat bookkeeping.
#     Hence no OPENWEBUI_ENDPOINT / OPENWEBUI_API_KEY / USER_AGENT valves. Note: the route overwrote any
#     client-supplied `metadata`, so the old `metadata.tool_ids` never reached the speakers either;
#     tool ids are still looked up for the "using tools" line but are not forwarded;
#   - no on_startup / on_valves_updated hooks exist for Functions: the speaker cache is filled on first
#     use and refilled whenever the valves change (that is what on_valves_updated did);
#   - message content given as a parts list (OpenAI content parts) is read as its text, both for the
#     user message and in the orderer's recent-context summary (the originals assumed plain strings).
# Everything else (prompt layout, history segregation, orchestration contract, avatars, markers) is the
# blueprint code unchanged.

from typing import List, Dict, Optional, AsyncGenerator, Tuple
import json
import re
import gc
from urllib.parse import quote
from datetime import datetime

from fastapi import Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Open WebUI internals: the Function runs inside the Open WebUI process.
from open_webui.models.models import Models
from open_webui.models.users import Users
from open_webui.utils.chat import generate_chat_completion


class Pipe:
    """Circle of Speakers - multi-speaker orchestration with streaming, conversation awareness and
    per-speaker prompt-cache-friendly request layout. One Function = one circle (SPEAKER_TAGS)."""

    class Valves(BaseModel):
        """Configuration valves (merged from the streaming blueprint and the circle blueprint)."""

        # ======================================================================
        # HISTORY
        # ======================================================================
        HISTORY_MAX_ROUNDS: int = Field(
            default=0,
            description="Maximum number of conversation rounds (user+assistant pairs) to keep in history. "
                        "Older rounds are dropped to stay within model context limits (e.g., --max-model-len 8192). "
                        "System messages are always preserved. 0 = no truncation (keep all history). Keep 0 or large "
                        "for prompt caching: a sliding window shifts the prefix every round.",
        )

        # ======================================================================
        # DEBUG
        # ======================================================================
        enable_full_debug: bool = Field(
            default=False,
            description="Enable heavy debugging logs, including payloads/results (masked & truncated).",
        )
        enable_step_debug: bool = Field(
            default=False,
            description="Enable step-by-step progress logs (concise, truncated).",
        )

        # ======================================================================
        # SPEAKER SELECTION - the cast of this circle
        # ======================================================================
        SPEAKER_TAGS: Optional[str] = Field(
            default="",
            description="Comma-separated list of tags to select which speakers to load. "
                        "Use ',' for OR (e.g., stoic,biblical) and '+' for AND (e.g., stoic+greek). "
                        "Leave empty to select from all speakers. Tags match case-insensitively.",
        )

        # ======================================================================
        # ORCHESTRATION - AI-driven speaker selection and ordering
        # ======================================================================
        ORCHESTRATION_MODEL_NAME: str = Field(
            default="Intelligent Speaker Ordering",
            description="Model name to use for intelligent speaker orchestration. "
                        "The model's OpenWebUI system prompt defines the selection rules; "
                        "the pipeline sends the available cast, conversation context, and user message.",
        )
        DECISION_CONTEXT_DEPTH: int = Field(
            default=4,
            description="Number of previous rounds of messages to include for ordering decisions "
                        "(0=no context, 2=last round, 4-6=recent history, 8-10=extended context for many speakers). "
                        "Higher values use more tokens.",
        )

        # ======================================================================
        # DYNAMIC INTRODUCTIONS
        # ======================================================================
        ENABLE_DYNAMIC_INTRODUCTIONS: bool = Field(
            default=True,
            description="Enable automatic speaker introductions when they first appear in a conversation",
        )

        # ======================================================================
        # USER PROFILE INTEGRATION
        # ======================================================================
        ENABLE_USER_PROFILE_INTEGRATION: bool = Field(
            default=True,
            description="Extract and use personal information from user's system prompt to personalize speaker responses",
        )

        # ======================================================================
        # PROFILE IMAGE CONFIGURATION
        # ======================================================================
        PROFILE_THUMBNAIL_ROOT: str = Field(
            default="/api/v1/models/model/profile/image?id={model_id}&width={size}",
            description="URL template for speaker avatars. Placeholders: {model_id} (URL-encoded Open WebUI model id) "
                        "and {size} (PROFILE_IMAGE_SIZE). The default uses Open WebUI's own model-image endpoint; "
                        "the width parameter needs the feature from open-webui discussion #32756, stock Open WebUI "
                        "ignores it and serves the stored size. A value without placeholders is treated as a prefix: "
                        "{root}{model_id}?width={size}. Leave empty to show names only.",
        )
        PROFILE_IMAGE_SIZE: int = Field(
            default=60,
            description="Requested avatar width in pixels (fills {size} in PROFILE_THUMBNAIL_ROOT).",
        )

        # ======================================================================
        # PROMPT PREFIXES - Customize speaker behavior
        # ======================================================================
        FIRST_SPEAKER_PROMPT_PREFIX: str = Field(
            default=(
                "📢 **YOU ARE THE FIRST SPEAKER:** You're opening this round of discussion, "
                "so focus on giving the user a helpful, engaging response that reflects your "
                "personality, background, and natural way of speaking as {speaker_name}."
            ),
            description="Terse cue for the first speaker of a round. Goes at the top of the per-round "
                        "context block in the final user turn (never in the system prompt).",
            json_schema_extra={"widget": "textarea", "rows": 3},
        )
        INTRODUCTION_PROMPT_PREFIX: str = Field(
            default=(
                "🎭 **FIRST TIME IN THIS CONVERSATION:** This is your first time participating "
                "in this specific conversation. Before responding to the user's question, briefly "
                "introduce yourself by name and mention your area of expertise or perspective. "
                "Keep it concise (1-2 sentences), then proceed with your response."
            ),
            description="Terse cue when a speaker appears for the first time in a conversation. Goes at the "
                        "top of the per-round context block in the final user turn (never in the system prompt).",
            json_schema_extra={"widget": "textarea", "rows": 3},
        )

    # =====================================================================
    # INITIALIZATION
    # =====================================================================

    def __init__(self):
        self.valves = self.Valves()
        self.available_models: List[dict] = []
        self.all_speakers: List[dict] = []                    # In-memory list of all possible speakers
        self.conversation_participants: Dict[str, set] = {}   # Tracks all unique speakers per conversation
        self.introduced_speakers: Dict[str, set] = {}         # Tracks which speakers have introduced themselves per conversation
        self._speakers_valves_snapshot: Optional[dict] = None  # Valves the speaker cache was built with
        self._update_tags_description()

    # =====================================================================
    # DEBUG HELPERS
    # =====================================================================

    def _is_full_debug(self) -> bool:
        v = getattr(self, "valves", None)
        return bool(getattr(v, "enable_full_debug", False) or getattr(v, "ENABLE_FULL_DEBUG", False))

    def _is_step_debug(self) -> bool:
        v = getattr(self, "valves", None)
        return bool(
            getattr(v, "enable_step_debug", False)
            or getattr(v, "ENABLE_STEP_DEBUG", False)
            or self._is_full_debug()
        )

    def _truncate(self, text, n: int = 200):
        try:
            s = str(text)
        except Exception:
            return text
        if len(s) <= n:
            return s
        return s[: n - 1] + "…"

    def _mask(self, value):
        _re = re

        def mask_str(s: str) -> str:
            out = s
            out = _re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", out)
            out = _re.sub(r"\+?\d[\d\-\.\s()]{6,}\d", "[REDACTED_PHONE]", out)
            out = _re.sub(r"([A-Za-z0-9-_]{20,})", "[REDACTED_TOKEN]", out)
            return out

        if isinstance(value, str):
            return mask_str(value)
        if isinstance(value, dict):
            red = {}
            for k, v in value.items():
                kl = str(k).lower()
                if kl in {"authorization", "api_key", "apikey", "token", "secret", "password"}:
                    red[k] = "[REDACTED]"
                else:
                    red[k] = self._mask(v)
            return red
        if isinstance(value, list):
            return [self._mask(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._mask(v) for v in value)
        return value

    def _print_safely(self, *parts):
        try:
            print(*parts)
        except Exception:
            pass

    def _dbg_step(self, *parts):
        if self._is_step_debug():
            self._print_safely(*parts)

    def _dbg_full(self, *parts):
        if self._is_full_debug():
            self._print_safely(*parts)

    # =====================================================================
    # OPEN WEBUI INTERNALS (model store + in-process chat completion)
    # =====================================================================

    @staticmethod
    def _internal_request(request: Request) -> Request:
        """A Request sharing the caller's scope (app, headers, client) but with its own state dict.
        generate_chat_completion writes bypass flags and metadata into request.state, and Starlette backs
        request.state with scope["state"], so nested calls must not reuse the outer request as is."""
        if request is None:
            raise ValueError("No request object available; the pipe needs Open WebUI's __request__.")
        outer = request.scope.get("state") or {}
        # Keep the auth state Open WebUI set for the HTTP request (connections with session auth read
        # request.state.token; forwarded user-info headers read auth_type), drop the chat-scoped rest.
        keep = {k: outer[k] for k in ("token", "user", "auth_type", "claims", "enable_api_keys", "internal") if k in outer}
        return Request({**request.scope, "state": keep})

    @staticmethod
    async def _resolve_user(user: Optional[dict]):
        user_id = (user or {}).get("id")
        if not user_id:
            raise ValueError("No user on the request; the circle calls Open WebUI as the chatting user.")
        resolved = await Users.get_user_by_id(user_id)
        if resolved is None:
            raise ValueError(f"User '{user_id}' not found.")
        return resolved

    async def _fetch_available_models(self) -> list:
        """Open WebUI's model registry (every workspace model row, the admin view the old
        /api/v1/models/list call gave) straight from the model store - returns a list of dicts shaped
        like the API items: id, name, meta, is_active, base_model_id."""
        self._dbg_step("--- Model Fetch Debug ---")
        try:
            rows = await Models.get_all_models()
        except Exception as e:
            self._dbg_step(f"  Result: CRITICAL - Reading the Open WebUI model store failed. Error: {e}")
            self.available_models = []
            return []

        seen = set()
        deduped = []
        for row in rows:
            mid = getattr(row, "id", None)
            # Rows without a base model are per-base-model admin settings, which the old list endpoint
            # filtered out (base_model_id != None); keep the registry to workspace models as before.
            if not mid or mid in seen or getattr(row, "base_model_id", None) is None:
                continue
            seen.add(mid)
            meta = getattr(row, "meta", None)
            deduped.append(
                {
                    "id": mid,
                    "name": getattr(row, "name", "") or "",
                    "meta": meta.model_dump() if hasattr(meta, "model_dump") else (meta or {}),
                    "is_active": getattr(row, "is_active", True),
                    "base_model_id": getattr(row, "base_model_id", None),
                }
            )

        self.available_models = deduped
        self._dbg_step(f"  Model store read. Models loaded: {len(self.available_models)}")
        return self.available_models

    def _sse_data_events(self, data_str: str, st: dict) -> Tuple[List[str], bool]:
        """Turn one SSE `data:` payload into the strings pipe() consumes (content and the marker protocol:
        __USAGE_DATA__:<json>, __REASONING_CHUNK__:<text>, __THINKING_TAG_CONTENT__:<text>,
        __THINKING_TAG_CLOSE__). Returns (events, done). `st` carries the per-stream state."""
        out: List[str] = []
        if data_str.strip() == "[DONE]":
            self._dbg_step(f"  Stream finished: [DONE] received. SSE lines={st['sse_lines_seen']} yielded_chars={st['content_chars_yielded']}")
            if st["captured_usage"]:
                out.append(f"__USAGE_DATA__:{json.dumps(st['captured_usage'])}")
            return out, True
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            self._dbg_step("    ⚠️ JSON Decode Error on data:", data_str)
            return [data_str], False

        if isinstance(data, dict) and "usage" in data:
            st["captured_usage"] = data["usage"]
            self._dbg_step(f"  📊 Usage captured: {st['captured_usage']}")

        if isinstance(data, dict) and "error" in data and "choices" not in data:
            err = data["error"]
            if isinstance(err, dict):
                msg = err.get("detail") or err.get("message") or json.dumps(err)
            else:
                msg = str(err)
            out.append(f"❌ Model error: {msg}")
            return out, False

        if isinstance(data, dict) and "choices" in data and isinstance(data["choices"], list) and data["choices"]:
            delta = data["choices"][0].get("delta") or {}

            reasoning = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
            if isinstance(reasoning, str) and reasoning:
                st["reasoning_active"] = True
                out.append(f"__REASONING_CHUNK__:{reasoning}")
                return out, False

            content = delta.get("content") if isinstance(delta, dict) else None
            if not isinstance(content, str) or not content:
                # Fallback: delta might be a string directly
                if isinstance(delta, str) and delta:
                    content = delta
                else:
                    content = None

            if content:
                if st["reasoning_active"]:
                    st["reasoning_active"] = False

                if st["in_thinking_tags"]:
                    # We're inside <think>...</think> - check for closing tag
                    close_match = re.search(r'</think>|</thinking>', content)
                    if close_match:
                        thinking_part = content[:close_match.start()]
                        regular_part = content[close_match.end():]
                        if thinking_part:
                            out.append(f"__THINKING_TAG_CONTENT__:{thinking_part}")
                        out.append("__THINKING_TAG_CLOSE__")
                        st["in_thinking_tags"] = False
                        if regular_part:
                            out.append(regular_part)
                    else:
                        out.append(f"__THINKING_TAG_CONTENT__:{content}")
                elif "<think>" in content or "<thinking>" in content:
                    # Opening tag found - enter thinking state
                    open_match = re.search(r'<think>|<thinking>', content)
                    if open_match:
                        before = content[:open_match.start()]
                        after = content[open_match.end():]
                    else:
                        before = ""
                        after = content
                    if before:
                        out.append(before)
                    st["in_thinking_tags"] = True
                    # Check if closing tag is also in this chunk (complete block)
                    close_match = re.search(r'</think>|</thinking>', after)
                    if close_match:
                        thinking_part = after[:close_match.start()]
                        regular_part = after[close_match.end():]
                        if thinking_part:
                            out.append(f"__THINKING_TAG_CONTENT__:{thinking_part}")
                        out.append("__THINKING_TAG_CLOSE__")
                        st["in_thinking_tags"] = False
                        if regular_part:
                            out.append(regular_part)
                    else:
                        if after:
                            out.append(f"__THINKING_TAG_CONTENT__:{after}")
                else:
                    st["content_chars_yielded"] += len(content)
                    out.append(content)

            if isinstance(delta, dict):
                images = delta.get("images") or delta.get("image")
                if isinstance(images, list):
                    for img in images:
                        img_url = None
                        if isinstance(img, dict):
                            img_url = img.get("image_url") or img.get("url")
                        if isinstance(img, str):
                            img_url = img
                        if img_url:
                            out.append(f"\n\n![image]({img_url})\n\n")
        return out, False

    async def _query_openwebui_model_streaming(
        self, model_name: str, messages: List[dict], tool_ids: Optional[List[str]] = None,
        include_usage: bool = False, request: Optional[Request] = None, user=None,
    ) -> AsyncGenerator[str, None]:
        """Query an Open WebUI model in-process (generate_chat_completion) with a streaming response.
        Automatically detects thinking/reasoning in output. Yields content plus the marker protocol."""

        self._dbg_step(f"--- DEBUG: Streaming Query to {model_name} ---")

        try:
            payload = {
                "model": model_name,
                "messages": messages,
                "stream": True,
                "metadata": {},
            }
            if include_usage:
                payload["stream_options"] = {"include_usage": True}
                self._dbg_step("  📊 Usage tracking enabled")
            if tool_ids:
                # Not forwarded: see the port-history note at the top of the file.
                self._dbg_step(f"  ℹ️ Speaker has tool ids {tool_ids}; not forwarded (as before)")

            self._dbg_step("  payload: " + ", ".join(f"{m.get('role')}:{len(str(m.get('content', '')))}ch" for m in messages))
            self._dbg_step(f"  📋 Payload contains {len(messages)} messages")

            response = await generate_chat_completion(self._internal_request(request), payload, user)

            st = {
                "reasoning_active": False,
                "in_thinking_tags": False,  # Track XML <think>/<thinking> tag state across chunks
                "captured_usage": None,
                "sse_lines_seen": 0,
                "content_chars_yielded": 0,
            }

            if isinstance(response, StreamingResponse):
                self._dbg_step(f"  Response: streaming, status {response.status_code}")
                buf = b""
                done = False
                async for chunk in response.body_iterator:
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        line_str = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                        if not line_str:
                            continue
                        st["sse_lines_seen"] += 1
                        self._dbg_full(f"  SSE line #{st['sse_lines_seen']}: {self._truncate(line_str, 400)}")
                        if not line_str.startswith("data:"):
                            continue
                        events, done = self._sse_data_events(line_str[5:].lstrip(), st)
                        for ev in events:
                            yield ev
                        if done:
                            break
                    if done:
                        break
                try:
                    await response.body_iterator.aclose()
                except Exception:
                    pass
                if not done:
                    tail = buf.decode("utf-8", errors="replace").strip()
                    if tail.startswith("data:"):
                        events, _ = self._sse_data_events(tail[5:].lstrip(), st)
                        for ev in events:
                            yield ev
                    if st["captured_usage"]:
                        yield f"__USAGE_DATA__:{json.dumps(st['captured_usage'])}"
            elif isinstance(response, dict):
                # Non-streaming answer (a model with stream_response off)
                choices = response.get("choices") or []
                message = (choices[0].get("message") or {}) if choices else {}
                content = message.get("content") or ""
                reasoning = message.get("reasoning_content") or message.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    yield f"__REASONING_CHUNK__:{reasoning}"
                    yield "__THINKING_TAG_CLOSE__"
                if content:
                    yield content
                if response.get("usage"):
                    yield f"__USAGE_DATA__:{json.dumps(response['usage'])}"
            else:
                # An error response object (JSONResponse) from the provider router
                body = getattr(response, "body", b"")
                text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
                status = getattr(response, "status_code", "?")
                self._dbg_step(f"  ERROR: Model '{model_name}' returned status {status}.")
                self._dbg_full("  Error content:", text)
                yield f"❌ Model {model_name} error: {status} - {text}"

        except Exception as e:
            self._dbg_step(
                f"  CRITICAL ERROR: Open WebUI model '{model_name}' failed. Exception: {str(e)}"
            )
            yield f"❌ Error: Could not run OpenWebUI model {model_name} - {str(e)}"
        self._dbg_step(f"--- END DEBUG: Streaming Query for {model_name} ---")

    def _resolve_model_id(self, model_display_name: str) -> str:
        """Resolve display name to actual model ID"""
        self._dbg_step("🔧 **MODEL RESOLUTION DEBUG**:")
        self._dbg_step(f"   Original: '{model_display_name}'")
        self._dbg_step(f"   Available models count: {len(self.available_models)}")

        if not model_display_name:
            self._dbg_step("   ❌ Empty model name provided")
            return ""

        self._dbg_step(f"🔍 **SEARCHING FOR**: '{model_display_name}'")
        for model in self.available_models:
            model_name = model.get("name", "")
            if model_name.lower() == model_display_name.lower():
                model_id = model.get("id", "")
                self._dbg_step(f"   ✅ **FOUND BY NAME**: '{model_name}' -> ID: '{model_id}'")
                return model_id

        for model in self.available_models:
            model_id = model.get("id", "")
            if model_id.lower() == model_display_name.lower():
                self._dbg_step(f"   ✅ **FOUND BY ID**: '{model_id}'")
                return model_id

        self._dbg_step(
            f"   ❌ **NOT FOUND** in {len(self.available_models)} available models - returning original"
        )
        return model_display_name

    def _get_model_tool_ids(self, model_id: str) -> List[str]:
        """Get the tool IDs associated with a model from the available_models data"""
        self._dbg_step(f"🔧 **TOOL LOOKUP**: Searching for tools for model '{model_id}'")
        self._dbg_step(f"   📊 Total models in available_models: {len(self.available_models)}")

        for model in self.available_models:
            model_id_field = model.get("id")
            model_name_field = model.get("name")
            self._dbg_step(f"   🔍 Checking model: id='{model_id_field}', name='{model_name_field}'")

            if model_id_field == model_id or model_name_field == model_id:
                self._dbg_step("   ✅ MATCH! Examining model structure…")
                self._dbg_step(f"   🔑 Model keys: {list(model.keys())}")

                if "meta" in model and isinstance(model["meta"], dict):
                    meta = model["meta"]
                    tool_ids = meta.get("toolIds", [])

                    if tool_ids:
                        self._dbg_step(f"   ✅ Found {len(tool_ids)} tool(s) in meta.toolIds: {tool_ids}")
                        return tool_ids
                    else:
                        self._dbg_step("   ℹ️ Model has no tools configured (meta.toolIds is empty)")
                        return []
                else:
                    raise ValueError(f"Model '{model_id}' has invalid structure - missing 'meta' object")

        raise ValueError(f"Model '{model_id}' not found in available models. Available model IDs: {[m.get('id') for m in self.available_models]}")

    def _generate_tool_citations(self, tool_ids: List[str], speaker_name: str, response_content: str = ""):
        """Generate citation events for tools configured for a speaker."""
        for tool_id in tool_ids:
            snippet = response_content[:300] if response_content else f"Tool used by {speaker_name}"

            citation_event = {
                "event": {
                    "type": "citation",
                    "data": {
                        "document": [snippet],
                        "metadata": [{"source": tool_id}],
                        "source": {"name": f"Tool: {tool_id.replace('_', ' ').title()}"}
                    }
                }
            }
            yield citation_event

    def _truncate_history(self, messages: List[dict]) -> List[dict]:
        """Truncate conversation history to the last N rounds (user+assistant pairs).

        Keeps all system messages and the most recent HISTORY_MAX_ROUNDS rounds.
        A 'round' is one user message followed by one assistant message.
        Returns the original list unchanged if HISTORY_MAX_ROUNDS is 0 (disabled).
        """
        max_rounds = getattr(self.valves, "HISTORY_MAX_ROUNDS", 0)
        if max_rounds <= 0 or not messages:
            return messages

        # Separate system messages from conversation messages
        system_msgs = [m for m in messages if m.get("role") == "system"]
        conv_msgs = [m for m in messages if m.get("role") != "system"]

        if not conv_msgs:
            return messages

        # Count rounds by walking backward through user messages
        rounds_seen = 0
        cut_index = len(conv_msgs)
        i = len(conv_msgs) - 1
        while i >= 0:
            if conv_msgs[i].get("role") == "user":
                rounds_seen += 1
                if rounds_seen > max_rounds:
                    cut_index = i + 1  # Keep from the next message onward
                    break
            i -= 1

        truncated_conv = conv_msgs[cut_index:] if rounds_seen > max_rounds else conv_msgs
        total_original = len(conv_msgs)
        total_kept = len(truncated_conv)

        if total_kept < total_original:
            dropped = total_original - total_kept
            self._dbg_step(
                f"✂️ History truncated: kept {total_kept}/{total_original} messages "
                f"({dropped} dropped, max_rounds={max_rounds})"
            )
        else:
            self._dbg_step(f"✂️ History within limits: {total_original} messages, max_rounds={max_rounds}")

        return system_msgs + truncated_conv

    def _extract_pipeline_system_prompt(self, body: dict) -> str:
        """Return the pipeline's own system prompt from body['messages'] (Open WebUI applies the circle
        model's system prompt into the messages before calling the pipe)."""
        body_messages = body.get("messages", [])

        if not body_messages:
            self._dbg_step("   ❌ No messages found in body dict")
            return ""

        for msg in body_messages:
            if msg.get("role") == "system":
                system_prompt = msg.get("content", "")
                if system_prompt:
                    self._dbg_step(f"   ✅ Found pipeline system prompt in body['messages'] ({len(system_prompt)} chars)")
                    return system_prompt
                break

        self._dbg_step("   ❌ No system message found in body['messages']")
        return ""

    @staticmethod
    def _message_text(message: Optional[dict]) -> str:
        """Text of a message whose content may be a string or an OpenAI content-parts list."""
        if not message:
            return ""
        content = message.get("content")
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
            )
        return content if isinstance(content, str) else ""

    def _last_user_message(self, messages: List[dict]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return self._message_text(message)
        return ""

    # =====================================================================
    # MAIN PIPE
    # =====================================================================

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __request__=None,
        __event_emitter__=None,
        __metadata__: Optional[dict] = None,
    ):
        """Main pipe with streaming and real-time conversation awareness (async generator)."""

        tag_query = str(self.valves.SPEAKER_TAGS or "").strip()

        try:
            self._internal_request(__request__)
            user = await self._resolve_user(__user__)
        except ValueError as e:
            yield f"❌ PIPELINE STOPPED: {e}"
            return

        messages: List[dict] = list(body.get("messages", []) or [])
        user_message = self._last_user_message(messages)

        # Truncate history early to stay within model context limits
        messages = self._truncate_history(messages)

        # Check if usage tracking is requested (from Open WebUI checkbox)
        include_usage = (body.get("stream_options") or {}).get("include_usage", False)
        self._dbg_step(f"📊 Usage tracking: {'enabled' if include_usage else 'disabled'}")

        # Speaker cache: filled on first use, refilled when the valves change (Functions have no
        # on_startup / on_valves_updated hooks).
        try:
            await self._ensure_speakers()
        except ValueError as e:
            yield f"❌ PIPELINE STOPPED: {e}"
            return

        # Extract web search context if present (inject into shared awareness)
        web_search_context = self._extract_web_search_context(messages)
        if web_search_context:
            yield "🔍 **Web search data detected - enhancing speaker responses**\n\n"

        # Extract memory context if present (inject into shared awareness)
        memory_context = self._extract_memory_context(messages)

        awareness_prompt_template = self._extract_pipeline_system_prompt(body)
        if not awareness_prompt_template:
            yield (
                "❌ PIPELINE STOPPED: The pipeline's System Prompt is empty. "
                "Please provide the awareness instructions in this circle model's system prompt field."
            )
            self._dbg_step("❌ Missing pipeline system prompt — aborting run.")
            return

        # Web search and memory context are per-turn: they go into the final user turn of each speaker
        # request (see _build_round_context), never into the system prompt, so the system prompt stays a
        # stable prompt-cache prefix for the engine.

        # --- 1. UNIFIED ORCHESTRATION ---
        if not self.all_speakers:
            yield "❌ PIPELINE STOPPED: Cannot fetch models from OpenWebUI and speaker list is empty."
            return

        try:
            initial_speakers = self._get_active_speakers(tag_query)
        except ValueError as e:
            yield f"❌ PIPELINE STOPPED: {e}"
            return
        if not initial_speakers:
            yield "❌ No speakers configured."
            return

        # Show status BEFORE the slow orchestrator AI call so the user sees progress
        yield {
            "event": {
                "type": "status",
                "data": {
                    "description": f"Found {len(initial_speakers)} speakers. Selecting order...",
                    "done": True,
                },
            }
        }

        # Get recent conversation context for better ordering decisions
        conv_key = self._get_conversation_key(body, __metadata__)
        recent_context = self._get_recent_conversation_context(messages)

        # Perform AI-driven selection and ordering
        try:
            active_speakers = await self._analyze_user_message_for_speaker_order(
                user_message, initial_speakers, recent_context, tag_query, memory_context,
                request=__request__, user=user,
            )
        except ValueError as e:
            yield f"❌ PIPELINE STOPPED: {e}"
            return

        # Visual feedback showing selected speakers
        speaker_count = len(active_speakers)
        speaker_list = ", ".join([s["name"] for s in active_speakers])
        yield f"💬 **{speaker_count} speaker{'s' if speaker_count != 1 else ''} selected:** {speaker_list}\n\n"

        # --- 2. MAIN RESPONSE LOOP ---
        # Initialize introduced speakers set for this conversation if needed
        if conv_key not in self.introduced_speakers:
            self.introduced_speakers[conv_key] = set()

        speaker_responses = []
        current_round_speakers_list = active_speakers

        # Track aggregated usage across all speakers (only if usage tracking enabled)
        total_usage = None
        if include_usage:
            total_usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }

        # Historical Awareness Logic
        if conv_key not in self.conversation_participants:
            self.conversation_participants[conv_key] = set()
        current_round_speaker_names = {s["name"] for s in current_round_speakers_list}
        self.conversation_participants[conv_key].update(current_round_speaker_names)

        # Historical speakers are anyone who has ever participated, minus the current speakers
        historical_speaker_names = (
            self.conversation_participants[conv_key] - current_round_speaker_names
        )

        def get_speaker_details(speaker_name):
            for speaker in self.all_speakers:
                if speaker["name"] == speaker_name:
                    description = speaker.get("description", "No description available.")
                    tags_set = speaker.get("tags", set())
                    tags = ", ".join(sorted(list(tags_set))) if tags_set else ""
                    return f"- {speaker_name} (Description: {description}, Tags: {tags})"
            return f"- {speaker_name}"

        current_speakers_str = (
            "\n".join(
                [get_speaker_details(name) for name in sorted(list(current_round_speaker_names))]
            )
            if current_round_speaker_names
            else "None"
        )
        historical_speakers_str = (
            "\n".join(
                [get_speaker_details(name) for name in sorted(list(historical_speaker_names))]
            )
            if historical_speaker_names
            else "None"
        )

        current_round_responded_speakers = []
        user_profile = (
            self._extract_user_profile(messages)
            if self.valves.ENABLE_USER_PROFILE_INTEGRATION
            else {"personal_info": "", "system_instructions": ""}
        )

        for i, speaker in enumerate(active_speakers):
            try:
                # Check if this speaker has already introduced themselves in this conversation
                speaker_name = speaker["name"]
                needs_introduction = (
                    self.valves.ENABLE_DYNAMIC_INTRODUCTIONS
                    and speaker_name not in self.introduced_speakers[conv_key]
                )

                # Prompt layout (2026-11-02): the system prompt is static per speaker (only {speaker_name}
                # is filled), the conversation history follows, and everything that changes per round
                # (date, the other speakers, previous takes, web research, memory, profile, introduction
                # cue, the user's message) goes into the final user turn. The engine's prompt cache keys on
                # the prefix, so system + history is reused across rounds and only the last turn is new.
                static_system = self._format_static_system_prompt(awareness_prompt_template, speaker_name)
                previous_takes = [
                    f"• {current_round_responded_speakers[j]['name']} said: {res[:200]}{'...' if len(res) > 200 else ''}"
                    for j, res in enumerate(speaker_responses)
                ]
                round_context = self._build_round_context(
                    speaker_name=speaker_name,
                    is_first=(i == 0),
                    needs_introduction=needs_introduction,
                    previous_takes=previous_takes,
                    current_speakers_str=current_speakers_str,
                    historical_speakers_str=historical_speakers_str,
                    web_search_context=web_search_context,
                    memory_context=memory_context,
                    user_context=self._build_user_context_section(user_profile),
                )

                # Mark this speaker as introduced
                if needs_introduction:
                    self.introduced_speakers[conv_key].add(speaker_name)

                # The question goes in as its own plain user turn, exactly as Open WebUI will store it in the
                # history, and the round context follows as a second user turn. That keeps "system + first user
                # turn" identical from round one onward, which is what a slot-cache proxy keys a conversation by,
                # and keeps the stored history a true prefix of this request.
                speaker_messages = [{"role": "system", "content": static_system}]
                speaker_messages.extend(self._history_for_speaker(messages, speaker_name))
                speaker_messages.append({"role": "user", "content": user_message})
                speaker_messages.append({"role": "user", "content": f"{round_context}\n\nAnswer the user's message in the previous turn now, as {speaker_name}."})

                # Get tool IDs for this speaker's model
                speaker_tool_ids = self._get_model_tool_ids(speaker["model"])

                # Inline feedback if tools are configured
                if speaker_tool_ids:
                    tool_names = [tool_id.replace('_', ' ').title() for tool_id in speaker_tool_ids]
                    tools_list = ", ".join(tool_names)
                    yield f"🔧 **{speaker['name']} using tools:** {tools_list}\n\n"

                # UI events and streaming response
                yield {
                    "event": {
                        "type": "tool",
                        "data": {
                            "tool": f"{speaker['name']} Generator",
                            "status": "running",
                            "description": f"{speaker['name']} is thinking...",
                        },
                    }
                }

                # Generate speaker image markdown
                yield self.get_speaker_image_markdown(speaker)

                # Stream and automatically detect thinking (from API fields or XML tags)
                reasoning_active = False
                speaker_usage = None

                full_response = ""
                async for chunk in self._query_openwebui_model_streaming(
                    speaker["model"], speaker_messages, tool_ids=speaker_tool_ids, include_usage=include_usage,
                    request=__request__, user=user,
                ):
                    if chunk and chunk.startswith("❌"):
                        # Error from the in-process call (model not found, 403 for this user, provider
                        # error): show it instead of leaving the turn blank. Not part of the take.
                        yield f"{chunk}\n\n"
                        continue
                    if chunk:
                        # Check for usage data marker
                        if chunk.startswith("__USAGE_DATA__:"):
                            try:
                                usage_json = chunk[len("__USAGE_DATA__:"):]
                                speaker_usage = json.loads(usage_json)
                                self._dbg_step(f"  📊 {speaker['name']} usage: {speaker_usage}")
                            except json.JSONDecodeError:
                                self._dbg_step(f"  ⚠️ Failed to parse usage data: {chunk}")
                            continue

                        # Check for API reasoning marker (o1, thinking models)
                        if chunk.startswith("__REASONING_CHUNK__:"):
                            reasoning_chunk = chunk[len("__REASONING_CHUNK__:"):]

                            # Open details tag on first reasoning chunk
                            if not reasoning_active:
                                yield f"<details>\n<summary>💭 {speaker['name']}'s Thinking</summary>\n\n"
                                reasoning_active = True

                            # Stream reasoning chunk immediately
                            yield reasoning_chunk
                            continue

                        # Check for XML thinking tags in content (Qwen, DeepSeek, etc.)
                        if chunk.startswith("__THINKING_TAG_CONTENT__:"):
                            tag_content = chunk[len("__THINKING_TAG_CONTENT__:"):]

                            # Open details tag on first thinking chunk
                            if not reasoning_active:
                                yield f"<details>\n<summary>💭 {speaker['name']}'s Thinking</summary>\n\n"
                                reasoning_active = True

                            # Strip any residual XML tags and stream directly
                            cleaned = re.sub(r'</?think>|</?thinking>', '', tag_content)
                            if cleaned:
                                yield cleaned
                            continue

                        # Check for thinking tag close marker
                        if chunk == "__THINKING_TAG_CLOSE__":
                            if reasoning_active:
                                yield "\n\n</details>\n\n"
                                reasoning_active = False
                            continue

                        # If we were streaming reasoning and now have regular content, close the details tag
                        if reasoning_active and chunk:
                            yield "\n\n</details>\n\n"
                            reasoning_active = False

                        # Safety: strip any residual think tags that slipped through
                        cleaned_chunk = re.sub(r'</?think>|</?thinking>', '', chunk)
                        if cleaned_chunk:
                            yield cleaned_chunk
                            full_response += cleaned_chunk

                # If reasoning was the last thing (edge case), close the details tag
                if reasoning_active:
                    yield "\n\n</details>\n\n"

                # Aggregate usage data from this speaker (only if tracking enabled)
                if include_usage and speaker_usage and total_usage is not None:
                    total_usage["prompt_tokens"] += speaker_usage.get("prompt_tokens", 0)
                    total_usage["completion_tokens"] += speaker_usage.get("completion_tokens", 0)
                    total_usage["total_tokens"] += speaker_usage.get("total_tokens", 0)

                # Yield citation events if tools were configured for this speaker
                if speaker_tool_ids:
                    for citation_event in self._generate_tool_citations(speaker_tool_ids, speaker["name"], full_response):
                        yield citation_event

                speaker_responses.append(full_response.strip())
                current_round_responded_speakers.append(speaker)

                yield "\n\n---\n\n"
                if i < len(active_speakers) - 1:
                    yield {
                        "event": {
                            "type": "tool",
                            "data": {
                                "tool": f"{active_speakers[i+1]['name']} Generator",
                                "status": "running",
                                "description": f"{active_speakers[i+1]['name']} is speaking...",
                            },
                        }
                    }

            except (ValueError, TypeError, KeyError) as e:
                yield f"❌ **{speaker['name']} Error**: {str(e)}\n\n"
                speaker_responses.append(f"Error: {str(e)}")
                current_round_responded_speakers.append(speaker)

        yield "✅ **Circle Complete** - All speakers have responded\n\n"

        yield {"event": {"type": "status", "data": {"description": "", "done": True}}}

        # Yield aggregated usage data if tracking was enabled and data was collected
        if include_usage and total_usage and total_usage.get("total_tokens", 0) > 0:
            self._dbg_step(f"📊 Total usage across all speakers: {total_usage}")
            yield {
                "usage": {
                    "prompt_tokens": total_usage["prompt_tokens"],
                    "completion_tokens": total_usage["completion_tokens"],
                    "total_tokens": total_usage["total_tokens"],
                }
            }

        gc.collect()

    # =====================================================================
    # SPEAKER MANAGEMENT
    # =====================================================================

    def _escape_format_braces(self, text: str) -> str:
        """
        Escape curly braces in user-provided content to prevent format string injection.
        This is critical for vLLM compatibility - unescaped braces cause parsing errors.
        """
        if not isinstance(text, str):
            return str(text) if text else ""
        return text.replace("{", "{{").replace("}", "}}")

    async def _ensure_speakers(self):
        """Fill the speaker cache on first use and refill it when the valves changed since it was built.
        Replaces the Pipelines-server on_startup / on_valves_updated hooks. Reads the model store, no auth."""
        snapshot = self.valves.model_dump()
        if self.all_speakers and snapshot == self._speakers_valves_snapshot:
            return
        self._dbg_step("🔄 Speaker cache empty or valves changed — fetching from API...")
        await self._initialize_speakers()
        self._speakers_valves_snapshot = snapshot
        self._update_tags_description()

    async def _initialize_speakers(self):
        """
        Initializes the in-memory speaker list by fetching all models directly
        from the OpenWebUI API. This is the SINGLE SOURCE OF TRUTH for speaker data.
        All tag processing happens here, once.
        """
        self._dbg_step("--- Speaker Initialization (In-Memory) ---")
        self.all_speakers = []  # CRITICAL: Clear previous list

        all_api_models = await self._fetch_available_models()
        self._dbg_step(f"  Fetched {len(all_api_models)} total models from OpenWebUI API.")

        api_models = [
            model
            for model in all_api_models
            if (model.get("meta", {}).get("description") or "").strip()
            and (
                (model.get("meta", {}).get("tags", []) or [])
                not in ["default", "system", ""]
            )
        ]
        self._dbg_step(
            f"  Filtered to {len(api_models)} models with description and >1 valid tag."
        )

        if not api_models:
            self._dbg_step("  Result: CRITICAL - Failed to fetch models from OpenWebUI API. No speakers will be available.")
            self._dbg_step("------------------------------------------\n")
            return

        self._dbg_step(f"  Processing {len(api_models)} models from API response...")

        in_memory_speakers = []
        for model in api_models:
            model_id = model.get("id")
            model_name = model.get("name")

            if not model_id or not model_name:
                self._dbg_step("    ⚠️ SKIPPED: Model missing 'id' or 'name'.")
                continue

            # This is the ONLY place where tags are processed
            meta = model.get("meta", {})
            raw_tags = meta.get("tags", []) if isinstance(meta, dict) else []

            # Filter tags: ignore 'system' tag and ensure tag is valid
            tags_set = {
                tag["name"].lower()
                for tag in raw_tags
                if isinstance(tag, dict)
                and tag.get("name")
                and tag["name"].lower() != "system"
            }

            description = meta.get("description", "") if isinstance(meta, dict) else ""

            in_memory_speakers.append(
                {
                    "name": model_name,
                    "model": model_id,
                    "enabled": model.get("is_active", True),
                    "tags": tags_set,
                    "description": description,
                }
            )
            self._dbg_step(
                f"    ✅ LOADED: Speaker '{model_name}' (ID: '{model_id}') with tags: {tags_set if tags_set else 'None'}"
            )

        self.all_speakers = in_memory_speakers
        self._dbg_step(
            f"  Result: OK - Loaded {len(self.all_speakers)} speakers directly from API into memory."
        )
        self._dbg_step("------------------------------------------\n")

    def _update_tags_description(self):
        """Logs the available tags and the SPEAKER_TAGS syntax guide (debug only)."""
        if not self.all_speakers:
            available_tags_str = "No speakers loaded."
        else:
            all_tags = set()
            for speaker in self.all_speakers:
                all_tags.update(speaker.get("tags", set()))

            if all_tags:
                available_tags_str = "Available tags: " + ", ".join(sorted(list(all_tags)))
            else:
                available_tags_str = "No tags defined on models."

        syntax_guide = "Use ',' for OR (e.g., tag1,tag2) and '+' for AND (e.g., tag1+tag2)."
        self._dbg_step(
            f"SPEAKER_TAGS Description Hint: Filter speakers by tags. {syntax_guide} {available_tags_str}"
        )

    def get_speaker_image_url(self, speaker: dict) -> Optional[str]:
        """
        Build the avatar URL for a speaker from PROFILE_THUMBNAIL_ROOT.

        Template form (contains {model_id} and/or {size}): placeholders are filled, the
        model id URL-encoded. Prefix form (no placeholders): legacy
        "{root}{model_id}?width={size}" used by external resizers. Empty root: None.
        """
        thumbnail_root = str(getattr(self.valves, "PROFILE_THUMBNAIL_ROOT", "") or "").strip()
        if not thumbnail_root:
            return None
        size = getattr(self.valves, "PROFILE_IMAGE_SIZE", 60)
        model_id = str(speaker["model"])
        if "{model_id}" in thumbnail_root or "{size}" in thumbnail_root:
            return thumbnail_root.replace("{model_id}", quote(model_id, safe="")).replace("{size}", str(size))
        return f"{thumbnail_root}{model_id}?width={size}"

    def get_speaker_image_markdown(self, speaker: dict, greeting: bool = False) -> str:
        """Generate consistent speaker image markdown with optional microphone emoji for greeting."""
        emoji = "🎤 " if greeting else ""
        profile_image_url = self.get_speaker_image_url(speaker)

        if profile_image_url:
            return f'![{speaker["name"]}]({profile_image_url}) **{speaker["name"]}**:\n'
        else:
            return f"**{emoji}{speaker['name']}:**\n"

    def _get_active_speakers(self, tag_query: str) -> List[dict]:
        """
        Get all enabled speakers from the in-memory list that match the tag query.
        If no speakers match the query, raises ValueError to fail fast.
        """
        active_speakers = []
        tag_query = str(tag_query or "").strip()

        if not self.all_speakers:
            self._dbg_step("⚠️ Speaker list is empty. Cannot get active speakers.")
            return []

        # Split the query into OR groups, ignoring empty strings from trailing commas
        or_groups = [
            group.strip() for group in tag_query.lower().split(",") if group.strip()
        ]
        self._dbg_step(f"  Tag Query: '{tag_query}' -> OR Groups: {or_groups}")

        # --- Primary Search ---
        if tag_query:
            self._dbg_step("  Performing primary search based on tags...")
            for speaker_config in self.all_speakers:
                if speaker_config.get("enabled"):
                    speaker_tags = speaker_config.get("tags", set())
                    self._dbg_step(
                        f"    Checking Speaker: '{speaker_config['name']}' with tags: {speaker_tags}"
                    )

                    for group in or_groups:
                        and_tags = {
                            tag.strip() for tag in group.split("+") if tag.strip()
                        }
                        self._dbg_step(f"      Matching against AND group: {and_tags}")
                        if and_tags.issubset(speaker_tags):
                            self._dbg_step(
                                f"        ✅ MATCH FOUND. Adding '{speaker_config['name']}' to active list."
                            )
                            active_speakers.append(
                                {
                                    "name": speaker_config["name"],
                                    "model": speaker_config["model"],
                                    "description": speaker_config.get("description", ""),
                                    "tags": speaker_config.get("tags", set()),
                                }
                            )
                            break  # Move to next speaker once a match is found
        else:
            # If no tags are provided, get all enabled speakers
            self._dbg_step("  No tag query provided. Getting all enabled speakers for AI selection.")
            for speaker_config in self.all_speakers:
                if speaker_config.get("enabled"):
                    self._dbg_step(f"    Adding enabled speaker: '{speaker_config['name']}'")
                    active_speakers.append(
                        {
                            "name": speaker_config["name"],
                            "model": speaker_config["model"],
                            "description": speaker_config.get("description", ""),
                            "tags": speaker_config.get("tags", set()),
                        }
                    )

        # Fail fast if no speakers match
        if not active_speakers and tag_query:
            raise ValueError(
                f"No speakers found matching tag query: '{tag_query}'. "
                f"Available speakers: {[s['name'] for s in self.all_speakers]}"
            )

        # Remove duplicates
        unique_speakers = []
        seen_names = set()
        for speaker in active_speakers:
            if speaker["name"] not in seen_names:
                unique_speakers.append(speaker)
                seen_names.add(speaker["name"])

        self._dbg_step(f"  Final speaker count: {len(unique_speakers)}")
        return unique_speakers

    # =====================================================================
    # CONVERSATION CONTEXT & ORCHESTRATION
    # =====================================================================

    def _get_conversation_key(self, body: dict, metadata: Optional[dict]) -> str:
        """Generate a unique key for conversation memory. Open WebUI passes the chat id in the
        metadata parameter of a Function; body keys are kept as fallbacks."""
        if metadata and metadata.get("chat_id"):
            return str(metadata["chat_id"])
        return body.get("chat_id") or body.get("session_id") or "default"

    def _get_recent_conversation_context(self, messages: List[dict]) -> str:
        """Extract recent context from OpenWebUI's message history"""
        recent_context = ""
        if self.valves.DECISION_CONTEXT_DEPTH <= 0 or not messages:
            return ""

        context_depth = min(self.valves.DECISION_CONTEXT_DEPTH, len(messages))

        if context_depth > 0:
            recent_messages = messages[-context_depth:]
            context_parts = []

            for msg in recent_messages:
                role = msg.get("role", "")
                content = self._message_text(msg)

                max_content_length = max(50, 200 - (context_depth * 15))
                content_truncated = content[:max_content_length]
                if len(content) > max_content_length:
                    content_truncated += "..."

                if role == "user":
                    context_parts.append(f"User: {content_truncated}")
                elif role == "assistant":
                    if ": " in content_truncated:
                        speaker_name, speaker_response = content_truncated.split(": ", 1)
                        context_parts.append(f"{speaker_name}: {speaker_response}")
                    else:
                        context_parts.append(f"Speaker: {content_truncated}")

            recent_context = "\n".join(context_parts)
        return recent_context

    async def _analyze_user_message_for_speaker_order(
        self,
        user_message: str,
        active_speakers: List[dict],
        conversation_context: str = "",
        tag_query: str = "",
        memory_context: str = "",
        request: Optional[Request] = None,
        user=None,
    ) -> List[dict]:
        """
        Analyze user message using AI to determine optimal speaker order.
        If tag_query is empty, performs SELECTION and ORDERING from all speakers.
        If tag_query has value, performs ORDERING on the pre-filtered speakers.
        """
        try:
            # Build context about available speakers with their expertise/personalities
            speaker_descriptions = []
            for speaker in active_speakers:
                description = speaker.get("description", "No description available.")
                tags = (
                    ", ".join(speaker.get("tags", []))
                    if isinstance(speaker.get("tags"), set)
                    else ""
                )
                speaker_descriptions.append(
                    f"## speaker\n## name: {speaker['name']}\n## description: {description}\n## tags: {tags}"
                )

            speakers_context = "\n".join(speaker_descriptions)

            # Add conversation context if available
            if conversation_context.strip():
                context_section = (
                    f"PREVIOUS CONVERSATION CONTEXT:\n"
                    f"{conversation_context[:500]}{'...' if len(conversation_context) > 500 else ''}"
                )
            else:
                context_section = ""

            # Add memory context if available
            if memory_context.strip():
                context_section += f"\n{memory_context}"

            # Build the dynamic content to send as user message
            dynamic_content = f"""**AVAILABLE CAST:**
{speakers_context}

**RECENT CONVERSATION:**
{context_section if context_section.strip() else "No previous context."}

**USER'S MESSAGE:**
"{user_message}"

Select and order the speakers now."""

            self._dbg_step("--- Orchestration Analysis ---")
            self._dbg_step(f"  Mode: {'SELECTION & ORDERING' if not tag_query else 'ORDERING'}")
            self._dbg_step(f"  User Message: {user_message}")
            self._dbg_step(f"  Dynamic Content Length: {len(dynamic_content)} chars")
            self._dbg_step("-----------------------------")

            ordering_messages = [
                {"role": "user", "content": dynamic_content},
            ]

            self._dbg_step(f"  Orchestration messages: {len(ordering_messages)} messages")

            ordering_model_id = self._resolve_model_id(self.valves.ORCHESTRATION_MODEL_NAME)
            self._dbg_step(f"  Orchestration model: name='{self.valves.ORCHESTRATION_MODEL_NAME}' resolved_id='{ordering_model_id}'")

            async def collect_ordering_response(messages: List[dict]) -> str:
                response = ""
                chunk_count = 0
                non_content_chunks = []
                first_error = None
                async for chunk in self._query_openwebui_model_streaming(
                    ordering_model_id, messages, request=request, user=user
                ):
                    chunk_count += 1
                    if chunk and chunk.startswith("❌"):
                        first_error = first_error or chunk
                        continue
                    if chunk:
                        if chunk.startswith("__"):
                            non_content_chunks.append(chunk[:200])
                            continue
                        response += chunk
                if first_error and not response.strip():
                    raise ValueError(
                        f"Orchestration model '{self.valves.ORCHESTRATION_MODEL_NAME}' failed: {first_error.lstrip('❌ ')}"
                    )

                self._dbg_step(f"  Orchestrator stream: {chunk_count} chunks, response len={len(response)}")
                if non_content_chunks:
                    self._dbg_step(f"  Orchestrator ignored {len(non_content_chunks)} marker chunks (e.g. __REASONING__, __THINKING__, __USAGE__)")
                    self._dbg_full(f"  Marker chunks: {non_content_chunks}")
                self._dbg_full("  Raw orchestrator response (repr, truncated 2000):")
                self._dbg_full(self._truncate(repr(response), 2000))
                return response

            def parse_ordered_names(response: str) -> List[str]:
                if not response.strip():
                    raise ValueError(
                        f"Orchestration model '{self.valves.ORCHESTRATION_MODEL_NAME}' returned empty response. "
                        f"The model may not be working properly or its system prompt may be incorrect."
                    )

                self._dbg_step(f"  --- PARSE BEGIN --- response len={len(response)} ---")
                self._dbg_full(f"  PARSE.input.repr: {self._truncate(repr(response), 800)}")
                lines = response.strip().split("\n")
                self._dbg_step(f"  PARSE: split into {len(lines)} lines")
                ordered_names = []

                for line_idx, line in enumerate(lines):
                    raw_line = line
                    line = line.strip()
                    has_prefix = line.startswith("#####")
                    self._dbg_full(f"  PARSE.line[{line_idx}].repr={self._truncate(repr(raw_line), 300)} stripped.repr={self._truncate(repr(line), 300)} has_##### prefix={has_prefix}")
                    if has_prefix:
                        speaker_name = line.replace("#####", "").strip()
                        self._dbg_step(f"  PARSE.line[{line_idx}] extracted name.repr={repr(speaker_name)}")
                        if speaker_name:
                            ordered_names.append(speaker_name)
                self._dbg_step(f"  --- PARSE END --- AI Ordered Names ({len(ordered_names)}): {ordered_names}")
                return ordered_names

            def match_ordered_names(ordered_names: List[str]) -> tuple:
                self._dbg_full(f"  --- MATCH BEGIN --- active_speakers count={len(active_speakers)}")
                self._dbg_full("  active_speakers (name/model pairs):")
                for sp in active_speakers:
                    self._dbg_full(f"    name.repr={repr(sp.get('name'))} model.repr={repr(sp.get('model'))}")

                ordered_speakers = []
                invalid_names = []
                matched_keys = set()
                speakers_by_name = {speaker["name"]: speaker for speaker in active_speakers}

                for name in ordered_names:
                    speaker = speakers_by_name.get(name.strip())
                    if speaker:
                        speaker_key = speaker.get("model") or speaker.get("name")
                        if speaker_key not in matched_keys:
                            ordered_speakers.append(speaker)
                            matched_keys.add(speaker_key)
                            self._dbg_step(f"  ✅ Matched exact speaker name='{speaker['name']}' model='{speaker.get('model')}'")
                    else:
                        invalid_names.append(name)
                        self._dbg_step(f"  ⚠️ No active speaker matched orchestrator name '{name}'")

                self._dbg_step(f"  --- MATCH END --- Final selection: {len(ordered_names)} names → {len(ordered_speakers)} matched speakers; invalid={invalid_names}")
                return ordered_speakers, invalid_names

            response = await collect_ordering_response(ordering_messages)
            ordered_names = parse_ordered_names(response)
            ordered_speakers, invalid_names = match_ordered_names(ordered_names)

            if invalid_names:
                self._dbg_step(f"  ⚠️ Ignoring invalid orchestrator names: {invalid_names}")

            if not ordered_speakers:
                raise ValueError(
                    "Orchestration returned no valid speakers from the available cast."
                )

            return ordered_speakers

        except (json.JSONDecodeError, ValueError) as e:
            self._dbg_step(f"❌ CRITICAL: Intelligent ordering failed: {e}")
            raise ValueError(f"Speaker orchestration failed: {e}") from e

    # =====================================================================
    # USER PROFILE & CONTEXT EXTRACTION
    # =====================================================================

    def _extract_user_profile(self, messages: List[dict]) -> dict:
        """Extract user profile information from system prompts using markdown separators"""
        profile = {
            "username": "friend",
            "personal_info": "",
            "system_instructions": "",
            "has_profile_section": False,
        }

        system_messages = [
            msg.get("content", "") for msg in messages if msg.get("role") == "system"
        ]
        self._dbg_step(f"🔍 DEBUG: Found {len(system_messages)} system messages")
        if not system_messages:
            self._dbg_step("🔍 DEBUG: No system messages found, returning default profile")
            return profile

        full_system_prompt = " ".join(system_messages)
        self._dbg_step(
            f"🔍 DEBUG: Full system prompt: '{self._truncate(full_system_prompt, 200)}'"
        )

        profile["system_instructions"] = full_system_prompt.strip()

        # Try to extract name from system prompt
        lines = full_system_prompt.split("\n")
        for line in lines:
            line_original = line.strip()
            line_lower = line_original.lower()
            if any(phrase in line_lower for phrase in ["my name is", "i am", "call me"]):
                for phrase in ["my name is", "i am", "call me"]:
                    if phrase in line_lower:
                        phrase_start = line_lower.find(phrase)
                        name_part = line_original[phrase_start + len(phrase):].strip()
                        if name_part:
                            profile["username"] = name_part.split()[0]
                        break
                break

        self._dbg_step(f"🔍 DEBUG: Final extracted profile - Username: '{profile['username']}'")
        return profile

    def _extract_personal_hints(self, system_instructions: str) -> str:
        """Extract personal information hints from general system instructions"""
        hints = []
        lines = system_instructions.lower().split("\n")

        personal_keywords = [
            "i am", "my name", "i work", "i study", "i like", "i enjoy",
            "my profession", "my job", "my role", "my interests", "my goals",
            "i struggle with", "i need help", "my challenge", "my background",
        ]

        for line in lines:
            line = line.strip()
            if any(keyword in line for keyword in personal_keywords):
                cleaned_line = line.replace("you should", "").replace("please", "").strip()
                if cleaned_line and len(cleaned_line) > 10:
                    hints.append(cleaned_line.capitalize())

        if hints:
            return ". ".join(hints[:3])
        return ""

    def _format_static_system_prompt(self, template: str, speaker_name: str) -> str:
        """Fill the per-speaker template with the speaker's name only. Every other placeholder of the older
        template shape (user message, date, takes, speaker lists) is filled with an empty string, so an
        un-migrated Open WebUI system prompt still works; the per-round values travel in the user turn."""
        # Open WebUI merges per-turn material into the same system message the template is read from:
        # the web-search <context> block (RAG_SYSTEM_CONTEXT) and the memory filter's "User Context:".
        # Both are delivered through the round context instead, so cut them out here or the system
        # prompt would change every round and the prompt cache would never hit.
        template = re.sub(r"<context>.*?</context>", "", template, flags=re.S)
        template = re.split(r"\n\s*User Context:", template, maxsplit=1)[0]
        name = self._escape_format_braces(speaker_name)
        try:
            return template.format(
                speaker_name=name, user_message="", previous_context="", current_date="",
                current_round_speakers="", historical_speakers="", introduction_instructions="",
            ).strip()
        except (KeyError, IndexError, ValueError):
            return template.replace("{speaker_name}", speaker_name).strip()

    def _history_for_speaker(self, messages: List[dict], speaker_name: str) -> List[dict]:
        """The conversation so far, segregated by speaker (2026-11-03). Every stored circle reply is split
        into its "**Name**: text" blocks: this speaker's own block becomes an assistant turn, the other
        speakers' blocks go into a user turn labelled as theirs. The model then only ever sees its own voice
        under the assistant role, so it cannot continue the other speakers' blocks as if they were its own
        (Father Spyridon wrote Peter's take in round 3 of the 2026-11-02 test; Peter took Paul's Damascus
        story from a take). System messages are dropped and the current user message is excluded: the
        caller re-sends it as its own plain turn before the round context. Already truncated by
        _truncate_history. The history is the same for every speaker except for which blocks sit under
        which role, so system + history stays a stable prefix across rounds."""
        conv = [
            m for m in messages
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
        ]
        if conv and conv[-1]["role"] == "user":
            conv = conv[:-1]  # the current question is re-sent by the caller as its own plain turn
        out: List[dict] = []
        for m in conv:
            content = m.get("content", "")
            if m["role"] == "user":
                if content.strip():
                    out.append({"role": "user", "content": content})
                continue
            blocks = self._split_circle_reply(content)
            if not blocks:
                # Not a circle reply (an error text, a reply from before the circle format): keep it as it was.
                cleaned = self._clean_history_turn("assistant", content)
                if cleaned:
                    out.append({"role": "assistant", "content": cleaned})
                continue
            own = [text for name, text in blocks if name == speaker_name]
            others = [(name, text) for name, text in blocks if name != speaker_name]
            if own:
                out.append({"role": "assistant", "content": "\n\n".join(own)})
            if others:
                head = (
                    "The other speakers' replies to that message. These are their words, not yours and not the user's:"
                    if own else
                    "You did not speak in that round. The speakers' replies to that message. These are their words, not yours and not the user's:"
                )
                body = "\n\n".join(f"**{name}**: {text}" for name, text in others)
                out.append({"role": "user", "content": f"{head}\n\n{body}"})
        return out

    _CHROME_LINE = re.compile(r"^\s*(🔍 \*\*|💬 \*\*|🔧 \*\*|✅ \*\*Circle Complete|❌ \*\*|---\s*$)")
    _AVATAR_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)\s*")
    # The heading lines get_speaker_image_markdown writes: "![Name](url) **Name**:" (alt text and bold name
    # must agree) or, without an avatar root, "**Name:**" / "**🎤 Name:**". Matched on the raw line, before
    # the avatar is stripped: a bold label the model wrote itself ("**Key points**:", or one speaker heading
    # its text with another's name) is body text, not a heading.
    _SPEAKER_HEADING_AVATAR = re.compile(r"^!\[([^\]]+)\]\([^)]*\)\s*\*\*\1\*\*:\s*$")
    _SPEAKER_HEADING_PLAIN = re.compile(r"^\*\*(?:🎤 )?([^*\n]+):\*\*\s*$")

    def _heading_speaker(self, raw_line: str, known: set) -> Optional[str]:
        """The avatar form is trusted on its own (alt text and bold name agree), so a speaker since renamed
        or removed in Open WebUI keeps its old blocks. The plain form is only written when
        PROFILE_THUMBNAIL_ROOT is empty, so it counts only then, and only for a configured speaker name."""
        m = self._SPEAKER_HEADING_AVATAR.match(raw_line)
        if m:
            return m.group(1).strip()
        if str(getattr(self.valves, "PROFILE_THUMBNAIL_ROOT", "") or "").strip():
            return None
        m = self._SPEAKER_HEADING_PLAIN.match(raw_line)
        if not m:
            return None
        name = m.group(1).strip()
        return name if name in known else None

    def _split_circle_reply(self, content: str) -> List[tuple]:
        """A stored circle reply as (speaker name, text) blocks in order, split on the heading lines the
        pipeline writes for configured speakers (avatar images and chrome removed from the text). Anything
        before the first heading (the "speakers selected" line) is dropped. Empty list when the text has no
        speaker headings."""
        known = {str(sp.get("name", "")).strip() for sp in (self.all_speakers or [])}
        known.discard("")
        blocks: List[tuple] = []
        name = None
        buf: List[str] = []
        for raw in content.splitlines():
            heading = self._heading_speaker(raw, known)
            if heading:
                if name is not None:
                    blocks.append((name, "\n".join(buf).strip()))
                name, buf = heading, []
                continue
            line = self._AVATAR_MD.sub("", raw)
            if self._CHROME_LINE.match(line):
                continue
            if name is not None:
                buf.append(line)
        if name is not None:
            blocks.append((name, "\n".join(buf).strip()))
        return [(n, re.sub(r"\n{3,}", "\n\n", t)) for n, t in blocks if t]

    def _clean_history_turn(self, role: str, content: str) -> str:
        """An assistant turn as Open WebUI stored it carries the pipeline's own chrome: status lines,
        avatar images, separators, the completion banner. Used for assistant turns that are not circle
        replies (no speaker headings); circle replies go through _split_circle_reply instead."""
        if role != "assistant":
            return content
        lines = [self._AVATAR_MD.sub("", ln) for ln in content.splitlines()]
        kept = [ln for ln in lines if not self._CHROME_LINE.match(ln)]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

    def _build_round_context(
        self, speaker_name: str, is_first: bool, needs_introduction: bool, previous_takes: List[str],
        current_speakers_str: str, historical_speakers_str: str, web_search_context: str,
        memory_context: str, user_context: str,
    ) -> str:
        """Everything that changes per round, as one block for the final user turn."""
        parts = []
        if needs_introduction:
            parts.append(str(self.valves.INTRODUCTION_PROMPT_PREFIX).format(speaker_name=speaker_name))
        elif is_first:
            parts.append(str(self.valves.FIRST_SPEAKER_PROMPT_PREFIX).format(speaker_name=speaker_name))
        parts.append(f"CURRENT DATE: {datetime.now().strftime('%B %d, %Y')}")
        parts.append(f"**Current Speakers in this Round:**\n{current_speakers_str}")
        parts.append("**Previous Speakers' Takes (in this round):**\n" + ("\n".join(previous_takes) if previous_takes else "None yet: you open this round."))
        parts.append(f"**Historical Speakers (not in this round, but spoke previously):**\n{historical_speakers_str}")
        if web_search_context:
            parts.append(web_search_context)
        if memory_context:
            parts.append(memory_context)
        if user_context and user_context.strip():
            parts.append(user_context.strip())
        return "\n\n".join(parts)

    def _build_user_context_section(self, user_profile: dict) -> str:
        """Build user context section to append to speaker prompts"""
        if not user_profile or not self.valves.ENABLE_USER_PROFILE_INTEGRATION:
            return ""

        context_parts = []

        if user_profile.get("personal_info"):
            context_parts.append(
                f"USER BACKGROUND: The user has shared the following about themselves:\n{user_profile['personal_info']}"
            )
        elif user_profile.get("system_instructions"):
            personal_hints = self._extract_personal_hints(user_profile["system_instructions"])
            if personal_hints:
                context_parts.append(f"USER CONTEXT: {personal_hints}")

        if context_parts:
            return (
                "\n\n"
                + "\n\n".join(context_parts)
                + "\n\nUse this information to provide more personalized and relevant responses."
            )

        return ""

    # =====================================================================
    # WEB SEARCH & MEMORY CONTEXT EXTRACTION
    # =====================================================================

    def _extract_web_search_context(self, messages: List[dict]) -> str:
        """Extract web search context from OpenWebUI system messages"""
        web_context = ""

        if self._is_step_debug():
            for i, msg in enumerate(messages):
                if msg.get("role") == "system":
                    content = msg.get("content", "")
                    self._dbg_step(
                        f"🔍 DEBUG: System Message {i+1}: {self._truncate(content, 200)}"
                    )

        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content", "")

                if "<source id=" in content and "</source>" in content:
                    if "<context>" in content and "</context>" in content:
                        context_start = content.find("<context>") + len("<context>")
                        context_end = content.find("</context>")
                        raw_context = content[context_start:context_end].strip()
                        web_context = self._format_web_search_for_speakers(raw_context)
                        break

        return web_context

    def _format_web_search_for_speakers(self, raw_context: str) -> str:
        """Format web search results for speaker consumption with clear markdown formatting"""
        if not raw_context:
            return ""

        formatted_sources = []

        sources = re.findall(
            r'<source id="(\d+)"[^>]*>(.*?)</source>', raw_context, re.DOTALL
        )

        for source_id, content in sources:
            clean_content = re.sub(r"\s+", " ", content).strip()
            if len(clean_content) > 300:
                clean_content = clean_content[:300] + "..."

            if clean_content:
                formatted_sources.append(f"**Source [{source_id}]:** {clean_content}")

        if formatted_sources:
            return f"""
---
# 🔍 **VERIFIED LIVE RESEARCH** - TREAT AS FACTUAL
## ⚡ **REAL-TIME DATA INJECTION** ⚡
**This is NOT speculation. This is NOT future data. This is CURRENT, VERIFIED information retrieved RIGHT NOW from live web sources.**

{chr(10).join(formatted_sources)}

---
## 🚨 **MANDATORY:** USE THIS VERIFIED DATA IN YOUR RESPONSE
**Do NOT dismiss this as "unverified" or "future dates" - this is LIVE, CURRENT, FACTUAL information that you MUST reference and use in your response.**
---"""

        return ""

    def _extract_memory_context(self, messages: List[dict]) -> str:
        """Extract user memory context from OpenWebUI system messages"""
        memory_context = ""

        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content", "")

                if "User Context:" in content:
                    context_start = content.find("User Context:")
                    context_content = content[context_start:]
                    memory_context = self._format_memory_for_speakers(context_content)
                    break

        return memory_context

    def _format_memory_for_speakers(self, raw_context: str) -> str:
        """Format memory context for speaker consumption with clear markdown formatting"""
        if not raw_context or not raw_context.strip():
            return ""

        lines = raw_context.strip().split("\n")
        formatted_lines = []

        for line in lines:
            line = line.strip()
            if line and line != "User Context:":
                formatted_lines.append(line)

        if formatted_lines:
            return f"""
---
# 📝 **USER MEMORY CONTEXT**
## 🧠 **Personal Information About This User**
**The following are verified facts about this specific user from previous conversations:**

{chr(10).join(formatted_lines)}

---
## 💡 **IMPORTANT:** Personalize your response using this context
**Reference these memories when relevant to provide more personalized and contextual responses.**
---"""

        return ""
