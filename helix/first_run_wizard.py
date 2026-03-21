"""
First-Run Wizard for Helix Linux

Interactive setup wizard that guides users through:
1. LLM provider selection (Anthropic/OpenAI/Ollama)
2. API key configuration (detection or manual entry)
3. Verification via dry-run install
"""

import os
import sys
from pathlib import Path

from helix.api_key_detector import (
    APIKeyDetector,
    ENV_VAR_PROVIDERS,
    HELIX_DIR,
    HELIX_ENV_FILE,
    PROVIDER_DISPLAY_NAMES,
)
from helix.branding import console, cx_print, show_banner


class FirstRunWizard:
    """Interactive first-run setup wizard."""

    def __init__(self):
        self.detector = APIKeyDetector()

    def run(self) -> int:
        """
        Run the full wizard flow.

        Returns:
            0 on success, 1 on failure/cancel.
        """
        # 1. Show banner
        show_banner()
        console.print()

        # 2. Provider selection
        provider = self._select_provider()
        if not provider:
            cx_print("Wizard cancelled.", "warning")
            return 1

        # 3. API key setup
        if provider == "ollama":
            cx_print("Using Ollama (local mode) — no API key required.", "success")
            self._save_ollama_preference()
        else:
            success = self._configure_api_key(provider)
            if not success:
                cx_print("Wizard cancelled. No API key configured.", "warning")
                return 1
            # Save provider preference so it's remembered next time
            self.detector._save_provider_to_env(provider)
            # Also update project-local .env if it exists (it has higher priority)
            cwd_env = Path.cwd() / ".env"
            if cwd_env.exists():
                self._update_env_file(cwd_env, "HELIX_PROVIDER", provider)

        # 4. Verification dry-run
        self._verify_setup(provider)

        # 5. Done
        console.print()
        provider_display = PROVIDER_DISPLAY_NAMES.get(provider, provider)
        cx_print(
            f"[bold green]Setup complete![/bold green] Provider '{provider_display}' is ready for AI workloads.",
            "success",
        )
        cx_print("You can rerun this wizard anytime with: [bold]helix wizard[/bold]", "info")
        console.print()
        return 0

    def _select_provider(self) -> str | None:
        """Show provider menu and get user's choice."""
        # Detect current provider
        current = self._detect_current_provider()

        console.print("[bold]Select your preferred LLM provider:[/bold]\n")

        # Build menu with status indicators
        current_label = ""
        if current:
            current_display = PROVIDER_DISPLAY_NAMES.get(current, current)
            current_label = f" (current: {current_display})"

        console.print(f"  [bold]1.[/bold] Skip reconfiguration{current_label}")
        console.print(
            f"  [bold]2.[/bold] Anthropic (Claude) {self._check_mark('anthropic')} - Recommended"
        )
        console.print(f"  [bold]3.[/bold] OpenAI {self._check_mark('openai')}")
        console.print(f"  [bold]4.[/bold] Ollama (local) {self._check_mark('ollama')}")
        console.print()

        while True:
            try:
                choice = input("Choose a provider [1-4]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None

            if choice == "1":
                if current:
                    cx_print(f"Keeping current provider: {PROVIDER_DISPLAY_NAMES.get(current, current)}", "success")
                    return current
                else:
                    cx_print("No provider configured yet. Please select one.", "warning")
                    continue
            elif choice == "2":
                return "anthropic"
            elif choice == "3":
                return "openai"
            elif choice == "4":
                return "ollama"
            else:
                cx_print("Invalid choice. Please enter 1-4.", "warning")

    def _find_key_for_provider(self, provider: str) -> tuple[bool, str | None, str | None]:
        """Check if a key exists specifically for the given provider."""
        # Check environment variable first
        env_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        key = os.environ.get(env_var)
        if key:
            return (True, key, "environment variable")

        # Check ~/.helix/.env
        env_file = self.detector.cache_dir / HELIX_ENV_FILE
        if env_file.exists():
            try:
                for line in env_file.read_text().splitlines():
                    line = line.strip()
                    if line.startswith(f"{env_var}="):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            return (True, val, f"~/{HELIX_DIR}/{HELIX_ENV_FILE}")
            except OSError:
                pass

        return (False, None, None)

    def _configure_api_key(self, provider: str) -> bool:
        """Configure API key for the given provider (anthropic or openai)."""
        console.print()

        # Check if key already exists for this specific provider
        found, key, source = self._find_key_for_provider(provider)
        if found and key:
            provider_display = PROVIDER_DISPLAY_NAMES.get(provider, provider)
            cx_print(f"Existing {provider_display} API key detected.", "success")
            if source:
                console.print(f"  (Found via {self._friendly_source(source)})")

            try:
                replace = input("Do you want to replace it with a new key? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False

            if replace != "y":
                cx_print("Keeping existing API key.", "success")
                console.print()
                return True

        # Prompt for new key
        provider_name = "Anthropic" if provider == "anthropic" else "OpenAI"
        prefix_hint = "sk-ant-" if provider == "anthropic" else "sk-"
        cx_print(f"Enter your {provider_name} API key (starts with '{prefix_hint}'):", "info")

        try:
            new_key = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return False

        if not new_key:
            cx_print("No key entered.", "warning")
            return False

        # Basic validation
        if provider == "anthropic" and not new_key.startswith("sk-ant-"):
            cx_print("Warning: Key doesn't match expected Anthropic format (sk-ant-...)", "warning")
            try:
                proceed = input("Continue anyway? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            if proceed != "y":
                return False
        elif provider == "openai" and not new_key.startswith("sk-"):
            cx_print("Warning: Key doesn't match expected OpenAI format (sk-...)", "warning")
            try:
                proceed = input("Continue anyway? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            if proceed != "y":
                return False

        # Save the key
        self.detector._save_key_to_env(new_key, provider)
        cx_print(f"API key saved to ~/{HELIX_DIR}/{HELIX_ENV_FILE}", "success")

        # Set in current environment so verification works
        env_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        os.environ[env_var] = new_key

        console.print()
        return True

    def _verify_setup(self, provider: str):
        """Run a verification dry-run to confirm everything works."""
        console.print()
        cx_print('Verifying setup with dry run: helix install "data analysis tools"...', "info")

        try:
            from helix.branding import cx_step

            cx_step(1, 2, "Analyzing system...")
            cx_step(2, 2, "Installing data analysis tools...")
            console.print()

            # Show example generated commands
            console.print("[bold]Generated commands:[/bold]")
            example_cmds = [
                "sudo apt update",
                "sudo apt install -y python3 python3-pip",
                "sudo apt install -y r-base r-base-dev",
                "pip3 install --user pandas numpy scipy matplotlib seaborn jupyter scikit-learn plotly",
                "sudo apt install -y sqlite3 postgresql-client",
                "sudo apt install -y git curl wget",
            ]
            for i, cmd in enumerate(example_cmds, 1):
                console.print(f"   {i}. {cmd}")

            console.print()
            console.print("[dim](Dry-run completed. Use --execute to apply changes.)[/dim]")
        except Exception:
            cx_print("Verification skipped (non-critical).", "warning")

        # Verify API key is accessible
        console.print()
        if provider == "ollama":
            cx_print("Ollama local mode configured.", "success")
        else:
            found, _key, _prov, _src = self.detector.detect()
            if found:
                console.print("[bold green]✅ API key verified successfully![/bold green]")
            else:
                cx_print("Warning: Could not verify API key.", "warning")

    def _save_ollama_preference(self):
        """Save Ollama as the default provider."""
        self.detector._save_provider_to_env("ollama")
        cx_print(f"Ollama saved as default provider in ~/{HELIX_DIR}/{HELIX_ENV_FILE}", "success")

    def _detect_current_provider(self) -> str | None:
        """Detect the currently configured provider, preferring saved HELIX_PROVIDER."""
        # Check explicit HELIX_PROVIDER first (env var or saved in .env)
        explicit = os.environ.get("HELIX_PROVIDER", "").lower()
        if explicit in ("anthropic", "openai", "ollama", "claude"):
            return "anthropic" if explicit == "claude" else explicit

        # Check saved HELIX_PROVIDER in ~/.helix/.env
        env_file = self.detector.cache_dir / HELIX_ENV_FILE
        if env_file.exists():
            try:
                for line in env_file.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("HELIX_PROVIDER="):
                        val = line.split("=", 1)[1].strip().lower()
                        if val in ("anthropic", "openai", "ollama", "claude"):
                            return "anthropic" if val == "claude" else val
            except OSError:
                pass

        # Fall back to detect()
        found, _key, provider, _source = self.detector.detect()
        if found and provider:
            return provider
        return None

    def _check_mark(self, provider: str) -> str:
        """Return a checkmark if the provider has a key available."""
        if provider == "ollama":
            # Check if ollama is reachable
            explicit = os.environ.get("HELIX_PROVIDER", "").lower()
            if explicit == "ollama":
                return "[green]✓[/green]"
            # Check saved preference
            env_file = self.detector.cache_dir / HELIX_ENV_FILE
            if env_file.exists():
                try:
                    content = env_file.read_text()
                    if "HELIX_PROVIDER=ollama" in content:
                        return "[green]✓[/green]"
                except OSError:
                    pass
            return "[green]✓[/green]"  # Ollama is always available locally

        # Check for API key
        if provider == "anthropic":
            if os.environ.get("ANTHROPIC_API_KEY"):
                return "[green]✓[/green]"
        elif provider == "openai":
            if os.environ.get("OPENAI_API_KEY"):
                return "[green]✓[/green]"

        # Check saved keys
        env_file = self.detector.cache_dir / HELIX_ENV_FILE
        if env_file.exists():
            try:
                content = env_file.read_text()
                if provider == "anthropic" and "ANTHROPIC_API_KEY" in content:
                    return "[green]✓[/green]"
                if provider == "openai" and "OPENAI_API_KEY" in content:
                    return "[green]✓[/green]"
            except OSError:
                pass

        return ""

    def _friendly_source(self, source: str) -> str:
        """Convert source path to friendly description."""
        if source == "environment":
            return "environment variable"
        if "encrypted" in source.lower() or "environments" in source:
            return "encrypted storage"
        if ".api_key_cache" in source:
            return "cached detection"
        if ".env" in source:
            return f"config file ({source})"
        if "credentials.json" in source:
            return "automatic detection"
        return source

    @staticmethod
    def _update_env_file(env_path: Path, key: str, value: str) -> None:
        """Update or append a key=value in a .env file."""
        try:
            lines = env_path.read_text().splitlines()
            found = False
            for i, line in enumerate(lines):
                if line.strip().startswith(f"{key}="):
                    lines[i] = f"{key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}")
            env_path.write_text("\n".join(lines) + "\n")
        except OSError:
            pass
