# Circle of Speakers — Open WebUI Pipe Function

A streaming Open WebUI **Pipe Function** that turns any tagged set of Open WebUI models into a
**multi-speaker conversation**: one prompt, several distinct voices answering in turn, with intelligent
ordering, one-time introductions and user-profile-aware responses.

One installed Function is one circle. Its cast is the `SPEAKER_TAGS` valve (`biblical`, `stoic`, …;
empty = every speaker). For another circle, install the same file again under another Function id and
title with its own valves, exactly as each shim file was its own pipeline before.

This is the Functions port of [circle-of-speakers-pipeline](https://github.com/beaudamore/circle-of-speakers-pipeline).
The Open WebUI Pipelines server that repo targeted is [archived](https://github.com/open-webui/pipelines);
everything it did now runs inside Open WebUI itself as a Function. No extra container, no mount, no
blueprint folder.

## Layout

```text
.
├── README.md
├── LICENSE
├── pipe/
│   └── circle_of_speakers.py      The Function (one file = one circle)
├── action/
│   └── play_circle_audio.py       Message action: read a circle reply aloud, one voice per speaker
├── docs/
│   ├── prompt-layout.md           Speaker request layout and prompt caching
│   ├── orchestration-cleanup-handoff.md   Why ordering rules live in Open WebUI
│   ├── user-profile-integration.md
│   └── web-search-context.md
└── prompt/
    ├── base/                      Speaker-ordering prompt + per-speaker circle prompt
    ├── biblical/                  38 biblical speaker system prompts
    └── shakespeare/
```

`docs/` and `prompt/` are carried over from the pipeline repo unchanged; they describe behaviour that
did not change in the port.

## What changed from the Pipelines version

| Pipelines server (old) | Function (this repo) |
| --- | --- |
| Three files: streaming blueprint, circle blueprint, one shim per circle, imported via `/app/blueprints` | One file, `pipe/circle_of_speakers.py`. Functions cannot import sibling files. |
| One shim file per circle, each importing the blueprints | One Function per circle, each a copy of this one file installed under its own id and title. The model id is the Function id. |
| Tag query fixed in each shim file | `SPEAKER_TAGS` valve on each installed Function (empty = everyone, the old Dynamic Circle). |
| HTTP loopback to `/api/v1/models/list` and `/api/v1/chat/completions` with an admin JWT (`OPENWEBUI_ENDPOINT`, `OPENWEBUI_API_KEY`, `USER_AGENT` valves), synchronous `requests` streaming | No HTTP. The speaker registry is read from Open WebUI's model store and the orderer and speaker calls go through Open WebUI's own `generate_chat_completion`, as the chatting user, streamed by an async generator. No endpoint, key or user-agent valves; nothing to re-paste after a container update. Model access is checked per user, so users need read access to the speaker models and the ordering model. |
| `on_startup` / `on_valves_updated` refilled the speaker cache | No such hooks exist for Functions. The cache is filled on first use and refilled whenever the valves change. Adding or renaming speaker models in Open WebUI still needs a valve save or a Function toggle to be picked up. |
| Errors raised out of the pipeline | Configuration errors (no API key, no speakers for the tag, orchestration failure) are shown in the chat as `❌ PIPELINE STOPPED: …`. |

The in-process call is Open WebUI's `generate_chat_completion`, the same function the old
`/api/v1/chat/completions` route ended in, but without the route's middleware. For the orderer and
speaker calls that means: the speaker model's own system prompt and parameters still apply; inlet and
outlet filters (user memory, speech tags, usage tracking) no longer run on those nested calls; a speaker
model's attached knowledge is no longer retrieved for it; the admin-wide default model parameters are
not merged in. Tools were never forwarded by the old route either. Errors from a speaker or the orderer
(model not found, no access for this user, provider error) are shown in the chat instead of leaving the
turn blank.

Everything else is the blueprint code as it was: prompt layout, per-speaker history segregation, the
orchestration contract, avatars, thinking-tag handling, usage aggregation, web search and memory
context extraction.

## Install

1. Open WebUI → Admin Panel → Functions → **+** → paste `pipe/circle_of_speakers.py`. Set the id and
   title to the circle you are installing (`circle_of_speakers` / "Circle of Speakers", or
   `stoic_circle` / "Stoic Circle"). Save.
2. Enable it. One model with that title appears in the picker.
3. Open the Function's valves and set `SPEAKER_TAGS` to the circle's tag query (`,` = OR, `+` = AND,
   case-insensitive; empty = every tagged speaker).
4. Admin Panel → Models → open that model and set its **System Prompt** to
   [`prompt/base/pipeline_prompt.md`](prompt/base/pipeline_prompt.md). The Function reads this prompt
   from the request and fills in `{speaker_name}`. Without it the chat stops with "The pipeline's
   System Prompt is empty".
5. Make sure an **Intelligent Speaker Ordering** model exists (any chat model with the system prompt from
   [`prompt/base/intelligent_speaker_ordering_v2.md`](prompt/base/intelligent_speaker_ordering_v2.md)),
   and that the speaker models carry the circle's tag and have a description. Users who chat with a
   circle need read access to those speaker models and to the ordering model, because the Function
   calls them as that user.

If you are coming from the Pipelines server: the old model rows (`biblical_circle` and friends) override
a model that no longer exists once the Pipelines connection is removed. Move their system prompt and
avatar to the new Function's model, then delete the old rows and the Pipelines connection.

## Play Circle Audio (action)

[`action/play_circle_audio.py`](action/play_circle_audio.py) is an Action Function. Installed and
enabled, it adds a button under every assistant message. On a Circle reply it splits the message into
the speakers' takes, sends each take to the TTS engine configured in **Settings → Audio** sentence by
sentence, and plays the clips in order in the browser. The voice for a speaker is resolved in this order:
the speaker model's **Voice** field (model editor, the same field the chat's speaker button uses), else
the speaker's model id when the TTS engine lists a voice of that name (Open WebUI's own voices lookup,
fetched once per press), else the default voice from Settings → Audio. The model id comes from the
avatar URL the pipe writes. Optional "Now speaking, &lt;name&gt;" announcements use the
`announcement_voice` valve. Nothing is written to disk; audio travels to the browser as base64.

