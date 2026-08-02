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

## Aim at the rating, not at "the code works"

Measured over 60 merged openclaw PRs (any author), the reviewer's rating is
what separates merged from stalled — not size and not risk area:

| rating | merged in sample |
|---|---|
| 🐚 platinum hermit (4/6) | 33 |
| 🦞 diamond lobster (5/6) | 14 |
| 🦐 gold shrimp (3/6) | 4 |
| 🦪 silver shellfish (2/6) | 3 |

Two intuitions that turn out to be **wrong** here, both checked against that
sample: 53% of merged PRs carry a 🚨 merge-risk label, so risk areas are not
excluded; and `size: XL` is the single largest merged bucket (17), median +214
lines, so small is not a requirement. Do not self-censor on either basis.

`Overall readiness` behaves as the MINIMUM of two sub-scores — consistent with
all three of ours:

| PR | Proof confidence | Patch quality | Overall | Outcome |
|---|---|---|---|---|
| #116958 | 4/6 | 4/6 | **4/6** | merged in ~5h |
| #115138 | 4/6 | 3/6 — one actionable P1 | **3/6** | stalled 4 days |
| #116260 | 3/6 — proof too synthetic | 4/6 | **3/6** | stalled |

So a strong leg cannot carry a weak one. #115138 had textbook evidence and
still sits at gold shrimp because one P1 finding was left unaddressed.

**Both legs, every time:**

1. *Proof confidence* — a real terminal transcript of the affected path, not
   test output. See the section above.
2. *Patch quality* — zero actionable findings. When ClawSweeper lists a P1,
   that PR is not "awaiting review", it is **awaiting us**. The `status: ⏳
   waiting on author` label says so explicitly.

And read the reviewer's re-reviews: it EDITS its original comment rather than
posting a new one, so a verdict published days later carries the original
comment's timestamp.

## `check-sqlite-session-flip-proof` fails from forks, not because of our code

Measured 2026-08-02 across 21 other open PRs plus our own:

| PR source | passes | fails |
|---|---|---|
| pushed to openclaw/openclaw directly | 14 | 0 |
| from a fork (incl. all three of ours) | 2 | 5 |

Three of our PRs failed the identical assertion in
`test/scripts/sqlite-sessions-transcripts-flip-proof.e2e.test.ts`
(`expected [ Array(1) ] to deeply equal []`) — a WhatsApp media-retry change, a
sandbox memory-flush change, and an SQLite mmap change. A change to WhatsApp
retry cannot break an SQLite session-flip harness the same way a memory-flush
change does; the common factor is the fork, not the diff.

Ruled out first, so do not re-derive it: a stale base (`gh pr update-branch`
onto current main still failed), an upstream breakage (17 same-repo PRs passed
the same hour), and flakiness (each ran once, and same-repo PRs never fail it).

**Do not spend a session fixing this one.** Note it on the PR in a sentence so a
maintainer can re-run it with repo credentials, and leave the code alone.

Beware the pooled statistic: counting all other PRs together gives a 5% failure
rate for this check, which reads as "ours". Split by fork vs same-repo before
concluding anything about a red check.
