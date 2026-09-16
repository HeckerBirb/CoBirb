# Memory

Facts CoBirb carries into a conversation — "I use tabs", "deploys go through make release".
They live in **catalogues**: named lists of facts you load when you want them.

Interactive only.

## Save a fact

```
/remember the test command here is pytest -q
```

You get a picker. Choose a catalogue; the fact is written and the catalogue stays loaded.

## Manage catalogues

```
/memories
```

- **Select a catalogue** — loads it, or unloads it if it's already loaded.
- **New…** — name it, and optionally give it a password.
- **Rename**, **Delete** — act on the highlighted one.

Loaded catalogues are listed first, above a separator.

## Public vs encrypted

Leave both password fields blank and the catalogue is **plain text** — readable in any editor,
`chmod 0600`. Give a password and it's encrypted with the same cipher sessions use.

A `public` catalogue always exists.

```
~/.cobirb/memories/
  public.md        ← plain
  work.md          ← plain
  personal.md.enc  ← password
```

A plain catalogue is just a bullet list. Edit it by hand if you like:

```markdown
- The user prefers tabs over spaces.
- Deploys go through `make release`, never `npm publish`.
```

## Loading

Nothing is loaded automatically, even `public`. A catalogue only reaches the model once you
load it, and only for that session. Load several and the model sees all of them; if two
contradict each other, that's the model's problem to sort out, not CoBirb's.

Worker agents in a [flock](flock.md) never see memory.
