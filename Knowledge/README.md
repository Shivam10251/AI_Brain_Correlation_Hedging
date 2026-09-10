# Knowledge

Reference material for the MT5 trading system: the documents the
strategy came from, and the written explanations of how the thing
actually works.

This is the folder to read to *understand* the system. `Specs/` is the
folder to read to *build* it — the plan, the phase gates, and the risk
profiles the code loads at runtime.

```
Knowledge/
  source/       the original briefs, as PDFs. Append-only.
  explainers/   written answers to "what is happening?" - generated on request
```

## `source/` — append-only

| Document | What it is |
|---|---|
| `Trading_Strategy_Explained.pdf` | The original strategy brief |
| `Trading_Rules_The5ers_100K_Summer_2Step_and_LucidFlex_25K.pdf` | The prop firm's rulebook. Every number in `Specs/risk/the5ers-100k.yaml` traces here |
| `Fable_5_1_Trading_System_Master_Prompt.pdf` | The master prompt the AI layer was derived from |

**Never edit these.** They are the record of what was originally asked
for, and half the value of keeping them is that they cannot drift to
match what was later built. Superseding one means adding a new file and
adding a row to the table above.

Moved here from `Specs/strategy/source/` so that reference material and
the build plan stop sharing a folder.

## `explainers/` — generated

When you ask what is happening — the current state, how a subsystem
works, why something behaves the way it does — the answer gets written
to a dated Markdown file in `explainers/` rather than only appearing in
a terminal that scrolls away.

Naming: `YYYY-MM-DD-short-slug.md`.

Each one records the commit it describes, so a stale explainer is
visibly stale rather than quietly wrong. They are snapshots, not living
documents: when something changes materially, write a new one instead of
editing the old.

| File | Subject |
|---|---|
| `2026-09-10-where-the-project-stands.md` | Full state of phases 0-5 at commit `6219029` |
