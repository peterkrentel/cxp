# Roadmap / Backlog

Where CXP could go next, captured so today's decisions and deferred ideas don't get lost. Nothing here is committed or scheduled — it's a backlog of options with the reasoning already done, so a future session (or future you) can pick one up without re-deriving the tradeoffs from scratch.

Related docs: [`architecture.md`](architecture.md) (how it works today, including the "is this actually a protocol" honesty check), [`../tests/STRATEGY.md`](../tests/STRATEGY.md) (testing tiers and the drafted tier-3 tests).

---

## Near-term — cheap, low-risk, try first

**Quantization bump for the existing model.** Currently running `qwen2.5:1.5b` at `Q4_K_M` (confirmed via `ollama show`) — the default tag's quantization, one of the more lossy common options. Bumping to `Q6_K` or `Q8_0` (same weights, less compression) is the cheapest lever available for the JSON-malformation and structured-output failures seen throughout today — no model swap, no resource risk (Q8_0 for a 1.5B model is roughly ~1.6-1.8GB, still comfortable within Ollama's current 4Gi limit even with both models loaded). Try this *before* anything below — it isolates "does precision matter" as its own variable.

**Let today's fixes prove themselves out.** Explicitly deferred per today's decision — don't change the model yet. Watch: does load average stay low under real (non-testing-session) use? Does halt frequency actually drop? Does the regression checker start showing a real trend once enough runs accumulate?

---

## Medium-term — real changes, once there's clean signal

**`qwen2.5-coder` swap for planner (and maybe executor).** Same size class (0.5b/1.5b/3b available), same resource footprint, but tuned specifically for code/structured output — directly targets planner's JSON-decomposition reliability, which has been the single most common failure class all session. Do this *after* the quantization bump, so if things improve you know which lever actually mattered.

**Bigger model for planner specifically, if headroom allows.** Real numbers from today: node has ~3-4GB of actual headroom once agents + infra are accounted for. `qwen2.5:3b` fits; `7b` (~4-5GB) is a real OOM/swap risk without either freeing more RAM elsewhere or raising Docker Desktop's VM memory allocation (a host-level setting, outside Kubernetes). Planner is the one whose job (strict JSON output) benefits most from size — a good candidate to upgrade alone rather than bumping every agent.

**Tier-3 test activation.** Already drafted in [`tests/STRATEGY.md`](../tests/STRATEGY.md) — 8 harder variants of the existing capability tests, ready to wire in once [`check_plateau.py`](../tests/check_plateau.py) reports 10 consecutive 8/8-pass runs on the current suite. Don't build validators for these until that signal actually fires — writing them now would be guessing at what the model's real failure modes turn out to be.

**Reflect maintaining more than one skill.** Right now `reflect.py` hardcodes `SKILL_TARGET = "executor"` — every failure, regardless of which agent actually caused it (planner's decomposition, verifier's judgment), results in the *executor* skill being rewritten. Planner and verifier have their own skill files that nothing currently updates. Worth fixing once it's clear planner's failures specifically need their own feedback loop rather than borrowing executor's.

---

## Longer-term / aspirational

**Where this sits relative to NVIDIA's "AI Factory" framing.** The agentic orchestration pattern (plan → build → verify → improve, quality gates, feedback loop) is a real, working instance of that shape — the gap is entirely in the infrastructure/serving layer (GPU-dense datacenters, optimized inference serving like NIM/Triton/TensorRT-LLM, real data-curation-and-fine-tuning loops), which this project deliberately doesn't have. The honest framing: this isn't "a smaller AI Factory," it's a different bet — bringing the *pattern* to commodity/local hardware, trading raw throughput for accessibility, privacy, and cost. If that bet is ever pushed further:
1. GPU-backed serving (a single GPU box running vLLM, or an actual NVIDIA NIM container) would close most of the reliability gap fought today — this is the point where "self-hosted but small" could start approaching what a hosted small model (Gemini Flash/Lite, etc.) already gives you, without breaking the self-hosted premise.
2. Real fine-tuning loops (vs. today's prompt-level skill-file rewriting) — a much heavier mechanism, only worth it if prompt-level self-improvement demonstrably plateaus even with a better base model.

**Dynamic test generation (tier 4, beyond the drafted tier-3 list).** Using assessor's accumulated `gaps` data (currently written to semantic memory, currently unused by anything) to generate genuinely new test cases targeting the swarm's actual observed weaknesses. Deliberately not pursued today because of the self-grading risk: a model can't reliably invent a hard test *and* grade its own attempt without an independent anchor. If ever built, needs either a human-approval gate on generated candidates, or a distinctly different/larger model doing the generating and grading — never the same model judging its own homework.

**CXP as an actual protocol**, not just an internal schema. Already sketched in [`architecture.md`](architecture.md#is-this-actually-a-protocol) — a standalone spec doc independent of the Python implementation, a `schema_version` field, and at least one external (non-Python, non-this-repo) consumer speaking it. Worth doing only if CXP is ever meant to be adopted by anything outside this repo.

**GitOps (Flux/ArgoCD) migration.** Mentioned in passing when discussing why Helm resource names hardcode a `cxp-` prefix rather than templating off the release name — that naming choice actually fits a GitOps model better (one `HelmRelease` per environment, not multiple installs of the same chart side-by-side). Not otherwise scoped or investigated; would need its own pass if pursued.

**Ad-hoc task history durability.** The web dashboard's Packets/Thinking-stream history is in-memory only — restart the pod (which happened many times today) and it's gone. Only the automated CronJob's test results are durable (via the `bot/test-results` git branch) and episodic memory (via the PVC). If casual interactive use ever needs its own history, it would need the same treatment: writing packet/thinking events somewhere durable instead of just holding them in the FastAPI process's memory.
