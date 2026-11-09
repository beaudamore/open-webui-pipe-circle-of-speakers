"""
title: Play Circle Audio
author: beaudamore
author_url: https://github.com/beaudamore/circle-of-speakers-pipeline
version: 3.2
license: MIT
description: Message action for Circle of Speakers replies - reads each speaker's take aloud in that speaker's own voice, streamed sentence by sentence, using the TTS engine configured in Open WebUI Settings > Audio.
icon_url: data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxZW0iIGhlaWdodD0iMWVtIiB2aWV3Qm94PSIwIDAgMjU2IDI1NiI+PHBhdGggZmlsbD0iY3VycmVudENvbG9yIiBkPSJNMTM5IDE2Ni4yNmE2NiA2NiAwIDEgMC02MiAwYy0yMiA2LjIyLTQxLjg4IDE5LjE1LTU3LjYxIDM3Ljg4YTYgNiAwIDAgMCA5LjE4IDcuNzJDNDkuMTEgMTg3LjQ1IDc3LjMxIDE3NCAxMDggMTc0czU4LjkgMTMuNDUgNzkuNDEgMzcuODZhNiA2IDAgMSAwIDkuMTgtNy43MkMxODAuODYgMTg1LjQxIDE2MSAxNzIuNDggMTM5IDE2Ni4yNk01NCAxMDhhNTQgNTQgMCAxIDEgNTQgNTRhNTQuMDYgNTQuMDYgMCAwIDEtNTQtNTRtMTUxLjUyLTQxLjYxYTEwNi4zMyAxMDYuMzMgMCAwIDEgMCA4My4yMmE2IDYgMCAwIDEtMTEtNC43MWE5NC4zIDk0LjMgMCAwIDAgMC03My44YTYgNiAwIDAgMSAxMS00LjcxTTI0NiAxMDhhMTM3LjIgMTM3LjIgMCAwIDEtMTEuMTIgNTQuMzdhNiA2IDAgMCAxLTExLTQuNzRhMTI2LjQgMTI2LjQgMCAwIDAgMC05OS4yNmE2IDYgMCAwIDEgMTEtNC43NEExMzcuMiAxMzcuMiAwIDAgMSAyNDYgMTA4Ii8+PC9zdmc+
"""

