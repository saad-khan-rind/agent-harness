---
name: daily-report
description: Write a daily report for the AI Engineering Lab, in the file name and section order the course requires.
allowed-tools:
  - write_file
  - read_file
  - edit_file
---

# Daily report

The course requires one report per person per working day. The format is
fixed, so most of the work is mechanical — which is exactly the kind of
thing worth putting in a skill rather than repeating in a prompt.

## File name

`daily_reports/{Name}_{WeekX}_{DayOfWeek}.md`, for example
`daily_reports/CarolLee_Week2_Monday.md` — the full name as it appears
on the existing reports, not a first name. Ask which name, week and day if the
request does not say; do not guess, because a wrong filename is invisible
until someone looks for the report and cannot find it.

## Sections, in this order

```markdown
# {Name} - Week {N}, {Day}

## What I did today
## Decisions and why
## What I am doing next
## Blockers
```

## Rules

- Every architectural or safety decision needs a line under **Decisions and
  why** naming who was consulted, and an ADR number if one was written. The
  oral exam reconstructs the project from these two things.
- Write "None" under **Blockers** rather than deleting the heading. A
  missing section reads as an oversight; an explicit "None" reads as an
  answer.
- Keep it factual and short. This is a log, not a status pitch.
- Read `reference.md` for a filled-in example before writing the first one.
