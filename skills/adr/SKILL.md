---
name: adr
description: Write an Architectural Decision Record in this project's decisions.md format, with the context, decision and consequences sections.
---

# Architectural Decision Records

`decisions.md` is the project's memory of *why*. The oral exam and every
later change are reconstructed from it, so an ADR that records only what was
decided has done half the job.

## Format

```markdown
### ADR-0NN: Short title in the imperative

**Context.** What forced a choice. Include the constraint that made the
obvious option wrong.

**Decision.** What we do now, in one or two sentences.

**Consequences.** What this costs, what it rules out, and what would have to
change to revisit it. Name the alternative that was rejected and why.
```

## Rules

- **Number it next.** Read the end of `decisions.md` first and take the next
  free number. Parallel branches append to this file at once, so
  reserve the number before writing, not after.
- **One decision per ADR.** If the title needs an "and", it is two records.
- **Write the rejected alternative down.** An ADR that lists only the chosen
  option cannot be argued with later, which is the same as not being able to
  defend it.
- **Never edit an ADR to change its decision.** Add a new one that
  supersedes it and say so in both. The record is a history, not a
  description of the present.
- Consequences must include at least one cost. A decision with no downside
  was not a decision.
