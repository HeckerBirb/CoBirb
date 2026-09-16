# Images

Show the model a screenshot. Interactive only.

## Attach one

```
/image docs/images/cobirb.png what is this?
```

Path first, then your message. Or attach and type separately:

```
/image docs/images/cobirb.png
what is wrong with this layout?
```

Quote a path with spaces: `/image "my screenshot.png" what is this?`

Relative paths resolve against the working directory, not wherever you launched CoBirb from.

PNG, JPEG, GIF, WebP and BMP. No size limit.

## Your model has to be able to see

Most local models can't. CoBirb asks the endpoint, and says so if the answer is no:

```
📎 shot.png queued — the current model doesn't support vision, so this will be
understood as text only. Switch with /model, or send anyway.
```

Vision-capable models include the llava, qwen-vl and llama-vision families. `/model` lists what
your endpoint has.

## Where it goes

Inside the [session](sessions.md), encrypted with everything else. Resume the session later and
the model can still see the image.

Without a session, the image is sent and not kept.

`/export` writes a `📎 filename` marker, never the bytes.
