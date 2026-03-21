#!/usr/bin/env python3
"""
Dependency Resolution System
Detects and resolves package dependencies using AI assistance
"""

import json
import logging
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from enum import Enum

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class LinuxPackageManager(str, Enum):
    """Supported Linux package managers."""

    APT = "apt"
    DNF = "dnf"
    YUM = "yum"
    PACMAN = "pacman"
    ZYPPER = "zypper"
    APK = "apk"
    UNKNOWN = "unknown"


@dataclass
class Dependency:
    """Represents a package dependency"""

    name: str
    version: str | None = None
    reason: str = ""  # Why this dependency is needed
    is_satisfied: bool = False
    installed_version: str | None = None


@dataclass
class DependencyGraph:
    """Complete dependency graph for a package"""

    package_name: str
    direct_dependencies: list[Dependency]
    all_dependencies: list[Dependency]
    conflicts: list[tuple[str, str]]  # (package1, package2)
    installation_order: list[str]
    package_manager: str = LinuxPackageManager.UNKNOWN.value
    dependency_source: str = "metadata"
    conflict_source: str = "metadata"


class DependencyResolver:
    """Resolves package dependencies intelligently"""

    FALLBACK_DEPENDENCY_THRESHOLD = 1

    PM_DEPENDENCY_QUERY_COMMANDS: dict[LinuxPackageManager, list[list[str]]] = {
        LinuxPackageManager.APT: [["apt-cache", "depends", "{package}"]],
        LinuxPackageManager.DNF: [
            ["dnf", "repoquery", "--requires", "{package}"],
            ["dnf", "deplist", "{package}"],
        ],
        LinuxPackageManager.YUM: [["yum", "deplist", "{package}"]],
        LinuxPackageManager.PACMAN: [["pacman", "-Si", "{package}"]],
        LinuxPackageManager.ZYPPER: [
            ["zypper", "--non-interactive", "info", "--requires", "{package}"]
        ],
        LinuxPackageManager.APK: [["apk", "info", "-R", "{package}"]],
    }

    PM_CONFLICT_QUERY_COMMANDS: dict[LinuxPackageManager, list[list[str]]] = {
        LinuxPackageManager.APT: [["apt-cache", "show", "{package}"]],
        LinuxPackageManager.DNF: [["dnf", "repoquery", "--conflicts", "{package}"]],
        LinuxPackageManager.YUM: [["repoquery", "--conflicts", "{package}"]],
        LinuxPackageManager.PACMAN: [["pacman", "-Si", "{package}"]],
        LinuxPackageManager.ZYPPER: [["zypper", "--non-interactive", "info", "{package}"]],
        LinuxPackageManager.APK: [["apk", "info", "-R", "{package}"]],
    }

    PM_REFRESH_COMMANDS: dict[LinuxPackageManager, str] = {
        LinuxPackageManager.APT: "sudo apt-get update",
        LinuxPackageManager.DNF: "sudo dnf makecache",
        LinuxPackageManager.YUM: "sudo yum makecache",
        LinuxPackageManager.PACMAN: "sudo pacman -Sy",
        LinuxPackageManager.ZYPPER: "sudo zypper refresh",
        LinuxPackageManager.APK: "sudo apk update",
    }

    PM_INSTALL_TEMPLATES: dict[LinuxPackageManager, str] = {
        LinuxPackageManager.APT: "sudo apt-get install -y {package}",
        LinuxPackageManager.DNF: "sudo dnf install -y {package}",
        LinuxPackageManager.YUM: "sudo yum install -y {package}",
        LinuxPackageManager.PACMAN: "sudo pacman -S --noconfirm {package}",
        LinuxPackageManager.ZYPPER: "sudo zypper --non-interactive install {package}",
        LinuxPackageManager.APK: "sudo apk add {package}",
    }

    PM_REMOVE_TEMPLATES: dict[LinuxPackageManager, str] = {
        LinuxPackageManager.APT: "sudo apt-get remove -y {package}",
        LinuxPackageManager.DNF: "sudo dnf remove -y {package}",
        LinuxPackageManager.YUM: "sudo yum remove -y {package}",
        LinuxPackageManager.PACMAN: "sudo pacman -R --noconfirm {package}",
        LinuxPackageManager.ZYPPER: "sudo zypper --non-interactive remove {package}",
        LinuxPackageManager.APK: "sudo apk del {package}",
    }

    # Common dependency patterns
    DEPENDENCY_PATTERNS = {
        "docker": {
            "direct": ["containerd", "docker-ce-cli", "docker-buildx-plugin"],
            "system": ["iptables", "ca-certificates", "curl", "gnupg"],
        },
        "postgresql": {
            "direct": ["postgresql-common", "postgresql-client"],
            "optional": ["postgresql-contrib"],
        },
        "nginx": {"direct": [], "runtime": ["libc6", "libpcre3", "zlib1g"]},
        "mysql-server": {
            "direct": ["mysql-client", "mysql-common"],
            "system": ["libaio1", "libmecab2"],
        },
        "python3-pip": {"direct": ["python3", "python3-setuptools"], "system": ["python3-wheel"]},
        "nodejs": {"direct": [], "optional": ["npm"]},
        "redis-server": {"direct": [], "runtime": ["libc6", "libjemalloc2"]},
        "apache2": {
            "direct": ["apache2-bin", "apache2-data", "apache2-utils"],
            "runtime": ["libapr1", "libaprutil1"],
        },
    }

    KNOWN_CONFLICTS = {
        "mysql-server": ["mariadb-server"],
        "mariadb-server": ["mysql-server"],
        "apache2": ["nginx"],
        "nginx": ["apache2"],
        "docker": ["podman-docker"],
        "podman-docker": ["docker"],
    }

    def __init__(self):
        self._cache_lock = threading.Lock()  # Protect dependency_cache
        self._packages_lock = threading.Lock()  # Protect installed_packages
        self.dependency_cache: dict[str, DependencyGraph] = {}
        self.installed_packages: set[str] = set()
        self.package_manager = self._detect_package_manager()
        self._refresh_installed_packages()

    def _detect_package_manager(self) -> LinuxPackageManager:
        """Detect available package manager in current Linux environment."""
        ordered = [
            ("apt-get", LinuxPackageManager.APT),
            ("dnf", LinuxPackageManager.DNF),
            ("yum", LinuxPackageManager.YUM),
            ("pacman", LinuxPackageManager.PACMAN),
            ("zypper", LinuxPackageManager.ZYPPER),
            ("apk", LinuxPackageManager.APK),
        ]

        for binary, manager in ordered:
            if shutil.which(binary):
                return manager

        try:
            with open("/etc/os-release", encoding="utf-8") as f:
                os_release = f.read().lower()
            if "debian" in os_release or "ubuntu" in os_release:
                return LinuxPackageManager.APT
            if "fedora" in os_release or "rhel" in os_release or "centos" in os_release:
                return LinuxPackageManager.DNF
            if "arch" in os_release:
                return LinuxPackageManager.PACMAN
            if "suse" in os_release:
                return LinuxPackageManager.ZYPPER
            if "alpine" in os_release:
                return LinuxPackageManager.APK
        except OSError:
            pass

        return LinuxPackageManager.UNKNOWN

    def _run_command(self, cmd: list[str]) -> tuple[bool, str, str]:
        """Execute command and return success, stdout, stderr"""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return (result.returncode == 0, result.stdout, result.stderr)
        except subprocess.TimeoutExpired:
            return (False, "", "Command timed out")
        except Exception as e:
            return (False, "", str(e))

    def _refresh_installed_packages(self) -> None:
        """Refresh cache of installed packages using active package manager."""
        logger.info(f"Refreshing installed packages cache ({self.package_manager.value})...")

        commands: list[list[str]] = []
        if self.package_manager == LinuxPackageManager.APT:
            commands = [["dpkg", "-l"]]
        elif self.package_manager in (LinuxPackageManager.DNF, LinuxPackageManager.YUM):
            commands = [["rpm", "-qa", "--qf", "%{NAME}\\n"]]
        elif self.package_manager == LinuxPackageManager.PACMAN:
            commands = [["pacman", "-Qq"]]
        elif self.package_manager == LinuxPackageManager.ZYPPER:
            commands = [["rpm", "-qa", "--qf", "%{NAME}\\n"]]
        elif self.package_manager == LinuxPackageManager.APK:
            commands = [["apk", "info"]]

        new_packages: set[str] = set()
        for cmd in commands:
            success, stdout, _ = self._run_command(cmd)
            if not success:
                continue

            if self.package_manager == LinuxPackageManager.APT:
                for line in stdout.splitlines():
                    if line.startswith("ii"):
                        parts = line.split()
                        if len(parts) >= 2:
                            pkg = parts[1]
                            new_packages.add(pkg)
                            if ":" in pkg:
                                new_packages.add(pkg.split(":", 1)[0])
            else:
                for line in stdout.splitlines():
                    package_name = line.strip().split()[0] if line.strip() else ""
                    if not package_name:
                        continue
                    if self.package_manager == LinuxPackageManager.APK:
                        package_name = re.sub(r"-[0-9].*$", "", package_name)
                    new_packages.add(package_name)

        with self._packages_lock:
            self.installed_packages = new_packages
            logger.info(f"Found {len(self.installed_packages)} installed packages")

    def is_package_installed(self, package_name: str) -> bool:
        """Check if package is installed (thread-safe)"""
        with self._packages_lock:
            return package_name in self.installed_packages

    def get_installed_version(self, package_name: str) -> str | None:
        """Get version of installed package"""
        if not self.is_package_installed(package_name):
            return None

        if self.package_manager == LinuxPackageManager.APT:
            success, stdout, _ = self._run_command(
                ["dpkg-query", "-W", "-f=${Version}", package_name]
            )
            return stdout.strip() if success else None

        if self.package_manager in (
            LinuxPackageManager.DNF,
            LinuxPackageManager.YUM,
            LinuxPackageManager.ZYPPER,
        ):
            success, stdout, _ = self._run_command(
                ["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", package_name]
            )
            return stdout.strip() if success else None

        if self.package_manager == LinuxPackageManager.PACMAN:
            success, stdout, _ = self._run_command(["pacman", "-Q", package_name])
            if success and stdout.strip():
                parts = stdout.strip().split(maxsplit=1)
                return parts[1] if len(parts) == 2 else None
            return None

        if self.package_manager == LinuxPackageManager.APK:
            success, stdout, _ = self._run_command(["apk", "info", "-v", package_name])
            if success:
                lines = stdout.strip().splitlines()
                return lines[0].strip() if lines else None

        return None

    def _normalize_dependency_name(self, raw_name: str) -> str:
        """Normalize dependency token from package-manager output."""
        dep_name = raw_name.strip()
        dep_name = re.sub(r":([a-z0-9_+-]+)$", "", dep_name)
        dep_name = re.sub(r"\s*\(.*?\)", "", dep_name)
        dep_name = re.sub(r"[<>=].*$", "", dep_name)
        if "/" in dep_name or dep_name.startswith("rpmlib("):
            return ""
        return dep_name.strip()

    def _extract_dependencies_from_output(self, output: str) -> list[Dependency]:
        """Extract dependency entries from different manager outputs."""
        dependencies: dict[str, Dependency] = {}
        markers = [
            "Depends:",
            "PreDepends:",
            "Recommends:",
            "Requires:",
            "requires:",
            "dependency:",
            "Dependencies:",
            "Depends On",
            "depends=",
        ]

        for line in output.splitlines():
            text = line.strip()
            if not text:
                continue

            reason = "Required dependency"
            if "recommend" in text.lower() or "optional" in text.lower():
                reason = "Recommended package"

            dep_candidate = None
            for marker in markers:
                if marker in text:
                    dep_candidate = text.split(marker, 1)[1].strip()
                    break

            if dep_candidate is None:
                if text.startswith("-"):
                    dep_candidate = text[1:].strip()
                elif text.lower().startswith("provider") and ":" in text:
                    dep_candidate = text.split(":", 1)[1].strip()
                else:
                    continue

            if "|" in dep_candidate:
                dep_candidate = dep_candidate.split("|", 1)[0].strip()

            dep_name = self._normalize_dependency_name(dep_candidate)
            if not dep_name or dep_name in dependencies:
                continue

            is_installed = self.is_package_installed(dep_name)
            dependencies[dep_name] = Dependency(
                name=dep_name,
                reason=reason,
                is_satisfied=is_installed,
                installed_version=self.get_installed_version(dep_name) if is_installed else None,
            )

        return list(dependencies.values())

    def get_package_manager_dependencies(self, package_name: str) -> list[Dependency]:
        """Get dependencies from active package-manager metadata."""
        templates = self.PM_DEPENDENCY_QUERY_COMMANDS.get(self.package_manager, [])
        commands = [[token.format(package=package_name) for token in cmd] for cmd in templates]

        for cmd in commands:
            success, stdout, stderr = self._run_command(cmd)
            if success and stdout.strip():
                return self._extract_dependencies_from_output(stdout)
            if stderr:
                logger.debug(
                    "Could not get dependencies for %s via '%s': %s",
                    package_name,
                    " ".join(cmd),
                    stderr,
                )

        return []

    def get_apt_dependencies(self, package_name: str) -> list[Dependency]:
        """Backward-compatible wrapper for legacy callers."""
        return self.get_package_manager_dependencies(package_name)

    def get_predefined_dependencies(self, package_name: str) -> list[Dependency]:
        """Get dependencies from predefined patterns"""
        dependencies = []

        if package_name not in self.DEPENDENCY_PATTERNS:
            return dependencies

        pattern = self.DEPENDENCY_PATTERNS[package_name]

        # Direct dependencies
        for dep in pattern.get("direct", []):
            is_installed = self.is_package_installed(dep)
            dependencies.append(
                Dependency(
                    name=dep,
                    reason="Required dependency",
                    is_satisfied=is_installed,
                    installed_version=self.get_installed_version(dep) if is_installed else None,
                )
            )

        # System dependencies
        for dep in pattern.get("system", []):
            is_installed = self.is_package_installed(dep)
            dependencies.append(
                Dependency(
                    name=dep,
                    reason="System dependency",
                    is_satisfied=is_installed,
                    installed_version=self.get_installed_version(dep) if is_installed else None,
                )
            )

        # Optional dependencies
        for dep in pattern.get("optional", []):
            is_installed = self.is_package_installed(dep)
            dependencies.append(
                Dependency(name=dep, reason="Optional enhancement", is_satisfied=is_installed)
            )

        return dependencies

    def get_manager_dependencies(self, package_name: str) -> list[Dependency]:
        """Get dependencies from package manager metadata at runtime."""
        return self.get_package_manager_dependencies(package_name)

    def _merge_dependencies(
        self,
        primary: list[Dependency],
        fallback: list[Dependency],
    ) -> list[Dependency]:
        """Merge dependencies with dynamic metadata entries taking precedence."""
        merged: dict[str, Dependency] = {}

        for dep in primary:
            merged[dep.name] = dep

        for dep in fallback:
            if dep.name not in merged:
                merged[dep.name] = dep

        return list(merged.values())

    def resolve_dependencies(self, package_name: str, recursive: bool = True) -> DependencyGraph:
        """
        Resolve all dependencies for a package

        Args:
            package_name: Package to resolve dependencies for
            recursive: Whether to resolve transitive dependencies
        """
        logger.info(f"Resolving dependencies for {package_name}...")

        # Check cache (thread-safe)
        with self._cache_lock:
            if package_name in self.dependency_cache:
                logger.info(f"Using cached dependencies for {package_name}")
                return self.dependency_cache[package_name]

        # Dynamic-first dependency resolution from package metadata.
        manager_deps = self.get_manager_dependencies(package_name)
        dependency_source = "metadata"

        # Heuristic fallback only when metadata is unavailable or sparse.
        fallback_deps: list[Dependency] = []
        if len(manager_deps) < self.FALLBACK_DEPENDENCY_THRESHOLD:
            fallback_deps = self.get_predefined_dependencies(package_name)
            if fallback_deps:
                dependency_source = "metadata+heuristic" if manager_deps else "heuristic"

        direct_dependencies = self._merge_dependencies(manager_deps, fallback_deps)
        all_deps: dict[str, Dependency] = {dep.name: dep for dep in direct_dependencies}

        # Resolve transitive dependencies if recursive
        transitive_deps: dict[str, Dependency] = {}
        if recursive:
            for dep in direct_dependencies:
                if not dep.is_satisfied:
                    # Get dependencies of this dependency
                    sub_deps = self.get_manager_dependencies(dep.name)
                    for sub_dep in sub_deps:
                        if sub_dep.name not in all_deps and sub_dep.name not in transitive_deps:
                            transitive_deps[sub_dep.name] = sub_dep

        all_dependencies = list(all_deps.values()) + list(transitive_deps.values())

        # Detect conflicts
        conflicts, conflict_source = self._detect_conflicts(package_name, all_dependencies)

        # Calculate installation order
        installation_order = self._calculate_installation_order(package_name, all_dependencies)

        graph = DependencyGraph(
            package_name=package_name,
            direct_dependencies=direct_dependencies,
            all_dependencies=all_dependencies,
            conflicts=conflicts,
            installation_order=installation_order,
            package_manager=self.package_manager.value,
            dependency_source=dependency_source,
            conflict_source=conflict_source,
        )

        # Cache result (thread-safe)
        with self._cache_lock:
            self.dependency_cache[package_name] = graph

        return graph

    def get_declared_conflicts(self, package_name: str) -> list[str]:
        """Get package conflicts declared by package metadata."""
        templates = self.PM_CONFLICT_QUERY_COMMANDS.get(self.package_manager, [])
        commands = [[token.format(package=package_name) for token in cmd] for cmd in templates]

        conflicts: set[str] = set()
        for cmd in commands:
            success, stdout, _ = self._run_command(cmd)
            if not success:
                continue

            for line in stdout.splitlines():
                text = line.strip()
                lowered = text.lower()
                if lowered.startswith("conflicts:") or "conflicts with" in lowered:
                    candidate = text.split(":", 1)[1].strip() if ":" in text else text
                    for part in re.split(r"[,|]", candidate):
                        name = self._normalize_dependency_name(part)
                        if name:
                            conflicts.add(name)

        return sorted(conflicts)

    def _detect_conflicts(
        self, package_name: str, dependencies: list[Dependency]
    ) -> tuple[list[tuple[str, str]], str]:
        """Detect conflicts using metadata first, with heuristic fallback."""
        conflicts: list[tuple[str, str]] = []

        declared_conflicts = self.get_declared_conflicts(package_name)
        for conflicting in declared_conflicts:
            if self.is_package_installed(conflicting):
                pair = (package_name, conflicting)
                if pair not in conflicts:
                    conflicts.append(pair)

        if conflicts:
            return conflicts, "metadata"

        # Fallback to known conflict map only when metadata did not yield results.
        dep_names = {dep.name for dep in dependencies}
        check_names = dep_names | {package_name}

        for dep_name in check_names:
            if dep_name in self.KNOWN_CONFLICTS:
                for conflicting in self.KNOWN_CONFLICTS[dep_name]:
                    if conflicting in check_names or self.is_package_installed(conflicting):
                        pair = (dep_name, conflicting)
                        if pair not in conflicts:
                            conflicts.append(pair)

        if conflicts:
            return conflicts, "heuristic"

        return [], "metadata"

    def _calculate_installation_order(
        self, package_name: str, dependencies: list[Dependency]
    ) -> list[str]:
        """Calculate optimal installation order"""
        # Simple topological sort based on dependency levels

        # Packages with no dependencies first
        no_deps = []
        has_deps = []

        for dep in dependencies:
            if not dep.is_satisfied:
                # Simple heuristic: system packages first, then others
                if "lib" in dep.name or dep.name in ["ca-certificates", "curl", "gnupg"]:
                    no_deps.append(dep.name)
                else:
                    has_deps.append(dep.name)

        # Build installation order
        order = no_deps + has_deps

        # Add main package last
        if package_name not in order:
            order.append(package_name)

        return order

    def get_missing_dependencies(self, package_name: str) -> list[Dependency]:
        """Get list of dependencies that need to be installed"""
        graph = self.resolve_dependencies(package_name)
        return [dep for dep in graph.all_dependencies if not dep.is_satisfied]

    def generate_install_plan(self, package_name: str) -> dict:
        """Generate complete installation plan"""
        graph = self.resolve_dependencies(package_name)
        missing = self.get_missing_dependencies(package_name)

        plan = {
            "package": package_name,
            "package_manager": graph.package_manager,
            "dependency_source": graph.dependency_source,
            "conflict_source": graph.conflict_source,
            "total_dependencies": len(graph.all_dependencies),
            "missing_dependencies": len(missing),
            "satisfied_dependencies": len(graph.all_dependencies) - len(missing),
            "conflicts": graph.conflicts,
            "installation_order": graph.installation_order,
            "install_commands": self._generate_install_commands(graph.installation_order),
            "estimated_time_minutes": len(missing) * 0.5,  # Rough estimate
        }

        return plan

    def _get_refresh_command(self) -> str:
        return self.PM_REFRESH_COMMANDS.get(self.package_manager, "")

    def _get_install_command(self, package: str) -> str:
        template = self.PM_INSTALL_TEMPLATES.get(self.package_manager)
        return template.format(package=package) if template else f"install {package}"

    def _get_remove_command(self, package: str) -> str:
        template = self.PM_REMOVE_TEMPLATES.get(self.package_manager)
        return template.format(package=package) if template else f"remove {package}"

    def _generate_install_commands(self, packages: list[str]) -> list[str]:
        """Generate package-manager-specific install commands"""
        commands = []

        refresh_cmd = self._get_refresh_command()
        if refresh_cmd:
            commands.append(refresh_cmd)

        # Install in order
        for package in packages:
            if not self.is_package_installed(package):
                commands.append(self._get_install_command(package))

        return commands

    def generate_conflict_resolution_plan(
        self,
        package_name: str,
        auto_remove_conflicts: bool = False,
    ) -> dict:
        """Generate a cross-distro dependency conflict resolution plan."""
        graph = self.resolve_dependencies(package_name)
        missing = [dep for dep in graph.all_dependencies if not dep.is_satisfied]

        removable_conflicts: list[str] = []
        for _, conflicting in graph.conflicts:
            if self.is_package_installed(conflicting):
                removable_conflicts.append(conflicting)

        seen = set()
        removable_conflicts = [pkg for pkg in removable_conflicts if not (pkg in seen or seen.add(pkg))]

        commands: list[str] = []
        refresh_cmd = self._get_refresh_command()
        if refresh_cmd:
            commands.append(refresh_cmd)

        if auto_remove_conflicts:
            for pkg in removable_conflicts:
                commands.append(self._get_remove_command(pkg))

        for pkg in graph.installation_order:
            if not self.is_package_installed(pkg):
                commands.append(self._get_install_command(pkg))

        return {
            "package": package_name,
            "package_manager": self.package_manager.value,
            "dependency_source": graph.dependency_source,
            "conflict_source": graph.conflict_source,
            "conflicts_detected": len(graph.conflicts),
            "conflicts": graph.conflicts,
            "removable_conflicts": removable_conflicts,
            "missing_dependencies": [asdict(dep) for dep in missing],
            "installation_order": graph.installation_order,
            "resolution_commands": commands,
            "safe_to_auto_apply": len(removable_conflicts) == 0 or auto_remove_conflicts,
        }

    def print_dependency_tree(self, package_name: str, indent: int = 0) -> None:
        """Print dependency tree"""
        graph = self.resolve_dependencies(package_name, recursive=False)

        prefix = "  " * indent
        status = "✅" if self.is_package_installed(package_name) else "❌"
        print(f"{prefix}{status} {package_name} [{graph.package_manager}]")

        for dep in graph.direct_dependencies:
            dep_prefix = "  " * (indent + 1)
            dep_status = "✅" if dep.is_satisfied else "❌"
            version_str = f" ({dep.installed_version})" if dep.installed_version else ""
            print(f"{dep_prefix}{dep_status} {dep.name}{version_str} - {dep.reason}")

    def export_graph_json(self, package_name: str, filepath: str) -> None:
        """Export dependency graph to JSON"""
        graph = self.resolve_dependencies(package_name)

        graph_dict = {
            "package_name": graph.package_name,
            "package_manager": graph.package_manager,
            "dependency_source": graph.dependency_source,
            "conflict_source": graph.conflict_source,
            "direct_dependencies": [asdict(dep) for dep in graph.direct_dependencies],
            "all_dependencies": [asdict(dep) for dep in graph.all_dependencies],
            "conflicts": graph.conflicts,
            "installation_order": graph.installation_order,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(graph_dict, f, indent=2)

        logger.info(f"Dependency graph exported to {filepath}")


# CLI Interface
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Resolve Linux package dependencies")
    parser.add_argument("package", help="Package name to analyze")
    parser.add_argument("--tree", action="store_true", help="Show dependency tree")
    parser.add_argument("--plan", action="store_true", help="Generate installation plan")
    parser.add_argument(
        "--resolve-conflicts",
        action="store_true",
        help="Generate conflict resolution plan",
    )
    parser.add_argument(
        "--auto-remove-conflicts",
        action="store_true",
        help="Include conflict removal commands",
    )
    parser.add_argument("--export", help="Export dependency graph to JSON file")
    parser.add_argument("--missing", action="store_true", help="Show only missing dependencies")

    args = parser.parse_args()

    resolver = DependencyResolver()

    if args.tree:
        print(f"\n📦 Dependency tree for {args.package}:")
        print("=" * 60)
        resolver.print_dependency_tree(args.package)

    if args.plan:
        print(f"\n📋 Installation plan for {args.package}:")
        print("=" * 60)
        plan = resolver.generate_install_plan(args.package)

        print(f"\nPackage: {plan['package']}")
        print(f"Package manager: {plan['package_manager']}")
        print(f"Dependency source: {plan['dependency_source']}")
        print(f"Conflict source: {plan['conflict_source']}")
        print(f"Total dependencies: {plan['total_dependencies']}")
        print(f"✅ Already satisfied: {plan['satisfied_dependencies']}")
        print(f"❌ Need to install: {plan['missing_dependencies']}")

        if plan["conflicts"]:
            print("\n⚠️  Conflicts detected:")
            for pkg1, pkg2 in plan["conflicts"]:
                print(f"   - {pkg1} conflicts with {pkg2}")

        print("\n📝 Installation order:")
        for i, pkg in enumerate(plan["installation_order"], 1):
            status = "✅" if resolver.is_package_installed(pkg) else "❌"
            print(f"   {i}. {status} {pkg}")

        print(f"\n⏱️  Estimated time: {plan['estimated_time_minutes']:.1f} minutes")

        print("\n💻 Commands to run:")
        for cmd in plan["install_commands"]:
            print(f"   {cmd}")

    if args.resolve_conflicts:
        print(f"\n🛠️  Conflict resolution plan for {args.package}:")
        print("=" * 60)
        plan = resolver.generate_conflict_resolution_plan(
            args.package,
            auto_remove_conflicts=args.auto_remove_conflicts,
        )

        print(f"\nPackage manager: {plan['package_manager']}")
        print(f"Dependency source: {plan['dependency_source']}")
        print(f"Conflict source: {plan['conflict_source']}")
        print(f"Conflicts detected: {plan['conflicts_detected']}")
        if plan["conflicts"]:
            print("\n⚠️  Conflicts:")
            for pkg1, pkg2 in plan["conflicts"]:
                print(f"   - {pkg1} ↔ {pkg2}")

        print("\n💻 Resolution commands:")
        for cmd in plan["resolution_commands"]:
            print(f"   {cmd}")

    if args.missing:
        print(f"\n❌ Missing dependencies for {args.package}:")
        print("=" * 60)
        missing = resolver.get_missing_dependencies(args.package)

        if missing:
            for dep in missing:
                print(f"  - {dep.name}: {dep.reason}")
        else:
            print("  All dependencies satisfied!")

    if args.export:
        resolver.export_graph_json(args.package, args.export)

    # Default: show summary
    if not any([args.tree, args.plan, args.resolve_conflicts, args.missing, args.export]):
        graph = resolver.resolve_dependencies(args.package)
        print(f"\n📦 {args.package} - Dependency Summary")
        print("=" * 60)
        print(f"Package manager: {graph.package_manager}")
        print(f"Dependency source: {graph.dependency_source}")
        print(f"Conflict source: {graph.conflict_source}")
        print(f"Direct dependencies: {len(graph.direct_dependencies)}")
        print(f"Total dependencies: {len(graph.all_dependencies)}")
        satisfied = sum(1 for d in graph.all_dependencies if d.is_satisfied)
        print(f"✅ Satisfied: {satisfied}")
        print(f"❌ Missing: {len(graph.all_dependencies) - satisfied}")
