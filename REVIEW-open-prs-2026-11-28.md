# Open PR audit, 2026-11-28

Read-only snapshot of every open PR authored by this account, taken from the GitHub API on 2026-11-28.
Nothing was closed, edited or commented on. The pipeline never closes PRs automatically (`AGENTS.md`), so the
actions below are for the owner.

## Summary

| Repository | Open | Merged | Closed unmerged | Conflicting | CI failing | Changes requested | Approved | Untouched >14 days |
|---|---|---|---|---|---|---|---|---|
| Comfy-Org/ComfyUI | 80 | 7 | 3 | 20 | 8 | 13 | 0 | 70 |
| openclaw/openclaw | 19 | 50 | 51 | 0 | 11 | 0 | 0 | 4 |
| langfuse/langfuse | 18 | 5 | 13 | 0 | 0 | 0 | 0 | 0 |
| run-llama/llama_index | 15 | 2 | 6 | 3 | 2 | 0 | 0 | 14 |
| google/adk-python | 8 | 0 | 58 | 2 | 1 | 0 | 1 | 0 |
| firecrawl/firecrawl | 6 | 0 | 1 | 0 | 6 | 0 | 4 | 6 |
| github/spec-kit | 5 | 29 | 9 | 2 | 1 | 0 | 0 | 0 |
| langgenius/dify | 4 | 7 | 5 | 0 | 1 | 0 | 0 | 4 |
| NousResearch/hermes-agent | 4 | 9 | 337 | 0 | 0 | 0 | 0 | 0 |
| unclecode/crawl4ai | 3 | 0 | 2 | 1 | 0 | 0 | 0 | 3 |
| BerriAI/litellm | 3 | 0 | 2 | 1 | 1 | 0 | 0 | 3 |
| vllm-project/vllm | 2 | 0 | 1 | 1 | 2 | 0 | 0 | 2 |
| langfuse/langfuse-python | 1 | n/a | n/a | 0 | 0 | 0 | 0 | 0 |
| **Total** | **168** | **109** | **488** | **30** | **33** | **13** | **5** | **106** |

A closed-unmerged PR is not always a rejection: Google ADK lands changes through Copybara and Hermes
maintainers cherry-pick into their own PRs, so those two rows undercount accepted work.

## What stands out

- **ComfyUI has 80 open PRs against 7 merged.** Median age 71 days, oldest 117; 70 untouched for over two weeks, 20 conflicting, 13 with changes requested. The pipeline has no open-PR cap for this repository (`repo_limits.OPEN_PR_CAPS` covers only openclaw and hermes), so the backlog grows without limit.
- **OpenClaw is at 19 of its 20-PR cap,** and 11 of those have failing CI. Each PR that is fixed and merged, or closed, frees a slot for a new fix. This cap and the 10-starts-a-day limit bind on OpenClaw; the model budget does not.
- **5 PRs are approved but not merged.** 4 of them have failing CI. These are the closest to a merge.
- **14 PRs sit on repositories the pipeline has disabled** (firecrawl, litellm, crawl4ai, vllm), so nothing maintains them.
- **llama_index: 15 open, 14 untouched for over two weeks.**

## Suggested order of work

1. Approved PRs with failing CI: fix or explain the failure so they can merge.
2. OpenClaw PRs with failing CI: repair or close, to free cap slots.
3. Conflicting PRs: rebase the ones still wanted; close the rest with a short note.
4. Changes requested: answer or apply the feedback; close if the maintainer has moved on.
5. ComfyUI: stop adding PRs until the backlog is reviewed, and add an open-PR cap for it.
6. Disabled repositories: decide per PR whether to finish by hand or close.

## PRs needing action

