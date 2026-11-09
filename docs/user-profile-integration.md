# User Profile Integration

The active Circle of Speakers blueprint can derive limited personal context from system messages and add it to every selected speaker's prompt.

## Configuration

`ENABLE_USER_PROFILE_INTEGRATION` is the only profile valve. It defaults to `true`. Disable it in the pipeline's Open WebUI valves to prevent profile hints from being added to speaker prompts.

There is no configurable profile separator and no `SUGGEST_PROFILE_FORMAT` valve.

## Recognized Hints

The pipeline scans system-message lines for these phrases:

```text
i am
my name
i work
i study
i like
i enjoy
my profession
my job
my role
my interests
my goals
i struggle with
i need help
my challenge
my background
```

It uses at most the first three matching lines. Matching is case-insensitive, and the extracted hints are shared with all speakers selected for the response.

For example:

```text
My name is Sarah Chen.
I work as a software engineer and recently became a team lead.
My goals include improving delegation and reducing technical debt.
```

The pipeline also looks for `my name is`, `i am`, or `call me` to derive a display name. It takes the first word after the first matching phrase.

## Data Handling

The pipeline reads the system messages supplied with each request. It does not maintain a separate profile store. Storage and retention of the underlying system prompt are controlled by Open WebUI.