# Version 3.0 (2026-11-08), from circleofspeakers/audio_generation/circle_audio_internal_action_v2_2_latest.py.
# Changes against 2.2:
#   - TTS comes from Open WebUI's own Settings > Audio (engine "openai", its base URL, key, model and
#     extra params), read through the Config store exactly as the audio router does. The tts_base_url /
#     tts_api_key / tts_model valves are gone, so the circle speaks through whatever engine the chat's
#     own speaker button uses (Chatterbox, openedai-speech, ...). The chat.tts permission is honoured.
#   - the HTML/XML stripper no longer loops forever on text with a ">" before a "<" ("2 > 1 but 1 < 2");
#     that loop ran synchronously inside the action and froze the server's event loop.
#   - the voice id is read from the avatar URL's `id` query parameter first (the pipe's default
#     thumbnail root is /api/v1/models/model/profile/image?id={model_id}&width={size}), then from the
#     last path segment (prefix-form roots such as an external resizer).
#   - the button icon is a data URL in the frontmatter (icon_url), which Open WebUI reads first. The
#     download-to-/static/cdn code is gone: that folder is read-only in the container and the sync
#     requests.get ran on the event loop at every button press.
#   - voice resolution per speaker (2026-11-08, Beau's design): the speaker model's own Voice field
#     (meta.voice.voice, set in the model editor and used by the chat's speaker button), else the model id
#     when the TTS engine lists a voice of that name, else the default voice from Settings > Audio. The
#     engine's voice list comes from the audio router's get_available_voices, fetched once per press.
#   - the no-avatar heading form is "**Name:**" (colon inside the bold), as the pipe writes it; 2.2 looked
#     for "**Name**:" and found nothing when PROFILE_THUMBNAIL_ROOT was empty.
#   - the blob's MIME type is the engine's Content-Type instead of a hard-coded audio/mpeg; the TTS
#     requests use Open WebUI's pooled aiohttp session and forward user-info headers like the router.
#   - unused parameters removed, debug steps renumbered, enable_debug defaults to off.
# Playback: each audio segment is sent to the browser as base64 and queued on the chat's hidden
# audioElement; nothing is written to disk.
#
# Version 3.1 (2026-11-08): pauses. Two valves, pause_after_announcement_seconds (between "Now speaking,
# <name>" and the take) and pause_after_speaker_seconds (after a speaker's last sentence, before the next
# announcement or speaker; none after the last speaker). The silence is a timed gap in the browser queue,
# not generated audio. A browser tab that already ran 3.0 keeps the old queue until it is reloaded.
#
# Version 3.2 (2026-11-09): the button is a toggle. Up to 3.1 nothing called the queue's stop(): a second
# press started a second generation on top of the first. Now every press first asks the browser (through
# __event_call__, which returns the script's value) whether the circle is generating or playing. If it
# is, the press stops playback, empties the queue and ends there. If it is not, the press claims a run id
# in the browser (window.circleAudioRun) and generates as before. Each clip is queued through
# __event_call__ too and is only accepted while its run id is still the claimed one, so the generation
# that was stopped ends at its next clip instead of running on through the TTS engine. No answer from
# the browser within _BROWSER_REPLY_SECONDS (tab closed, or the user moved to another chat: Open WebUI
# drops execute events for a chat that is not open) ends the run the same way. A run id the browser has
# not heard from for _RUN_STALE_SECONDS no longer counts as generating, so a run that ended out of the
# browser's sight does not swallow the next press. The browser queue object itself is unchanged, so a tab
# that already ran 3.1 needs no reload.

from typing import Optional, Callable, Awaitable, List, Dict, Tuple
from types import SimpleNamespace
import asyncio
import base64
import json
import re
import uuid
from urllib.parse import urlsplit, parse_qs

from pydantic import BaseModel, Field

# Open WebUI internals: the Function runs inside the Open WebUI process.
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL, ENABLE_FORWARD_USER_INFO_HEADERS
from open_webui.models.config import Config
from open_webui.models.models import Models
from open_webui.routers.audio import get_available_voices
from open_webui.utils.access_control import has_permission
from open_webui.utils.headers import include_user_info_headers
from open_webui.utils.session_pool import get_session


ICON_DATA_URL = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxZW0iIGhlaWdodD0iMWVtIiB2aWV3Qm94PSIwIDAgMjU2IDI1NiI+"
    "PHBhdGggZmlsbD0iY3VycmVudENvbG9yIiBkPSJNMTM5IDE2Ni4yNmE2NiA2NiAwIDEgMC02MiAwYy0yMiA2LjIyLTQxLjg4IDE5LjE1LTU3LjYxIDM3Ljg4"
    "YTYgNiAwIDAgMCA5LjE4IDcuNzJDNDkuMTEgMTg3LjQ1IDc3LjMxIDE3NCAxMDggMTc0czU4LjkgMTMuNDUgNzkuNDEgMzcuODZhNiA2IDAgMSAwIDkuMTgt"
    "Ny43MkMxODAuODYgMTg1LjQxIDE2MSAxNzIuNDggMTM5IDE2Ni4yNk01NCAxMDhhNTQgNTQgMCAxIDEgNTQgNTRhNTQuMDYgNTQuMDYgMCAwIDEtNTQtNTRt"
    "MTUxLjUyLTQxLjYxYTEwNi4zMyAxMDYuMzMgMCAwIDEgMCA4My4yMmE2IDYgMCAwIDEtMTEtNC43MWE5NC4zIDk0LjMgMCAwIDAgMC03My44YTYgNiAwIDAg"
    "MSAxMS00LjcxTTI0NiAxMDhhMTM3LjIgMTM3LjIgMCAwIDEtMTEuMTIgNTQuMzdhNiA2IDAgMCAxLTExLTQuNzRhMTI2LjQgMTI2LjQgMCAwIDAgMC05OS4y"
    "NmE2IDYgMCAwIDEgMTEtNC43NEExMzcuMiAxMzcuMiAwIDAgMSAyNDYgMTA4Ii8+PC9zdmc+"
)


