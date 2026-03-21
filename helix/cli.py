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
    def __init__(self, verbose: bool = False):
        self.spinner_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.spinner_idx = 0
        self.verbose = verbose

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

    # ─── install ────────────────────────────────────────────────────────

    def install(
        self,
        software: str,
        execute: bool = False,
        dry_run: bool = False,
        parallel: bool = False,
        json_output: bool = False,
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

    # ─── stack ──────────────────────────────────────────────────────────

    def resolve(self, args: argparse.Namespace) -> int:
        """Resolve dependency conflicts for Linux packages or parse resolver errors."""
        from helix.dependency_resolver import DependencyResolver
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

        if not getattr(args, "package", None):
            self._print_error("Please provide a package name or --error/--error-file input")
            return 1

        resolver = DependencyResolver()

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
    table.add_row("rollback <id>", "Undo a previous installation")
    table.add_row("config show", "Show configuration")

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
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")

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

    # ── Resolve command ──
    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Resolve Linux dependency conflicts for a package",
    )
    resolve_parser.add_argument("package", nargs="?", help="Package name to resolve")
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

    # ── Rollback command ──
    rollback_parser = subparsers.add_parser("rollback", help="Rollback a previous installation")
    rollback_id_arg = rollback_parser.add_argument("id", help="Installation ID to rollback")
    rollback_parser.add_argument("--dry-run", action="store_true", help="Show what would be rolled back")

    # ── Uninstall command ──
    uninstall_parser = subparsers.add_parser("uninstall", help="Remove installed software")
    uninstall_software_arg = uninstall_parser.add_argument("software", type=str, help="Software to remove")
    uninstall_parser.add_argument("--execute", action="store_true", help="Execute removal (dry-run is default)")
    uninstall_parser.add_argument("--dry-run", action="store_true", help="Show commands only (default)")

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
    cli = HelixCLI(verbose=args.verbose)

    try:
        if args.command == "ask":
            return cli.ask(args.question)
        elif args.command == "install":
            return cli.install(
                args.software,
                execute=args.execute,
                dry_run=args.dry_run,
                parallel=args.parallel,
            )
        elif args.command == "resolve":
            return cli.resolve(args)
        elif args.command == "stack":
            return cli.stack(args)
        elif args.command == "config":
            return cli.config(args)
        elif args.command == "history":
            return cli.history(args.limit, args.status, getattr(args, "show_id", None), args.clear)
        elif args.command == "rollback":
            return cli.rollback(args.id, args.dry_run)
        elif args.command == "uninstall":
            return cli.uninstall(args.software, execute=args.execute, dry_run=args.dry_run)
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
        if "--verbose" in sys.argv or "-v" in sys.argv:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
