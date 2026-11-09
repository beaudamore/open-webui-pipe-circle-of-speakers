# Circle of Speakers Orchestration Cleanup Handoff

## Goal

Clean up the live `circle_of_speakers_blueprint.py` orchestration path so that:

- Open WebUI owns the orchestration prompt/configuration.
- The Python pipeline only sends data to the orchestration model.
- The Python pipeline validates model output against the tag-filtered cast.
- No hard-coded retry/correction prompting exists in the pipeline.
- No fuzzy or partial speaker-name matching is allowed.

## Intended Architecture

```text
Open WebUI orchestration model:
  Owns the system prompt and all speaker-selection rules.

Pipeline:
  Fetches models.
  Filters models by SPEAKER_TAGS.
  Sends AVAILABLE CAST + RECENT CONVERSATION + USER'S MESSAGE to the orchestration model.
  Parses the final response.
  Accepts exact speaker names only from the active cast.
```

## File Touched

- `pipelines/blueprints/circle_of_speakers_blueprint.py`

## Changes Made

### 1. Removed Dead/Confusing Orchestration Valves

Removed these valves from `Pipeline.Valves`:

```python
ORCHESTRATION_MAX_RETRIES
ORCHESTRATION_MIN_SPEAKERS
ORCHESTRATION_MAX_SPEAKERS
```

Reason: these supported a hard-coded retry/correction flow inside Python. That conflicted with the desired design where the Open WebUI orchestration model owns prompting and behavior.

### 2. Updated `ORCHESTRATION_MODEL_NAME` Description

Changed the description so it no longer implies the pipeline fetches/formats the orchestration model prompt with placeholders.

Desired description:

```python
ORCHESTRATION_MODEL_NAME: str = Field(
    default="Intelligent Speaker Ordering",
    description="Model name to use for intelligent speaker orchestration. "
                "The model's OpenWebUI system prompt defines the selection rules; "
                "the pipeline sends the available cast, conversation context, and user message.",
)
```

### 3. Deleted `_get_orchestration_prompt_template()`

Removed the unused helper:

```python
def _get_orchestration_prompt_template(self) -> str:
    ...
```

Reason: it was dead code. The live orchestration path does not call it. Current Open WebUI applies the selected model's saved `params.system` prompt automatically when calling `/api/v1/chat/completions`, so the pipeline should not fetch/format that prompt itself.

### 4. Removed Hard-Coded Correction Retry Prompt

Removed these local helper functions and retry loop from `_analyze_user_message_for_speaker_order()`:

```python
build_correction_messages(...)
merge_valid_speakers(...)
while retry_count < max_retries ...
```

Reason: the retry prompt hard-coded selection instructions like "copy 3-5 exactly," which conflicted with the Open WebUI-configurable orchestration prompt and could make hallucination/failure behavior worse.

### 5. Ignored Reasoning/Thinking Marker Chunks During Orchestration

Changed orchestration response collection so marker chunks are not appended into the parsed response.

Desired behavior:

```python
for chunk in self._query_openwebui_model_streaming(ordering_model_id, messages):
    chunk_count += 1
    if chunk and not chunk.startswith("ERROR"):
        if chunk.startswith("__"):
            non_content_chunks.append(chunk[:200])
            continue
        response += chunk
```

Note: in the actual file, preserve the existing error marker check. The important behavior is that `__REASONING_CHUNK__`, `__THINKING_TAG_CONTENT__`, `__THINKING_TAG_CLOSE__`, and `__USAGE_DATA__` marker chunks are ignored for orchestration parsing.

Reason: reasoning models can emit reasoning/thinking/usage markers. Those should not be parsed as speaker selections, even if the orchestration prompt includes `/nothink`.

### 6. Removed Fuzzy Matching

Removed partial/fuzzy matching checks such as:

```python
name_norm in speaker_name_norm
speaker_name_norm in name_norm
speaker_name_norm.split(",")[0].strip() == name_norm
speaker_name_norm.split()[0] == name_norm
```

Replaced with exact active-cast name matching only:

```python
speakers_by_name = {speaker["name"]: speaker for speaker in active_speakers}

for name in ordered_names:
    speaker = speakers_by_name.get(name.strip())
    if speaker:
        ...
    else:
        invalid_names.append(name)
```

Reason: the prompt requires exact names copied from `## name:`. Fuzzy matching allowed bad outputs like `John`, `Paul`, aliases, or partial historical names to silently pass.

### 7. Simplified Final Orchestration Handling

Current desired flow:

```python
response = collect_ordering_response(ordering_messages)
ordered_names = parse_ordered_names(response)
ordered_speakers, invalid_names = match_ordered_names(ordered_names)

if invalid_names:
    self._dbg_step(f"  Ignoring invalid orchestrator names: {invalid_names}")

if not ordered_speakers:
    raise ValueError(
        "Orchestration returned no valid speakers from the available cast."
    )

return ordered_speakers
```

Reason: if the model returns invented names, ignore them. If it returns no valid names, fail clearly instead of pretending the response was good.

## Expected Behavior After Cleanup

- `SPEAKER_TAGS` still filters the available model list before orchestration.
- The orchestration model receives:
  - `AVAILABLE CAST`
  - `RECENT CONVERSATION`
  - `USER'S MESSAGE`
  - memory context if present
- The Open WebUI orchestration model system prompt controls selection behavior.
- The pipeline only accepts exact names from the filtered `active_speakers` list.
- Invalid/hallucinated names are ignored.
- If all returned names are invalid, the pipeline raises a clear orchestration error.

## Verification Run

After edits, run:

```powershell
Set-Location C:\Source\circle-of-speakers-pipeline
python -m py_compile pipelines\blueprints\circle_of_speakers_blueprint.py
```

Expected result:

```text
(no output)
```

No output from `py_compile` means the file has no Python syntax errors.

## Important Notes

- Do not reintroduce hard-coded orchestration prompts in Python.
- Do not reintroduce fuzzy matching unless explicitly desired.
- Do not use `_get_orchestration_prompt_template()` unless changing architecture so the pipeline owns prompting again.
- Current desired architecture is Option A:
  - Open WebUI model prompt owns orchestration rules.
  - Pipeline sends data and validates exact cast membership.
