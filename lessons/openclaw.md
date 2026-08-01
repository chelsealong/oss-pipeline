# openclaw — lessons from actual outcomes

## Bring real runtime evidence in the PR body from the first push

openclaw gates external PRs on *real behavior proof*, and its reviewer bot
(`clawsweeper`) distinguishes sharply between "the tests pass" and "here is the
command I ran and what it printed". Two of our PRs prove the difference — same
repo, same reviewer, same week:

| PR | Evidence supplied | Outcome |
|---|---|---|
| #116958 | terminal transcript against a **real QMD binary**: status showed the new command, indexing created the database, follow-up status cleared the missing-index condition | `proof confidence 4/6` → **merged by steipete** ~5h after opening |
| #116260 | a focused Vitest case driven through a **synthetic transport seam** | `⛔ needs proof`, stalled; the extra evidence had to be produced in a second round |

The reviewer's own wording on #116260: *"it runs through a synthetic Vitest
request seam rather than a real Codex app-server setup; add a redacted real
round-trip transcript or runtime log before merge."*

**So: before opening a PR here, actually run the affected command or code path
and paste the redacted output.** Not the test output — the real thing. Redact
private endpoints, IPs, keys and identifiers first; the reviewer asks for this
explicitly. Adding it up front costs one command; adding it after a `needs
proof` verdict costs a full review cycle and leaves the PR sitting.

## The PR body is machine-checked

`scripts/github/real-behavior-proof-policy.mjs` applies `triage: needs-pr-context`
unless it finds *authored* prose under exactly these two headings:

    ## What Problem This Solves
    ## Evidence

Template boilerplate does not satisfy it. Put the reproduction and the real
command output under **Evidence**, with measured numbers where the change makes
a performance claim.

## Some things are not ours to settle

Two PRs are open not because anything is wrong with them but because they need a
human decision:

* **#115138** — `proof: sufficient`, `platinum hermit`, no findings. Blocked on
  whether the owner accepts mmap-backed RSS growth for every local-WAL
  connection. That is a runtime policy trade-off, not a defect.
* **#116260** — `Findings: None, Security: None`, yet the reviewer states that
  approving a credential-redaction change *"requires direct source and
  real-runtime verification rather than an automated repair attempt."*

When a verdict says a maintainer must decide, stop. Adding another round of
automated evidence does not move it and the requirement count grew 4 → 6 when we
tried.

## Repo-specific gates

* Abort the run if the author already has ≥18 open PRs: the bot applies
  `r: too-many-prs` at 20 and then auto-closes **all** of that author's PRs.
* Skip anything labelled `clawsweeper:no-new-fix-pr` — their own triage agent
  decided not to queue a fix, and ~83% of issues carry it.
* Never submit refactor-only, test-only, CI-only or docs-only changes; those are
  closed on sight.
* Duplicate saturation is high. Run the file/symbol search, not just the
  issue-number search — a competing PR often describes the same fix in different
  words and never references the issue.
