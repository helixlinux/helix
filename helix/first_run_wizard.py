"""
First-Run Wizard for Helix Linux

Interactive setup wizard: provider selection, API key config, verification dry-run.
Re-run anytime with: helix wizard
"""

import os
from pathlib import Path

from helix.api_key_detector import (
    APIKeyDetector,
    HELIX_DIR,
    HELIX_ENV_FILE,
    PROVIDER_DISPLAY_NAMES,
)
from helix.branding import console, cx_print, cx_step, show_banner


class FirstRunWizard:
    """Interactive first-run setup wizard."""

    def __init__(self):
        self.detector = APIKeyDetector()

    def run(self) -> int:
        try:
            return self._run_steps()
        except KeyboardInterrupt:
            console.print("\n")
            cx_print("Wizard cancelled.", "warning")
            return 1

    def _run_steps(self) -> int:
        # 1. Banner
        show_banner()
        console.print()

        # 2. Provider selection
        provider = self._select_provider()
        if not provider:
            return 1

        # 3. API key setup
        if provider == "ollama":
            cx_print("Using Ollama (local mode) — no API key required.", "success")
            self.detector._save_provider_to_env("ollama")
        else:
            if not self._configure_api_key(provider):
                cx_print("Wizard cancelled. No API key configured.", "warning")
                return 1
            # Save provider preference to ~/.helix/.env only (never touch project .env)
            save_provider = "claude" if provider == "anthropic" else provider
            self.detector._save_provider_to_env(save_provider)

        # 4. Verification dry-run
        self._verify_setup(provider)

        # 5. Done
        console.print()
        provider_display = PROVIDER_DISPLAY_NAMES.get(provider, provider)
        cx_print(f"[✓] Setup complete! Provider '{provider_display}' is ready for AI workloads.", "success")
        cx_print("You can rerun this wizard anytime with: helix wizard", "info")
        console.print()
        return 0

    # ── Provider selection ───────────────────────────────────────────

    def _select_provider(self) -> str | None:
        current = self._detect_current_provider()

        console.print("[bold]Select your preferred LLM provider:[/bold]\n")

        current_label = ""
        if current:
            current_display = PROVIDER_DISPLAY_NAMES.get(current, current)
            current_label = f" (current: {current_display})"

        console.print(f"  [bold]1.[/bold] Skip reconfiguration{current_label}")
        console.print(f"  [bold]2.[/bold] Anthropic (Claude) {self._check_mark('anthropic')} - Recommended")
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
                cx_print("No provider configured yet. Please select one.", "warning")
            elif choice == "2":
                return "anthropic"
            elif choice == "3":
                return "openai"
            elif choice == "4":
                return "ollama"
            else:
                cx_print("Invalid choice. Please enter 1-4.", "warning")

    # ── API key configuration ────────────────────────────────────────

    def _configure_api_key(self, provider: str) -> bool:
        console.print()

        # Check if key already exists
        found, key, source = self._find_key_for_provider(provider)
        if found and key:
            provider_display = PROVIDER_DISPLAY_NAMES.get(provider, provider)
            cx_print(f"Existing {provider_display} API key detected.", "success")
            console.print(f"  (Found via {source})")

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

        # Save the key
        self.detector._save_key_to_env(new_key, provider)
        cx_print(f"API key saved to ~/{HELIX_DIR}/{HELIX_ENV_FILE}", "success")

        # Set in current process so verification works
        env_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        os.environ[env_var] = new_key
        console.print()
        return True

    # ── Verification dry-run ─────────────────────────────────────────

    # Prompts shuffled each run so the dry-run is never the same
    _VERIFY_PROMPTS = [
        "scientific computing tools",
        "web development framework",
        "data analysis tools",
        "machine learning environment",
        "docker and containerization tools",
        "network monitoring utilities",
        "video editing software",
        "game development toolkit",
    ]

    def _verify_setup(self, provider: str):
        import random

        prompt = random.choice(self._VERIFY_PROMPTS)

        console.print()
        cx_print(f'Verifying setup with dry run: helix install "{prompt}"...', "info")

        try:
            from helix.llm.interpreter import CommandInterpreter

            found, key, _, _ = self.detector.detect()
            if not found or not key:
                cx_print("Could not retrieve API key for verification.", "warning")
                return

            interpreter_provider = "claude" if provider == "anthropic" else provider

            cx_step(1, 2, "Analyzing system...")
            interpreter = CommandInterpreter(api_key=key, provider=interpreter_provider)

            cx_step(2, 2, f"Installing {prompt}...")
            commands = interpreter.parse(f"install {prompt}")

            console.print()
            if commands:
                console.print("[bold]Generated commands:[/bold]")
                for i, cmd in enumerate(commands, 1):
                    console.print(f"   {i}. {cmd}")
                console.print()
                console.print("[dim](Dry-run completed. Use --execute to apply changes.)[/dim]")
                console.print()
                console.print("[bold green]✅ API key verified successfully![/bold green]")
            else:
                cx_print("LLM returned no commands.", "warning")

        except Exception as e:
            cx_print(f"Verification failed: {e}", "warning")
            cx_print('Your key was saved but could not be verified. Test with: helix install "curl" --dry-run', "info")

    # ── Helpers ──────────────────────────────────────────────────────

    def _save_provider(self, provider: str):
        save_provider = "claude" if provider == "anthropic" else provider
        self.detector._save_provider_to_env(save_provider)
        os.environ["HELIX_PROVIDER"] = save_provider

    def _detect_current_provider(self) -> str | None:
        explicit = os.environ.get("HELIX_PROVIDER", "").lower()
        if explicit in ("anthropic", "openai", "ollama", "claude"):
            return "anthropic" if explicit == "claude" else explicit

        env_file = self.detector.cache_dir / HELIX_ENV_FILE
        if env_file.exists():
            try:
                for line in env_file.read_text().splitlines():
                    if line.strip().startswith("HELIX_PROVIDER="):
                        val = line.split("=", 1)[1].strip().lower()
                        if val in ("anthropic", "openai", "ollama", "claude"):
                            return "anthropic" if val == "claude" else val
            except OSError:
                pass

        found, _key, provider, _ = self.detector.detect()
        return provider if found else None

    def _find_key_for_provider(self, provider: str) -> tuple[bool, str | None, str | None]:
        env_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        key = os.environ.get(env_var)
        if key:
            return (True, key, "automatic detection")

        env_file = self.detector.cache_dir / HELIX_ENV_FILE
        if env_file.exists():
            try:
                for line in env_file.read_text().splitlines():
                    if line.strip().startswith(f"{env_var}="):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            return (True, val, f"~/{HELIX_DIR}/{HELIX_ENV_FILE}")
            except OSError:
                pass

        return (False, None, None)

    def _check_mark(self, provider: str) -> str:
        if provider == "ollama":
            return "[green]✓[/green]"
        if provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
            return "[green]✓[/green]"
        if provider == "openai" and os.environ.get("OPENAI_API_KEY"):
            return "[green]✓[/green]"

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

    @staticmethod
    def _update_env_file(env_path: Path, key: str, value: str) -> None:
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
