# hermes-agent

## How work actually lands here

Not by our PR being merged. A maintainer cherry-picks our commits into their
own PR and merges that, so our PR shows CLOSED and theirs shows a self-merge.
Both landed changes went this way and kept our authorship:

- `#75790` → teknium1's `#75910`, merged 16h after we opened, carries 2 of our commits
- `#75792` → kshitijk4poor's `#78390`, merged 151h later, carries 3

So never measure this repo by PR merge state. Count commits on the upstream
default branch authored by `chelsealong@126.com`. `health.py check_landed`
does this; a naive merge count reports zero here.

## The feedback loop is fast and technical — use it

Its AI triage names the function and the line. Two real defects in our own
patches were found and fixed the same hour:

- `#81134` — the zh injection patterns could not match across a line break:
  `_FILLER_CJK` used `.` and the inline `.{0,6}` spans, neither of which
  matches `\n`, while the English originals used `\s+` and did. Fixed in
  `9fe6778d`, 6 minutes after the review.
- `#81204` — `extractOoxmlPreviewText` read the whole archive into memory
  before any cap applied, while the pre-existing path capped at 512 KiB.
  Fixed in `d8f4449f`, 13 minutes after.

`#74733` was called the `best fix` of three competing PRs by that same triage.

## Size is not the constraint here

Merged PRs run a median of +350 and up to +2005 — the opposite of openclaw's
+61. Our +520 and +254 patches are in normal range, and fix-one.yml applies no
size ceiling to this repo deliberately.

## A no-op that passes its own test — `#82124`

Closed by OutThisLife on 2026-08-09 with the clearest rejection we have had:

> this fix is a no-op and I'd rather not merge something that closes the issue
> without changing behavior.

Three separate mistakes, each generalisable:

1. **The precedent did not transfer.** We called `setFocusable(true)` on the
   HUD window because the pet overlay does. But the pet overlay creates itself
   with `focusable: false` on purpose and flips it while its composer wants the
   keyboard. Electron defaults `focusable` to `true` and `spawnHudWindow()`
   never sets it, so our call set the value to what it already was. Copying a
   pattern requires knowing why the original site needs it.
2. **The test was a tautology.** It asserted the helper calls
   `setFocusable(true)`. That fails without the helper — so it passes the
   "would it fail without the source change" check — and still proves nothing
   about behaviour. Assert the observable effect, not that a line executed.
3. **The report was stale.** The `click-through.ts` it quoted is no longer on
   main; it was replaced in `717b49c`, which is not an ancestor of the build in
   the report. The maintainer could not reproduce on current main. Read the code
   an issue quotes on current main before fixing it.

fix-one.yml's adversarial reviewer now opens with a NO-OP front covering all
three, and the TEST HONESTY front says explicitly that failing-without-the-change
is necessary but not sufficient.

## Why 34 PRs produced 5 commits, and all five on one day

Measured 2026-08-11. Nothing has landed since 2026-08-01, while the PR
count went from ~5 to 34.

The repo is not the problem. hermes merges 45-135 PRs a day; teknium1 has
opened 253 PRs since 08-05 and kshitijk4poor 114. None of the six issues
behind our oldest open PRs has been closed by anyone else, so the work
was not taken. Engagement is real too: 10 of our 30 open PRs have a human
on them, and on `#81591` an unrelated user cherry-picked our fix and
confirmed it end to end.

Size is not the differentiator here either. The three external PRs merged
by someone other than their author in the last three days run +13, +108
and +1679.

What separates them from us:

| author | merged | comments on threads they did not open |
|---|---:|---:|
| helix4u | 117 | 22 |
| embwl0x | 5 | 39 (35 on issues) |
| victor-kyriazakos | 5 | 2 |
| **us** | **0** | **0** |

We answer reviewers on our own PRs — 13 of 30 carry a reply from us — and
we have never once spoken on a thread we did not open. Every contributor
who lands work here is a participant in the tracker first. embwl0x has
commented on 35 issues and merged 5 PRs; we have commented on none and
merged none.

Volume made it worse. The two that landed were salvaged on 2026-08-01,
when we held four or five open PRs. The quota then went to 10/day, the
open count reached 30, and the landing rate went to zero — in a repo
where a single author's stack competes with maintainers pushing 250 PRs a
week. Cap is 4/day now.