Pauses are two valves, in seconds, `0` = none: `pause_after_announcement_seconds` (default `0.8`) is the
silence between the announcement and the speaker's first sentence, and `pause_after_speaker_seconds`
(default `1.5`) is the silence after a speaker's last sentence, before the next announcement or speaker.
There is no pause after the last speaker. After updating the action, reload the chat tab once so the
browser picks up the new playback queue.

The button is a toggle (3.2). A press while the circle is generating or playing stops playback, empties
the queue and ends the generation at its next clip; the sentence already at the TTS engine finishes
there and is discarded. The next press starts the reply again from the first speaker. There is no
pause/resume. A press on any Circle reply's button stops whatever is playing. Generation also ends when
the browser stops answering for 20 seconds (tab closed, or another chat opened). A generation the browser
has not heard from for 60 seconds no longer counts as running, so the next press starts a fresh one.

Requirements: Settings → Audio TTS engine set to the OpenAI-compatible engine with a base URL. Give
each speaker model a Voice in the model editor, or name the engine's voice clips after the model ids;
speakers with neither fall back to the default voice. The `announcement_voice` valve must exist on the
engine. Users need the `chat.tts`
permission, as for the chat's own speaker button. Port history and fixes against the 2.2 action are in
the file header.

## How it works

Each "speaker" is an Open WebUI model: a base model with a system prompt, a LoRA, anything registered.
The Function filters the registry by tag, sends the cast, recent conversation and the user's message to
the ordering model, and streams the chosen speakers one after another through Open WebUI. The ordering
rules live in the ordering model's Open WebUI system prompt, not in Python; the Function only validates
that the names it gets back exist in the filtered cast.

Each speaker request is built as system prompt (static per speaker), conversation history (segregated so
a speaker only ever sees its own words under the assistant role), the user's message as its own turn,
then a per-round context block. Details and the reasoning behind the layout:
[`docs/prompt-layout.md`](docs/prompt-layout.md).

## Configuration cheat sheet

| Valve | Default | What it does |
| --- | --- | --- |
| `SPEAKER_TAGS` | `""` | Tag query for this circle's cast (`,` = OR, `+` = AND). Empty = every speaker. |
| `ORCHESTRATION_MODEL_NAME` | `Intelligent Speaker Ordering` | The Open WebUI model that decides turn order. |
| `DECISION_CONTEXT_DEPTH` | `4` | How many recent messages the orderer sees. |
| `HISTORY_MAX_ROUNDS` | `0` | Rounds of history kept (`0` = all). Keep `0` or large for prompt caching. |
| `ENABLE_DYNAMIC_INTRODUCTIONS` | `true` | One-time intro the first time a speaker appears in a chat. |
| `ENABLE_USER_PROFILE_INTEGRATION` | `true` | Weave personal info from the user's system prompt into responses. |
| `PROFILE_THUMBNAIL_ROOT` | `/api/v1/models/model/profile/image?id={model_id}&width={size}` | Avatar URL template. Empty = names only. |
| `PROFILE_IMAGE_SIZE` | `60` | Pixel width filled into `{size}`. |
| `FIRST_SPEAKER_PROMPT_PREFIX` / `INTRODUCTION_PROMPT_PREFIX` | (text) | Cues placed at the top of the per-round context block. |
| `enable_full_debug` / `enable_step_debug` | `false` | Verbose logging to the Open WebUI container log. |

There is no endpoint or API key valve: the Function runs inside Open WebUI and uses its internals.

## Status

Ported 2026-11-08 from circle-of-speakers-pipeline at commit `980177b`. Not yet verified in a live chat.

## Author

Built by [Beau D'Amore](https://damore.ai). License: MIT.