### Comfy-Org/ComfyUI (34 of 80)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#15515](https://github.com/Comfy-Org/ComfyUI/pull/15515) | 2026-08-12 | 2026-08-12 | conflict, changes requested | Fix LTX VAE decoder RoPE crash on MPS with float64 |
| [#16069](https://github.com/Comfy-Org/ComfyUI/pull/16069) | 2026-09-03 | 2026-09-03 | conflict, changes requested | Force CPU for mesh postprocess kernels on MPS |
| [#16262](https://github.com/Comfy-Org/ComfyUI/pull/16262) | 2026-09-11 | 2026-09-20 | conflict, CI failing | Raise clear error when a non-generation model is used with TextGenerat |
| [#16450](https://github.com/Comfy-Org/ComfyUI/pull/16450) | 2026-09-21 | 2026-09-21 | conflict, CI failing | Fix native abort: pause block-weight malloc graph while reading Qwen-I |
| [#16688](https://github.com/Comfy-Org/ComfyUI/pull/16688) | 2026-09-30 | 2026-09-30 | conflict, CI failing | Fix MiniMax-H3 VAE int8 rms_norm crash on offloaded norm weight |
| [#17104](https://github.com/Comfy-Org/ComfyUI/pull/17104) | 2026-10-17 | 2026-10-17 | CI failing, changes requested | Fix portable startup when the asset database is in use |
| [#17191](https://github.com/Comfy-Org/ComfyUI/pull/17191) | 2026-10-19 | 2026-10-19 | conflict, changes requested | Fix static assets from symlinked frontend packages |
| [#15399](https://github.com/Comfy-Org/ComfyUI/pull/15399) | 2026-08-07 | 2026-11-16 | conflict | Fail fast when an Autogrow group input is given a value directly |
| [#15459](https://github.com/Comfy-Org/ComfyUI/pull/15459) | 2026-08-10 | 2026-08-10 | conflict | Report corrupt safetensors files with a clear error message |
| [#15495](https://github.com/Comfy-Org/ComfyUI/pull/15495) | 2026-08-11 | 2026-08-11 | conflict | Add --pinned-memory override for the pinned host memory cap |
| [#15520](https://github.com/Comfy-Org/ComfyUI/pull/15520) | 2026-08-12 | 2026-11-15 | changes requested | Fail loudly when quantization scale tensors go unrecognized instead of |
| [#15560](https://github.com/Comfy-Org/ComfyUI/pull/15560) | 2026-08-13 | 2026-08-13 | conflict | Fix MiniMax H3 per-step host syncs in _forward |
| [#15561](https://github.com/Comfy-Org/ComfyUI/pull/15561) | 2026-08-13 | 2026-09-26 | conflict | Fix MiniMax H3 tiled VAE decode producing a grid of mismatched tiles |
| [#15638](https://github.com/Comfy-Org/ComfyUI/pull/15638) | 2026-08-15 | 2026-08-15 | conflict | Fix crash when Llama-based text encoder KV cache init runs on CPU |
| [#15765](https://github.com/Comfy-Org/ComfyUI/pull/15765) | 2026-08-21 | 2026-08-21 | CI failing | Fix WebcamCapture raising "Invalid file path" on run |
| [#15769](https://github.com/Comfy-Org/ComfyUI/pull/15769) | 2026-08-21 | 2026-08-21 | changes requested | Fix MiniMax H3 i2v crash: pad condition video latents before patchify |
| [#15800](https://github.com/Comfy-Org/ComfyUI/pull/15800) | 2026-08-22 | 2026-08-22 | changes requested | Increase MiniMaxH3 memory usage factor to avoid OOM |
| [#15819](https://github.com/Comfy-Org/ComfyUI/pull/15819) | 2026-08-23 | 2026-08-23 | changes requested | Include bfloat16 in the macOS attention-upcast workaround |
| [#15854](https://github.com/Comfy-Org/ComfyUI/pull/15854) | 2026-08-24 | 2026-08-24 | changes requested | Fix ZeroDivisionError in LossGraphNode for constant loss series |
| [#15855](https://github.com/Comfy-Org/ComfyUI/pull/15855) | 2026-08-24 | 2026-08-25 | conflict | Fix CheckpointError from memory-dependent attention chunking during Lo |
| [#15857](https://github.com/Comfy-Org/ComfyUI/pull/15857) | 2026-08-24 | 2026-08-24 | CI failing | Retry transient 401 from api.comfy.org in API node client |
| [#16067](https://github.com/Comfy-Org/ComfyUI/pull/16067) | 2026-09-03 | 2026-09-03 | changes requested | Warn when --use-ck-attention is enabled for TRELLIS.2 |
| [#16090](https://github.com/Comfy-Org/ComfyUI/pull/16090) | 2026-09-04 | 2026-09-04 | conflict | fix: is_within_directory rejects subfolders of a bare UNC share root |
| [#16182](https://github.com/Comfy-Org/ComfyUI/pull/16182) | 2026-09-08 | 2026-09-12 | CI failing | Fix apply_rope shape crash when Wan reference latent has odd spatial s |
| [#16367](https://github.com/Comfy-Org/ComfyUI/pull/16367) | 2026-09-16 | 2026-09-16 | conflict | Fix quant capability checks crashing when device is CPU |
| [#16391](https://github.com/Comfy-Org/ComfyUI/pull/16391) | 2026-09-18 | 2026-09-26 | conflict | Fix MiniMax H3 VAE attention qk_norm_scale device mismatch on non-CUDA |
| [#16528](https://github.com/Comfy-Org/ComfyUI/pull/16528) | 2026-09-24 | 2026-09-24 | changes requested | Fix Anima cross-attention bypassing ComfyUI's optimized attention disp |
| [#16545](https://github.com/Comfy-Org/ComfyUI/pull/16545) | 2026-09-25 | 2026-09-25 | conflict | Fix SaveVideo H.264 failure on odd width or height |
| [#16594](https://github.com/Comfy-Org/ComfyUI/pull/16594) | 2026-09-26 | 2026-09-28 | CI failing | Fix washed-out TAEHV previews for models with normalized latents (ligh |
| [#17077](https://github.com/Comfy-Org/ComfyUI/pull/17077) | 2026-10-16 | 2026-10-16 | conflict | Fix Flux block gradients under checkpointing |
| [#17353](https://github.com/Comfy-Org/ComfyUI/pull/17353) | 2026-10-26 | 2026-11-15 | changes requested | Fix Blur and Sharpen on images smaller than the filter radius |
| [#17372](https://github.com/Comfy-Org/ComfyUI/pull/17372) | 2026-10-26 | 2026-10-26 | changes requested | Fix slow input file listing in node definitions |
| [#17378](https://github.com/Comfy-Org/ComfyUI/pull/17378) | 2026-10-27 | 2026-10-27 | conflict | Fix color space preservation when cropping tensor-backed videos |
| [#17485](https://github.com/Comfy-Org/ComfyUI/pull/17485) | 2026-11-01 | 2026-11-08 | conflict | Fix Create Video widget order for existing workflows |

### openclaw/openclaw (11 of 19)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#142692](https://github.com/openclaw/openclaw/pull/142692) | 2026-09-08 | 2026-11-18 | CI failing | fix(meeting): own a fresh root admission for agent consults |
| [#150063](https://github.com/openclaw/openclaw/pull/150063) | 2026-09-16 | 2026-11-19 | CI failing | fix(secrets): yield during multi-agent auth-store preparation |
| [#161462](https://github.com/openclaw/openclaw/pull/161462) | 2026-09-30 | 2026-11-27 | CI failing | fix(microsoft-foundry): support MAI-Image-2.6 image edits |
| [#161925](https://github.com/openclaw/openclaw/pull/161925) | 2026-09-30 | 2026-11-25 | CI failing | docs(test-audit): make completeness claims and quality gates fail clos |
| [#164200](https://github.com/openclaw/openclaw/pull/164200) | 2026-10-03 | 2026-11-19 | CI failing | fix: send iOS approval pushes across production and sandbox relays |
| [#191615](https://github.com/openclaw/openclaw/pull/191615) | 2026-11-03 | 2026-11-20 | CI failing | fix: give session observer utility fallbacks time to answer |
| [#191745](https://github.com/openclaw/openclaw/pull/191745) | 2026-11-03 | 2026-11-21 | CI failing | fix(voice-call): post-call summaries fail after startup caller complet |
| [#198173](https://github.com/openclaw/openclaw/pull/198173) | 2026-11-13 | 2026-11-19 | CI failing | fix: identify policy-blocked MCP servers in the chat Plugins menu |
| [#202424](https://github.com/openclaw/openclaw/pull/202424) | 2026-11-20 | 2026-11-28 | CI failing | fix: discover Claude bundle commands from declared files and symlinked |
| [#204482](https://github.com/openclaw/openclaw/pull/204482) | 2026-11-24 | 2026-11-24 | CI failing | fix: preserve Gemini 4 prompt prefixes through tool turns |
| [#205629](https://github.com/openclaw/openclaw/pull/205629) | 2026-11-25 | 2026-11-26 | CI failing | fix: explain why deep research stops before searching |

### run-llama/llama_index (4 of 15)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#22750](https://github.com/run-llama/llama_index/pull/22750) | 2026-08-19 | 2026-11-09 | conflict, CI failing | fix(core): restore token-by-token streaming for default Refine program |
| [#22957](https://github.com/run-llama/llama_index/pull/22957) | 2026-09-04 | 2026-10-30 | conflict | fix(redis-chat-store): import redis.exceptions to fix NameError on con |
| [#23188](https://github.com/run-llama/llama_index/pull/23188) | 2026-09-22 | 2026-09-22 | conflict | fix: close leaked file handles in Vectara, SemanticScholar, and BGE-M3 |
| [#23279](https://github.com/run-llama/llama_index/pull/23279) | 2026-09-27 | 2026-11-15 | CI failing | fix(core): use .get() for path lookup in MediaResource.validate_mimety |

### google/adk-python (4 of 8)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#6545](https://github.com/google/adk-python/pull/6545) | 2026-08-01 | 2026-11-18 | approved | fix(sessions): preserve original Event.id from raw_event in VertexAiSe |
| [#7251](https://github.com/google/adk-python/pull/7251) | 2026-09-23 | 2026-11-18 | conflict | fix(a2a): handle message-less failed and canceled task updates |
| [#7256](https://github.com/google/adk-python/pull/7256) | 2026-09-23 | 2026-11-18 | conflict | fix(a2a): aggregate streamed text into a final non-partial event in Re |
| [#7267](https://github.com/google/adk-python/pull/7267) | 2026-09-24 | 2026-11-15 | CI failing | fix(auth): populate OAuth scopes from server metadata discovery |

### firecrawl/firecrawl (6 of 6)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#4299](https://github.com/firecrawl/firecrawl/pull/4299) | 2026-08-13 | 2026-08-13 | approved, CI failing | fix(api): make crawl limit check in lockURL atomic |
| [#4377](https://github.com/firecrawl/firecrawl/pull/4377) | 2026-08-21 | 2026-08-21 | approved, CI failing | fix(js-sdk): don't auto-paginate crawl/batch status while job is runni |
| [#4529](https://github.com/firecrawl/firecrawl/pull/4529) | 2026-09-04 | 2026-09-04 | approved, CI failing | fix(docker): reap zombie worker subprocesses in the api container |
| [#4778](https://github.com/firecrawl/firecrawl/pull/4778) | 2026-09-27 | 2026-09-27 | approved, CI failing | docs: replace broken blog-article images with their alt text |
| [#4662](https://github.com/firecrawl/firecrawl/pull/4662) | 2026-09-16 | 2026-09-26 | CI failing | fix(api): trim markdown to the model's input budget before JSON/query  |
| [#4951](https://github.com/firecrawl/firecrawl/pull/4951) | 2026-10-05 | 2026-10-05 | CI failing | fix(python-sdk): allow keyless scrape and search in unified clients |

### github/spec-kit (3 of 5)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#4745](https://github.com/github/spec-kit/pull/4745) | 2026-09-25 | 2026-11-16 | conflict | fix: harden ExtensionManager.remove and PresetManager.remove against u |
| [#5134](https://github.com/github/spec-kit/pull/5134) | 2026-10-26 | 2026-11-17 | CI failing | fix(extensions): reject aliases that resolve to installed skill names |
| [#5322](https://github.com/github/spec-kit/pull/5322) | 2026-11-09 | 2026-11-16 | conflict | fix: save init options before re-registering extensions (#5321) |

### langgenius/dify (1 of 4)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#40926](https://github.com/langgenius/dify/pull/40926) | 2026-08-18 | 2026-08-18 | CI failing | fix(tools): fix stale secret-input decrypt cache reused across loop it |

### unclecode/crawl4ai (1 of 3)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#2264](https://github.com/unclecode/crawl4ai/pull/2264) | 2026-09-14 | 2026-09-14 | conflict | fix(docs): escape untrusted fields in LinkedIn app graph view template |

### BerriAI/litellm (1 of 3)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#37412](https://github.com/BerriAI/litellm/pull/37412) | 2026-08-19 | 2026-10-13 | conflict, CI failing | fix(proxy): stop /{provider}/v1/files from capturing /anthropic passth |

### vllm-project/vllm (2 of 2)

| PR | Opened | Last update | State | Title |
|---|---|---|---|---|
| [#51474](https://github.com/vllm-project/vllm/pull/51474) | 2026-08-08 | 2026-10-28 | conflict, CI failing | [Bugfix][Rust Frontend] Return an error instead of panicking on empty- |
| [#51623](https://github.com/vllm-project/vllm/pull/51623) | 2026-08-10 | 2026-11-09 | CI failing | [Bugfix] Handle empty YAML config file in FlexibleArgumentParser |

101 of 168 open PRs have none of these flags. A PR with no flag can still be stale; see the summary table.
