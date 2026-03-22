# PYTHON_ARGCOMPLETE_OK
import argparse
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.markdown import Markdown

from helix.api_key_detector import auto_detect_api_key, setup_api_key
from helix.ask import AskHandler
from helix.branding import VERSION, console, cx_header, cx_print, show_banner
from helix.coordinator import InstallationCoordinator, InstallationStep, StepStatus
from helix.installation_history import (
    InstallationHistory,
    InstallationStatus,
    InstallationType,
)
from helix.llm.interpreter import CommandInterpreter
from helix.network_config import NetworkConfig
from helix.stack_manager import StackManager
from helix.validators import validate_api_key, validate_install_request
from helix.version_manager import get_version_string

try:
    import argcomplete
    from helix.completion import no_complete, stack_name_completer
    _ARGCOMPLETE_AVAILABLE = True
except ImportError:
    _ARGCOMPLETE_AVAILABLE = False

# Ensure helix package is importable when run from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger(__name__)


class HelixCLI:
    def __init__(self):
        self.spinner_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.spinner_idx = 0
        self.verbose = False

    def _debug(self, message: str):
        """Print debug info only in verbose mode"""
        if self.verbose:
            console.print(f"[dim][DEBUG] {message}[/dim]")

    def _get_api_key(self) -> str | None:
        # 1. Check explicit provider override first (fake/ollama need no key)
        explicit_provider = os.environ.get("HELIX_PROVIDER", "").lower()
        if explicit_provider == "fake":
            self._debug("Using Fake provider for testing")
            return "fake-key"
        if explicit_provider == "ollama":
            self._debug("Using Ollama (no API key required)")
            return "ollama-local"

        # 2. Try auto-detection + prompt to save (setup_api_key handles both)
        success, key, detected_provider = setup_api_key()
        if success:
            self._debug(f"Using {detected_provider} API key")
            # Store detected provider so _get_provider can use it
            self._detected_provider = detected_provider
            return key

        # Still no key
        self._print_error("No API key found or provided")
        cx_print("Run 'helix wizard' to configure your API key.", "info")
        cx_print("Or use HELIX_PROVIDER=ollama for offline mode.", "info")
        return None

    def _get_provider(self) -> str:
        # Check environment variable for explicit provider choice
        explicit_provider = os.environ.get("HELIX_PROVIDER", "").lower()
        if explicit_provider in ["ollama", "openai", "claude", "fake"]:
            return explicit_provider
        if explicit_provider == "anthropic":
            return "claude"

        # Use provider from auto-detection (set by _get_api_key)
        detected = getattr(self, "_detected_provider", None)
        if detected == "anthropic":
            return "claude"
        elif detected == "openai":
            return "openai"

        # Check env vars (may have been set by auto-detect)
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "claude"
        elif os.environ.get("OPENAI_API_KEY"):
            return "openai"

        # Fallback to Ollama for offline mode
        return "ollama"

    def _print_status(self, emoji: str, message: str):
        """Legacy status print - maps to cx_print for Rich output"""
        status_map = {
            "🧠": "thinking",
            "📦": "info",
            "⚙️": "info",
            "🔍": "info",
        }
        status = status_map.get(emoji, "info")
        cx_print(message, status)

    def _print_error(self, message: str):
        cx_print(f"Error: {message}", "error")

    def _print_success(self, message: str):
        cx_print(message, "success")

    def _animate_spinner(self, message: str):
        sys.stdout.write(f"\r{self.spinner_chars[self.spinner_idx]} {message}")
        sys.stdout.flush()
        self.spinner_idx = (self.spinner_idx + 1) % len(self.spinner_chars)
        time.sleep(0.1)

    def _clear_line(self):
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def _resolve_daemon_script_path(self, script_name: str) -> str:
        """Resolve daemon script path for both repo and alternate layouts."""
        this_file = Path(__file__).resolve()
        candidates: list[Path] = []

        # Walk upward from this module and look for daemon/scripts/<script>
        for parent in this_file.parents:
            candidates.append(parent / "daemon" / "scripts" / script_name)

        # Fallbacks from current working directory
        cwd = Path.cwd().resolve()
        candidates.append(cwd / "daemon" / "scripts" / script_name)
        candidates.append(cwd / "scripts" / script_name)

        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)

        checked = "\n  - " + "\n  - ".join(str(c) for c in candidates)
        raise FileNotFoundError(
            f"Could not locate daemon script '{script_name}'. Checked:{checked}"
        )

    # ─── ask ────────────────────────────────────────────────────────────

    def ask(self, question: str) -> int:
        """Answer a natural language question about the system."""
        api_key = self._get_api_key()
        if not api_key:
            return 1

        provider = self._get_provider()
        self._debug(f"Using provider: {provider}")

        try:
            handler = AskHandler(
                api_key=api_key,
                provider=provider,
            )
            answer = handler.ask(question)
            # Render as markdown for proper formatting in terminal
            console.print(Markdown(answer))
            return 0
        except ImportError as e:
            # Provide a helpful message if provider SDK is missing
            self._print_error(str(e))
            cx_print(
                "Install the required SDK or set HELIX_PROVIDER=ollama for local mode.", "info"
            )
            return 1
        except ValueError as e:
            self._print_error(str(e))
            return 1
        except RuntimeError as e:
            self._print_error(str(e))
            return 1

    # ─── install confirmation ────────────────────────────────────────────

    def _confirm_commands(self, commands: list[str], prompt: str = "") -> tuple[str, list[str] | str]:
        """
        Prompt user to confirm, edit, regenerate, or discard commands.
        Returns ('proceed', commands), ('edited', edited_commands),
                ('discard', commands), or ('regenerate', detail_str).
        """
        original_commands = list(commands)

        try:
            answer = input("\nProceed with installation? [Y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return ("discard", commands)

        if answer in ("y", "yes"):
            return ("proceed", commands)

        # Show 3 options
        print()
        print("  [e] Edit       - Open nano to edit the commands")
        print("  [r] Regenerate - Send back to AI for better commands")
        print("  [d] Discard    - Cancel installation")
        print()

        try:
            choice = input("Choice [e/r/d]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return ("discard", commands)

        if choice == "e":
            import os
            import subprocess
            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="helix_cmds_"
            ) as f:
                tmp_path = f.name
                f.write("# Edit the commands below. One command per line.\n")
                f.write("# Lines starting with # are ignored. Save and close to continue.\n\n")
                for cmd in commands:
                    f.write(cmd + "\n")

            try:
                subprocess.run(["nano", tmp_path])
                with open(tmp_path) as f:
                    edited = [
                        line.strip()
                        for line in f.readlines()
                        if line.strip() and not line.strip().startswith("#")
                    ]
            except FileNotFoundError:
                cx_print("nano not found. Keeping original commands.", "warning")
                edited = list(original_commands)
            finally:
                os.unlink(tmp_path)

            if not edited:
                cx_print("No commands after editing. Installation discarded.", "warning")
                return ("discard", commands)

            print("\nEdited commands:")
            for i, cmd in enumerate(edited, 1):
                print(f"  {i}. {cmd}")
            return ("edited", edited)

        elif choice == "r":
            try:
                detail = input("\nDescribe what's wrong or add more detail: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.")
                return ("discard", commands)
            return ("regenerate", detail)

        else:
            cx_print("Installation discarded.", "info")
            return ("discard", commands)

    # ─── install ────────────────────────────────────────────────────────

    def install(
        self,
        software: str,
        execute: bool = False,
        dry_run: bool = False,
        parallel: bool = False,
        json_output: bool = False,
        sandbox: bool | None = None,
    ):
        # Initialize installation history
        history = InstallationHistory()
        install_id = None
        start_time = datetime.now()

        # Validate input first
        is_valid, error = validate_install_request(software)
        if not is_valid:
            if json_output:
                print(json.dumps({"success": False, "error": error, "error_type": "ValueError"}))
            else:
                self._print_error(error)
            return 1

        # Special-case the ml-cpu stack:
        normalized = " ".join(software.split()).lower()

        if normalized == "pytorch-cpu jupyter numpy pandas":
            software = (
                "pip3 install torch torchvision torchaudio "
                "--index-url https://download.pytorch.org/whl/cpu && "
                "pip3 install jupyter numpy pandas"
            )

        api_key = self._get_api_key()
        if not api_key:
            error_msg = "No API key found. Please configure an API provider."
            try:
                packages = [software.split()[0]]
                install_id = history.record_installation(
                    InstallationType.INSTALL, packages, [], start_time
                )
            except Exception:
                pass

            if install_id:
                history.update_installation(install_id, InstallationStatus.FAILED, error_msg)

            if json_output:
                print(
                    json.dumps({"success": False, "error": error_msg, "error_type": "RuntimeError"})
                )
            else:
                self._print_error(error_msg)
            return 1

        provider = self._get_provider()
        self._debug(f"Using provider: {provider}")
        self._debug(f"API key: {api_key[:10]}...{api_key[-4:]}")

        try:
            if not json_output:
                self._print_status("🧠", "Understanding request...")

            interpreter = CommandInterpreter(api_key=api_key, provider=provider)

            if not json_output:
                self._print_status("📦", "Planning installation...")

                for _ in range(10):
                    self._animate_spinner("Analyzing system requirements...")
                self._clear_line()

            commands = interpreter.parse(f"install {software}")

            if not commands:
                self._print_error("No commands generated")
                return 1

            # Extract packages from commands for tracking
            packages = history._extract_packages_from_commands(commands)

            # If JSON output requested, return structured data and exit early
            if json_output:
                output = {
                    "success": True,
                    "commands": commands,
                    "packages": packages,
                    "install_id": install_id,
                }
                print(json.dumps(output, indent=2))
                return 0

            self._print_status("⚙️", f"Installing {software}...")
            print("\nGenerated commands:")
            for i, cmd in enumerate(commands, 1):
                print(f"  {i}. {cmd}")

            # Confirmation loop (runs for both dry-run and execute)
            edit_id: int | None = None
            original_commands = list(commands)
            while True:
                action, result = self._confirm_commands(commands, prompt=software)

                if action == "proceed":
                    commands = result
                    break
                elif action == "edited":
                    edited_commands = result
                    edit_id = history.save_edit(
                        software, original_commands, edited_commands, was_installed=False
                    )
                    commands = edited_commands
                    break
                elif action == "discard":
                    return 0
                elif action == "regenerate":
                    detail = result
                    cx_print("Regenerating commands...", "info")
                    try:
                        commands = interpreter.parse(
                            f"install {software}. Additional context: {detail}"
                        )
                    except Exception as e:
                        self._print_error(f"Failed to regenerate: {e}")
                        return 1
                    if not commands:
                        self._print_error("No commands generated")
                        return 1
                    print("\nRegenerated commands:")
                    for i, cmd in enumerate(commands, 1):
                        print(f"  {i}. {cmd}")

            if dry_run or not execute:
                print(f"\n(Dry-run completed. Use --execute to apply changes.)")
                if not execute:
                    print("To execute these commands, run with --execute flag")
                    print(f"Example: helix install {software} --execute")
                return 0

            # Only record to history when actually executing
            if execute:
                install_id = history.record_installation(
                    InstallationType.INSTALL, packages, commands, start_time
                )

            if execute:

                def progress_callback(current, total, step):
                    status_emoji = "⏳"
                    if step.status == StepStatus.SUCCESS:
                        status_emoji = "✅"
                    elif step.status == StepStatus.FAILED:
                        status_emoji = "❌"
                    print(f"\n[{current}/{total}] {status_emoji} {step.description}")
                    print(f"  Command: {step.command}")

                print(f"\nExecuting installation...")

                # ── Sandbox execution path ──
                use_sandbox = sandbox
                if use_sandbox is not False:
                    try:
                        from helix.sandbox.docker_sandbox import DockerSandbox
                        ds = DockerSandbox()
                        if ds.check_docker():
                            if not json_output:
                                cx_print("[sandbox] Docker detected. Testing in container first...", "info")
                            sandbox_result = ds.test_and_promote(
                                commands=commands,
                                packages=packages,
                                dry_run=False,
                            )
                            if sandbox_result.success:
                                self._print_success(f"{software} installed successfully (sandbox-verified)")
                                total_dur = (datetime.now() - start_time).total_seconds()
                                print(f"\nCompleted in {total_dur:.2f} seconds")
                                if install_id:
                                    history.update_installation(install_id, InstallationStatus.SUCCESS)
                                    print(f"\n📝 Installation recorded (ID: {install_id})")
                                return 0
                            else:
                                if use_sandbox is True:
                                    # User explicitly requested sandbox — fail
                                    error_msg = f"Sandbox failed: {sandbox_result.message}"
                                    if install_id:
                                        history.update_installation(install_id, InstallationStatus.FAILED, error_msg)
                                    self._print_error(error_msg)
                                    if sandbox_result.test_results:
                                        for tr in sandbox_result.test_results:
                                            print(f"  {tr.result.value}: {tr.name} — {tr.message}")
                                    return 1
                                else:
                                    # Auto mode — command failed in sandbox, don't run broken commands on host
                                    error_msg = f"Sandbox failed: {sandbox_result.message}"
                                    if install_id:
                                        history.update_installation(install_id, InstallationStatus.FAILED, error_msg)
                                    self._print_error(error_msg)
                                    return 1
                        elif use_sandbox is True:
                            self._print_error("Docker not available. Install Docker or use --no-sandbox.")
                            return 1
                        else:
                            cx_print("[info] Docker not found. Running without sandbox.", "info")
                    except ImportError:
                        if use_sandbox is True:
                            self._print_error("Sandbox module not available.")
                            return 1

                if parallel:
                    import asyncio

                    from helix.install_parallel import run_parallel_install

                    def parallel_log_callback(message: str, level: str = "info"):
                        if level == "success":
                            cx_print(f"  ✅ {message}", "success")
                        elif level == "error":
                            cx_print(f"  ❌ {message}", "error")
                        else:
                            cx_print(f"  ℹ {message}", "info")

                    try:
                        success, parallel_tasks = asyncio.run(
                            run_parallel_install(
                                commands=commands,
                                descriptions=[f"Step {i + 1}" for i in range(len(commands))],
                                timeout=300,
                                stop_on_error=True,
                                log_callback=parallel_log_callback,
                            )
                        )

                        total_duration = 0.0
                        if parallel_tasks:
                            max_end = max(
                                (t.end_time for t in parallel_tasks if t.end_time is not None),
                                default=None,
                            )
                            min_start = min(
                                (t.start_time for t in parallel_tasks if t.start_time is not None),
                                default=None,
                            )
                            if max_end is not None and min_start is not None:
                                total_duration = max_end - min_start

                        if success:
                            self._print_success(f"{software} installed successfully")
                            print(
                                f"\nCompleted in {total_duration:.2f} seconds"
                            )

                            if install_id:
                                history.update_installation(install_id, InstallationStatus.SUCCESS)
                                print(f"\n📝 Installation recorded (ID: {install_id})")
                                if edit_id and edit_id > 0:
                                    history.mark_edit_installed(edit_id)

                            return 0

                        failed_tasks = [
                            t for t in parallel_tasks if getattr(t.status, "value", "") == "failed"
                        ]
                        error_msg = failed_tasks[0].error if failed_tasks else "Installation failed"

                        if install_id:
                            history.update_installation(
                                install_id,
                                InstallationStatus.FAILED,
                                error_msg,
                            )

                        self._print_error("Installation failed")
                        if error_msg:
                            print(f"  Error: {error_msg}", file=sys.stderr)
                        if install_id:
                            print(f"\n📝 Installation recorded (ID: {install_id})")
                        return 1

                    except (ValueError, OSError) as e:
                        if install_id:
                            history.update_installation(
                                install_id, InstallationStatus.FAILED, str(e)
                            )
                        self._print_error(f"Parallel execution failed: {str(e)}")
                        return 1
                    except Exception as e:
                        if install_id:
                            history.update_installation(
                                install_id, InstallationStatus.FAILED, str(e)
                            )
                        self._print_error(f"Unexpected parallel execution error: {str(e)}")
                        if self.verbose:
                            import traceback

                            traceback.print_exc()
                        return 1

                coordinator = InstallationCoordinator(
                    commands=commands,
                    descriptions=[f"Step {i + 1}" for i in range(len(commands))],
                    timeout=300,
                    stop_on_error=True,
                    progress_callback=progress_callback,
                )

                result = coordinator.execute()

                if result.success:
                    self._print_success(f"{software} installed successfully")
                    print(f"\nCompleted in {result.total_duration:.2f} seconds")

                    # Record successful installation
                    if install_id:
                        history.update_installation(install_id, InstallationStatus.SUCCESS)
                        print(f"\n📝 Installation recorded (ID: {install_id})")
                        if edit_id and edit_id > 0:
                            history.mark_edit_installed(edit_id)

                    return 0
                else:
                    # Record failed installation
                    if install_id:
                        error_msg = result.error_message or "Installation failed"
                        history.update_installation(
                            install_id, InstallationStatus.FAILED, error_msg
                        )

                    if result.failed_step is not None:
                        self._print_error(f"Installation failed at step {result.failed_step + 1}")
                    else:
                        self._print_error("Installation failed")
                    if result.error_message:
                        print(f"  Error: {result.error_message}", file=sys.stderr)
                    if install_id:
                        print(f"\n📝 Installation recorded (ID: {install_id})")
                    return 1
            return 0

        except ValueError as e:
            if install_id:
                history.update_installation(install_id, InstallationStatus.FAILED, str(e))
            if json_output:
                print(json.dumps({"success": False, "error": str(e), "error_type": "ValueError"}))
            else:
                self._print_error(str(e))
            return 1
        except RuntimeError as e:
            if install_id:
                history.update_installation(install_id, InstallationStatus.FAILED, str(e))
            if json_output:
                print(json.dumps({"success": False, "error": str(e), "error_type": "RuntimeError"}))
            else:
                self._print_error(f"API call failed: {str(e)}")
            return 1
        except OSError as e:
            if install_id:
                history.update_installation(install_id, InstallationStatus.FAILED, str(e))
            if json_output:
                print(json.dumps({"success": False, "error": str(e), "error_type": "OSError"}))
            else:
                self._print_error(f"System error: {str(e)}")
            return 1
        except Exception as e:
            if install_id:
                history.update_installation(install_id, InstallationStatus.FAILED, str(e))
            self._print_error(f"Unexpected error: {str(e)}")
            if self.verbose:
                import traceback

                traceback.print_exc()
            return 1

    # ─── uninstall ────────────────────────────────────────────────────

    def uninstall(
        self,
        software: str,
        execute: bool = False,
        dry_run: bool = False,
    ):
        """Remove a package using the appropriate package manager."""
        history = InstallationHistory()
        install_id = None
        start_time = datetime.now()

        # Validate input
        is_valid, error = validate_install_request(software)
        if not is_valid:
            self._print_error(error)
            return 1

        api_key = self._get_api_key()
        if not api_key:
            self._print_error("No API key found. Please configure an API provider.")
            return 1

        provider = self._get_provider()
        self._debug(f"Using provider: {provider}")

        try:
            self._print_status("🧠", "Understanding removal request...")

            interpreter = CommandInterpreter(api_key=api_key, provider=provider)

            self._print_status("🗑️", "Planning removal...")
            for _ in range(10):
                self._animate_spinner("Analyzing dependencies...")
            self._clear_line()

            commands = interpreter.parse(f"uninstall {software}")

            if not commands:
                self._print_error("No commands generated")
                return 1

            # Extract packages from commands for tracking
            packages = history._extract_packages_from_commands(commands)

            self._print_status("⚙️", f"Removing {software}...")
            print("\nGenerated commands:")
            for i, cmd in enumerate(commands, 1):
                print(f"  {i}. {cmd}")

            # Dry-run is the default for uninstall (destructive operation)
            if not execute or dry_run:
                print(f"\n(Dry-run completed. Use --execute to apply changes.)")
                print(f"Example: helix uninstall {software} --execute")
                return 0

            # Only record to history when actually executing
            install_id = history.record_installation(
                InstallationType.REMOVE, packages, commands, start_time
            )

            # Execute removal
            def progress_callback(current, total, step):
                status_emoji = "⏳"
                if step.status == StepStatus.SUCCESS:
                    status_emoji = "✅"
                elif step.status == StepStatus.FAILED:
                    status_emoji = "❌"
                print(f"\n[{current}/{total}] {status_emoji} {step.description}")
                print(f"  Command: {step.command}")

            print(f"\nExecuting removal...")

            coordinator = InstallationCoordinator(
                commands=commands,
                descriptions=[f"Step {i + 1}" for i in range(len(commands))],
                timeout=300,
                stop_on_error=True,
                progress_callback=progress_callback,
            )

            result = coordinator.execute()

            if result.success:
                self._print_success(f"{software} removed successfully")
                print(f"\nCompleted in {result.total_duration:.2f} seconds")

                if install_id:
                    history.update_installation(install_id, InstallationStatus.SUCCESS)
                    print(f"\n📝 Removal recorded (ID: {install_id})")
                return 0
            else:
                if install_id:
                    error_msg = result.error_message or "Removal failed"
                    history.update_installation(
                        install_id, InstallationStatus.FAILED, error_msg
                    )

                if result.failed_step is not None:
                    self._print_error(f"Removal failed at step {result.failed_step + 1}")
                else:
                    self._print_error("Removal failed")
                if result.error_message:
                    print(f"  Error: {result.error_message}", file=sys.stderr)
                if install_id:
                    print(f"\n📝 Removal recorded (ID: {install_id})")
                return 1

        except ValueError as e:
            if install_id:
                history.update_installation(install_id, InstallationStatus.FAILED, str(e))
            self._print_error(str(e))
            return 1
        except RuntimeError as e:
            if install_id:
                history.update_installation(install_id, InstallationStatus.FAILED, str(e))
            self._print_error(f"API call failed: {str(e)}")
            return 1
        except Exception as e:
            if install_id:
                history.update_installation(install_id, InstallationStatus.FAILED, str(e))
            self._print_error(f"Unexpected error: {str(e)}")
            if self.verbose:
                import traceback

                traceback.print_exc()
            return 1

    # ─── sandbox ─────────────────────────────────────────────────────────

    def sandbox(self, action: str | None = None):
        """Manage Docker sandbox environments."""
        from helix.sandbox.docker_sandbox import DockerSandbox

        ds = DockerSandbox()

        if action == "status":
            available = ds.check_docker()
            if available:
                cx_print("Docker: available", "success")
            else:
                cx_print("Docker: not found", "error")
                cx_print("Install: https://docs.docker.com/get-docker/", "info")

            sandboxes = ds.list_sandboxes()
            if sandboxes:
                print(f"\nActive sandboxes: {len(sandboxes)}")
                for sb in sandboxes:
                    print(f"  {sb.name} ({sb.state.value}) — {sb.image} — {', '.join(sb.packages) or 'no packages'}")
            else:
                print("\nNo active sandboxes.")
            return 0

        elif action == "cleanup":
            count = ds.cleanup_all()
            if count:
                cx_print(f"Removed {count} sandbox(es)", "success")
            else:
                cx_print("No sandboxes to clean up", "info")
            return 0

        else:
            print("Usage: helix sandbox {status|cleanup}")
            return 1

    # ─── stack ──────────────────────────────────────────────────────────

    def resolve(self, args: argparse.Namespace) -> int:
        """Resolve dependency conflicts for Linux packages or parse resolver errors."""
        from helix.dependency_resolver import DependencyResolver, PackageEcosystem
        from helix.error_parser import ErrorParser

        if getattr(args, "error", None) or getattr(args, "error_file", None):
            error_message = ""
            if args.error_file:
                try:
                    with open(args.error_file, encoding="utf-8") as f:
                        error_message = f.read()
                except OSError as e:
                    self._print_error(f"Could not read error file: {e}")
                    return 1
            else:
                error_message = args.error

            parser = ErrorParser()
            analysis = parser.parse_error(error_message)

            if args.json:
                output = {
                    "package_manager": parser.package_manager,
                    "primary_category": analysis.primary_category.value,
                    "severity": analysis.severity,
                    "is_fixable": analysis.is_fixable,
                    "suggested_fixes": analysis.suggested_fixes,
                    "automatic_fix_available": analysis.automatic_fix_available,
                    "automatic_fix_command": analysis.automatic_fix_command,
                }
                print(json.dumps(output, indent=2))
                return 0

            parser.print_analysis(analysis)
            return 0

        resolver = DependencyResolver()

        ecosystem = getattr(args, "ecosystem", PackageEcosystem.LINUX.value)
        if ecosystem != PackageEcosystem.LINUX.value:
            project_path = getattr(args, "project_path", ".")
            universal_plan = resolver.analyze_project_conflicts(
                project_path=project_path,
                ecosystem=PackageEcosystem(ecosystem),
            )

            if args.json:
                print(json.dumps(universal_plan, indent=2))
                return 0

            print("\n🧩 Universal dependency conflict analysis")
            print("=" * 60)
            print(f"Ecosystem: {universal_plan['ecosystem']}")
            print(f"Project path: {universal_plan['project_path']}")
            print(f"Total conflicts: {universal_plan['total_conflicts']}")

            for name, analysis in universal_plan["analyses"].items():
                print(f"\n{name.upper()}:")
                print(f"  Conflicts: {len(analysis.get('conflicts', []))}")
                if analysis.get("runtime_issues"):
                    for issue in analysis["runtime_issues"]:
                        print(f"  Runtime issue: {issue}")
                for conflict in analysis.get("conflicts", []):
                    print(
                        f"  - {conflict.get('package')}: {', '.join(conflict.get('constraints', []))}"
                    )

                commands = analysis.get("resolution_commands", [])
                if commands:
                    print("  Suggested commands:")
                    for cmd in commands:
                        print(f"    {cmd}")

            if universal_plan["safe_to_auto_apply"]:
                cx_print("No manifest-level conflicts detected.", "success")
                return 0

            self._print_error("Conflicts detected. Review the suggested commands above.")
            return 1

        if not getattr(args, "package", None):
            self._print_error("Please provide a package name or --error/--error-file input")
            return 1

        if args.tree:
            print(f"\n📦 Dependency tree for {args.package}:")
            print("=" * 60)
            resolver.print_dependency_tree(args.package)

        plan = resolver.generate_conflict_resolution_plan(
            args.package,
            auto_remove_conflicts=args.auto_remove_conflicts,
        )

        if args.json:
            print(json.dumps(plan, indent=2))
            return 0

        print(f"\n🛠️  Dependency conflict resolution plan for: {args.package}")
        print("=" * 60)
        print(f"Package manager: {plan['package_manager']}")
        print(f"Dependency source: {plan.get('dependency_source', 'metadata')}")
        print(f"Conflict source: {plan.get('conflict_source', 'metadata')}")
        print(f"Conflicts detected: {plan['conflicts_detected']}")
        print(f"Missing dependencies: {len(plan['missing_dependencies'])}")

        if plan["conflicts"]:
            print("\n⚠️  Conflicts:")
            for pkg1, pkg2 in plan["conflicts"]:
                print(f"  - {pkg1} conflicts with {pkg2}")
            if plan["removable_conflicts"] and not args.auto_remove_conflicts:
                cx_print(
                    "Use --auto-remove-conflicts to include removal commands in the plan.",
                    "warning",
                )

        if plan["resolution_commands"]:
            print("\n💻 Resolution commands:")
            for i, cmd in enumerate(plan["resolution_commands"], 1):
                print(f"  {i}. {cmd}")
        else:
            cx_print("No resolver commands required. Dependencies appear satisfied.", "success")

        if not args.apply:
            cx_print("Dry run only. Re-run with --apply to execute the plan.", "info")
            return 0

        if not plan["safe_to_auto_apply"]:
            self._print_error(
                "Plan includes unresolved conflicts. Re-run with --auto-remove-conflicts if intended."
            )
            return 1

        if not plan["resolution_commands"]:
            return 0

        def progress_callback(current: int, total: int, step: InstallationStep):
            print(f"[{current}/{total}] {step.description}")
            print(f"  Command: {step.command}")

        coordinator = InstallationCoordinator(
            commands=plan["resolution_commands"],
            descriptions=[f"Resolve step {i + 1}" for i in range(len(plan["resolution_commands"]))],
            timeout=300,
            stop_on_error=True,
            progress_callback=progress_callback,
        )

        result = coordinator.execute()
        if result.success:
            self._print_success("Dependency conflict resolution completed")
            return 0

        self._print_error("Dependency conflict resolution failed")
        if result.error_message:
            print(f"  Error: {result.error_message}", file=sys.stderr)
        return 1

    def stack(self, args: argparse.Namespace) -> int:
        """Handle `helix stack` commands (list/describe/install/dry-run)."""
        try:
            manager = StackManager()

            # Validate --dry-run requires a stack name
            if args.dry_run and not args.name:
                self._print_error(
                    "--dry-run requires a stack name (e.g., `helix stack ml --dry-run`)"
                )
                return 1

            # List stacks (default when no name/describe)
            if args.list or (not args.name and not args.describe):
                return self._handle_stack_list(manager)

            # Describe a specific stack
            if args.describe:
                return self._handle_stack_describe(manager, args.describe)

            # Install a stack (only remaining path)
            return self._handle_stack_install(manager, args)

        except FileNotFoundError as e:
            self._print_error(f"stacks.json not found. Ensure helix/stacks.json exists: {e}")
            return 1
        except ValueError as e:
            self._print_error(f"stacks.json is invalid or malformed: {e}")
            return 1

    def _handle_stack_list(self, manager: StackManager) -> int:
        """List all available stacks."""
        stacks = manager.list_stacks()
        cx_print(f"\n📦 Available Stacks:\n", "info")
        for stack in stacks:
            pkg_count = len(stack.get("packages", []))
            console.print(f"  [green]{stack.get('id', 'unknown')}[/green]")
            console.print(f"    {stack.get('name', 'Unnamed Stack')}")
            console.print(f"    {stack.get('description', 'No description')}")
            console.print(f"    [dim]({pkg_count} packages)[/dim]\n")
        cx_print("Use: helix stack <name> to install a stack", "info")
        return 0

    def _handle_stack_describe(self, manager: StackManager, stack_id: str) -> int:
        """Describe a specific stack."""
        stack = manager.find_stack(stack_id)
        if not stack:
            self._print_error(f"Stack '{stack_id}' not found. Use --list to see available stacks.")
            return 1
        description = manager.describe_stack(stack_id)
        console.print(description)
        return 0

    def _handle_stack_install(self, manager: StackManager, args: argparse.Namespace) -> int:
        """Install a stack with optional hardware-aware selection."""
        original_name = args.name
        suggested_name = manager.suggest_stack(args.name)

        if suggested_name != original_name:
            cx_print(
                f"💡 No GPU detected, using '{suggested_name}' instead of '{original_name}'",
                "info",
            )

        stack = manager.find_stack(suggested_name)
        if not stack:
            self._print_error(f"Stack '{suggested_name}' not found. Use --list to see available stacks.")
            return 1

        packages = stack.get("packages", [])
        if not packages:
            self._print_error(f"Stack '{suggested_name}' has no packages configured.")
            return 1

        if args.dry_run or not getattr(args, "execute", False):
            return self._handle_stack_dry_run(stack, packages)

        return self._handle_stack_real_install(stack, packages)

    def _handle_stack_dry_run(self, stack: dict[str, Any], packages: list[str]) -> int:
        """Preview packages that would be installed without executing."""
        cx_print(f"\n📋 Installing stack: {stack['name']}", "info")
        console.print(f"\nPackages that would be installed:")
        for pkg in packages:
            console.print(f"  • {pkg}")
        console.print(f"\nTotal: {len(packages)} packages")
        cx_print(f"\nDry run only - no commands executed", "warning")
        return 0

    def _handle_stack_real_install(self, stack: dict[str, Any], packages: list[str]) -> int:
        """Install all packages in the stack."""
        cx_print(f"\n🚀 Installing stack: {stack['name']}\n", "success")

        # Batch into a single LLM request
        packages_str = " ".join(packages)
        result = self.install(software=packages_str, execute=True, dry_run=False)

        if result != 0:
            self._print_error(f"Failed to install stack '{stack['name']}'")
            return 1

        self._print_success(f"\n✅ Stack '{stack['name']}' installed successfully!")
        console.print(f"Installed {len(packages)} packages")
        return 0

    # ─── config ─────────────────────────────────────────────────────────

    def config(self, args: argparse.Namespace) -> int:
        """Handle configuration commands."""
        action = getattr(args, "config_action", None)

        if not action:
            cx_print("Please specify a subcommand (show)", "error")
            return 1

        if action == "show":
            return self._config_show()
        else:
            self._print_error(f"Unknown config action: {action}")
            return 1

    def _config_show(self) -> int:
        """Show all current configuration."""
        cx_header("Helix Configuration")

        # API Provider
        provider = self._get_provider()
        console.print(f"[bold]LLM Provider:[/bold]")
        console.print(f"  {provider}")
        console.print()

        # Config paths
        console.print(f"[bold]Config Paths:[/bold]")
        console.print(f"  Preferences: ~/.helix/preferences.yaml")
        console.print(f"  History: ~/.helix/history.db")
        console.print()

        return 0

    # ─── configure ──────────────────────────────────────────────────────

    def configure(self, args: argparse.Namespace) -> int:
        """Auto-generate config files for the current project or a named service."""
        from helix.configure import ProjectConfigurator

        api_key = self._get_api_key()
        if not api_key:
            return 1

        provider = self._get_provider()
        self._debug(f"Using provider: {provider}")

        cx_header("Project Configuration")

        try:
            configurator = ProjectConfigurator(api_key=api_key, provider=provider)
            return configurator.run(
                project_dir=Path.cwd(),
                target=getattr(args, "target", None),
                dry_run=getattr(args, "dry_run", False),
                force=getattr(args, "force", False),
                only=getattr(args, "only", None),
            )
        except KeyboardInterrupt:
            cx_print("Configuration cancelled.", "info")
            return 130
        except Exception as e:
            self._print_error(f"Configuration failed: {e}")
            if self.verbose:
                import traceback
                traceback.print_exc()
            return 1

    # ─── edits ──────────────────────────────────────────────────────────

    def _run_edited_commands(self, edit: dict) -> int:
        """Execute commands from a saved edit entry."""
        import datetime

        commands = edit["edited_cmds"]
        software = edit["prompt"]
        history = InstallationHistory()
        start_time = datetime.datetime.now()

        install_id = history.record_installation(
            InstallationType.INSTALL, [], commands, start_time
        )

        def progress_callback(current, total, step):
            status_emoji = "⏳"
            if step.status == StepStatus.SUCCESS:
                status_emoji = "✅"
            elif step.status == StepStatus.FAILED:
                status_emoji = "❌"
            print(f"\n[{current}/{total}] {status_emoji} {step.description}")
            print(f"  Command: {step.command}")

        coordinator = InstallationCoordinator(
            commands=commands,
            descriptions=[f"Step {i + 1}" for i in range(len(commands))],
            timeout=300,
            stop_on_error=True,
            progress_callback=progress_callback,
        )

        result = coordinator.execute()

        if result.success:
            self._print_success(f"Installation complete")
            if install_id:
                history.update_installation(install_id, InstallationStatus.SUCCESS)
                history.mark_edit_installed(edit["id"])
            return 0
        else:
            self._print_error("Installation failed")
            if install_id:
                history.update_installation(
                    install_id,
                    InstallationStatus.FAILED,
                    result.error_message,
                )
            return 1

    def edits(self, limit: int = 20) -> int:
        """Browse edited command history with arrow-key navigation."""
        import sys
        import termios
        import tty

        history = InstallationHistory()
        edit_list = history.get_edits(limit)

        if not edit_list:
            cx_print("No edit history found.", "info")
            return 0

        idx = 0

        def show_edit(e: dict) -> None:
            console.print()
            cx_header(f"Edit #{e['id']}  —  {e['timestamp'][:19].replace('T', ' ')}")
            console.print(f"[bold]Prompt:[/bold]  {e['prompt']}")
            status_str = "[green]Installed[/green]" if e["was_installed"] else "[yellow]Not installed[/yellow]"
            console.print(f"[bold]Status:[/bold]  {status_str}")
            console.print(f"[bold]Record:[/bold]  {idx + 1} / {len(edit_list)}")
            console.print()
            console.print("[bold yellow]NOTE:[/bold yellow] The commands listed below will be executed if you press [bold]Y[/bold] and if want exit press [bold]Q[/bold] .")
            console.print()
            console.print("[bold]Commands:[/bold]")
            for i, cmd in enumerate(e["edited_cmds"], 1):
                console.print(f"  {i}. {cmd}")
            console.print()
            console.print(
                "[dim]  ↑  previous  |  ↓  next [/dim]"
            )

        def getch() -> str:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    ch2 = sys.stdin.read(2)
                    return ch + ch2
                return ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

        show_edit(edit_list[idx])

        while True:
            try:
                ch = getch()
            except (EOFError, KeyboardInterrupt):
                return 0

            if ch == "\x1b[A":  # up arrow — go to newer edit
                if idx > 0:
                    idx -= 1
                    show_edit(edit_list[idx])
            elif ch == "\x1b[B":  # down arrow — go to older edit
                if idx < len(edit_list) - 1:
                    idx += 1
                    show_edit(edit_list[idx])
            elif ch.lower() == "y":
                console.print()
                cx_print("Starting installation...", "info")
                return self._run_edited_commands(edit_list[idx])
            elif ch.lower() == "q" or ch == "\x03":
                console.print()
                return 0

    # ─── history ────────────────────────────────────────────────────────

    def history(self, limit: int = 20, status: str | None = None, show_id: str | None = None, clear: bool = False):
        """Show installation history"""
        history = InstallationHistory()

        try:
            if clear:
                deleted = history.clear_all()
                if deleted > 0:
                    self._print_success(f"Cleared {deleted} history record(s)")
                else:
                    cx_print("No history records to clear.", "info")
                return 0

            if show_id:
                record = history.get_installation(show_id)

                if not record:
                    self._print_error(f"Installation {show_id} not found")
                    return 1

                console.print(f"\n[bold]Installation Details: {record.id}[/bold]")
                console.print("=" * 60)
                console.print(f"Timestamp:  {record.timestamp}")
                console.print(f"Operation:  {record.operation_type.value}")
                console.print(f"Status:     {record.status.value}")
                if record.duration_seconds:
                    console.print(f"Duration:   {record.duration_seconds:.2f}s")
                else:
                    console.print("Duration:   N/A")
                console.print(f"\nPackages:   {', '.join(record.packages)}")

                if record.error_message:
                    console.print(f"\n[red]Error: {record.error_message}[/red]")

                if record.commands_executed:
                    console.print("\n[bold]Commands executed:[/bold]")
                    for cmd in record.commands_executed:
                        console.print(f"  {cmd}")

                console.print(f"\nRollback available: {record.rollback_available}")
                return 0
            else:
                status_filter = InstallationStatus(status) if status else None
                records = history.get_history(limit, status_filter)

                if not records:
                    cx_print("No installation records found.", "info")
                    return 0

                from rich.table import Table

                table = Table(show_header=True, header_style="bold cyan", box=None)
                table.add_column("ID", style="green")
                table.add_column("Date")
                table.add_column("Operation")
                table.add_column("Packages")
                table.add_column("Status")

                for r in records:
                    date = r.timestamp[:19].replace("T", " ")
                    packages = ", ".join(r.packages[:2])
                    if len(r.packages) > 2:
                        packages += f" +{len(r.packages) - 2}"

                    status_style = "green" if r.status == InstallationStatus.SUCCESS else "red"
                    table.add_row(
                        r.id,
                        date,
                        r.operation_type.value,
                        packages,
                        f"[{status_style}]{r.status.value}[/{status_style}]",
                    )

                console.print(table)
                return 0
        except (ValueError, OSError) as e:
            self._print_error(f"Failed to retrieve history: {str(e)}")
            return 1
        except Exception as e:
            self._print_error(f"Unexpected error retrieving history: {str(e)}")
            if self.verbose:
                import traceback

                traceback.print_exc()
            return 1

    def rollback(self, install_id: str, dry_run: bool = False):
        """Rollback an installation"""
        history = InstallationHistory()

        try:
            success, message = history.rollback(install_id, dry_run)

            if dry_run:
                cx_header("Rollback actions (dry run)")
                console.print(message)
                return 0
            elif success:
                self._print_success(message)
                return 0
            else:
                self._print_error(message)
                return 1
        except (ValueError, OSError) as e:
            self._print_error(f"Rollback failed: {str(e)}")
            return 1
        except Exception as e:
            self._print_error(f"Unexpected rollback error: {str(e)}")
            if self.verbose:
                import traceback

                traceback.print_exc()
            return 1

    def daemon(self, args: argparse.Namespace) -> int:
        """Handle daemon commands: install, uninstall, config, reload-config, version, ping, shutdown, analyze-packages, alerts, security-install, run-tests."""
        action = getattr(args, "daemon_action", None)

        if action == "install":
            return self._daemon_install(args)
        elif action == "uninstall":
            return self._daemon_uninstall(args)
        elif action == "config":
            return self._daemon_config()
        elif action == "reload-config":
            return self._daemon_reload_config()
        elif action == "version":
            return self._daemon_version()
        elif action == "ping":
            return self._daemon_ping()
        elif action == "shutdown":
            return self._daemon_shutdown()
        elif action == "alerts":
            return self._daemon_alerts()
        elif action == "security-install":
            return self._daemon_security_install()
        elif action == "run-tests":
            return self._daemon_run_tests(args)
        elif action == "analyze-packages":
            return self._daemon_analyze_packages()
        else:
            cx_print("Usage: helix daemon <command>", "info")
            return 0

    def _daemon_install(self, args: argparse.Namespace) -> int:
        """Install and enable the daemon systemd service."""
        cx_print("🔧 Daemon Installation", "info")
        
        if not args.execute:
            cx_print("Dry run: daemon installation would proceed with the following steps:", "warning")
            cx_print("  1. Copy helixd binary to /usr/local/bin/", "info")
            cx_print("  2. Copy helixd.service to /etc/systemd/system/", "info")
            cx_print("  3. Create /run/helix directory", "info")
            cx_print("  4. Enable and start helixd service", "info")
            cx_print("Run with --execute to actually install the daemon", "warning")
            return 0
        
        # Run the actual installation script
        try:
            import subprocess
            cx_print("Installing helixd daemon...", "info")
            script_path = self._resolve_daemon_script_path("install.sh")
            result = subprocess.run(["sudo", "bash", script_path], check=True)
            cx_print("Daemon installed successfully!", "success")
            return 0
        except Exception as e:
            self._print_error(f"Failed to install daemon: {str(e)}")
            return 1

    def _daemon_uninstall(self, args: argparse.Namespace) -> int:
        """Stop and remove the daemon systemd service."""
        cx_print("🗑️  Daemon Uninstallation", "info")
        
        if not args.execute:
            cx_print("Dry run: daemon uninstallation would proceed with the following steps:", "warning")
            cx_print("  1. Stop the helixd service", "info")
            cx_print("  2. Disable the helixd service", "info")
            cx_print("  3. Remove helixd binary from /usr/local/bin/", "info")
            cx_print("  4. Remove helixd.service from /etc/systemd/system/", "info")
            cx_print("Run with --execute to actually uninstall the daemon", "warning")
            return 0
        
        # Run the actual uninstallation script
        try:
            import subprocess
            cx_print("Uninstalling helixd daemon...", "info")
            script_path = self._resolve_daemon_script_path("install.sh")
            result = subprocess.run(["sudo", "bash", script_path, "uninstall"], check=True)
            cx_print("Daemon uninstalled successfully!", "success")
            return 0
        except Exception as e:
            self._print_error(f"Failed to uninstall daemon: {str(e)}")
            return 1

    def _daemon_config(self) -> int:
        """Get and display current daemon configuration via IPC."""
        try:
            from helix.daemon_client import DaemonClient, DaemonConnectionError, DaemonNotInstalledError
            
            client = DaemonClient()
            response = client.config_get()
            
            if not response.success:
                self._print_error(f"Failed to get daemon config: {response.error}")
                return 1
            
            cx_print("📋 Daemon Configuration", "info")
            console.print(f"[bold]Socket Path:[/bold] {response.result.get('socket_path')}")
            console.print(f"[bold]Socket Backlog:[/bold] {response.result.get('socket_backlog')}")
            console.print(f"[bold]Socket Timeout (ms):[/bold] {response.result.get('socket_timeout_ms')}")
            console.print(f"[bold]Max Requests/sec:[/bold] {response.result.get('max_requests_per_sec')}")
            console.print(f"[bold]Log Level:[/bold] {response.result.get('log_level')}")
            return 0
            
        except DaemonNotInstalledError as e:
            self._print_error(str(e))
            return 1
        except DaemonConnectionError as e:
            self._print_error(str(e))
            return 1

    def _daemon_reload_config(self) -> int:
        """Reload daemon configuration from disk via IPC."""
        try:
            from helix.daemon_client import DaemonClient, DaemonConnectionError, DaemonNotInstalledError
            
            client = DaemonClient()
            response = client.config_reload()
            
            if not response.success:
                self._print_error(f"Failed to reload daemon config: {response.error}")
                return 1
            
            cx_print("✅ Daemon configuration reloaded successfully", "success")
            return 0
            
        except DaemonNotInstalledError as e:
            self._print_error(str(e))
            return 1
        except DaemonConnectionError as e:
            self._print_error(str(e))
            return 1

    def _daemon_version(self) -> int:
        """Get daemon version information via IPC."""
        try:
            from helix.daemon_client import DaemonClient, DaemonConnectionError, DaemonNotInstalledError
            
            client = DaemonClient()
            response = client.version()
            
            if not response.success:
                self._print_error(f"Failed to get daemon version: {response.error}")
                return 1
            
            cx_print(f"{response.result.get('name')} {response.result.get('version')}", "success")
            return 0
            
        except DaemonNotInstalledError as e:
            self._print_error(str(e))
            return 1
        except DaemonConnectionError as e:
            self._print_error(str(e))
            return 1

    def _daemon_ping(self) -> int:
        """Test daemon connectivity via IPC."""
        try:
            from helix.daemon_client import DaemonClient, DaemonConnectionError, DaemonNotInstalledError
            
            client = DaemonClient()
            response = client.ping()
            
            if not response.success:
                self._print_error(f"Daemon ping failed: {response.error}")
                return 1
            
            cx_print("✅ Daemon is running and responding", "success")
            return 0
            
        except DaemonNotInstalledError as e:
            self._print_error(str(e))
            return 1
        except DaemonConnectionError as e:
            self._print_error(str(e))
            return 1

    def _daemon_shutdown(self) -> int:
        """Request daemon shutdown via IPC."""
        try:
            from helix.daemon_client import DaemonClient, DaemonConnectionError, DaemonNotInstalledError
            
            client = DaemonClient()
            response = client.shutdown()
            
            if not response.success:
                self._print_error(f"Failed to shutdown daemon: {response.error}")
                return 1
            
            cx_print("✅ Daemon shutdown initiated", "success")
            return 0
            
        except DaemonNotInstalledError as e:
            self._print_error(str(e))
            return 1
        except DaemonConnectionError as e:
            self._print_error(str(e))
            return 1

    def _daemon_analyze_packages(self) -> int:
        """Analyze system packages for outdated versions via daemon IPC."""
        from rich.table import Table

        try:
            from helix.daemon_client import DaemonClient, DaemonConnectionError, DaemonNotInstalledError

            cx_print("Analyzing packages...", "thinking")
            client = DaemonClient()
            response = client.analyze_packages()

            if not response.success:
                if "Method not found: packages.analyze" in (response.error or ""):
                    self._print_error(
                        "Your running helixd daemon is outdated and does not support analyze-packages. "
                        "Rebuild and reinstall helixd, then restart the service."
                    )
                    return 1
                self._print_error(f"Package analysis failed: {response.error}")
                return 1

            packages = response.result.get("packages", [])

            if not packages:
                cx_print("All packages are up to date!", "success")
                return 0

            table = Table(
                title=f"Outdated Packages ({len(packages)})",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Package", style="white")
            table.add_column("Current", style="yellow")
            table.add_column("Latest", style="green")

            for pkg in packages:
                table.add_row(
                    pkg.get("name", ""),
                    pkg.get("current_version", ""),
                    pkg.get("latest_version", ""),
                )

            console.print()
            console.print(table)
            console.print()
            cx_print(f"{len(packages)} package(s) can be upgraded.", "info")
            cx_print("Run 'sudo apt upgrade' to update all packages.", "info")
            return 0

        except DaemonNotInstalledError as e:
            self._print_error(str(e))
            return 1
        except DaemonConnectionError as e:
            self._print_error(str(e))
            return 1
        
    def _daemon_alerts(self) -> int:
        """Get security alerts from daemon analysis."""
        try:
            from helix.daemon_client import DaemonClient, DaemonConnectionError, DaemonNotInstalledError

            client = DaemonClient()
            response = client.alerts_get()

            if not response.success:
                self._print_error(f"Failed to get daemon alerts: {response.error}")
                return 1

            result = response.result or {}
            has_alerts = bool(result.get("has_alerts", False))
            missing = int(result.get("missing_security_updates", 0) or 0)
            package_manager = result.get("package_manager", "unknown")

            cx_print(f"Security scan completed (package manager: {package_manager})", "info")
            if not has_alerts:
                cx_print("No security update alerts detected.", "success")
                return 0

            alerts = result.get("alerts", [])
            console.print(f"[bold]Missing security updates:[/bold] {missing}")
            for idx, alert in enumerate(alerts, 1):
                title = alert.get("title", "Security Alert")
                message = alert.get("message", "")
                details = alert.get("details", "")
                console.print(f"\n[bold]{idx}. {title}[/bold]")
                if message:
                    console.print(message)
                if details:
                    console.print("[dim]Details:[/dim]")
                    console.print(f"[dim]{details}[/dim]")

            cx_print("Run 'helix daemon security-install' to install missing security updates.", "warning")
            return 0

        except DaemonNotInstalledError as e:
            self._print_error(str(e))
            return 1
        except DaemonConnectionError as e:
            self._print_error(str(e))
            return 1

    def _daemon_security_install(self) -> int:
        """Install missing security updates via daemon."""
        try:
            from helix.daemon_client import DaemonClient, DaemonConnectionError, DaemonNotInstalledError

            client = DaemonClient()
            response = client.security_patches_install()

            if not response.success:
                error_message = response.error or "unknown error"

                # systemd sandbox may block apt/dpkg write paths for daemon process.
                # Fall back to a local privileged install from CLI context.
                if "Read-only file system" in error_message or "ensure sudo access" in error_message:
                    cx_print(
                        "Daemon install path is restricted on this system. Falling back to local privileged install...",
                        "warning",
                    )
                    return self._daemon_security_install_local()

                self._print_error(f"Failed to install security updates: {error_message}")
                return 1

            result = response.result or {}
            package_manager = result.get("package_manager", "unknown")
            cx_print(f"Security updates installed successfully (package manager: {package_manager})", "success")

            output = result.get("output", "")
            if output:
                console.print("[dim]Installer output:[/dim]")
                console.print(f"[dim]{output}[/dim]")
            return 0

        except DaemonNotInstalledError as e:
            self._print_error(str(e))
            return 1
        except DaemonConnectionError as e:
            cx_print(
                "Daemon is unavailable. Falling back to local privileged install...",
                "warning",
            )
            return self._daemon_security_install_local()

    def _daemon_security_install_local(self) -> int:
        """Install security updates directly from CLI process as a fallback path."""
        try:
            import shutil
            import subprocess

            commands: list[list[str]]
            package_manager: str

            if shutil.which("apt-get"):
                package_manager = "apt"
                commands = [
                    ["sudo", "apt-get", "update"],
                    ["sudo", "apt-get", "upgrade", "-y"],
                ]
            elif shutil.which("dnf"):
                package_manager = "dnf"
                commands = [["sudo", "dnf", "upgrade", "--security", "-y"]]
            elif shutil.which("yum"):
                package_manager = "yum"
                commands = [["sudo", "yum", "update", "--security", "-y"]]
            elif shutil.which("zypper"):
                package_manager = "zypper"
                commands = [["sudo", "zypper", "--non-interactive", "patch", "--category", "security"]]
            elif shutil.which("pacman"):
                package_manager = "pacman"
                commands = [["sudo", "pacman", "-Syu", "--noconfirm"]]
            else:
                self._print_error("Could not detect a supported package manager for security updates")
                return 1

            cx_print(f"Installing security updates locally (package manager: {package_manager})", "info")

            for cmd in commands:
                result = subprocess.run(cmd, check=False)
                if result.returncode != 0:
                    self._print_error(
                        f"Security update command failed with exit code {result.returncode}: {' '.join(cmd)}"
                    )
                    return result.returncode

            cx_print("Security updates installed successfully", "success")
            return 0

        except Exception as e:
            self._print_error(f"Local security update installation failed: {str(e)}")
            return 1
    def _daemon_run_tests(self, args: argparse.Namespace) -> int:
        """Run daemon test suite."""
        try:
            import subprocess
            cx_print("🧪 Running Daemon Tests", "info")
            
            # Determine which tests to run
            cmd = ["bash", self._resolve_daemon_script_path("build.sh")]
            
            if getattr(args, "unit", False):
                cmd.append("--unit")
            elif getattr(args, "integration", False):
                cmd.append("--integration")
            
            if getattr(args, "test", None):
                cmd.extend(["-t", args.test])
            
            if getattr(args, "verbose", False):
                cmd.append("-v")
            
            result = subprocess.run(cmd, check=False)
            return result.returncode
            
        except Exception as e:
            self._print_error(f"Failed to run daemon tests: {str(e)}")
            return 1


# ─── help display ───────────────────────────────────────────────────────

def show_rich_help():
    """Display a beautifully formatted help table using the Rich library."""
    from rich.table import Table

    show_banner(show_version=True)
    console.print()

    console.print("[bold]AI-powered package manager for Linux[/bold]")
    console.print("[dim]Just tell Helix what you want to install.[/dim]")
    console.print()

    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Command", style="green")
    table.add_column("Description")

    table.add_row("ask <question>", "Ask about your system")
    table.add_row("install <pkg>", "Install software")
    table.add_row("resolve <pkg>", "Resolve dependency conflicts")
    table.add_row("uninstall <pkg>", "Remove installed software")
    table.add_row("stack <name>", "Install a pre-built stack")
    table.add_row("history", "View installation history")
    table.add_row("edits", "Browse and re-run edited command history")
    table.add_row("rollback <id>", "Undo a previous installation")
    table.add_row("config show", "Show configuration")
    table.add_row("configure [service]", "Auto-generate config files for project or service")

    console.print(table)
    console.print()
    console.print("[dim]Learn more: https://helixlinux.com/docs[/dim]")


# ─── main ───────────────────────────────────────────────────────────────

def main():
    # Load environment variables from .env files BEFORE accessing any API keys
    from helix.env_loader import load_env

    load_env()

    # Auto-configure network settings (proxy detection, VPN compatibility)
    try:
        network = NetworkConfig(auto_detect=False)

        temp_parser = argparse.ArgumentParser(add_help=False)
        temp_parser.add_argument("command", nargs="?")
        temp_args, _ = temp_parser.parse_known_args()

        NETWORK_COMMANDS = ["install", "uninstall", "resolve", "stack"]

        if temp_args.command in NETWORK_COMMANDS:
            network.detect(check_quality=True)
            network.auto_configure()

    except Exception as e:
        console.print(f"[yellow]⚠️  Network auto-config failed: {e}[/yellow]")

    parser = argparse.ArgumentParser(
        prog="helix",
        description="AI-powered Linux command interpreter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global flags
    parser.add_argument("--version", "-V", action="version", version=f"helix {VERSION}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── Ask command ──
    ask_parser = subparsers.add_parser("ask", help="Ask a question about your system")
    ask_question_arg = ask_parser.add_argument("question", type=str, help="Natural language question")

    # ── Install command ──
    install_parser = subparsers.add_parser("install", help="Install software")
    install_software_arg = install_parser.add_argument("software", type=str, help="Software to install")
    install_parser.add_argument("--execute", action="store_true", help="Execute commands")
    install_parser.add_argument("--dry-run", action="store_true", help="Show commands only")
    install_parser.add_argument(
        "--parallel",
        action="store_true",
        help="Enable parallel execution for multi-step installs",
    )
    install_parser.add_argument(
        "--sandbox",
        action="store_true",
        default=None,
        help="Require Docker sandbox (test in container before installing on host)",
    )
    install_parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="Skip Docker sandbox even if available",
    )

    # ── Resolve command ──
    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Resolve dependency conflicts (linux/python/npm/cargo/ruby)",
    )
    resolve_parser.add_argument("package", nargs="?", help="Package name to resolve")
    resolve_parser.add_argument(
        "--ecosystem",
        choices=["linux", "python", "npm", "cargo", "ruby", "all"],
        default="linux",
        help="Dependency ecosystem to analyze",
    )
    resolve_parser.add_argument(
        "--project-path",
        default=".",
        help="Project path for non-linux ecosystem analysis",
    )
    resolve_parser.add_argument(
        "--tree",
        action="store_true",
        help="Show dependency tree before generating plan",
    )
    resolve_parser.add_argument(
        "--auto-remove-conflicts",
        action="store_true",
        help="Include commands to remove conflicting packages",
    )
    resolve_parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute generated resolution commands",
    )
    resolve_parser.add_argument("--json", action="store_true", help="Output JSON")
    resolve_error_group = resolve_parser.add_mutually_exclusive_group()
    resolve_error_group.add_argument(
        "--error",
        help="Parse an installation error message and suggest fixes",
    )
    resolve_error_group.add_argument(
        "--error-file",
        help="Read installation error from file and suggest fixes",
    )

    # ── Stack command ──
    stack_parser = subparsers.add_parser("stack", help="Manage pre-built package stacks")
    stack_name_arg = stack_parser.add_argument(
        "name", nargs="?", help="Stack name to install (ml, ml-cpu, webdev, devops, data)"
    )
    stack_group = stack_parser.add_mutually_exclusive_group()
    stack_group.add_argument("--list", "-l", action="store_true", help="List all available stacks")
    stack_group.add_argument("--describe", "-d", metavar="STACK", help="Show details about a stack")
    stack_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be installed (requires stack name)"
    )
    stack_parser.add_argument(
        "--execute", action="store_true", help="Execute the installation (default is dry-run)"
    )

    # ── Config command ──
    config_parser = subparsers.add_parser("config", help="Configure Helix settings")
    config_subs = config_parser.add_subparsers(dest="config_action", help="Configuration actions")
    config_subs.add_parser("show", help="Show all current configuration")

    # ── History command ──
    history_parser = subparsers.add_parser("history", help="View installation history")
    history_id_arg = history_parser.add_argument("show_id", nargs="?", help="Installation ID to show details")
    history_parser.add_argument("--limit", type=int, default=20, help="Number of records to show (default: 20)")
    history_parser.add_argument(
        "--status", choices=["success", "failed"], help="Filter by status"
    )
    history_parser.add_argument(
        "--clear", action="store_true", help="Delete all installation history"
    )

    # ── Edits command ──
    edits_parser = subparsers.add_parser("edits", help="Browse and re-run edited command history")
    edits_parser.add_argument("--limit", type=int, default=20, help="Number of edits to show (default: 20)")

    # ── Rollback command ──
    rollback_parser = subparsers.add_parser("rollback", help="Rollback a previous installation")
    rollback_id_arg = rollback_parser.add_argument("id", help="Installation ID to rollback")
    rollback_parser.add_argument("--dry-run", action="store_true", help="Show what would be rolled back")

    # ── Uninstall command ──
    uninstall_parser = subparsers.add_parser("uninstall", help="Remove installed software")
    uninstall_software_arg = uninstall_parser.add_argument("software", type=str, help="Software to remove")
    uninstall_parser.add_argument("--execute", action="store_true", help="Execute removal (dry-run is default)")
    uninstall_parser.add_argument("--dry-run", action="store_true", help="Show commands only (default)")

    # ── Daemon command ──
    daemon_parser = subparsers.add_parser("daemon", help="Manage the helixd background daemon")
    daemon_subs = daemon_parser.add_subparsers(dest="daemon_action", help="Daemon actions")

    # daemon install [--execute]
    daemon_install_parser = daemon_subs.add_parser(
        "install", help="Install and enable the daemon service"
    )
    daemon_install_parser.add_argument(
        "--execute", action="store_true", help="Actually run the installation"
    )

    # daemon uninstall [--execute]
    daemon_uninstall_parser = daemon_subs.add_parser(
        "uninstall", help="Stop and remove the daemon service"
    )
    daemon_uninstall_parser.add_argument(
        "--execute", action="store_true", help="Actually run the uninstallation"
    )

    # daemon config - uses config.get IPC handler
    daemon_subs.add_parser("config", help="Show current daemon configuration")

    # daemon reload-config - uses config.reload IPC handler
    daemon_subs.add_parser("reload-config", help="Reload daemon configuration from disk")

    # daemon version - uses version IPC handler
    daemon_subs.add_parser("version", help="Show daemon version")

    # daemon ping - uses ping IPC handler
    daemon_subs.add_parser("ping", help="Test daemon connectivity")

    # daemon shutdown - uses shutdown IPC handler
    daemon_subs.add_parser("shutdown", help="Request daemon shutdown")

    # daemon analyze-packages - list outdated packages via daemon
    daemon_subs.add_parser("analyze-packages", help="List outdated packages on the system")

    # daemon alerts - run security alert analysis
    daemon_subs.add_parser("alerts", help="Show daemon security alerts and missing security updates")

    # daemon security-install - install missing security updates
    daemon_subs.add_parser("security-install", help="Install missing security updates")

    # daemon run-tests - run daemon test suite
    daemon_run_tests_parser = daemon_subs.add_parser(
        "run-tests",
        help="Run daemon test suite (runs all tests by default when no filters are provided)",
    )
    daemon_run_tests_parser.add_argument("--unit", action="store_true", help="Run only unit tests")
    daemon_run_tests_parser.add_argument(
        "--integration", action="store_true", help="Run only integration tests"
    )
    daemon_run_tests_parser.add_argument(
        "--test",
        "-t",
        type=str,
        metavar="NAME",
        help="Run a specific test (e.g., test_config, test_daemon)",
    )
    daemon_run_tests_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show verbose test output"
    )

    # ── Sandbox command ──
    sandbox_parser = subparsers.add_parser("sandbox", help="Manage Docker sandbox environments")
    sandbox_sub = sandbox_parser.add_subparsers(dest="sandbox_action")
    sandbox_sub.add_parser("status", help="Show Docker availability and active sandboxes")
    sandbox_sub.add_parser("cleanup", help="Remove all sandbox containers")

    # ── Configure command ──
    configure_parser = subparsers.add_parser(
        "configure",
        help="Auto-generate config files for the current project or a named service",
    )
    configure_parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Optional service/tool to configure (e.g. 'samba', 'nginx'). Omit to auto-detect.",
    )
    configure_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing files",
    )
    configure_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config files",
    )
    configure_parser.add_argument(
        "--only",
        metavar="TYPE",
        help="Only generate configs of this type: linting, docker, env, framework, tooling, general, service",
    )

    # Wizard
    subparsers.add_parser("wizard", help="Run the first-run setup wizard (API key configuration)")

    # ── Shell completion ──
    if _ARGCOMPLETE_AVAILABLE:
        ask_question_arg.completer = no_complete
        install_software_arg.completer = no_complete
        stack_name_arg.completer = stack_name_completer
        history_id_arg.completer = no_complete
        rollback_id_arg.completer = no_complete
        uninstall_software_arg.completer = no_complete
        argcomplete.autocomplete(parser)

    # ── Parse and route ──
    args = parser.parse_args()

    if not args.command:
        show_rich_help()
        return 0

    # Initialize the CLI handler
    cli = HelixCLI()

    try:
        if args.command == "ask":
            return cli.ask(args.question)
        elif args.command == "install":
            # Resolve sandbox flag: --sandbox=True, --no-sandbox=None(False), default=None(auto)
            sandbox_flag = None
            if getattr(args, "sandbox", False):
                sandbox_flag = True
            elif getattr(args, "no_sandbox", False):
                sandbox_flag = False
            return cli.install(
                args.software,
                execute=args.execute,
                dry_run=args.dry_run,
                parallel=args.parallel,
                sandbox=sandbox_flag,
            )
        elif args.command == "resolve":
            return cli.resolve(args)
        elif args.command == "stack":
            return cli.stack(args)
        elif args.command == "config":
            return cli.config(args)
        elif args.command == "history":
            return cli.history(args.limit, args.status, getattr(args, "show_id", None), args.clear)
        elif args.command == "edits":
            return cli.edits(args.limit)
        elif args.command == "rollback":
            return cli.rollback(args.id, args.dry_run)
        elif args.command == "uninstall":
            return cli.uninstall(args.software, execute=args.execute, dry_run=args.dry_run)
        elif args.command == "daemon":
            return cli.daemon(args)
        elif args.command == "sandbox":
            return cli.sandbox(getattr(args, "sandbox_action", None))
        elif args.command == "configure":
            return cli.configure(args)
        elif args.command == "wizard":
            from helix.first_run_wizard import FirstRunWizard
            wizard = FirstRunWizard()
            return wizard.run()
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        print("\n❌ Operation cancelled", file=sys.stderr)
        return 130
    except (ValueError, ImportError, OSError) as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"❌ Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
