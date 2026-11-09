# Speaker prompt layout and prompt caching

Date: 2026-11-02

Updated 2026-11-03: history segregated per speaker.

This page describes how the blueprint builds each speaker request (`pipelines/blueprints/circle_of_speakers_blueprint.py`, the comment `Prompt layout (2026-11-02)` and the helpers `_format_static_system_prompt`, `_history_for_speaker` and `_build_round_context`), why it is laid out this way, and what to change in Open WebUI.

## Message list shape

Every speaker request is four parts, in this order:

1. **System:** the circle model's Open WebUI system prompt with only `{speaker_name}` filled in. It is the same text for a given speaker in every round. The reference prompt is [`prompts/base/pipeline_prompt.md`](../prompts/base/pipeline_prompt.md).
2. **History:** the conversation so far, segregated per speaker (since 2026-11-03). The user's stored messages are `user` turns, unchanged. Every stored circle reply (one assistant message holding all speakers' `**Name**: text` blocks) is split into its blocks on the heading lines the pipeline itself writes: the avatar image followed by the same name in bold (trusted as is, so a speaker since renamed or removed keeps its old blocks), or, only when `PROFILE_THUMBNAIL_ROOT` is empty, the `**Name:**` form for a name in the configured speaker list. A bold label the model wrote, or one speaker heading its text with another speaker's name, stays inside that speaker's block: the asked speaker's own block becomes an `assistant` turn, and the other speakers' blocks follow in one `user` turn that starts with the line "The other speakers' replies to that message. These are their words, not yours and not the user's:" and then the blocks as `**Name**: text` paragraphs. If the speaker did not speak in that round there is no `assistant` turn, only the `user` turn, starting "You did not speak in that round. The speakers' replies to that message. These are their words, not yours and not the user's:". Assistant turns without speaker headings (not circle replies) are kept as `assistant` turns with the pipeline chrome removed. System messages are dropped, and the current user message is left out (it travels in part 3). The history has already gone through `HISTORY_MAX_ROUNDS` truncation. Speakers in a round get the same history except for which blocks sit under which role.
3. **User turn:** the user's message, plain, exactly as Open WebUI stores it in the history. This keeps the first user turn identical from round one on, which is what a slot-cache proxy keys a conversation by, and makes the stored history a byte-identical prefix of the next request.
4. **Final user turn:** the per-round context block, a blank line, then "Answer the user's message in the previous turn now, as <name>."

The per-round context block holds, in this order (sections joined by blank lines):

