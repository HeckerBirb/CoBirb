# Sessions

A session is a conversation saved to disk, encrypted. Off by default — without one, nothing is
written.

## Start one

```bash
cobirb -w            # prompts for a password, no echo
cobirb -w hunter2    # or give it inline (visible in shell history)
```

Creates `~/.cobirb/sessions/session-<timestamp>.json`.

## Come back to it

```bash
cobirb --session ~/.cobirb/sessions/session-20260912-185817.json -w
```

Or just pick up where you left off:

```bash
cobirb --continue
```

That reopens whichever session you touched most recently and asks for its password — no path to
remember. Give the password inline with `-w hunter2` if you're scripting it.

Or open the **Sessions** tab in the app and pick one — it lists what's there and can resume,
branch or start a new one.

## Branch one

Try a different direction without disturbing the original:

```bash
cobirb --branch ~/.cobirb/sessions/old.json               # the whole thing
cobirb --branch ~/.cobirb/sessions/old.json --branch-at 4 # only turns 0–4
```

The source is only ever read.

## Read one

```bash
cobirb --export session.md
```

Or `/export` in the app. **The export is plaintext** — that's the point of asking for one. The
session itself stays encrypted.

## Start over without losing it

```
/clear
```

Clears the screen and the model's context together. Your next message starts a fresh
conversation, and the model is no longer answering from what came before.

**Nothing is deleted.** `/clear` records a marker turn — a point in the history, the way
committing an emptied file is a new commit rather than a rewrite of the ones before it. The
earlier turns stay in the session file, so it remains a complete record of what actually
happened, and `/export` still writes all of it out.

Reopening a cleared session picks up from the marker: you see what you saw when you left, not the
whole conversation again.

## What's in the file

AES-256-GCM over a key derived from your password with scrypt. The password is never stored.
Every turn carries a content hash, so editing the file is detected when it's reopened.
Attached images ride inside the same encrypted blob.

Lose the password and the session is gone. There is no recovery.