**Do not read this as "comment more to look busy."** The measurable thing
is that our PRs arrive with no prior presence on the thread. Where the
pipeline already has an announce step (adk), that is the shape to reuse.

Caveat on method: GitHub's `commenter:` search index is unreliable — it
reported 2 comments for us when a direct count over the same PRs found 13.
The 0 above is the `commenter:X -author:X` form, which agrees with a
manual check.

## Salvage lands 3-5 days late — do not judge inside that window

On 2026-08-12 I concluded hermes was failing: 34 PRs, "nothing landed since
08-01", 6% conversion, and cut it to 4 PRs/day. The evidence was that our
commits on main stopped at 08-01.

On 2026-08-15 two more landed:

| commit | we wrote it | teknium1's PR | merged |
|---|---|---|---|
| `f0748b45` stop empty REST transcript refresh | 08-10 | #86588 | 08-15 03:23 |
| `49d72a02` verify Windows gateway cold-start | 08-12 | #86687 | 08-15 05:03 |

Three and five days between our PR and the salvage. The first two salvages
took 16h and 151h, and I generalised from the 16h one. hermes is now the
repo with the most landed commits of any (7), and both of these arrived
*after* the cut.

**The rule: a repo whose landing path is salvage cannot be judged on a
window shorter than a week.** Our PR staying open means nothing there —
the maintainer's PR is where the work appears, under their number, and
`is:merged` on our account never shows it. Count commits on the default
branch, and give it seven days before drawing a conclusion.

Cap back to 7/day on 2026-08-15. Not 10: the run to 30+ open PRs in one
repo was its own problem, and the 20,623-PR backlog there means volume
still buys nothing.
- [2026-08-30] issue #98243 blocked by review:  FRONT 0 (NO-OP) — FAILS, this is the deciding issue.  `hermes_cli/model_switch.py::list_authenticated_providers()` already contains a post-pass (lines ~3890-3906, added in commit d474ba5615, "Surface a custom / 

- [2026-08-30] issue #98273 blocked by review:  FRONT 0 — NO-OP (fails the PR):  The added `term.refresh(0, term.rows - 1)` after `webgl.dispose()` almost certainly duplicates a repaint the real @xterm/addon-webgl + @xterm/xterm 

