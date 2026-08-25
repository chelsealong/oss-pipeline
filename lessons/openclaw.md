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

## "fail-open" means invert the predicate, not extend the denylist

openclaw#115138 spent five days and four rounds going **down** in rating:

| round | what we pushed | rating after |
|---|---|---|
| — | original mmap gate | 🦐 gold shrimp 3/6 |
| 1 | distinguish verified-local from unclassified WAL | 3/6 |
| 2 | keep mmap off local Windows drives | 3/6 |
| 3 | exclude MacFUSE/OSXFUSE | 3/6 |
| 4 | exclude generic FUSE mounts | 🧂 unranked krab **1/6** |

The reviewer's objection never changed across any of them:

> Mmap gate remains fail-open: the branch enables mmap whenever the journal
> policy equals "wal". That policy is also returned for any matched mount type
> that is not one of the explicitly rejected network or FUSE types, so it is not
> a positive local-filesystem proof.
> **[P1] Make mmap eligibility fail closed for unlisted mounts**

`resolveMountTypeJournalPolicy` returns `"wal"` as a *compatibility default* for
anything it does not recognise. The PR reused that default as evidence of being
on a local disk. Every round added one more entry to the reject list — Windows,
MacFUSE, FUSE — while the flaw was that the list exists at all.

**When a reviewer says a predicate is fail-open, adding exclusions is the wrong
direction and will be read as not having understood.** Proof confidence stayed
at 4/6 the whole time; patch quality fell 3 → 1. Enumerating cases *lowered* the
score, because each round demonstrated the same misreading.

Invert it: require a positively-recognised local mount type, and let everything
unlisted fall through to disabled. One condition, no list.

**Stop rule:** if the reviewer's headline finding is unchanged after two rounds,
the next push must change the shape of the fix, not add another case. A third
identical round is evidence the objection was never read.

## Keep it small — this repo merges outside PRs by size, not by score

Measured 07-29..08-03 over 21 fork PRs that merged, with self-merges by people
who hold write access excluded (`fuller-stack-dev` merged four of his own at
+228..+1469, which is not the same act as a maintainer accepting a stranger's
patch and must not be averaged in):

| additions | merged fork PRs |
|---|---|
| ≤120 lines | 12 of 15 |
| 121–200 | 0 |
| >200 | 3, all merged by one maintainer during a two-day queue sweep |

Median accepted size: **+61 lines**. Time from open to merge: median 3h, max 14h.
Nothing merges slowly here — a fork PR is taken within hours or it sits.

Our own record matches exactly. #116958 was **+38/-2** and merged in 5 hours.
The four that stalled are +190, +287, +297 and **+1234/29 files** — and two of
them are CI-green `platinum hermit` / `diamond lobster` sitting untouched for
days. Score is not the constraint; nobody has hours to read 29 files, and no
rating fixes that.

**So on openclaw: target one file, under ~100 added lines, one behaviour.** If
the honest fix is larger, prefer a different issue — a large well-rated PR here
is worth less than a small one, because it will not be read. This does not
generalise: hermes salvages large patches routinely (#75792 was +274 across 3
commits and was cherry-picked with authorship preserved).

Earlier in this file a table shows `size: XL` as the largest merged bucket. That
counted **all** merged PRs, which on this repo are overwhelmingly internal
branches with write access. For a fork account it is misleading — use the
numbers above.

## Never touch dependencies, lockfiles or CI — not even to "sync with main"

On 2026-08-07 the fixer working on #115138 pushed
`fix(deps): sync stale security overrides with main`, then reverted it fifteen
minutes later as `revert(deps): drop unauthorized dependency-graph changes`. It
caught itself, which is good, but the commit should never have existed: this is
a two-file SQLite change and the dependency graph is not part of it.

Scope creep is not a style problem here, it is the difference between merged and
ignored. hermes triage rejected a competing PR for exactly this — #75321 was
told to remove "unrelated uv.lock, scipy-marker and vercel-workers changes"
while our #74733 was picked as `best fix` at +49/-2. The reviewer reads the file
list before the diff.

If a build genuinely fails because a dependency is stale on the branch, that is
a merge-base problem: rebase or merge upstream, do not edit the manifest.

## Size is the gate, and it is now mechanical

Measured 2026-08-08 across all eight PRs we have opened here:

| PR | added | state |
|---|---|---|
| #116958 | +38 | **merged** |
| #118377 | +47 | open, one node test red |
| #120398 | +140 | open, lint + test-types red |
| #117719 | +189 | open |
| #116260 | +191 | open, in a clawsweeper re-review loop |
| #117757 | +287 | open, CHANGES_REQUESTED, taken over |
| #115138 | +358 | open |
| #117176 | +1234 | open, taken over |

The only one that merged is the smallest, and external PRs that land here run
a median of +61. "Keep it minimal" was advice in a prompt and produced a
1234-line patch, so fix-one.yml now refuses to open an openclaw PR that adds
more than 120 lines, and the generator is told the number up front.

If the smallest correct fix does not fit under the ceiling, the right move is
to stop and say so — not to write the large one. A patch that size will not
merge here, and it spends attention we need for the small ones.
- [2026-08-25] #129377 skipped: Root cause found but the correct fix is architectural, exceeds the size gate, and needs a maintainer product decision — not a minimal patch.
