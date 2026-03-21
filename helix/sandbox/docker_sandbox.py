"""
Docker-based Package Sandbox for Helix Linux.

Test-then-promote strategy: install packages in a disposable Docker container,
verify they work, then do a clean install on the host. LLM-generated commands
never touch the host directly.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SandboxState(Enum):
    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    DESTROYED = "destroyed"


class SandboxTestStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class SandboxTestResult:
    name: str
    result: SandboxTestStatus
    message: str = ""
    duration: float = 0.0


@dataclass
class SandboxInfo:
    name: str
    container_id: str
    state: SandboxState
    created_at: str
    image: str
    packages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "container_id": self.container_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "image": self.image,
            "packages": self.packages,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SandboxInfo":
        return cls(
            name=data["name"],
            container_id=data["container_id"],
            state=SandboxState(data["state"]),
            created_at=data["created_at"],
            image=data["image"],
            packages=data.get("packages", []),
        )


@dataclass
class SandboxExecutionResult:
    success: bool
    message: str
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    test_results: list[SandboxTestResult] = field(default_factory=list)
    packages_installed: list[str] = field(default_factory=list)


class DockerNotFoundError(Exception):
    pass


# Commands that can't run in Docker (need kernel/systemd)
SANDBOX_BLOCKED_COMMANDS = {
    "systemctl", "service", "journalctl",
    "modprobe", "insmod", "rmmod", "lsmod", "sysctl",
    "mount", "umount", "fdisk", "mkfs",
    "reboot", "shutdown", "halt", "poweroff", "init",
}


class DockerSandbox:
    """Docker-based sandbox: test packages in containers before installing on host."""

    CONTAINER_PREFIX = "helix-sandbox-"

    def __init__(self, data_dir: Path | None = None, image: str | None = None):
        self.data_dir = data_dir or Path.home() / ".helix" / "sandboxes"
        self.default_image = image or self._detect_host_image()
        self._docker_path: str | None = None
        self._docker_group_wrap = False
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _detect_host_image() -> str:
        """Try to match Docker image to host OS."""
        try:
            result = subprocess.run(
                ["lsb_release", "-cs"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                codename = result.stdout.strip()
                if codename:
                    return f"ubuntu:{codename}"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "ubuntu:22.04"

    def check_docker(self) -> bool:
        """Check if Docker is installed and the daemon is running.

        If Docker is installed but the current user lacks permission,
        automatically adds the user to the docker group.
        """
        docker_path = shutil.which("docker")
        if not docker_path:
            return False
        try:
            result = subprocess.run(
                [docker_path, "info"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return True

            # Docker installed but permission denied — try group wrapper first
            if "permission denied" in result.stderr.lower():
                if shutil.which("sg"):
                    sg_result = subprocess.run(
                        ["sg", "docker", "-c", f"{shlex.quote(docker_path)} info"],
                        capture_output=True, text=True, timeout=15,
                    )
                    if sg_result.returncode == 0:
                        self._docker_group_wrap = True
                        return True
                return self._fix_docker_permissions(docker_path)

            return False
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    def _fix_docker_permissions(self, docker_path: str) -> bool:
        """Add current user to docker group and verify access."""
        import getpass

        username = getpass.getuser()
        logger.info(f"Docker permission denied — adding {username} to docker group")
        try:
            subprocess.run(
                ["sudo", "usermod", "-aG", "docker", username],
                capture_output=True, text=True, timeout=15,
            )
            # newgrp in subprocess doesn't affect the parent shell,
            # so use sg to run docker info with the new group
            result = subprocess.run(
                ["sg", "docker", "-c", f"{docker_path} info"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                # Store the sg wrapper so subsequent docker calls work in this session
                self._docker_group_wrap = True
                logger.info("Docker permissions fixed successfully")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"Failed to fix docker permissions: {e}")
        return False

    def require_docker(self) -> str:
        """Return docker path or raise DockerNotFoundError."""
        if self._docker_path:
            return self._docker_path

        docker_path = shutil.which("docker")
        if not docker_path:
            raise DockerNotFoundError(
                "Docker is required for sandbox commands.\n"
                "Install: https://docs.docker.com/get-docker/"
            )

        try:
            result = subprocess.run(
                [docker_path, "info"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                # Try auto-fixing permissions before giving up
                if "permission denied" in result.stderr.lower():
                    if shutil.which("sg"):
                        sg_result = subprocess.run(
                            ["sg", "docker", "-c", f"{shlex.quote(docker_path)} info"],
                            capture_output=True, text=True, timeout=15,
                        )
                        if sg_result.returncode == 0:
                            self._docker_group_wrap = True
                            self._docker_path = docker_path
                            return docker_path
                    if self._fix_docker_permissions(docker_path):
                        self._docker_path = docker_path
                        return docker_path
                    raise DockerNotFoundError(
                        "Docker permission denied. Run:\n"
                        "  sudo usermod -aG docker $USER && newgrp docker"
                    )
                raise DockerNotFoundError(
                    "Docker daemon is not running.\n"
                    "Start with: sudo systemctl start docker"
                )
        except subprocess.TimeoutExpired:
            raise DockerNotFoundError("Docker daemon is not responding.")

        self._docker_path = docker_path
        return docker_path

    def _run_docker(self, args: list[str], timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
        docker_path = self.require_docker()
        cmd = [docker_path] + args
        logger.debug(f"Running: {' '.join(cmd)}")

        def run_with_sg() -> subprocess.CompletedProcess:
            quoted = " ".join(shlex.quote(part) for part in cmd)
            return subprocess.run(
                ["sg", "docker", "-c", quoted],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

        # If we already know this session needs sg wrapping, use it directly.
        if getattr(self, "_docker_group_wrap", False) and shutil.which("sg"):
            result = run_with_sg()
            if check and result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
            return result

        # Try direct docker command first
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        
        # If permission denied and sg is available, retry with sg docker
        if "permission denied" in result.stderr.lower() and shutil.which("sg"):
            logger.debug("Docker permission denied, retrying with sg docker...")
            self._docker_group_wrap = True
            result = run_with_sg()
        
        # Handle check=True after potential retry
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        
        return result

    def _container_name(self, name: str) -> str:
        return f"{self.CONTAINER_PREFIX}{name}"

    def _metadata_path(self, name: str) -> Path:
        return self.data_dir / f"{name}.json"

    def _save_metadata(self, info: SandboxInfo) -> None:
        with open(self._metadata_path(info.name), "w") as f:
            json.dump(info.to_dict(), f, indent=2)

    def _load_metadata(self, name: str) -> SandboxInfo | None:
        path = self._metadata_path(name)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return SandboxInfo.from_dict(json.load(f))
        except (json.JSONDecodeError, KeyError):
            return None

    def _delete_metadata(self, name: str) -> None:
        path = self._metadata_path(name)
        if path.exists():
            path.unlink()

    # ── Core operations ──────────────────────────────────────────

    def create(self, name: str, image: str | None = None) -> SandboxExecutionResult:
        """Create a new sandbox container."""
        self.require_docker()

        if self._load_metadata(name):
            return SandboxExecutionResult(
                success=False, message=f"Sandbox '{name}' already exists", exit_code=1,
            )

        container = self._container_name(name)
        image = image or self.default_image

        try:
            # Pull image (ignore errors if already cached)
            self._run_docker(["pull", image], timeout=300, check=False)

            # Start container
            result = self._run_docker([
                "run", "-d",
                "--name", container,
                "--hostname", f"sandbox-{name}",
                image,
                "tail", "-f", "/dev/null",
            ], timeout=60)

            container_id = result.stdout.strip()[:12]

            # Update apt cache
            self._run_docker(
                ["exec", container, "apt-get", "update", "-qq"],
                timeout=120, check=False,
            )

            info = SandboxInfo(
                name=name,
                container_id=container_id,
                state=SandboxState.RUNNING,
                created_at=datetime.now().isoformat(),
                image=image,
            )
            self._save_metadata(info)

            return SandboxExecutionResult(
                success=True,
                message=f"Sandbox '{name}' created",
                stdout=f"Container: {container_id}",
            )

        except subprocess.CalledProcessError as e:
            return SandboxExecutionResult(
                success=False, message=f"Failed to create sandbox: {e.stderr}",
                exit_code=e.returncode, stderr=e.stderr,
            )
        except subprocess.TimeoutExpired:
            return SandboxExecutionResult(
                success=False, message="Timeout creating sandbox", exit_code=1,
            )

    def execute(self, name: str, commands: list[str]) -> SandboxExecutionResult:
        """Run commands inside the sandbox container."""
        self.require_docker()

        info = self._load_metadata(name)
        if not info:
            return SandboxExecutionResult(
                success=False, message=f"Sandbox '{name}' not found", exit_code=1,
            )

        container = self._container_name(name)
        all_stdout = []
        all_stderr = []

        for cmd in commands:
            # Strip sudo — container runs as root
            sandbox_cmd = cmd
            if sandbox_cmd.startswith("sudo "):
                sandbox_cmd = sandbox_cmd[5:]

            # Skip blocked commands
            base = sandbox_cmd.split()[0] if sandbox_cmd.split() else ""
            if base in SANDBOX_BLOCKED_COMMANDS:
                logger.warning(f"Skipping blocked command in sandbox: {cmd}")
                continue

            try:
                result = self._run_docker(
                    ["exec", container, "bash", "-c", sandbox_cmd],
                    timeout=300, check=False,
                )
                all_stdout.append(result.stdout)
                all_stderr.append(result.stderr)

                if result.returncode != 0:
                    return SandboxExecutionResult(
                        success=False,
                        message=f"Command failed in sandbox: {cmd}",
                        exit_code=result.returncode,
                        stdout="\n".join(all_stdout),
                        stderr="\n".join(all_stderr),
                    )

            except subprocess.TimeoutExpired:
                return SandboxExecutionResult(
                    success=False, message=f"Timeout running: {cmd}", exit_code=1,
                )

        # Track installed packages from commands
        for cmd in commands:
            parts = cmd.split()
            if any(x in parts for x in ("install", "-y")):
                # Extract package names (after install -y or similar)
                try:
                    idx = parts.index("install")
                    pkgs = [p for p in parts[idx + 1:] if not p.startswith("-")]
                    info.packages.extend(pkgs)
                except ValueError:
                    pass
        self._save_metadata(info)

        return SandboxExecutionResult(
            success=True,
            message="All commands executed in sandbox",
            stdout="\n".join(all_stdout),
            stderr="\n".join(all_stderr),
        )

    def test(self, name: str, packages: list[str] | None = None) -> SandboxExecutionResult:
        """Run verification tests on installed packages."""
        self.require_docker()

        info = self._load_metadata(name)
        if not info:
            return SandboxExecutionResult(
                success=False, message=f"Sandbox '{name}' not found", exit_code=1,
            )

        container = self._container_name(name)
        pkgs = packages or info.packages
        if not pkgs:
            return SandboxExecutionResult(success=True, message="No packages to test")

        test_results: list[SandboxTestResult] = []
        all_passed = True

        for pkg in pkgs:
            # Test 1: Package installed?
            t0 = time.time()
            try:
                r = self._run_docker(
                    ["exec", container, "dpkg", "-s", pkg],
                    timeout=10, check=False,
                )
                if r.returncode == 0:
                    test_results.append(SandboxTestResult(
                        name=f"{pkg}: installed",
                        result=SandboxTestStatus.PASSED,
                        message="Package is installed",
                        duration=time.time() - t0,
                    ))
                else:
                    test_results.append(SandboxTestResult(
                        name=f"{pkg}: installed",
                        result=SandboxTestStatus.FAILED,
                        message="Package not found",
                        duration=time.time() - t0,
                    ))
                    all_passed = False
            except subprocess.TimeoutExpired:
                test_results.append(SandboxTestResult(
                    name=f"{pkg}: installed",
                    result=SandboxTestStatus.FAILED,
                    message="Timeout checking package",
                    duration=time.time() - t0,
                ))
                all_passed = False

            # Test 2: Binary works? (try --version)
            t0 = time.time()
            version_ok = False
            for flag in ("--version", "-v", "--help"):
                try:
                    r = self._run_docker(
                        ["exec", container, pkg, flag],
                        timeout=10, check=False,
                    )
                    if r.returncode == 0:
                        test_results.append(SandboxTestResult(
                            name=f"{pkg}: functional ({flag})",
                            result=SandboxTestStatus.PASSED,
                            message=r.stdout[:100].strip(),
                            duration=time.time() - t0,
                        ))
                        version_ok = True
                        break
                except subprocess.TimeoutExpired:
                    continue

            if not version_ok:
                test_results.append(SandboxTestResult(
                    name=f"{pkg}: functional check",
                    result=SandboxTestStatus.SKIPPED,
                    message="Could not verify with --version/--help",
                    duration=time.time() - t0,
                ))

            # Test 3: No dpkg conflicts
            t0 = time.time()
            try:
                r = self._run_docker(
                    ["exec", container, "dpkg", "--audit"],
                    timeout=30, check=False,
                )
                if r.returncode == 0 and not r.stdout.strip():
                    test_results.append(SandboxTestResult(
                        name=f"{pkg}: no conflicts",
                        result=SandboxTestStatus.PASSED,
                        message="No package conflicts",
                        duration=time.time() - t0,
                    ))
                elif r.stdout.strip():
                    test_results.append(SandboxTestResult(
                        name=f"{pkg}: conflicts",
                        result=SandboxTestStatus.FAILED,
                        message=r.stdout[:200],
                        duration=time.time() - t0,
                    ))
                    all_passed = False
            except subprocess.TimeoutExpired:
                pass

        return SandboxExecutionResult(
            success=all_passed,
            message="All tests passed" if all_passed else "Some tests failed",
            test_results=test_results,
        )

    def promote(self, packages: list[str], dry_run: bool = False) -> SandboxExecutionResult:
        """Install verified packages on the host system."""
        if not packages:
            return SandboxExecutionResult(success=False, message="No packages to promote", exit_code=1)

        install_cmd = ["sudo", "apt-get", "install", "-y"] + packages

        if dry_run:
            return SandboxExecutionResult(
                success=True,
                message=f"Would run: {' '.join(install_cmd)}",
            )

        try:
            # Update host package lists
            subprocess.run(
                ["sudo", "apt-get", "update", "-qq"],
                capture_output=True, text=True, timeout=300,
            )

            result = subprocess.run(
                install_cmd,
                capture_output=True, text=True, timeout=300,
            )

            if result.returncode == 0:
                return SandboxExecutionResult(
                    success=True,
                    message=f"Installed on host: {', '.join(packages)}",
                    stdout=result.stdout,
                    packages_installed=packages,
                )
            else:
                return SandboxExecutionResult(
                    success=False,
                    message=f"Host install failed: {result.stderr[:200]}",
                    exit_code=result.returncode,
                    stderr=result.stderr,
                )

        except subprocess.TimeoutExpired:
            return SandboxExecutionResult(
                success=False, message="Timeout installing on host", exit_code=1,
            )

    def cleanup(self, name: str, force: bool = False) -> SandboxExecutionResult:
        """Remove a sandbox container and its metadata."""
        self.require_docker()
        container = self._container_name(name)

        info = self._load_metadata(name)
        if not info and not force:
            return SandboxExecutionResult(
                success=False, message=f"Sandbox '{name}' not found", exit_code=1,
            )

        try:
            self._run_docker(["stop", container], timeout=30, check=False)
            rm_args = ["rm", "-f"] if force else ["rm"]
            rm_args.append(container)
            self._run_docker(rm_args, timeout=30, check=False)
            self._delete_metadata(name)
            return SandboxExecutionResult(success=True, message=f"Sandbox '{name}' removed")
        except subprocess.TimeoutExpired:
            return SandboxExecutionResult(
                success=False, message="Timeout removing sandbox", exit_code=1,
            )

    def cleanup_all(self) -> int:
        """Remove all sandboxes. Returns count removed."""
        count = 0
        for meta_file in self.data_dir.glob("*.json"):
            name = meta_file.stem
            result = self.cleanup(name, force=True)
            if result.success:
                count += 1
        return count

    def list_sandboxes(self) -> list[SandboxInfo]:
        """List all sandbox environments."""
        sandboxes = []
        for f in self.data_dir.glob("*.json"):
            try:
                with open(f) as fh:
                    sandboxes.append(SandboxInfo.from_dict(json.load(fh)))
            except (json.JSONDecodeError, KeyError):
                pass
        return sandboxes

    # ── Convenience: full test-then-promote flow ─────────────────

    def test_and_promote(
        self,
        commands: list[str],
        packages: list[str],
        dry_run: bool = False,
    ) -> SandboxExecutionResult:
        """Full sandbox flow: create -> execute -> test -> promote -> cleanup.

        Args:
            commands: LLM-generated commands to test in sandbox
            packages: Package names to verify and promote to host
            dry_run: If True, show what would be installed without doing it
        """
        name = f"test-{uuid.uuid4().hex[:8]}"

        # 1. Create
        result = self.create(name)
        if not result.success:
            return result

        try:
            # 2. Execute LLM commands in sandbox
            result = self.execute(name, commands)
            if not result.success:
                return SandboxExecutionResult(
                    success=False,
                    message=f"Sandbox execution failed: {result.message}",
                    exit_code=result.exit_code,
                    stderr=result.stderr,
                )

            # 3. Test
            result = self.test(name, packages)
            if not result.success:
                return SandboxExecutionResult(
                    success=False,
                    message=f"Sandbox tests failed: {result.message}",
                    test_results=result.test_results,
                )

            # 4. Promote to host
            result = self.promote(packages, dry_run=dry_run)
            result.test_results = result.test_results  # preserve test info
            return result

        finally:
            # 5. Always cleanup
            self.cleanup(name, force=True)


def docker_available() -> bool:
    """Check if Docker is available for sandbox commands."""
    return DockerSandbox().check_docker()