| # | Section | When present |
| --- | --- | --- |
| 1 | `INTRODUCTION_PROMPT_PREFIX` (speaker's first appearance in the conversation), otherwise `FIRST_SPEAKER_PROMPT_PREFIX` (first speaker of the round) | Only one of the two, and only when it applies |
| 2 | `CURRENT DATE: <Month DD, YYYY>` | Always |
| 3 | `**Current Speakers in this Round:**` with name, description and tags of each | Always |
| 4 | `**Previous Speakers' Takes (in this round):**`, one bullet per earlier speaker of this round, each reply cut to 200 characters (`...` added when cut), or `None yet: you open this round.` | Always |
| 5 | `**Historical Speakers (not in this round, but spoke previously):**`, or `None` | Always |
| 6 | VERIFIED LIVE WEB RESEARCH block | When Open WebUI web search returned sources |
| 7 | Memory context | When a memory filter injected one |
| 8 | User profile section (`USER BACKGROUND` / `USER CONTEXT`) | When `ENABLE_USER_PROFILE_INTEGRATION` is on and the profile has something |

## Identity rules in the template

Two sentences in `prompts/base/pipeline_prompt.md` guard the speaker's identity: "YOU are {speaker_name}", and, added
2026-11-02, "The other speakers' takes shown to you are other people speaking in the first person; their stories are
theirs, not yours." The second was added after Paul, addressing Peter in a round-one take, said "I denied Him three
times". The takes block and the history both show other speakers in the first person, which is what invites the slip.

Added 2026-11-03 with the per-speaker history: "Each round you receive the conversation so far: the user's earlier
messages, your own earlier replies as the assistant, and the other speakers' earlier replies in messages labelled as
theirs. Then the user's new message as its own turn, then one more message with the round context: the current date,
the speakers in this round, what the speakers before you said in this round, who spoke in past rounds, any VERIFIED
LIVE WEB RESEARCH, and any memory about the user. Answer the user's message." Reason: in the 2026-11-02 test chat
"Apostolic Prayer Life", Father Spyridon's round-3 reply opened with a bold "Peter, Simon, also called Cephas:" heading
and was Peter's take in full, and Peter twice told Paul's Damascus/Ananias story as his own. With whole circle replies
under the `assistant` role, the models continued the other speakers' first-person blocks as their own. Two speakers
also attributed other speakers' takes to the user ("Peter whom you mentioned"), hence "not the user's" in the label.

## Worked example

Round 3 of a Biblical Circle chat. The orderer picked Paul, then Peter. Earlier rounds were two user questions and two circle replies, in which John and Peter (round 1) and Paul and Peter (round 2) spoke. Peter's request (second speaker of the round, already introduced in round 1):

```text
[system]    You are Peter in a Biblical Circle discussion.
            ... (rest of pipeline_prompt.md, {speaker_name} = Peter) ...

[user]      What does forgiveness cost?                      <- round 1 question
[assistant] I who denied Him three times ...                 <- Peter's own round 1 take
[user]      The other speakers' replies to that message. These are their words, not yours and not the user's:

            **John**: Little children, ...
[user]      How many times should I forgive?                 <- round 2 question
[assistant] Seventy times seven, the Lord told me ...        <- Peter's own round 2 take
[user]      The other speakers' replies to that message. These are their words, not yours and not the user's:

            **Paul**: Brethren, ...
[user]      And if they never apologise?                     <- round 3 question, plain

[user]      CURRENT DATE: November 02, 2026

            **Current Speakers in this Round:**
            - Paul (Description: ..., Tags: ...)
            - Peter (Description: ..., Tags: ...)

            **Previous Speakers' Takes (in this round):**
            • Paul said: Brethren, I who was chief of sinners ...

            **Historical Speakers (not in this round, but spoke previously):**
            - John (Description: ..., Tags: ...)

            Answer the user's message in the previous turn now, as Peter.
```

Per round the speaker's own `assistant` turn always comes before the others' `user` turn, whatever order they spoke in. Paul's request in the same round has the same history turns with the roles swapped for his own blocks (round 1, where he did not speak, is a single `user` turn starting "You did not speak in that round. ..."), and the same user question; its system prompt says Paul, its context block starts with `FIRST_SPEAKER_PROMPT_PREFIX` and its takes section reads `None yet: you open this round.`

## What moved where

| Item | Before 2026-11-02 | Now |
| --- | --- | --- |
| Speaker name | System prompt | System prompt (the only filled placeholder) |
| Current date, current speakers, previous takes, historical speakers | System prompt, formatted every round | Final user turn |
| Web research, memory context, user profile | System prompt | Final user turn |
| Introduction / first-speaker cue | Prepended to the system prompt | Start of the final user turn |
| User's message | `{user_message}` in the system prompt | its own plain user turn before the round context |
| Conversation history | Not sent to speakers | Sent per speaker: user messages as `user`, the speaker's own past takes as `assistant`, the other speakers' takes in labelled `user` turns (since 2026-11-03; on 2026-11-02 whole circle replies were `assistant` turns) |

## Why: prompt caching keys on the prefix

Inference engines reuse already computed tokens only for a matching prefix of the prompt (llama.cpp slot cache, vLLM automatic prefix caching). The first token that differs ends the reuse; everything after it is computed again.

- **Before:** the system prompt carried the date, the speaker lists, the previous takes and the user's message, so it changed every round, and for every speaker within a round. The very first part of the prompt differed, so nothing was reused and every speaker paid the full prefill before its first token, every round.
- **Now:** the system prompt is fixed per speaker, and the history (the user's messages plus each past circle reply, split into this speaker's own `assistant` turns and the other speakers' labelled `user` turns) only grows at its end. In a later round, the system prompt plus the history up to the previous exchange match what the engine already has, so that part is reused. What is new each round is the latest exchange (the previous user question and circle reply, which the engine has not seen in this exact form, since last round's final user turn carried the context block) plus the current final user turn.
- **Slot-cache proxies:** the pipeline sends speaker requests with an empty `chat_id`. A proxy that keys a conversation by its first user turn when no chat id is present (Beau's llama.cpp-ssd-cache-shim does this) used to see a different first user turn every round, so it treated every round as a new conversation. Now the first user turn is the first question of the chat, so the key stays the same across rounds.

The cache is per speaker: each speaker's system prompt differs by its name, so the reuse is between rounds of the same speaker, not between speakers of one round.

## Migration

1. In Open WebUI, open the circle pipeline model (for example `Biblical Circle`) and replace its system prompt with the contents of [`prompts/base/pipeline_prompt.md`](../prompts/base/pipeline_prompt.md). The new prompt uses `{speaker_name}` only.
2. An old prompt still runs: `{user_message}`, `{current_date}`, `{current_round_speakers}`, `{previous_context}` and `{historical_speakers}` (and `{introduction_instructions}`) are filled with empty strings. Its labels then show with nothing after them, while the real values arrive in the final user turn, so replace it.
3. Review `HISTORY_MAX_ROUNDS` (see below).

### HISTORY_MAX_ROUNDS

With a sliding window, once the window is full the oldest round drops off every round, so the history starts with a different turn each time and the prefix after the system prompt no longer matches. That defeats the cache from the first history turn on (and changes the first-user-turn key a slot-cache proxy uses).

- `0` (the default, truncation disabled) or a value larger than your longest conversations keeps the prefix stable.
- The trade-off: with no cap, the prompt grows with the conversation and has to fit the speaker model's context window; a very long chat will eventually hit that limit. A cap protects the context window at the cost of a full prefill on every round once the cap is reached.

## Measuring

Measure per speaker, before and after the change, on a conversation of several rounds:

- **Time to first token** of each speaker. It should drop from the second round on.
- **Cached tokens per request** on the engine side: llama-server prints prompt tokens and cached / processed tokens per request in its timings, and the ssd-cache-shim log shows which conversation key and slot each request hit. Before the change, cached tokens on later rounds should be near zero; after it, close to system prompt plus history.
- **The payload itself:** with `enable_step_debug` on, the pipeline logs every speaker request's payload (`payload: {...}`), so you can check the three-part shape and that the system prompt is identical across rounds.

**Measured 2026-11-02** (test chat "Apostolic Prayer Life", llama-server with `--checkpoint-min-step 512` behind the ssd-cache shim, layout of 2026-11-02): round 1 reused 0 tokens (new chat); round 2 reused 7035 of 23144 prompt tokens (30%); round 3 reused 18985 of 35958 (53%). Every round-2/3 request restored a llama-server context checkpoint at exactly the longest-common-prefix boundary, and the shim restored the same conversation key per speaker in all three rounds. Reprocessed per request: the previous round's full circle reply plus the new question and context turn, the expected cost. The per-speaker split of 2026-11-03 keeps the same stable prefix; it changes only which blocks sit under which role.