- [2026-08-30] #98295 skipped: issue #98295 (mechanical wheel/touchpad scrolling stuck at 1 row/event in native terminals due to nativeStep's 40ms acceleration window in ui-tui/src/lib/wheelAccel.ts) is already fixed by open PR #83675 ("fix(tui): accelerate mechanical wheel scrolling in nat
- [2026-08-30] #98299 skipped: issue #98299 asks for /v1/runs on the API server to gain the full native GoalManager lifecycle (goal creation via /goal, judge wait/continue verdicts, process/session/timed wait parking, wake delivery, pause/resume/clear, budget accounting, and goal-lifecycle 
- [2026-08-30] #98308 skipped: issue #98308 (Volcengine Ark plan/v3 rejects empty assistant content after replayed pure-reasoning turns, in agent/codex_responses_adapter.py::_chat_messages_to_responses_input) is a duplicate of issue #89761, which already has an open PR #89783 ("fix(codex): 
- [2026-08-30] #98321 skipped: issue #98321 is a subjective LLM answer-quality regression report (canonical Bot Chat vs regular session), not a concrete code bug. The issue itself identifies only a "primary known differential" (the Bot-Mode protocol/message_agent injection in tools/bot_mode
- [2026-08-30] #98330 skipped: issue #98330 (Hermes Desktop has no way to review pending skill/memory write-approval writes) asks the maintainer to pick between three competing designs — (a) make /skills execute on Desktop, (b) build a Settings > Skills pending-review panel, (c) add a bad
- [2026-08-30] #98332 skipped: issue #98332 is a native SIGSEGV in CPython's sqlite3 C extension (bounded_lru_cache_wrapper -> _PyDict_GetItem_KnownHash), triggered when a background worker thread executes a sqlite3 query concurrently with something else around a delegation child's 600s har
- [2026-08-30] #98336 skipped: issue #98336 asks that a stray, non-desktop-owned `hermes serve` / `hermes gateway run` process be auto-stopped (and relaunched after update) during the Windows Desktop update preflight so it no longer holds hermes.exe locked. This is not a small classificatio
- [2026-08-30] #98351 skipped: issue #98351 (Desktop: 1500+ message session permanently stuck in "summarizing thread", UI dead) sits inside a subsystem that is already being actively and heavily reworked by others, and cannot be safely or minimally fixed without the reporter's actual sessio
- [2026-08-30] #98352 skipped: issue #98352 requests a brand-new "Pokemon Dex" Telegram plugin (menus, pagination, generation-5/6/7 compatibility, D3 terminal cards, Chinese species/ability/move names, base stats/weaknesses/resistances) that must work entirely from local data — no PokeAPI
- [2026-08-30] #98359 skipped: issue #98359 quotes a bug in ~/.hermes/plugins/context-broker-preflight/__init__.py, a user's local third-party plugin. Verified this plugin (and any "context-broker" MCP) does not exist anywhere in NousResearch/hermes-agent (checked plugins/, optional-mcps/, 
- [2026-08-30] #98377 skipped: issue #98377 (venv-blocker scan on Windows exceeds its own 15s SCAN_TIMEOUT_MS on hosts with many processes, apps/desktop/electron/venv-blocker-scan.ts:48) is already fixed by open PR #84981 ("fix(desktop): venv-blocker scan timeout aborts every in-app update 
- [2026-08-30] #98387 skipped: issue #98387 (provider-level custom_providers context_length override dropped in get_custom_provider_context_length()) is already fixed by open PR #92938 ("fix(fallback): honor destination context overrides", opened 2026-08-23). That PR's hermes_cli/config.py 
- [2026-08-30] #98438 skipped: issue #98438 (providers/__init__.py._discover_entry_point_providers() calls ep.load() on every enabled entry point without checking its kind first, so a pip-installed platform plugin gets eagerly imported during hermes_cli.config's import-time provider discove
- [2026-08-31] #98808 skipped: issue #98808 (Kanban workers lose kanban_complete/kanban_block/kanban_heartbeat when their own profile disables the "kanban" toolset, in model_tools.py::_compute_tool_definitions - the disabled_toolsets subtraction pass strips the lifecycle tools back out even
- [2026-09-01] #100472 skipped: issue #100472 (MCP stdio tool calls leak an un-awaited `_watch_stdio_children()` coroutine in `_make_tool_handler`, tools/mcp_tool.py ~line 6186) is already fixed by open PR #98088 ("fix(mcp): reuse stdio watcher coroutine", opened 2026-08-29) and open PR #962
- [2026-09-01] #100376 skipped: issue #100376 (VS Code Marketplace theme packs with 3+ variants only exposing the first light/dark pair) is already substantially addressed by open PR #64682 ("feat(desktop): install multi-variant marketplace themes as individual entries"), which a collaborato
- [2026-09-02] #101035 skipped: issue #101035 (SQLite busy_timeout=0 in hermes_state.py causing SQLITE_BUSY crash loop) is stale against current main. Verified hermes_state.py: the SessionDB writer connection opens with timeout=1.0 (not 0), deliberately short because _execute_write() layers 
- [2026-09-02] #100996 skipped: issue #100996 (Desktop in-app Browser <webview> lacks allowpopups so window.open()/OAuth popups die silently, apps/desktop/src/app/chat/right-rail/preview-pane.tsx) is already claimed and substantially fixed by two open PRs. A repo collaborator (alt-glitch) co
- [2026-09-02] #101294 skipped: issue #101294 is a release-notes tracking index authored by a prolific external contributor (x7peeps), summarizing their 31 open PRs into 6 narrative "story lines" for a release writer to reference in v0.21.x/v0.22.0 notes. It is not a bug report or feature re
- [2026-09-02] #101341 skipped: issue #101341 is a feature request (Kanban task skills cannot pin the required skill revision/digest) that the issue author explicitly frames as needing a maintainer design decision among three incompatible contracts (semver range, exact content-digest, or tas
- [2026-09-02] #101394 skipped: issue #101394 is a large feature request (Hermes Remote mobile PWA — pairing/QR auth, device management, new gateway config, hermes remote CLI subcommands) explicitly scoped by the author as 'Large (new module or significant refactor)', with the author stati
- [2026-09-03] #101748 skipped: issue #101748 is a design question, not a mechanical bug. The dashboard's single HTTP server on 127.0.0.1:<port> serves ONE static frontend bundle per process, chosen once at startup by whether HERMES_WEB_DIST is an Electron-packaged (app.asar) path. Desktop's
- [2026-09-03] #101638 skipped: issue #101638 has two parts. Part 2 (dead workers leaving stale `running` task_runs rows) is already correctly handled on current main — verified empirically: release_stale_claims() -> _end_run() closes the stale run row (ended_at stamped, outcome='reclaimed
- [2026-09-04] #102497 skipped: issue #102497 is a UI feature/design request (label type/feature, P3 cosmetic), not a bug fix. It asks to add bot avatars to Desktop session tabs and explicitly offers multiple incompatible design options for how the status indicator should combine with the av
- [2026-09-04] #102563 skipped: issue #102563 is a feature request to bump numerous npm dependencies (several major versions: @eslint/js, @nous-research/ui, @types/node, chalk, cli-boxes, supports-hyperlinks, type-fest, typescript, wrap-ansi, etc.) across package.json/lockfiles ahead of a re
- [2026-09-04] #102566 skipped: issue #102566 is a deep, unreproduced production-only bug report (assistant final response occasionally persisted as the literal string "[response interrupted]" despite finish_reason=stop and real API output tokens), and after real investigation I could not fi
- [2026-09-04] #102574 skipped: issue #102574 (PeriodicScheduler blocking-callback stall) is already fixed by open PR #102458 by jfreshpicks, 'fix(agent): isolate periodic scheduler callbacks from blocking siblings'. That PR modifies exactly agent/periodic_scheduler.py::_run (dispatches each
- [2026-09-04] #102585 skipped: issue #102585 is explicitly sequenced behind two prerequisite issues (#102582, #102584) via tracking issue #102586, and neither has landed. Verified on current main: hermes_cli/moa_cmd.py::_pick_slot() only prompts for provider/model; there is no per-slot reas
- [2026-09-04] issue #102592 blocked by review:  FRONT 0 — NO-OP (fails, decisive): The diff adds `discover_plugins()` inside `web_server.start_server()`, right after `apply_nofile_soft_limit()`. But the ONLY production caller of `start_server()` in the entire repo is `cmd_dashboard()` in hermes_cli/main.py 

