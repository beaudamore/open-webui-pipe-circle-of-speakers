# Web Search Context

The active Circle of Speakers blueprint consumes web-search context that Open WebUI has already inserted into system messages. The pipeline does not run searches, choose URLs, or call retrieval endpoints itself.

## Flow

1. Open WebUI performs web search outside the Circle pipeline.
2. Open WebUI supplies results in a system message containing `<context>` and `<source>` elements.
3. The pipeline reads the first matching context block.
4. Each source is collapsed to one line and truncated to 300 characters.
5. The formatted research goes to the orchestrator, and to each speaker inside the per-round context block of the final user turn (never into the system prompt, which stays a stable prompt-cache prefix; since 2026-11-02).
6. Every selected speaker receives the same research context.

When matching context is found, the pipeline emits this message before selecting speakers:

```text
Web search data detected - enhancing speaker responses
```

## Configuration

There are no Circle-specific web-search valves. In particular, the active pipeline has no `SPEAKER_X_WEB_SEARCH` controls and does not perform separate searches for individual speakers. Web-search availability and retrieval behavior are configured in Open WebUI.
