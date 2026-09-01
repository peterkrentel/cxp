"""Deployer agent — applies YAML artifacts in a namespace-scoped kubectl apply.
Python/shell artifacts run as a plain subprocess on this pod — NOT namespace-
or network-isolated. Only the YAML path is actually sandboxed."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time

import yaml

from ..agent_shell import AgentShell, strip_code_fence
from ..candidate_evaluation import build_self_improvement_inputs
from ..packet import CXPPacket, PacketType, Payload

log = logging.getLogger(__name__)

# Only deploy to this namespace — never cluster-admin
DEPLOY_NAMESPACE = os.environ.get("CXP_DEPLOY_NAMESPACE", "cxp-sandbox")
# Minimum score required before deployment
DEPLOY_THRESHOLD = float(os.environ.get("CXP_DEPLOY_THRESHOLD", "0.85"))
# How long to give an applied Deployment to actually become healthy before
# reporting the deploy as failed rather than just "applied". Generous
# enough for a real image pull; a made-up image reference (the common
# case with a small model) shows ImagePullBackOff well within this.
DEPLOY_READY_TIMEOUT_SECONDS = 30
DEPLOY_READY_POLL_SECONDS = 3


class DeployerAgent(AgentShell):
    def __init__(self) -> None:
        super().__init__("deployer-1", capabilities=["deploy"])

    async def _execute(self, packet: CXPPacket) -> str:
        artifact = packet.payload.context or ""
        goal = packet.payload.goal or ""
        score = float(packet.payload.instructions or "0")

        # Safety gate: never deploy below threshold
        if score < DEPLOY_THRESHOLD:
            return json.dumps({
                "deployed": False,
                "reason": f"score {score:.2f} below threshold {DEPLOY_THRESHOLD}",
                "artifact_chars": len(artifact),
            })

        # Backup memory before executing untrusted code
        memory_backup_path = "/data/memory.backup"
        memory_path = "/data/memory.json"
        backup_restored = False

        try:
            # Create backup if memory file exists
            if os.path.exists(memory_path):
                shutil.copy2(memory_path, memory_backup_path)
                log.info(f"💾 Memory backup created: {memory_path} → {memory_backup_path}")

            # Execute the artifact
            result = await self._try_deploy(artifact, goal)
            
            # Check if execution failed
            if not result.get("deployed", False):
                log.warning(f"Deploy failed: {result.get('outcome', 'unknown')} — restoring memory from backup")
                if os.path.exists(memory_backup_path):
                    shutil.copy2(memory_backup_path, memory_path)
                    backup_restored = True
                    log.info(f"✓ Memory restored from backup after failed deploy")

        except Exception as e:
            # Safety net: restore on any exception
            log.error(f"Deploy exception: {e} — restoring memory from backup")
            if os.path.exists(memory_backup_path):
                try:
                    shutil.copy2(memory_backup_path, memory_path)
                    backup_restored = True
                    log.info(f"✓ Memory restored from backup after exception")
                except Exception as restore_err:
                    log.error(f"Failed to restore memory: {restore_err}")
            result = {"deployed": False, "reason": str(e), "outcome": "exception"}

        finally:
            # Clean up backup file
            if os.path.exists(memory_backup_path):
                try:
                    os.unlink(memory_backup_path)
                except Exception as e:
                    log.debug(f"Could not delete backup: {e}")

        # Record deployment in semantic memory only if not restored
        if not backup_restored:
            self._memory.add_semantic(
                f"Deployed: {goal[:80]} — {result.get('outcome', 'unknown')}"
            )
            await self._memory.save()

        # #86: verifier already scored this artifact >= DEPLOY_THRESHOLD (the
        # early return above is the only way to reach this point without
        # that being true) -- a real execution failure here is much stronger,
        # deterministic evidence that something is actually wrong than
        # verifier's own opinion, and previously vanished silently instead of
        # ever reaching reflect. Confirmed live 2026-08-30 twice (a circle-
        # area function and a doubling function, each with a wrong expected
        # value in their own generated test that verifier scored 0.9/passed).
        if not result.get("deployed", False):
            reflect = CXPPacket(
                origin=self.agent_id,
                type=PacketType.REFLECT,
                capability="reflect",
                priority=3,
                task_id=packet.task_id,
                parent_packet_id=packet.id,
                payload=Payload(
                    goal="Self-improve: verifier passed an artifact that failed at actual execution",
                    instructions=(
                        f"Verifier scored this artifact {score:.2f} (>= {DEPLOY_THRESHOLD}) and it "
                        f"was deployed, but it failed at actual execution: {result.get('outcome', 'unknown')}\n"
                        f"stderr: {result.get('stderr', '')[:500]}\n"
                        "Propose a one-paragraph update to the executor skill file to prevent this."
                    ),
                    context=artifact,
                    inputs=build_self_improvement_inputs(
                        target_role="executor", source_attempt_id=packet.task_id,
                        evidence_class="deterministic-validator",
                    ),
                ),
            )
            reflect.append_trace(self.agent_id, "created", "spawned due to deploy failure despite passing score")
            await self.emit_packet(reflect)

        return json.dumps(result)

    async def _try_deploy(self, artifact: str, goal: str) -> dict:
        # Detect artifact type and strip markdown code blocks
        artifact_stripped = artifact.strip()
        log.info(f"Attempting deployment: {goal[:60]}")

        # Strip markdown code blocks (```language ... ```)
        artifact_stripped = strip_code_fence(artifact_stripped)

        if self._looks_like_yaml(artifact_stripped):
            log.debug("Detected YAML artifact")
            return await self._deploy_yaml(artifact_stripped)
        # Shebang is a far more specific signal than a bare "import " substring
        # match, so it must be checked first — a bash script that merely
        # echoes the word "import" was previously misrouted to the Python path.
        elif artifact_stripped.startswith("#!/bin/bash") or artifact_stripped.startswith("#!/bin/sh"):
            log.debug("Detected shell script — wrapping as Python subprocess")
            py = f"import subprocess\nresult = subprocess.run(['bash','-c',{repr(artifact_stripped)}],capture_output=True,text=True,timeout=10)\nprint(result.stdout)\nif result.returncode != 0: raise RuntimeError(result.stderr)"
            return await self._run_python(py, goal)
        elif artifact_stripped.startswith("def ") or "import " in artifact_stripped or artifact_stripped.startswith("print("):
            log.debug("Detected Python artifact")
            return await self._run_python(artifact_stripped, goal)
        else:
            log.warning(f"Unrecognized artifact type: {artifact_stripped[:100]}")
            return {"deployed": False, "reason": "unrecognized artifact type", "preview": artifact_stripped[:200]}

    def _looks_like_yaml(self, text: str) -> bool:
        return any(text.startswith(k) for k in ("apiVersion:", "kind:", "---\napiVersion"))

    async def _deploy_yaml(self, yaml_text: str) -> dict:
        """kubectl apply in sandbox namespace only, then verify any applied
        Deployments actually become healthy. `kubectl apply` succeeding only
        means the API server accepted the manifest -- it says nothing about
        whether the pods it creates ever actually run. Found live
        2026-08-17: every YAML deploy reported "deployed": true even though
        the LLM's invented image references meant every single one was
        permanently stuck in ImagePullBackOff, because nothing after the
        apply ever checked. This was a real signal quietly poisoning
        reflect/memory: every deploy attempt was being recorded as a
        success regardless of whether anything real ever ran."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(yaml_text)
            fname = f.name

        try:
            # dry-run first
            dry = subprocess.run(
                ["kubectl", "apply", "-f", fname, "--dry-run=server",
                 f"--namespace={DEPLOY_NAMESPACE}"],
                capture_output=True, text=True, timeout=30
            )
            if dry.returncode != 0:
                return {"deployed": False, "outcome": "dry-run failed", "stderr": dry.stderr[:400]}

            # real apply
            result = subprocess.run(
                ["kubectl", "apply", "-f", fname, f"--namespace={DEPLOY_NAMESPACE}"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return {
                    "deployed": False, "outcome": "failed",
                    "stdout": result.stdout[:400], "stderr": result.stderr[:200],
                }

            deployment_names = self._deployment_names(yaml_text)
            if deployment_names:
                all_ready, not_ready = self._wait_for_deployments_ready(deployment_names)
                if not all_ready:
                    return {
                        "deployed": False,
                        "outcome": "applied but never became healthy",
                        "stdout": result.stdout[:400],
                        "unhealthy_deployments": not_ready,
                    }

            return {
                "deployed": True,
                "outcome": "applied",
                "stdout": result.stdout[:400],
                "stderr": "",
            }
        except subprocess.TimeoutExpired:
            return {"deployed": False, "outcome": "timeout"}
        finally:
            os.unlink(fname)

    @staticmethod
    def _deployment_names(yaml_text: str) -> list[str]:
        """Names of any Deployment resources in a (possibly multi-document)
        manifest -- the only resource kind here with an objectively
        checkable "did it actually work" signal (readyReplicas), as
        opposed to a Service/ConfigMap which just exist-or-don't."""
        names = []
        try:
            for doc in yaml.safe_load_all(yaml_text):
                if isinstance(doc, dict) and doc.get("kind") == "Deployment":
                    name = (doc.get("metadata") or {}).get("name")
                    if name:
                        names.append(name)
        except yaml.YAMLError:
            pass
        return names

    def _wait_for_deployments_ready(self, names: list[str]) -> tuple[bool, list[str]]:
        """Poll each named Deployment for real readiness. Returns
        (all_ready, names_still_not_ready_at_the_deadline)."""
        deadline = time.time() + DEPLOY_READY_TIMEOUT_SECONDS
        remaining = set(names)
        while remaining and time.time() < deadline:
            for name in list(remaining):
                probe = subprocess.run(
                    ["kubectl", "get", "deployment", name,
                     f"--namespace={DEPLOY_NAMESPACE}", "-o", "json"],
                    capture_output=True, text=True, timeout=10,
                )
                if probe.returncode != 0:
                    continue
                try:
                    ready = json.loads(probe.stdout).get("status", {}).get("readyReplicas", 0)
                except json.JSONDecodeError:
                    ready = 0
                if ready:
                    remaining.discard(name)
            if remaining:
                time.sleep(DEPLOY_READY_POLL_SECONDS)
        return not remaining, sorted(remaining)

    async def _run_python(self, code: str, goal: str) -> dict:
        """Run Python in isolated subprocess with timeout."""
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            fname = f.name

        try:
            # Minimal explicit env — don't hand generated code NATS_URL,
            # OLLAMA_URL, or anything else this pod happens to carry.
            minimal_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": "/app/packages",
            }
            result = subprocess.run(
                ["python3", fname],
                capture_output=True, text=True, timeout=15,
                env=minimal_env,
            )
            return {
                "deployed": result.returncode == 0,
                "outcome": "ran" if result.returncode == 0 else "error",
                "stdout": result.stdout[:400],
                "stderr": result.stderr[:200] if result.returncode != 0 else "",
            }
        except subprocess.TimeoutExpired:
            return {"deployed": False, "outcome": "timeout (15s)"}
        finally:
            os.unlink(fname)