- [2026-09-04] #102619 skipped: issue #102619 is already fixed on current main. hermes_cli/local_runtime/hardware.py::probe_budget() already budgets Apple Silicon / unified-memory machines from RAM (sysctl hw.memsize) minus a 20% headroom (_UMA_HEADROOM_FRACTION) instead of discrete GPU memo
- [2026-09-04] #102632 skipped: issue #102632 (Nix sealed venv missing hermes_state_holders / hermes_state_registry in pyproject.toml py-modules) is already fully fixed by open PR #102200 ("fix(nix): include missing hermes state modules", opened by plasma-penguin), which adds both hermes_sta
- [2026-09-04] #102642 skipped: issue #102642 describes a bug in Hermes Studio's Python "Agent Bridge" component (agent-bridge/python/bridge_server.py, a Node-server -> Python-broker -> Python-worker chain with server.listen(16)/server.listen(64) backlogs). That component does not exist anyw
- [2026-09-04] #102643 skipped: issue #102643 (i18n support for hermes_cli/commands.py::CommandDef.description, so the desktop slash-command popup for e.g. /model, /compress, /approve stops showing English-only descriptions to non-English users) is already substantially fixed by our own open
- [2026-09-04] #102652 skipped: issue #102652 (desktop_preview fails on diagrams.net URLs with long #create= compressed fragments, "invalid literal/lengths set" zlib error) has no reproducible bug in hermes-agent's code. Traced the full path end to end: tools/preview_tool.py -> tools/open_pr
- [2026-09-04] #102653 skipped: issue #102653 is a sprawling 10-section product/UX redesign request ("Bot Mode product polish") explicitly framed by its author as deserving "product-level treatment" — spanning Desktop conversation architecture, session/gateway abstraction, routines, approv
- [2026-09-04] #102681 skipped: issue #102681 is a (Chinese-language) duplicate report of the well-known "Desktop serve mode never calls register_from_config() to register profile shell hooks" bug, whose canonical tracking issue is #102504 (labeled duplicate, P1, comp/desktop). Confirmed via
- [2026-09-04] #102687 skipped: issue #102687 is a large feature request for a brand-new 'cross-board gate record' subsystem (producer/consumer linkage across Kanban boards, CLI/API, audit events, fail-closed reason codes, idempotent re-evaluation), not a defect with a minimal fix. No existi
- [2026-09-04] #102693 skipped: issue #102693 asks for a new trust-classification policy ("human-vocabulary" vs "bot/automation" author strings) to be invented and enforced across three call sites (hermes_cli/kanban_db.py::add_comment, the CLI --author flag in hermes_cli/kanban.py, and Comme
- [2026-09-04] #102762 skipped: issue #102762 (Desktop main-process crash "hermesLog.push of undefined" from readPersistedPoolLimits() running before hermesLog was declared, apps/desktop/electron/main.ts) is already fixed on current main. Verified: hermesLog is declared at main.ts:1471, befo
- [2026-09-04] #102769 skipped: issue #102769 (cross-profile TERMINAL_* leak via _reapply_terminal_config_bridge following the context-local set_hermes_home_override instead of the true process home) is already fixed by open PR #97014 ("fix(env): scope terminal config re-bridge to the true p
- [2026-09-04] #102778 skipped: issue #102778 is a deep, extensively-instrumented React state bug (9 config-backed
- [2026-09-04] #102801 skipped: issue #102801 is a large, staged, multi-phase feature request (opt-in macOS "Computer History" activity collector: new companion process, local SQLite/JSONL event store, redaction policy, bounded retention, CLI surface `hermes computer-history ...`, Desktop ti
- [2026-09-04] #102731 skipped: issue #102731 is a large, multi-phase product/security feature request (secure local-browser pairing/handoff for VPS-hosted agents to a user's authenticated local Chrome session, spanning new Companion pairing infrastructure, Browser Watch streaming, secret re
- [2026-09-04] #102827 skipped: issue #102827 is a probabilistic production SQLite corruption report (state.db "database disk image is malformed" / "file is not a database" after a cron job's teardown triggered the #94736/#99509 self-heal reopen path under concurrent writer traffic). The rep
- [2026-09-05] #103287 skipped: issue #103287 (gateway /steer confirms 'queued' but silently strands text when no run is active, tui_gateway/methods_tools.py::_cmd_steer calling agent.steer(arg) unconditionally without checking session["running"]) is already substantially fixed by open PR #9
- [2026-09-05] #102887 skipped: issue #102887 is a documentation/feature request, not a code bug on current main. Both mechanisms it asks for already exist and are already documented: (1) `platform_hints.cron.replace`/`.append` config override for the "no user present" hint (website/docs/dev
- [2026-09-05] #103456 skipped: issue #103456 (Desktop sidebar sometimes binds the runtime to an earlier compressed ancestor instead of the active tip of a long compression chain) names its own suspected root cause as unconfirmed: the reporter explicitly says "no runtime log has yet captured
- [2026-09-05] #103469 skipped: issue #103469 is a large feature request for a brand-new versioned "atomic review-child / terminal-parent witness" public Kanban API (canonical keys, terminal-parent digest/generation tokens, invalidation semantics, snapshot versioning, redaction contract). Th
- [2026-09-05] #103608 skipped: issue #103608 is a broad, multi-phase Desktop UX redesign proposal (P1/P2/P3 sections: durations-not-ranges, collapsing micro-steps into grouped rows, an optional "Show precise timestamps" setting, unifying label->metadata pattern and alignment rules across fo
- [2026-09-05] #103490 skipped: issue #103490 is explicitly not a bug report ("Not a new bug report — data for the scaling roadmap"). The reporter (RikETS) shares production observations from a 6-gateway/12-profile topology and states the amplification was already resolved by upstream work