class Action:
    """
    Message action: generates TTS audio for a Circle of Speakers reply on button press and streams it
    to the browser, one sentence at a time, each speaker in the voice named after its model id.
    """

    class Valves(BaseModel):
        speaker_announcement: bool = Field(
            default=True,
            description="Say 'Now speaking, <Speaker Name>' before each speaker's take.",
        )
        use_custom_announcer_voice: bool = Field(
            default=True,
            description="Use a separate voice for the announcements. Off = each speaker announces themselves in their own voice.",
        )
        announcement_voice: str = Field(
            default="echo",
            description="Voice for the announcements when use_custom_announcer_voice is on. Must exist on the TTS engine configured in Settings > Audio.",
        )
        pause_after_announcement_seconds: float = Field(
            default=0.8,
            ge=0,
            description="Silence between 'Now speaking, <name>' and the speaker's first sentence. 0 = none. Only applies when speaker_announcement is on.",
        )
        pause_after_speaker_seconds: float = Field(
            default=1.5,
            ge=0,
            description="Silence after each speaker's last sentence, before the next announcement or speaker. 0 = none.",
        )
        enable_debug: bool = Field(
            default=False,
            description="Step-by-step logging to the Open WebUI container log.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # How long the browser gets to answer one __event_call__. An open chat answers in milliseconds; a
    # closed tab or another chat never answers, and Open WebUI's own wait has no time limit by default.
    _BROWSER_REPLY_SECONDS = 20

    # A claimed run id counts as "generating" only while the browser heard from that run this recently
    # (the claim itself, then every accepted clip). A run that ended where the browser could not be told
    # (another chat was opened, socket down) would otherwise keep its id claimed and turn the next press
    # into a stop of nothing. A single clip that takes longer than this at the TTS engine while nothing is
    # playing makes a press restart the reply instead of stopping it.
    _RUN_STALE_SECONDS = 60

    # ------------------------------------------------------------------ helpers

    def _dbg(self, *args) -> None:
        if self.valves.enable_debug:
            try:
                print("[CircleAudio]", *args)
            except Exception:
                pass

    @staticmethod
    def _strip_markup(text: str) -> str:
        """Remove HTML/XML tags and comments. Repeats until nothing changes, so a '>' before a '<' in
        prose ('2 > 1 but 1 < 2') cannot make it loop forever (the 2.2 version did)."""
        previous = None
        while previous != text:
            previous = text
            text = re.sub(r"<[^<>]*>", "", text)
        return text.strip()

    @staticmethod
    def _name_to_voice_id(speaker_name: str) -> str:
        """'Claude Sonnet 4' -> 'claude-sonnet-4'."""
        voice_id = speaker_name.lower()
        voice_id = re.sub(r"[^\w\s-]", "", voice_id)
        voice_id = re.sub(r"\s+", "-", voice_id)
        voice_id = re.sub(r"-+", "-", voice_id)
        return voice_id.strip("-")

    @staticmethod
    def _model_id_from_image_url(image_url: str) -> Optional[str]:
        """The speaker's model id as the circle pipe encodes it in the avatar URL.

        Template form (pipe default): /api/v1/models/model/profile/image?id=<model_id>&width=60 -> the `id`
        query parameter. Prefix form (external resizer): https://host/image/<model_id>?width=60 -> the
        last path segment, file extension removed."""
        try:
            parts = urlsplit(image_url)
            ids = parse_qs(parts.query).get("id")
            if ids and ids[0].strip():
                return ids[0].strip()
            segments = parts.path.rstrip("/").split("/")
            last = segments[-1] if segments else ""
            last = re.sub(r"\.(png|jpg|jpeg|gif|webp|svg)$", "", last, flags=re.IGNORECASE)
            return last or None
        except Exception:
            return None

    # Heading lines the circle pipe writes: "![Name](url) **Name**:" or, without an avatar root,
    # "**Name:**" / "**🎤 Name:**". A take runs to the next "---" separator, the next heading, or the end.
    _WITH_IMAGE = re.compile(
        r"!\[([^\]]+?)\]\(([^)]+?)\)\s+\*\*([^\*]+?)\*\*\s*:\s*\n(.*?)(?=\n---\n|\n!\[|\n\*\*(?:🎤\s)?[^\*]+?\*\*\s*:|\Z)",
        re.DOTALL,
    )
    _WITHOUT_IMAGE = re.compile(
        r"\*\*(?:🎤\s)?([^\*\n]+?):\*\*\s*\n(.*?)(?=\n---\n|\n\*\*(?:🎤\s)?[^\*\n]+?:\*\*\s*\n|\Z)",
        re.DOTALL,
    )

    def _clean_dialogue(self, dialogue: str) -> str:
        """Drop a thinking block and all markup from a speaker's take."""
        if "</details>" in dialogue:
            dialogue = dialogue.split("</details>", 1)[1]
        return self._strip_markup(dialogue)

    def _extract_speakers(self, content: str) -> List[Dict[str, str]]:
        """[{name, dialogue, model_id}] for every speaker heading in a circle reply."""
        speakers: List[Dict[str, str]] = []

        for match in self._WITH_IMAGE.finditer(content):
            image_url = match.group(2).strip()
            name = match.group(3).strip()  # the bold name is what the user sees
            dialogue = self._clean_dialogue(match.group(4))
            if not dialogue:
                continue
            model_id = self._model_id_from_image_url(image_url) or self._name_to_voice_id(name)
            self._dbg(f"[EXTRACT] {name!r} image={image_url!r} voice={model_id!r}")
            speakers.append({"name": name, "dialogue": dialogue, "model_id": model_id})

        if not speakers:
            for match in self._WITHOUT_IMAGE.finditer(content):
                name = match.group(1).strip()
                dialogue = self._clean_dialogue(match.group(2))
                if not dialogue:
                    continue
                model_id = self._name_to_voice_id(name)
                self._dbg(f"[EXTRACT] {name!r} no image, voice={model_id!r}")
                speakers.append({"name": name, "dialogue": dialogue, "model_id": model_id})

        return speakers

    @staticmethod
    def _split_into_sentences(text: str) -> List[str]:
        if not text:
            return []
        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]

    # ------------------------------------------------------------------ voices + TTS (Settings > Audio)

    async def _resolve_voice(self, speaker: Dict[str, str], engine_voices: Dict[str, str], default_voice: str) -> str:
        """The speaker model's Voice field, else its model id when the engine has a voice of that name
        (engine_voices: lower-cased id -> engine id), else the default voice from Settings > Audio."""
        model_id = speaker["model_id"]
        try:
            row = await Models.get_model_by_id(model_id)
        except Exception:
            row = None
        voice_field = None
        if row is not None:
            meta = getattr(row, "meta", None)
            voice_obj = getattr(meta, "voice", None)
            voice_field = getattr(voice_obj, "voice", None) if voice_obj is not None else None
            if voice_field is None and isinstance(meta, dict):
                voice_field = (meta.get("voice") or {}).get("voice")
        if voice_field:
            self._dbg(f"[VOICE] {speaker['name']!r}: model voice field {voice_field!r}")
            return voice_field
        if model_id.lower() in engine_voices:
            engine_id = engine_voices[model_id.lower()]
            self._dbg(f"[VOICE] {speaker['name']!r}: engine has voice {engine_id!r}")
            return engine_id
        self._dbg(f"[VOICE] {speaker['name']!r}: no model voice, {model_id!r} not on the engine -> default {default_voice!r}")
        return default_voice

    async def _tts(self, text: str, voice: str, user: dict) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """One TTS request through the engine configured in Settings > Audio.
        Returns (base64 audio, content type, error). Mirrors the audio router's OpenAI-compatible call."""
        try:
            engine = await Config.get("audio.tts.engine")
            if engine != "openai":
                return None, None, f"Settings > Audio TTS engine is {engine!r}; this action supports the OpenAI-compatible engine only."
            payload = {
                "model": await Config.get("audio.tts.model"),
                "input": text,
                "voice": voice or await Config.get("audio.tts.voice"),
            }
            payload = {**payload, **(await Config.get("audio.tts.openai.params") or {})}
            api_key = await Config.get("audio.tts.openai.api_key")
            api_base_url = str(await Config.get("audio.tts.openai.api_base_url") or "").rstrip("/")
            if not api_base_url:
                return None, None, "Settings > Audio has no TTS API base URL."

            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            if ENABLE_FORWARD_USER_INFO_HEADERS:
                # the helper reads user.id/.name/.email/.role; __user__ is a dict
                headers = include_user_info_headers(
                    headers,
                    SimpleNamespace(**{k: (user or {}).get(k) for k in ("id", "name", "email", "role")}),
                )
            self._dbg(f"[TTS] voice={voice!r} chars={len(text)} -> {api_base_url}/audio/speech")

            session = await get_session()  # Open WebUI's pooled session; never closed here
            async with session.post(
                url=f"{api_base_url}/audio/speech",
                json=payload,
                headers=headers,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as r:
                if r.status >= 400:
                    detail = (await r.text())[:300]
                    return None, None, f"TTS returned {r.status} for voice {voice!r}: {detail}"
                audio = await r.read()
                content_type = (r.headers.get("Content-Type") or "audio/mpeg").split(";")[0].strip()
            self._dbg(f"[TTS] {len(audio)} bytes, {content_type}")
            return base64.b64encode(audio).decode("ascii"), content_type, None
        except Exception as e:
            self._dbg(f"[TTS] failed: {e}")
            return None, None, str(e)

    # ------------------------------------------------------------------ action

    async def action(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
        __event_call__: Optional[Callable[[dict], Awaitable[object]]] = None,
        __request__=None,
    ) -> dict:
        self._dbg("=== ACTION TRIGGERED ===")
        last_error = ""

        async def emit(event: dict) -> None:
            if __event_emitter__:
                await __event_emitter__(event)

        async def status(description: str, done: bool) -> None:
            await emit({"type": "status", "data": {"description": description, "done": done}})

        async def run_js(code: str) -> None:
            await emit({"type": "execute", "data": {"code": code}})

        async def ask_js(code: str):
            """Run code in the browser and return the value it returns. None = no answer: the tab is
            closed, the user moved to another chat, or the reply took too long."""
            if not __event_call__:
                return None
            try:
                return await asyncio.wait_for(
                    __event_call__({"type": "execute", "data": {"code": code}}),
                    timeout=self._BROWSER_REPLY_SECONDS,
                )
            except Exception as e:  # the timeout included
                self._dbg(f"[JS] no answer from the browser: {e!r}")
                return None

        # 1. the same gate the chat's own speaker button has
        user = __user__ or {}
        if user.get("role") != "admin" and not await has_permission(
            user.get("id"), "chat.tts", await Config.get("user.permissions")
        ):
            await status("Text-to-speech is not enabled for your account.", True)
            return body

        # 2. the button is a toggle: a press while the circle is generating or playing stops it and ends
        #    here; otherwise the browser claims this press's run id. Clips are accepted only for that id.
        run_id = uuid.uuid4().hex
        toggled = await ask_js(self._toggle_js(run_id))
        if toggled == "stopped":
            self._dbg("circle was active: stopped")
            await status("Circle audio stopped.", True)
            return body
        if toggled != "started":
            self._dbg(f"browser did not claim the run ({toggled!r}); nothing played")
            return body

        try:
            # 3. the clicked message must be a circle reply
            messages = body.get("messages", [])
            if not messages or messages[-1].get("role") != "assistant":
                self._dbg("last message is not an assistant message")
                return body
            content = messages[-1].get("content", "")
            if not isinstance(content, str) or not content:
                return body

            speakers = self._extract_speakers(content)
            self._dbg(f"{len(speakers)} speakers found")
            if not speakers:
                await status("No Circle of Speakers takes found in this message.", True)
                return body

            # 4. browser queue + loading icon
            await run_js(self._queue_init_js(idle_icon_url=ICON_DATA_URL))
            await run_js(self._if_current_run_js(run_id, "window.circleAudioQueue.updateIcon('loading');"))
            await status("Generating speech for the circle...", False)

            # 5. the engine's voices, once per press, and the default voice
            try:
                # lower-cased id -> the engine's own id (case-sensitive engines get their exact spelling back)
                engine_voices = {str(k).lower(): str(k) for k in (await get_available_voices(__request__)).keys()}
            except Exception as e:
                self._dbg(f"[VOICE] could not list engine voices: {e}")
                engine_voices = {}
            default_voice = str(await Config.get("audio.tts.voice") or "")
            self._dbg(f"[VOICE] engine lists {len(engine_voices)} voices; default {default_voice!r}")

            announcement_pause_ms = int(self.valves.pause_after_announcement_seconds * 1000)
            speaker_pause_ms = int(self.valves.pause_after_speaker_seconds * 1000)

            total = 0
            for idx, speaker in enumerate(speakers):
                name, dialogue = speaker["name"], speaker["dialogue"]
                voice = await self._resolve_voice(speaker, engine_voices, default_voice)
                await status(f"Generating audio for {name} ({idx + 1}/{len(speakers)})...", False)

                clips = []  # (text, voice, silence in ms queued after this clip)
                if self.valves.speaker_announcement:
                    announcer = self.valves.announcement_voice if self.valves.use_custom_announcer_voice else voice
                    clips.append((f"Now speaking, {name}", announcer, announcement_pause_ms))
                clips.extend((sentence, voice, 0) for sentence in self._split_into_sentences(dialogue))

                sentences_queued = 0
                for text, clip_voice, pause_ms in clips:
                    clip, mime, error = await self._tts(text, clip_voice, user)
                    if clip:
                        # the browser takes the clip only while this run is the claimed one
                        if await ask_js(self._enqueue_js(run_id, clip, mime, pause_ms)) is not True:
                            self._dbg("run stopped from the browser (or no answer); generation ends")
                            await status("Circle audio stopped.", True)
                            return body
                        total += 1
                        if pause_ms <= 0:  # only the announcement clip carries a pause
                            sentences_queued += 1
                    elif error:
                        last_error = error

                # silence between this speaker's take and the next speaker; nothing after the last speaker
                if sentences_queued and speaker_pause_ms > 0 and idx < len(speakers) - 1:
                    await run_js(
                        self._if_current_run_js(run_id, f"window.circleAudioQueue.enqueuePause({speaker_pause_ms});")
                    )

            if total == 0:
                await run_js(self._if_current_run_js(run_id, "window.circleAudioQueue.updateIcon('idle');"))
                await status(f"No audio generated. {last_error or 'The TTS engine returned nothing.'}", True)
                return body

            # still mid-queue: show 'playing'. A queue that already ran dry has set 'idle' itself.
            await run_js(
                self._if_current_run_js(
                    run_id, "if (window.circleAudioQueue.isPlaying) window.circleAudioQueue.updateIcon('playing');"
                )
            )
            await status(f"Audio ready: {total} segments queued", True)
            self._dbg("=== ACTION COMPLETE ===")
            return body
        finally:
            # generation is over (finished, failed or stopped): release the run id if it is still this one.
            # From here the queue's own state ('playing' until it runs dry) is what the next press sees.
            await run_js(f"if (window.circleAudioRun === '{run_id}') window.circleAudioRun = null;")

    # ------------------------------------------------------------------ browser side

    @classmethod
    def _toggle_js(cls, run_id: str) -> str:
        """One press. Circle generating (a run id is claimed and that run was heard from within
        _RUN_STALE_SECONDS) or playing (the queue's isPlaying, or clips still queued): stop it, return 'stopped'. Otherwise claim
        run_id for this press and return 'started'. Always returns, so the server's __event_call__ always
        gets its answer."""
        return f"""
        try {{
            const q = window.circleAudioQueue;
            const generating = window.circleAudioRun != null
                && (Date.now() - (window.circleAudioRunAt || 0)) < {cls._RUN_STALE_SECONDS * 1000};
            // clips can sit queued with isPlaying false: the chat's own read-aloud holds the shared audio element
            const playing = !!(q && (q.isPlaying || q.queue.length > 0));
            if (generating || playing) {{
                window.circleAudioRun = null;
                if (q) {{ q.stop(); }}
                return 'stopped';
            }}
            window.circleAudioRun = '{run_id}';
            window.circleAudioRunAt = Date.now();
            return 'started';
        }} catch (err) {{
            console.error('[CircleAudio] toggle failed:', err);
            return 'error';
        }}
        """

    @staticmethod
    def _if_current_run_js(run_id: str, statement: str) -> str:
        """statement, run only while run_id is still the claimed run and the queue exists."""
        return f"if (window.circleAudioRun === '{run_id}' && window.circleAudioQueue) {{ {statement} }}"

    @staticmethod
    def _enqueue_js(run_id: str, clip_b64: str, mime: Optional[str], pause_ms: int) -> str:
        """Queue one clip (and the silence after it) if run_id is still the claimed run. Returns true when
        queued, false when the run was stopped or replaced."""
        pause = f"q.enqueuePause({int(pause_ms)});" if pause_ms > 0 else ""
        return f"""
        try {{
            const q = window.circleAudioQueue;
            if (!q || window.circleAudioRun !== '{run_id}') {{ return false; }}
            q.enqueueBlobFromBase64('{clip_b64}', {json.dumps(mime)});
            {pause}
            window.circleAudioRunAt = Date.now();
            return true;
        }} catch (err) {{
            console.error('[CircleAudio] enqueue failed:', err);
            return false;
        }}
        """

    @staticmethod
    def _queue_init_js(idle_icon_url: str) -> str:
        """Browser-side queue: base64 -> blob URLs played in order on the chat's hidden audioElement,
        with the action button's icon switching idle -> loading -> playing -> idle."""
        return f"""
        if (!window.circleAudioQueue) {{
            window.circleAudioState = 'idle';
            window.circleAudioQueue = {{
                queue: [],
                blobUrls: [],
                current: null,
                pauseTimer: null,
                audio: document.getElementById('audioElement'),
                isPlaying: false,
                cachedButton: null,

                findActionButton: function() {{
                    if (this.cachedButton && document.contains(this.cachedButton)) {{
                        return this.cachedButton;
                    }}
                    const buttons = document.querySelectorAll('button[aria-label]');
                    for (const btn of buttons) {{
                        const label = btn.getAttribute('aria-label');
                        if (label && label.toLowerCase().includes('circle')) {{
                            this.cachedButton = btn;
                            return btn;
                        }}
                    }}
                    return null;
                }},

                updateIcon: function(state) {{
                    window.circleAudioState = state;
                    const button = this.findActionButton();
                    if (!button) {{
                        console.warn('[CircleAudio] Could not find action button');
                        return;
                    }}
                    const icons = {{
                        'idle': `<div class="size-4"><img src="{idle_icon_url}" alt="" class="w-4 h-4 dark:invert-[80%]"></div>`,
                        'loading': `<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                            <style>
                                .spinner_S1WN {{ animation: spinner_MGfb 0.8s linear infinite; animation-delay: -0.8s; }}
                                .spinner_Km9P {{ animation-delay: -0.65s; }}
                                .spinner_JApP {{ animation-delay: -0.5s; }}
                                @keyframes spinner_MGfb {{ 93.75%, 100% {{ opacity: 0.2; }} }}
                            </style>
                            <circle class="spinner_S1WN" cx="4" cy="12" r="3"/>
                            <circle class="spinner_S1WN spinner_Km9P" cx="12" cy="12" r="3"/>
                            <circle class="spinner_S1WN spinner_JApP" cx="20" cy="12" r="3"/>
                        </svg>`,
                        'playing': `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.3" stroke="currentColor" class="w-4 h-4">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M17.25 9.75 19.5 12m0 0 2.25 2.25M19.5 12l2.25-2.25M19.5 12l-2.25 2.25m-10.5-6 4.72-4.72a.75.75 0 0 1 1.28.53v15.88a.75.75 0 0 1-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.009 9.009 0 0 1 2.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75Z"/>
                        </svg>`
                    }};
                    if (icons[state]) {{
                        button.innerHTML = icons[state];
                    }}
                }},

                // queue items: a blob URL (string) = a clip, a number = milliseconds of silence
                enqueue: function(item) {{
                    this.queue.push(item);
                    if (!this.isPlaying && this.audio.paused) {{
                        this.next();
                    }}
                }},

                enqueuePause: function(ms) {{
                    if (ms > 0) {{
                        this.enqueue(ms);
                    }}
                }},

                enqueueBlobFromBase64: function(base64Data, mimeType) {{
                    try {{
                        const byteCharacters = atob(base64Data);
                        const byteNumbers = new Array(byteCharacters.length);
                        for (let i = 0; i < byteCharacters.length; i++) {{
                            byteNumbers[i] = byteCharacters.charCodeAt(i);
                        }}
                        const blob = new Blob([new Uint8Array(byteNumbers)], {{ type: mimeType || 'audio/mpeg' }});
                        const blobUrl = URL.createObjectURL(blob);
                        this.blobUrls.push(blobUrl);
                        this.enqueue(blobUrl);
                    }} catch (err) {{
                        console.error('[CircleAudio] Failed to create blob from base64:', err);
                    }}
                }},

                next: function() {{
                    // a silence is running: only its own timer (which clears pauseTimer first) may advance.
                    // audioElement is shared with the chat's own speaker button, whose 'ended' also lands here.
                    if (this.pauseTimer) {{
                        return;
                    }}
                    if (this.queue.length === 0) {{
                        this.isPlaying = false;
                        this.updateIcon('idle');
                        return;
                    }}
                    const item = this.queue.shift();
                    this.isPlaying = true;
                    if (typeof item === 'number') {{
                        this.current = null;
                        this.pauseTimer = setTimeout(() => {{
                            this.pauseTimer = null;
                            this.next();
                        }}, item);
                        return;
                    }}
                    this.current = item;
                    this.updateIcon('playing');
                    this.audio.src = this.current;
                    this.audio.play().catch(err => {{
                        console.error('[CircleAudio] Playback error:', err);
                        this.next();
                    }});
                }},

                stop: function() {{
                    clearTimeout(this.pauseTimer);
                    this.pauseTimer = null;
                    this.audio.pause();
                    this.audio.currentTime = 0;
                    this.audio.src = '';
                    this.queue = [];
                    this.current = null;
                    this.isPlaying = false;
                    this.blobUrls.forEach(url => URL.revokeObjectURL(url));
                    this.blobUrls = [];
                    this.updateIcon('idle');
                }}
            }};

            window.circleAudioQueue.audio.addEventListener('ended', function() {{
                window.circleAudioQueue.next();
            }});
            window.addEventListener('beforeunload', function() {{
                if (window.circleAudioQueue) {{
                    window.circleAudioQueue.blobUrls.forEach(url => URL.revokeObjectURL(url));
                }}
            }});
        }}
        """
