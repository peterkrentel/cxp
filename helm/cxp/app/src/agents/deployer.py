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

from ..agent_shell import AgentShell, strip_code_fence
from ..packet import CXPPacket

log = logging.getLogger(__name__)

# Only deploy to this namespace — never cluster-admin
DEPLOY_NAMESPACE = os.environ.get("CXP_DEPLOY_NAMESPACE", "cxp-sandbox")
# Minimum score required before deployment
DEPLOY_THRESHOLD = float(os.environ.get("CXP_DEPLOY_THRESHOLD", "0.85"))


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

    async def _deploy_yaml(self, yaml: str) -> dict:
        """kubectl apply in sandbox namespace only."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(yaml)
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
            return {
                "deployed": result.returncode == 0,
                "outcome": "applied" if result.returncode == 0 else "failed",
                "stdout": result.stdout[:400],
                "stderr": result.stderr[:200] if result.returncode != 0 else "",
            }
        except subprocess.TimeoutExpired:
            return {"deployed": False, "outcome": "timeout"}
        finally:
            os.unlink(fname)

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
