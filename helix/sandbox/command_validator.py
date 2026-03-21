"""
Command Validator for Helix Linux

Validates LLM-generated commands against allowlists and blocklists
before execution. Provides a security layer independent of Docker sandboxing.
"""

import logging
import re
import shlex

logger = logging.getLogger(__name__)


class CommandValidator:
    """Validates commands against allowlists and blocklists."""

    # Allowed base commands for package installation workflows
    ALLOWED_COMMANDS = {
        "apt-get", "apt", "dpkg",
        "pip", "pip3", "python", "python3",
        "npm", "yarn", "node",
        "git", "make", "cmake", "gcc", "g++", "clang",
        "curl", "wget",
        "tar", "unzip", "zip",
        "echo", "cat", "grep", "sed", "awk",
        "ls", "pwd", "mkdir", "touch", "chmod",
    }

    # Sudo commands that are explicitly allowed
    SUDO_ALLOWED_PREFIXES = {
        "apt-get install", "apt-get update", "apt-get upgrade",
        "apt-get remove", "apt-get autoremove", "apt-get purge",
        "apt install", "apt update", "apt upgrade",
        "apt remove", "apt autoremove", "apt purge",
        "dpkg -i", "dpkg --configure",
    }

    # Commands that are always blocked
    BLOCKED_COMMANDS = {
        "rm", "dd", "mkfs", "fdisk", "parted",
        "reboot", "shutdown", "halt", "poweroff", "init",
        "mount", "umount", "modprobe", "insmod", "rmmod",
        "iptables", "ip6tables", "nft",
        "useradd", "userdel", "usermod", "passwd",
        "chroot", "nsenter",
    }

    # Dangerous patterns (regex)
    DANGEROUS_PATTERNS = [
        r"rm\s+(-rf?|--recursive)\s+/",
        r">\s*/dev/sd[a-z]",
        r"mkfs\.",
        r"dd\s+if=",
        r":\(\)\{.*\}",  # fork bomb
        r"\|\s*sh\b",
        r"\|\s*bash\b",
        r"curl\s+.*\|\s*(sudo\s+)?(sh|bash)",
        r"wget\s+.*\|\s*(sudo\s+)?(sh|bash)",
    ]

    def __init__(self):
        self._dangerous_re = [re.compile(p, re.IGNORECASE) for p in self.DANGEROUS_PATTERNS]

    def validate(self, command: str) -> tuple[bool, str | None]:
        """Validate a single command.

        Returns:
            (is_allowed, rejection_reason) — (True, None) if safe.
        """
        command = command.strip()
        if not command:
            return False, "Empty command"

        # Check dangerous patterns first
        for pattern in self._dangerous_re:
            if pattern.search(command):
                return False, f"Blocked: matches dangerous pattern '{pattern.pattern}'"

        # Extract base command (handle sudo prefix)
        parts = command.split()
        base_cmd = parts[0]
        is_sudo = base_cmd == "sudo"

        if is_sudo:
            if len(parts) < 2:
                return False, "Blocked: bare sudo"
            actual_cmd = parts[1]

            # Check if sudo + subcommand is in the allowed prefixes
            rest = " ".join(parts[1:])
            allowed = any(rest.startswith(prefix) for prefix in self.SUDO_ALLOWED_PREFIXES)
            if not allowed:
                if actual_cmd in self.BLOCKED_COMMANDS:
                    return False, f"Blocked: '{actual_cmd}' is not allowed"
                if actual_cmd not in self.ALLOWED_COMMANDS:
                    return False, f"Blocked: 'sudo {actual_cmd}' is not in the allowed list"
        else:
            if base_cmd in self.BLOCKED_COMMANDS:
                return False, f"Blocked: '{base_cmd}' is not allowed"
            if base_cmd not in self.ALLOWED_COMMANDS:
                return False, f"Blocked: '{base_cmd}' is not in the allowed list"

        return True, None

    def validate_plan(self, commands: list[str]) -> list[tuple[str, bool, str | None]]:
        """Validate a list of commands.

        Returns:
            List of (command, is_allowed, reason) tuples.
        """
        return [(cmd, *self.validate(cmd)) for cmd in commands]
