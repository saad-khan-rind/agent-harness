# Worked example

A level-three file: it exists so the skill body can stay short, and it is
only ever loaded if the body tells the agent to read it.

```markdown
# Carol - Week 2, Monday

## What I did today

- Added long-term memory as markdown files under a third sandbox root, with
  the core file injected per run and note filenames advertised for
  just-in-time reading.
- Reshaped the `MemoryStore` seam from key-value to file-shaped and updated
  the seam test in the same commit.
- Added the cross-restart end-to-end test: two `cli.main()` calls over one
  memory directory, asserting the fact reaches the second run's prompt.

## Decisions and why

- Memory lives outside the workspace so the file tools cannot rewrite it.
  Discussed with Bob and Alice at standup; written up as ADR-035.
- Both candidate memory tool surfaces were built and measured rather than
  chosen by taste. ADR-037 records the numbers and the shipped default.

## What I am doing next

- Phase 2: the skills loader and its two example skills.

## Blockers

None.
```

## Things that go wrong

- **Wrong week number.** The week is the course week, not the calendar week.
- **A decision with no ADR.** If a choice would be hard to reconstruct in
  three weeks, it needs an ADR, not just a report line.
- **Reports written in bulk on Thursday.** They are dated, and it shows.
