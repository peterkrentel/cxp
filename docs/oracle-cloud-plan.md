# Plan: Move off the laptop to an Oracle Cloud "Always Free" VM

Why: today's session hit a real gap — the laptop sleeping mid-session silently caused the kind cluster's control plane to freeze, and a scheduled test run got missed with only a faint `Unauthorized` event as a trace. A cloud VM doesn't sleep when you close the lid.

**One caveat on "always on":** Oracle's free tier can reclaim an "Always Free" instance if it sits genuinely idle (near-zero CPU) for an extended period — documented threshold is around 7 days. Since this swarm does real periodic work (the hourly cron), that should keep it classified as active, but it's not an unconditional guarantee for a truly untouched instance.

## Phase 1 — account + VM (you do this, in your browser)

1. **Sign up** for Oracle Cloud Free Tier. Needs email, phone verification, and a credit/debit card for identity verification — Oracle states no charge unless you explicitly upgrade off the free tier.
2. **Home Region is a one-time, irreversible choice** at signup. The free-tier Ampere A1 (ARM) shapes are popular and sometimes show "out of capacity" in busy regions — if provisioning fails, you may need to try a different region.
3. **Create the instance**: Console → Compute → Instances → Create Instance.
   - Image: Ubuntu, a recent LTS build, **ARM64/aarch64** — same architecture as this Mac, so nothing built today needs porting.
   - Shape: **Ampere → VM.Standard.A1.Flex**, maxed to **4 OCPUs / 24GB RAM** — the full "Always Free" allowance in one instance. More RAM than the current 7.7GB laptop setup that today's whole session was spent fighting contention on.
   - SSH key: reuse an existing local key (e.g., `~/.ssh/id_ed25519.pub` or `~/.ssh/id_rsa.pub`) rather than generating a new one, so the same key already trusted on this Mac works against the new VM too.
4. **Open ports 80/443** on the instance's default Security List (SSH/22 is open by default; the web dashboard needs 80/443 too). **Do not** open NATS's port 4222 to the public internet — that should stay internal/SSH-tunnel-only on a real cloud box, unlike the local kind setup where it's only mapped to localhost.

## Phase 2 — remote setup (once the VM exists and is SSH-reachable)

Given the VM's public IP and SSH access, the remaining setup is mechanical and can be done over SSH from a working session:

1. Install Docker, `kind`, `kubectl`, `helm` on the Ubuntu ARM64 box (all have ARM64 builds).
2. Clone this repo onto the VM (needs either a public HTTPS clone or a deploy key/token — reuse the same `gh auth token` pattern used for `cxp-git-creds` today if a private clone is needed).
3. Run `kind create cluster --config kind-config.yaml` (or `make cluster`), then `make deploy`.
4. Re-pull the models into both Ollama instances, same as done locally today (`ollama pull qwen2.5:1.5b` on the main instance, `qwen2.5:0.5b` on `ollama-small`) — auto-pull is disabled by design (see `values.yaml`'s comments), so this is a manual step every fresh cluster, cloud or local.
5. Verify: `kubectl get pods -n cxp`, check the web dashboard is reachable at the VM's public IP, confirm the hourly CronJob fires and (unlike today) survives without a host sleep interrupting it.

## Open questions for when this actually happens

- Does the VM's public IP get a domain name / reverse proxy, or is bare-IP access fine for now?
- Backup/snapshot strategy for the boot volume, given this becomes the only copy of live cluster state (skills in KV, reputation in `memory.json`) once it's no longer just a disposable local prototype?
- Whether to keep `make reset`'s full-wipe behavior as-is on a "production-ish" always-on box, or add a safeguard so an accidental `make reset` doesn't casually nuke weeks of accumulated skill revisions and episodic history.
