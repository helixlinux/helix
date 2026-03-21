#!/usr/bin/env python3
"""
Helix Configure Module

Automatically generates configuration files for software projects and system services.

Two modes:
  - Auto-detect mode:  `helix configure`        — scans project dir + installed system packages
  - Explicit mode:     `helix configure samba`   — user names the service/tool to configure

Approach: Hybrid template + LLM
  Stage 1 (Templates): fast, offline, deterministic — generates configs from project context
  Stage 2 (LLM):       additive, non-blocking     — fills custom values, suggests extras
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


# ─── Enums & Dataclasses ─────────────────────────────────────────────────────


class ConfigType(Enum):
    LINTING = "linting"
    DOCKER = "docker"
    ENV = "env"
    FRAMEWORK = "framework"
    TOOLING = "tooling"
    GENERAL = "general"
    SERVICE = "service"


@dataclass
class ConfigFileSpec:
    """Describes a single config file to be generated."""

    filename: str  # relative path (e.g. ".eslintrc.json") or absolute (e.g. "/etc/samba/smb.conf")
    config_type: ConfigType
    content: str
    reason: str
    already_exists: bool = False
    source: str = "template"  # "template" | "llm" | "template+llm"


@dataclass
class ProjectContext:
    """All information gathered about the project/system for config generation."""

    project_dir: Path
    ecosystems: list[str] = field(default_factory=list)       # e.g. ["python", "node"]
    primary_ecosystem: str = "unknown"
    all_packages: list[str] = field(default_factory=list)     # prod package names
    dev_packages: list[str] = field(default_factory=list)
    existing_configs: list[str] = field(default_factory=list) # filenames already present
    framework_hints: dict[str, Any] = field(default_factory=dict)
    project_name: str = ""
    python_version: str | None = None
    node_version: str | None = None
    has_docker: bool = False
    git_root: Path | None = None
    system_services: list[str] = field(default_factory=list)  # detected installed services
    # Raw file data for accurate config generation
    package_json: dict[str, Any] = field(default_factory=dict)   # parsed package.json
    pyproject_data: dict[str, Any] = field(default_factory=dict) # parsed pyproject.toml sections


# ─── Known system services ────────────────────────────────────────────────────

# Maps system package name → (config filename, default config path)
KNOWN_SERVICES: dict[str, tuple[str, str]] = {
    "samba": ("smb.conf", "/etc/samba/smb.conf"),
    "nginx": ("nginx.conf", "/etc/nginx/nginx.conf"),
    "postgresql": ("postgresql.conf", "/etc/postgresql/postgresql.conf"),
    "postgresql-14": ("postgresql.conf", "/etc/postgresql/14/main/postgresql.conf"),
    "postgresql-15": ("postgresql.conf", "/etc/postgresql/15/main/postgresql.conf"),
    "postgresql-16": ("postgresql.conf", "/etc/postgresql/16/main/postgresql.conf"),
    "redis": ("redis.conf", "/etc/redis/redis.conf"),
    "redis-server": ("redis.conf", "/etc/redis/redis.conf"),
    "mysql-server": ("my.cnf", "/etc/mysql/my.cnf"),
    "mysql-server-8.0": ("my.cnf", "/etc/mysql/my.cnf"),
    "mariadb-server": ("50-server.cnf", "/etc/mysql/mariadb.conf.d/50-server.cnf"),
    "apache2": ("apache2.conf", "/etc/apache2/apache2.conf"),
    "openssh-server": ("sshd_config", "/etc/ssh/sshd_config"),
    "bind9": ("named.conf", "/etc/bind/named.conf"),
    "postfix": ("main.cf", "/etc/postfix/main.cf"),
    "dovecot-core": ("dovecot.conf", "/etc/dovecot/dovecot.conf"),
    "fail2ban": ("jail.conf", "/etc/fail2ban/jail.conf"),
    "ufw": ("ufw.conf", "/etc/ufw/ufw.conf"),
    "haproxy": ("haproxy.cfg", "/etc/haproxy/haproxy.cfg"),
    "varnish": ("default.vcl", "/etc/varnish/default.vcl"),
}


# ─── ProjectAnalyzer ──────────────────────────────────────────────────────────

class ProjectAnalyzer:
    """Scans the project directory and system to build ProjectContext."""

    # Indicator files that reveal framework/tool usage
    INDICATOR_FILES: dict[str, str] = {
        "next.config.js": "nextjs",
        "next.config.ts": "nextjs",
        "next.config.mjs": "nextjs",
        "nuxt.config.ts": "nuxtjs",
        "nuxt.config.js": "nuxtjs",
        "vite.config.ts": "vite",
        "vite.config.js": "vite",
        "svelte.config.js": "svelte",
        "angular.json": "angular",
        "manage.py": "django",
        "wsgi.py": "flask_candidate",
        "app.py": "flask_candidate",
        "asgi.py": "fastapi_candidate",
        "pyproject.toml": "python_modern",
        "setup.py": "python_classic",
        "setup.cfg": "python_classic",
        "tsconfig.json": "typescript",
        ".nvmrc": "node_version_file",
        ".node-version": "node_version_file",
        "Dockerfile": "docker_existing",
        "docker-compose.yml": "compose_existing",
        "docker-compose.yaml": "compose_existing",
        ".env.example": "env_templated",
        ".git": "git_repo",
        "Makefile": "makefile_existing",
    }

    # All known config filename variants (to detect already-existing configs)
    CONFIG_VARIANTS: list[str] = [
        "pyproject.toml", "setup.cfg", "setup.py",
        "pytest.ini", "tox.ini", ".pytest.ini",
        ".flake8", "flake8.cfg",
        "mypy.ini", ".mypy.ini",
        ".env", ".env.local", ".env.example",
        ".eslintrc", ".eslintrc.json", ".eslintrc.js", ".eslintrc.yaml",
        ".eslintrc.yml", "eslint.config.js", "eslint.config.mjs",
        ".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierrc.yaml",
        "prettier.config.js",
        "jest.config.js", "jest.config.ts", "jest.config.mjs",
        "tsconfig.json", "tsconfig.base.json",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".dockerignore",
        ".gitignore",
        ".editorconfig",
        "Makefile",
    ]

    def analyze(self, project_dir: Path) -> ProjectContext:
        """Main entry point: scan directory and system, return ProjectContext."""
        ctx = ProjectContext(project_dir=project_dir)
        ctx.project_name = self._sanitize_project_name(project_dir.name)

        # Read raw project files for accurate config generation
        self._read_raw_files(ctx, project_dir)

        # Detect project ecosystems via dependency files
        self._detect_ecosystems(ctx, project_dir)

        # Detect framework hints from indicator files + package names
        self._detect_framework_hints(ctx, project_dir)

        # Read language version hints
        ctx.python_version = self._read_python_version(project_dir)
        ctx.node_version = self._read_node_version(project_dir)

        # Detect existing config files (to avoid overwriting)
        ctx.existing_configs = self._detect_existing_configs(project_dir)

        # Detect Docker
        ctx.has_docker = (project_dir / "Dockerfile").exists() or (project_dir / "docker-compose.yml").exists()

        # Detect git root
        ctx.git_root = self._find_git_root(project_dir)

        # Detect installed system services (only those actually used by the project)
        all_project_pkgs = ctx.all_packages + ctx.dev_packages
        ctx.system_services = self._detect_system_services(all_project_pkgs)

        return ctx

    def _read_raw_files(self, ctx: ProjectContext, project_dir: Path) -> None:
        """Read package.json and pyproject.toml for accurate metadata."""
        # package.json
        pkg_json = project_dir / "package.json"
        if pkg_json.exists():
            try:
                ctx.package_json = json.loads(pkg_json.read_text(encoding="utf-8"))
                # Use name from package.json if present
                if ctx.package_json.get("name"):
                    ctx.project_name = self._sanitize_project_name(ctx.package_json["name"])
            except Exception:
                pass

        # pyproject.toml — parse key sections without external dependency
        pyproject = project_dir / "pyproject.toml"
        if pyproject.exists():
            try:
                ctx.pyproject_data = self._parse_toml_basic(pyproject.read_text(encoding="utf-8"))
                # Pull project name
                project_section = ctx.pyproject_data.get("project", {})
                if project_section.get("name"):
                    ctx.project_name = self._sanitize_project_name(project_section["name"])
            except Exception:
                pass

    def _parse_toml_basic(self, content: str) -> dict[str, Any]:
        """Minimal TOML parser: extracts top-level sections as dicts of key=value strings."""
        result: dict[str, Any] = {}
        current_section: str | None = None
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Section header [project] or [[tool.something]]
            if line.startswith("["):
                # Normalise [[x]] → [x]
                header = line.strip("[]").strip()
                current_section = header
                if current_section not in result:
                    result[current_section] = {}
                continue
            if current_section is not None and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                result[current_section][key] = val
        return result

    def _detect_ecosystems(self, ctx: ProjectContext, project_dir: Path) -> None:
        """Use DependencyImporter to detect ecosystems and gather package lists."""
        try:
            from helix.dependency_importer import DependencyImporter
            importer = DependencyImporter(base_path=str(project_dir))
            results = importer.scan_directory(str(project_dir), include_dev=True)

            ecosystems_seen: list[str] = []
            for file_path, parse_result in results.items():
                eco = parse_result.ecosystem.value
                if eco not in ecosystems_seen:
                    ecosystems_seen.append(eco)
                for pkg in parse_result.packages:
                    if pkg.name not in ctx.all_packages:
                        ctx.all_packages.append(pkg.name)
                for pkg in parse_result.dev_packages:
                    if pkg.name not in ctx.dev_packages:
                        ctx.dev_packages.append(pkg.name)

            ctx.ecosystems = ecosystems_seen
            ctx.primary_ecosystem = ecosystems_seen[0] if ecosystems_seen else "unknown"
        except Exception:
            pass  # Non-fatal: proceed without ecosystem info

    def _detect_framework_hints(self, ctx: ProjectContext, project_dir: Path) -> None:
        """Detect frameworks from indicator files and package names."""
        hints: dict[str, Any] = {}
        all_pkgs = set(p.lower() for p in ctx.all_packages + ctx.dev_packages)

        for filename, hint in self.INDICATOR_FILES.items():
            if (project_dir / filename).exists():
                hints[hint] = True

        # Framework detection: cross-reference indicator files + packages
        hints["is_nextjs"] = hints.get("nextjs", False) or "next" in all_pkgs
        hints["is_nuxtjs"] = hints.get("nuxtjs", False) or "nuxt" in all_pkgs
        hints["is_vite"] = hints.get("vite", False) or "vite" in all_pkgs
        hints["is_svelte"] = hints.get("svelte", False) or "svelte" in all_pkgs
        hints["is_angular"] = hints.get("angular", False) or "@angular/core" in all_pkgs
        hints["is_react"] = "react" in all_pkgs or "react-dom" in all_pkgs
        hints["is_typescript"] = hints.get("typescript", False) or "typescript" in all_pkgs
        hints["is_django"] = hints.get("django", False) or "django" in all_pkgs
        hints["is_flask"] = ("flask" in all_pkgs) and (hints.get("flask_candidate", False) or "flask" in all_pkgs)
        hints["is_fastapi"] = "fastapi" in all_pkgs
        hints["has_pytest"] = "pytest" in all_pkgs
        hints["has_mypy"] = "mypy" in all_pkgs
        hints["has_flake8"] = "flake8" in all_pkgs
        hints["has_jest"] = "jest" in all_pkgs
        hints["has_eslint"] = "eslint" in all_pkgs
        hints["has_prettier"] = "prettier" in all_pkgs
        hints["has_tailwind"] = "tailwindcss" in all_pkgs
        hints["has_postgres"] = any(p in all_pkgs for p in ["psycopg2", "psycopg2-binary", "pg", "postgres", "postgresql"])
        hints["has_redis"] = any(p in all_pkgs for p in ["redis", "ioredis", "redis-py"])
        hints["has_docker_compose_dep"] = hints.get("compose_existing", False)

        ctx.framework_hints = hints

    def _detect_existing_configs(self, project_dir: Path) -> list[str]:
        """Find ALL files already present in the project directory (top-level)."""
        existing = []
        try:
            for p in project_dir.iterdir():
                if p.is_file():
                    existing.append(p.name)
                elif p.is_dir() and p.name.startswith("."):
                    # Include hidden dirs like .github as folder names
                    existing.append(p.name + "/")
        except PermissionError:
            pass
        return existing

    # Maps installed service package → project-level package names that indicate
    # the project actually connects to / uses that service.
    # A service is only included if BOTH: it is installed on the system AND
    # the project depends on at least one of its client packages.
    SERVICE_PROJECT_INDICATORS: dict[str, list[str]] = {
        "samba": [],  # No client library — include if installed (always relevant for file sharing)
        "nginx": ["nginx", "http-proxy", "express"],
        "postgresql": ["psycopg2", "psycopg2-binary", "asyncpg", "pg", "postgres",
                       "sqlalchemy", "django", "tortoise-orm"],
        "postgresql-14": ["psycopg2", "psycopg2-binary", "asyncpg", "pg", "postgres",
                          "sqlalchemy", "django", "tortoise-orm"],
        "postgresql-15": ["psycopg2", "psycopg2-binary", "asyncpg", "pg", "postgres",
                          "sqlalchemy", "django", "tortoise-orm"],
        "postgresql-16": ["psycopg2", "psycopg2-binary", "asyncpg", "pg", "postgres",
                          "sqlalchemy", "django", "tortoise-orm"],
        "redis": ["redis", "ioredis", "redis-py", "celery", "django-redis",
                  "flask-caching", "bull", "bullmq"],
        "redis-server": ["redis", "ioredis", "redis-py", "celery", "django-redis",
                         "flask-caching", "bull", "bullmq"],
        "mysql-server": ["mysqlclient", "pymysql", "mysql2", "mysql",
                         "sqlalchemy", "django"],
        "mysql-server-8.0": ["mysqlclient", "pymysql", "mysql2", "mysql",
                             "sqlalchemy", "django"],
        "mariadb-server": ["mysqlclient", "pymysql", "mysql2", "mariadb",
                           "sqlalchemy", "django"],
        "apache2": ["apache-airflow", "mod-wsgi"],
        "openssh-server": [],  # Always relevant if installed
        "bind9": [],
        "postfix": ["smtplib", "django", "flask-mail", "nodemailer"],
        "dovecot-core": [],
        "fail2ban": [],
        "ufw": ["__server__"],   # Only for server/backend projects
        "haproxy": ["__server__"],
        "varnish": ["__server__"],
    }

    # Packages that signal a project is backend/server-side
    BACKEND_INDICATORS = {
        "flask", "django", "fastapi", "uvicorn", "gunicorn", "express",
        "koa", "hapi", "fastify", "nest", "@nestjs/core", "spring",
        "rails", "sinatra", "laravel", "symfony",
    }

    def _detect_system_services(self, project_packages: list[str]) -> list[str]:
        """Query installed system packages and filter to those actually used by the project."""
        detected: list[str] = []
        proj_pkgs_lower = {p.lower() for p in project_packages}
        is_backend = bool(proj_pkgs_lower & self.BACKEND_INDICATORS)

        try:
            from helix.dependency_resolver import DependencyResolver
            resolver = DependencyResolver()
            for service_pkg in KNOWN_SERVICES:
                if not resolver.is_package_installed(service_pkg):
                    continue
                indicators = self.SERVICE_PROJECT_INDICATORS.get(service_pkg, [])
                if not indicators:
                    # No indicators = always include (samba, ssh, bind9, etc.)
                    detected.append(service_pkg)
                    continue
                if indicators == ["__server__"]:
                    # Server-only services: only include for backend projects
                    if is_backend:
                        detected.append(service_pkg)
                    continue
                # Include if at least one indicator package is in the project deps
                if any(ind.lower() in proj_pkgs_lower for ind in indicators):
                    detected.append(service_pkg)
        except Exception:
            pass  # Non-fatal
        return detected

    def _read_python_version(self, project_dir: Path) -> str | None:
        """Detect Python version from .python-version, pyproject.toml, or sys.version."""
        pv_file = project_dir / ".python-version"
        if pv_file.exists():
            version = pv_file.read_text().strip()
            if version:
                return version

        pyproject = project_dir / "pyproject.toml"
        if pyproject.exists():
            content = pyproject.read_text()
            m = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']', content)
            if m:
                return m.group(1)
            m = re.search(r'python\s*=\s*["\']([^"\']+)["\']', content)
            if m:
                return m.group(1)

        return f"{sys.version_info.major}.{sys.version_info.minor}"

    def _read_node_version(self, project_dir: Path) -> str | None:
        """Detect Node.js version from .nvmrc or .node-version."""
        for fname in [".nvmrc", ".node-version"]:
            f = project_dir / fname
            if f.exists():
                version = f.read_text().strip().lstrip("v")
                if version:
                    return version
        # Try package.json engines field
        pkg_json = project_dir / "package.json"
        if pkg_json.exists():
            try:
                data = json.loads(pkg_json.read_text())
                engines = data.get("engines", {})
                node_spec = engines.get("node", "")
                m = re.search(r"(\d+)", node_spec)
                if m:
                    return m.group(1)
            except Exception:
                pass
        return None

    def _find_git_root(self, project_dir: Path) -> Path | None:
        """Walk up to find .git directory."""
        current = project_dir
        for _ in range(10):
            if (current / ".git").exists():
                return current
            parent = current.parent
            if parent == current:
                break
            current = parent
        return None

    def _sanitize_project_name(self, name: str) -> str:
        """Sanitize project name for use in config files."""
        name = name.lower()
        name = re.sub(r"[^a-z0-9_-]", "-", name)
        name = re.sub(r"-+", "-", name).strip("-")
        return name or "my-project"


# ─── TemplateLibrary ──────────────────────────────────────────────────────────

class TemplateLibrary:
    """Generates ConfigFileSpec objects from templates based on ProjectContext."""

    def get_configs_for_context(self, ctx: ProjectContext) -> list[ConfigFileSpec]:
        """Return all applicable template-based config specs for this context."""
        specs: list[ConfigFileSpec] = []

        if ctx.primary_ecosystem == "python" or "python" in ctx.ecosystems:
            specs.extend(self._python_configs(ctx))

        if ctx.primary_ecosystem == "node" or "node" in ctx.ecosystems:
            specs.extend(self._node_configs(ctx))

        specs.extend(self._docker_configs(ctx))
        specs.extend(self._general_configs(ctx))
        specs.extend(self._service_configs(ctx))

        # Mark already-existing files
        for spec in specs:
            filename = Path(spec.filename).name
            if filename in ctx.existing_configs or spec.filename in ctx.existing_configs:
                spec.already_exists = True

        return specs

    # ── Python templates ──────────────────────────────────────────────────

    def _python_configs(self, ctx: ProjectContext) -> list[ConfigFileSpec]:
        specs = []
        hints = ctx.framework_hints

        # pyproject.toml — only if no python_modern hint (file already exists)
        if not hints.get("python_modern", False):
            specs.append(ConfigFileSpec(
                filename="pyproject.toml",
                config_type=ConfigType.FRAMEWORK,
                content=self._render_pyproject_toml(ctx),
                reason="Python project metadata and build config",
                source="template",
            ))

        # pytest.ini
        if hints.get("has_pytest", False) or "pytest" in (ctx.all_packages + ctx.dev_packages):
            specs.append(ConfigFileSpec(
                filename="pytest.ini",
                config_type=ConfigType.TOOLING,
                content=self._render_pytest_ini(ctx),
                reason="pytest test runner configuration",
                source="template",
            ))

        # .flake8
        if hints.get("has_flake8", False):
            specs.append(ConfigFileSpec(
                filename=".flake8",
                config_type=ConfigType.LINTING,
                content=self._render_flake8(),
                reason="flake8 linting configuration",
                source="template",
            ))

        # mypy.ini
        if hints.get("has_mypy", False):
            specs.append(ConfigFileSpec(
                filename="mypy.ini",
                config_type=ConfigType.LINTING,
                content=self._render_mypy_ini(ctx),
                reason="mypy static type checker configuration",
                source="template",
            ))

        # .env (only if no .env already)
        specs.append(ConfigFileSpec(
            filename=".env",
            config_type=ConfigType.ENV,
            content=self._render_python_dotenv(ctx),
            reason="Environment variable template for Python project",
            source="template",
        ))

        return specs

    # ── Node.js templates ─────────────────────────────────────────────────

    def _node_configs(self, ctx: ProjectContext) -> list[ConfigFileSpec]:
        specs = []
        hints = ctx.framework_hints

        # .eslintrc.json
        if hints.get("has_eslint", False):
            specs.append(ConfigFileSpec(
                filename=".eslintrc.json",
                config_type=ConfigType.LINTING,
                content=self._render_eslintrc(ctx),
                reason="ESLint linting configuration",
                source="template",
            ))

        # .prettierrc
        if hints.get("has_prettier", False):
            specs.append(ConfigFileSpec(
                filename=".prettierrc",
                config_type=ConfigType.LINTING,
                content=self._render_prettierrc(),
                reason="Prettier code formatting configuration",
                source="template",
            ))

        # jest.config.js
        if hints.get("has_jest", False):
            specs.append(ConfigFileSpec(
                filename="jest.config.js",
                config_type=ConfigType.TOOLING,
                content=self._render_jest_config(ctx),
                reason="Jest test runner configuration",
                source="template",
            ))

        # tsconfig.json
        if hints.get("is_typescript", False):
            specs.append(ConfigFileSpec(
                filename="tsconfig.json",
                config_type=ConfigType.FRAMEWORK,
                content=self._render_tsconfig(ctx),
                reason="TypeScript compiler configuration",
                source="template",
            ))

        # .env for Node.js
        specs.append(ConfigFileSpec(
            filename=".env",
            config_type=ConfigType.ENV,
            content=self._render_node_dotenv(ctx),
            reason="Environment variable template for Node.js project",
            source="template",
        ))

        return specs

    # ── Docker templates ──────────────────────────────────────────────────

    def _docker_configs(self, ctx: ProjectContext) -> list[ConfigFileSpec]:
        specs = []
        # Only generate Docker configs if not already present
        if ctx.has_docker:
            return specs

        if ctx.primary_ecosystem in ("python", "node") or ctx.ecosystems:
            specs.append(ConfigFileSpec(
                filename="Dockerfile",
                config_type=ConfigType.DOCKER,
                content=self._render_dockerfile(ctx),
                reason=f"Dockerfile for {ctx.primary_ecosystem or 'project'} application",
                source="template",
            ))
            specs.append(ConfigFileSpec(
                filename="docker-compose.yml",
                config_type=ConfigType.DOCKER,
                content=self._render_docker_compose(ctx),
                reason="Docker Compose for local development",
                source="template",
            ))
            specs.append(ConfigFileSpec(
                filename=".dockerignore",
                config_type=ConfigType.DOCKER,
                content=self._render_dockerignore(ctx),
                reason="Files to exclude from Docker build context",
                source="template",
            ))

        return specs

    # ── General templates ─────────────────────────────────────────────────

    def _general_configs(self, ctx: ProjectContext) -> list[ConfigFileSpec]:
        specs = []

        # .gitignore — always generate unless present
        specs.append(ConfigFileSpec(
            filename=".gitignore",
            config_type=ConfigType.GENERAL,
            content=self._render_gitignore(ctx),
            reason=f"Git ignore rules for {ctx.primary_ecosystem or 'project'}",
            source="template",
        ))

        # .editorconfig — always generate unless present
        specs.append(ConfigFileSpec(
            filename=".editorconfig",
            config_type=ConfigType.GENERAL,
            content=self._render_editorconfig(),
            reason="Editor configuration for consistent coding style",
            source="template",
        ))

        return specs

    # ── System service templates ──────────────────────────────────────────

    def _service_configs(self, ctx: ProjectContext) -> list[ConfigFileSpec]:
        specs = []
        for service in ctx.system_services:
            if service not in KNOWN_SERVICES:
                continue
            config_filename, config_path = KNOWN_SERVICES[service]
            # Check if config already exists on the system (may fail due to permissions)
            try:
                already_exists = Path(config_path).exists()
            except PermissionError:
                already_exists = False
            content = self._render_service_config(service)
            if content:
                specs.append(ConfigFileSpec(
                    filename=config_path,  # absolute path for system configs
                    config_type=ConfigType.SERVICE,
                    content=content,
                    reason=f"Default configuration for {service}",
                    already_exists=already_exists,
                    source="template",
                ))
        return specs

    # ─────────────────────────────────────────────────────────────────────
    # Individual template renderers
    # ─────────────────────────────────────────────────────────────────────

    def _render_pyproject_toml(self, ctx: ProjectContext) -> str:
        py_ver = ctx.python_version or "3.11"
        ver_match = re.search(r"(\d+\.\d+)", py_ver)
        py_ver_short = ver_match.group(1) if ver_match else "3.11"

        # Use actual production deps from requirements.txt if available
        deps_lines = ""
        if ctx.all_packages:
            deps = [f'  "{p}"' for p in ctx.all_packages[:30]]
            deps_lines = "\n" + ",\n".join(deps) + "\n"

        return f"""[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "{ctx.project_name}"
version = "0.1.0"
description = ""
requires-python = ">={py_ver_short}"
dependencies = [{deps_lines}]

[tool.setuptools.packages.find]
where = ["."]

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py", "*_test.py"]

[tool.mypy]
python_version = "{py_ver_short}"
strict = false
ignore_missing_imports = true
"""

    def _render_pytest_ini(self, ctx: ProjectContext) -> str:
        return f"""[pytest]
testpaths = tests
python_files = test_*.py *_test.py
python_classes = Test*
python_functions = test_*
addopts = -v --tb=short
"""

    def _render_flake8(self) -> str:
        return """[flake8]
max-line-length = 100
extend-ignore = E203, W503
exclude =
    .git,
    __pycache__,
    .venv,
    venv,
    dist,
    build
"""

    def _render_mypy_ini(self, ctx: ProjectContext) -> str:
        py_ver = ctx.python_version or "3.11"
        ver_match = re.search(r"(\d+\.\d+)", py_ver)
        py_ver_short = ver_match.group(1) if ver_match else "3.11"
        return f"""[mypy]
python_version = {py_ver_short}
strict = False
ignore_missing_imports = True
warn_return_any = True
warn_unused_configs = True
"""

    def _render_python_dotenv(self, ctx: ProjectContext) -> str:
        hints = ctx.framework_hints
        lines = [
            "# Environment variables for this project",
            "# Copy this file to .env and fill in values",
            "",
            "# Application",
            f"APP_NAME={ctx.project_name}",
            "DEBUG=true",
            "LOG_LEVEL=INFO",
            "",
        ]
        if hints.get("is_django", False):
            lines += [
                "# Django",
                "SECRET_KEY=change-me-to-a-random-secret-key",
                "ALLOWED_HOSTS=localhost,127.0.0.1",
                "",
            ]
        if hints.get("is_flask", False) or hints.get("is_fastapi", False):
            lines += [
                "# API",
                "HOST=0.0.0.0",
                "PORT=8000",
                "",
            ]
        if hints.get("has_postgres", False):
            lines += [
                "# Database",
                "DATABASE_URL=postgresql://user:password@localhost:5432/dbname",
                "",
            ]
        if hints.get("has_redis", False):
            lines += [
                "# Redis",
                "REDIS_URL=redis://localhost:6379/0",
                "",
            ]
        return "\n".join(lines)

    def _render_eslintrc(self, ctx: ProjectContext) -> str:
        hints = ctx.framework_hints
        extends = ["eslint:recommended"]
        plugins: list[str] = []

        if hints.get("is_typescript", False):
            extends += ["plugin:@typescript-eslint/recommended"]
            plugins.append("@typescript-eslint")

        if hints.get("is_react", False):
            extends += ["plugin:react/recommended", "plugin:react-hooks/recommended"]
            plugins.append("react")

        if hints.get("is_nextjs", False):
            extends.append("next/core-web-vitals")

        config = {
            "env": {"browser": True, "es2021": True, "node": True},
            "extends": extends,
            "parser": "@typescript-eslint/parser" if hints.get("is_typescript", False) else None,
            "plugins": plugins if plugins else None,
            "rules": {
                "no-console": "warn",
                "no-unused-vars": "warn",
            },
        }
        # Remove null values
        config = {k: v for k, v in config.items() if v is not None}
        return json.dumps(config, indent=2)

    def _render_prettierrc(self) -> str:
        config = {
            "semi": True,
            "singleQuote": True,
            "tabWidth": 2,
            "trailingComma": "es5",
            "printWidth": 100,
        }
        return json.dumps(config, indent=2)

    def _render_jest_config(self, ctx: ProjectContext) -> str:
        hints = ctx.framework_hints
        # Browser-based projects (React, Next.js) use jsdom; backend uses node
        test_env = "jsdom" if (hints.get("is_react", False) or hints.get("is_nextjs", False)) else "node"

        if hints.get("is_typescript", False):
            setup = ""
            if hints.get("is_react", False):
                setup = "\n  setupFilesAfterFramework: ['@testing-library/jest-dom'],"
            return f"""/** @type {{import('jest').Config}} */
const config = {{
  preset: 'ts-jest',
  testEnvironment: '{test_env}',
  testMatch: ['**/*.test.ts', '**/*.spec.ts', '**/*.test.tsx', '**/*.spec.tsx'],
  collectCoverageFrom: ['src/**/*.{{ts,tsx}}', '!src/**/*.d.ts'],{setup}
}};

module.exports = config;
"""
        return f"""/** @type {{import('jest').Config}} */
const config = {{
  testEnvironment: '{test_env}',
  testMatch: ['**/*.test.{{js,jsx}}', '**/*.spec.{{js,jsx}}'],
  collectCoverageFrom: ['src/**/*.{{js,jsx}}'],
}};

module.exports = config;
"""

    def _render_tsconfig(self, ctx: ProjectContext) -> str:
        hints = ctx.framework_hints
        config: dict[str, Any] = {
            "compilerOptions": {
                "target": "ES2020",
                "module": "commonjs",
                "lib": ["ES2020"],
                "strict": True,
                "esModuleInterop": True,
                "skipLibCheck": True,
                "forceConsistentCasingInFileNames": True,
                "resolveJsonModule": True,
                "outDir": "./dist",
                "rootDir": "./src",
            },
            "include": ["src/**/*"],
            "exclude": ["node_modules", "dist"],
        }
        if hints.get("is_nextjs", False):
            config["compilerOptions"]["jsx"] = "preserve"
            config["compilerOptions"]["module"] = "esnext"
            config["compilerOptions"]["moduleResolution"] = "bundler"
            config.pop("include", None)
            config.pop("exclude", None)
            config["include"] = ["next-env.d.ts", "**/*.ts", "**/*.tsx"]
            config["exclude"] = ["node_modules"]
        elif hints.get("is_react", False):
            config["compilerOptions"]["jsx"] = "react-jsx"
            config["compilerOptions"]["module"] = "esnext"
            config["compilerOptions"]["moduleResolution"] = "node"
        return json.dumps(config, indent=2)

    def _render_node_dotenv(self, ctx: ProjectContext) -> str:
        hints = ctx.framework_hints
        pkg = ctx.package_json
        port = "3000" if hints.get("is_nextjs", False) or hints.get("is_react", False) else "8080"
        # Try to extract port from scripts
        for script_val in pkg.get("scripts", {}).values():
            m = re.search(r"PORT[=\s]+(\d{4,5})", str(script_val))
            if m:
                port = m.group(1)
                break
        # Use real project name from package.json if available
        app_name = pkg.get("name", ctx.project_name) or ctx.project_name
        lines = [
            "# Environment variables for this project",
            "# Copy this file to .env.local and fill in values",
            "",
            f"PORT={port}",
            "NODE_ENV=development",
            f"APP_NAME={app_name}",
            "",
        ]
        if hints.get("has_postgres", False):
            lines += [
                "# Database",
                "DATABASE_URL=postgresql://user:password@localhost:5432/dbname",
                "",
            ]
        if hints.get("has_redis", False):
            lines += [
                "# Redis",
                "REDIS_URL=redis://localhost:6379",
                "",
            ]
        return "\n".join(lines)

    def _render_dockerfile(self, ctx: ProjectContext) -> str:
        if ctx.primary_ecosystem == "python":
            py_ver = ctx.python_version or "3.11"
            ver_match = re.search(r"(\d+\.\d+)", py_ver)
            py_ver_short = ver_match.group(1) if ver_match else "3.11"
            hints = ctx.framework_hints
            port = "5000" if hints.get("is_flask", False) else "8000"

            # Pick the right start command based on framework
            if hints.get("is_django", False):
                cmd = f'CMD ["python", "manage.py", "runserver", "0.0.0.0:{port}"]'
            elif hints.get("is_flask", False):
                cmd = f'CMD ["python", "-m", "flask", "run", "--host", "0.0.0.0", "--port", "{port}"]'
            elif hints.get("is_fastapi", False):
                cmd = f'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{port}"]'
            else:
                cmd = f'CMD ["python", "main.py"]'

            # Use requirements.txt if it exists, else pyproject.toml
            req_file = (ctx.project_dir / "requirements.txt").exists()
            install_cmd = "RUN pip install --no-cache-dir -r requirements.txt" if req_file else \
                          "RUN pip install --no-cache-dir ."

            return f"""# Build stage
FROM python:{py_ver_short}-slim AS builder
WORKDIR /app
{"COPY requirements*.txt ./" if req_file else "COPY pyproject.toml ./"}
{install_cmd}

# Runtime stage
FROM python:{py_ver_short}-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python{py_ver_short}/site-packages /usr/local/lib/python{py_ver_short}/site-packages
COPY . .
EXPOSE {port}
{cmd}
"""
        elif ctx.primary_ecosystem == "node":
            node_ver = ctx.node_version or "20"
            hints = ctx.framework_hints
            pkg = ctx.package_json

            # Determine port from package.json scripts or framework
            port = "3000" if hints.get("is_nextjs", False) or hints.get("is_react", False) else "8080"
            # Check if PORT is referenced in scripts
            scripts = pkg.get("scripts", {})
            for script_val in scripts.values():
                m = re.search(r"PORT[=\s]+(\d{4,5})", str(script_val))
                if m:
                    port = m.group(1)
                    break

            # Determine start command from package.json scripts
            if "start" in scripts:
                start_cmd = f'CMD ["npm", "start"]'
            elif hints.get("is_nextjs", False):
                start_cmd = f'CMD ["npm", "run", "start"]'
            else:
                main = pkg.get("main", "index.js")
                start_cmd = f'CMD ["node", "{main}"]'

            # Next.js needs a build step
            if hints.get("is_nextjs", False):
                return f"""FROM node:{node_ver}-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:{node_ver}-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE {port}
{start_cmd}
"""
            return f"""# Build stage
FROM node:{node_ver}-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev

# Runtime stage
FROM node:{node_ver}-alpine
WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY . .
EXPOSE {port}
{start_cmd}
"""
        # Fallback generic
        return """FROM ubuntu:22.04
WORKDIR /app
COPY . .
CMD ["/bin/bash"]
"""

    def _render_docker_compose(self, ctx: ProjectContext) -> str:
        hints = ctx.framework_hints
        pkg = ctx.package_json
        # Derive port: check package.json scripts for PORT=, then fall back to framework default
        port = "3000" if hints.get("is_nextjs", False) or hints.get("is_react", False) else (
            "5000" if hints.get("is_flask", False) else "8000"
        )
        for script_val in pkg.get("scripts", {}).values():
            m = re.search(r"PORT[=\s]+(\d{4,5})", str(script_val))
            if m:
                port = m.group(1)
                break

        services: dict[str, Any] = {
            "app": {
                "build": ".",
                "ports": [f"{port}:{port}"],
                "env_file": [".env"],
                "volumes": [".:/app"],
                "restart": "unless-stopped",
            }
        }

        if hints.get("has_postgres", False):
            services["db"] = {
                "image": "postgres:16-alpine",
                "environment": {
                    "POSTGRES_DB": ctx.project_name,
                    "POSTGRES_USER": "postgres",
                    "POSTGRES_PASSWORD": "postgres",
                },
                "ports": ["5432:5432"],
                "volumes": ["postgres_data:/var/lib/postgresql/data"],
            }
            services["app"].setdefault("depends_on", []).append("db")

        if hints.get("has_redis", False):
            services["redis"] = {
                "image": "redis:7-alpine",
                "ports": ["6379:6379"],
            }
            services["app"].setdefault("depends_on", []).append("redis")

        compose: dict[str, Any] = {"services": services}

        # Add named volumes if postgres is present
        if hints.get("has_postgres", False):
            compose["volumes"] = {"postgres_data": None}

        # Serialize manually (no pyyaml dependency)
        return self._dict_to_yaml(compose)

    def _render_dockerignore(self, ctx: ProjectContext) -> str:
        lines = [
            ".git",
            ".gitignore",
            "*.md",
            ".env",
            ".env.*",
            "!.env.example",
            "__pycache__/",
            "*.pyc",
            "*.pyo",
            ".pytest_cache/",
            "node_modules/",
            "dist/",
            "build/",
            ".next/",
            "coverage/",
            ".DS_Store",
            "Thumbs.db",
        ]
        return "\n".join(lines) + "\n"

    def _render_gitignore(self, ctx: ProjectContext) -> str:
        lines = [
            "# General",
            ".DS_Store",
            "Thumbs.db",
            "*.log",
            "",
            "# Environment",
            ".env",
            ".env.local",
            ".env.*.local",
            "",
        ]
        if "python" in ctx.ecosystems or ctx.primary_ecosystem == "python":
            lines += [
                "# Python",
                "__pycache__/",
                "*.py[cod]",
                "*$py.class",
                "*.so",
                ".venv/",
                "venv/",
                "dist/",
                "build/",
                "*.egg-info/",
                ".pytest_cache/",
                ".mypy_cache/",
                ".coverage",
                "htmlcov/",
                "",
            ]
        if "node" in ctx.ecosystems or ctx.primary_ecosystem == "node":
            lines += [
                "# Node.js",
                "node_modules/",
                "dist/",
                ".next/",
                ".nuxt/",
                "coverage/",
                "*.tsbuildinfo",
                "",
            ]
        if "rust" in ctx.ecosystems:
            lines += [
                "# Rust",
                "target/",
                "Cargo.lock",
                "",
            ]
        if "go" in ctx.ecosystems:
            lines += [
                "# Go",
                "*.exe",
                "*.test",
                "vendor/",
                "",
            ]
        lines += [
            "# Docker",
            "*.pid",
            "",
            "# IDE",
            ".idea/",
            ".vscode/",
            "*.swp",
            "*.swo",
        ]
        return "\n".join(lines) + "\n"

    def _render_editorconfig(self) -> str:
        return """root = true

[*]
indent_style = space
indent_size = 4
end_of_line = lf
charset = utf-8
trim_trailing_whitespace = true
insert_final_newline = true

[*.{js,ts,jsx,tsx,json,yaml,yml,html,css,scss}]
indent_size = 2

[*.md]
trim_trailing_whitespace = false

[Makefile]
indent_style = tab
"""

    def _render_service_config(self, service: str) -> str | None:
        """Return a sensible default config for known system services."""
        configs: dict[str, str] = {
            "samba": """[global]
   workgroup = WORKGROUP
   server string = Samba Server
   server role = standalone server
   log file = /var/log/samba/log.%m
   max log size = 1000
   logging = file
   panic action = /usr/share/samba/panic-action %d
   obey pam restrictions = yes
   unix password sync = yes
   passwd program = /usr/bin/passwd %u
   passwd chat = *Enter\\snew\\s*\\spassword:* %n\\n *Retype\\snew\\s*\\spassword:* %n\\n *password\\supdated\\ssuccessfully* .
   pam password change = yes
   map to guest = bad user
   security = user

# Example share — uncomment and edit:
# [shared]
#    path = /srv/samba/shared
#    browseable = yes
#    read only = no
#    guest ok = no
""",
            "nginx": """user www-data;
worker_processes auto;
pid /run/nginx.pid;

events {
    worker_connections 768;
    multi_accept on;
}

http {
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    types_hash_max_size 2048;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;

    access_log /var/log/nginx/access.log;
    error_log /var/log/nginx/error.log;

    gzip on;
    gzip_disable "msie6";

    include /etc/nginx/conf.d/*.conf;
    include /etc/nginx/sites-enabled/*;
}
""",
            "redis": """bind 127.0.0.1 -::1
protected-mode yes
port 6379
tcp-backlog 511
timeout 0
tcp-keepalive 300
daemonize no
pidfile /var/run/redis/redis-server.pid
loglevel notice
logfile /var/log/redis/redis-server.log
databases 16
save 900 1
save 300 10
save 60 10000
maxmemory-policy allkeys-lru
""",
            "redis-server": """bind 127.0.0.1 -::1
protected-mode yes
port 6379
tcp-backlog 511
timeout 0
tcp-keepalive 300
daemonize no
loglevel notice
databases 16
save 900 1
save 300 10
save 60 10000
maxmemory-policy allkeys-lru
""",
            "openssh-server": """Port 22
ListenAddress 0.0.0.0
Protocol 2
HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key
SyslogFacility AUTH
LogLevel INFO
LoginGraceTime 2m
PermitRootLogin prohibit-password
StrictModes yes
MaxAuthTries 6
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
PasswordAuthentication yes
ChallengeResponseAuthentication no
UsePAM yes
X11Forwarding yes
PrintMotd no
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
""",
            "fail2ban": """[DEFAULT]
bantime  = 10m
findtime  = 10m
maxretry = 5
backend = auto
usedns = warn

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
backend = %(sshd_backend)s
""",
        }
        return configs.get(service)

    def _dict_to_yaml(self, d: Any, indent: int = 0) -> str:
        """Simple recursive dict-to-YAML serializer (no external dependency)."""
        lines = []
        prefix = "  " * indent
        if isinstance(d, dict):
            for k, v in d.items():
                if v is None:
                    lines.append(f"{prefix}{k}: {{}}")
                elif isinstance(v, dict):
                    lines.append(f"{prefix}{k}:")
                    lines.append(self._dict_to_yaml(v, indent + 1))
                elif isinstance(v, list):
                    lines.append(f"{prefix}{k}:")
                    for item in v:
                        if isinstance(item, str):
                            lines.append(f"{prefix}  - {item}")
                        else:
                            lines.append(f"{prefix}  -")
                            lines.append(self._dict_to_yaml(item, indent + 2))
                elif isinstance(v, bool):
                    lines.append(f"{prefix}{k}: {'true' if v else 'false'}")
                elif isinstance(v, str) and any(c in v for c in [":", "#", "{"]):
                    lines.append(f'{prefix}{k}: "{v}"')
                else:
                    lines.append(f"{prefix}{k}: {v}")
        elif isinstance(d, list):
            for item in d:
                if isinstance(item, str):
                    lines.append(f"{prefix}- {item}")
                else:
                    lines.append(f"{prefix}-")
                    lines.append(self._dict_to_yaml(item, indent + 1))
        else:
            return str(d)
        return "\n".join(filter(None, lines))


# ─── ConfigLLMAdvisor ─────────────────────────────────────────────────────────

class ConfigLLMAdvisor:
    """Calls the LLM to suggest additional configs and customize template values."""

    def __init__(self, api_key: str, provider: str):
        self.api_key = api_key
        self.provider = provider.lower()
        self._initialize_client()

    def _initialize_client(self):
        if self.provider == "openai":
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=self.api_key)
                self.model = "gpt-4"
            except ImportError:
                self.client = None
        elif self.provider == "claude":
            try:
                from anthropic import Anthropic
                self.client = Anthropic(api_key=self.api_key)
                self.model = "claude-sonnet-4-20250514"
            except ImportError:
                self.client = None
        elif self.provider == "ollama":
            try:
                from openai import OpenAI
                from helix.config_utils import get_ollama_model
                ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
                self.client = OpenAI(api_key="ollama", base_url=f"{ollama_base_url}/v1")
                self.model = get_ollama_model()
            except ImportError:
                self.client = None
        else:
            self.client = None

    def _get_system_prompt(self) -> str:
        return """You are a project configuration expert. You receive a JSON description of a software project and a list of config files already generated from templates.

Your job is to:
1. Suggest additional config files not already covered (only if clearly needed)
2. Provide improved/customized content for any of the template configs where you can do better

Output ONLY a valid JSON object in this exact format:
{
  "additional_configs": [
    {
      "filename": ".github/workflows/ci.yml",
      "config_type": "tooling",
      "content": "... full file content ...",
      "reason": "GitHub Actions CI pipeline"
    }
  ],
  "template_overrides": [
    {
      "filename": "docker-compose.yml",
      "content": "... full improved content ...",
      "reason": "Updated ports and service names for this specific project"
    }
  ]
}

Rules:
- CRITICAL: Never suggest any filename listed in DO_NOT_CREATE_these_already_exist_or_are_already_being_generated. Those files already exist on disk or are already being generated by templates. Suggesting them would overwrite existing work.
- Only suggest a config if you are confident it is needed based on the detected stack
- All file content must be complete and valid — no placeholders, no TODO markers
- config_type must be one of: linting, docker, env, framework, tooling, general, service
- Prefer fewer, high-quality suggestions over many speculative ones
- If there is nothing meaningful to add or override, return empty arrays"""

    def _build_user_prompt(self, ctx: ProjectContext, template_specs: list[ConfigFileSpec]) -> str:
        # Combine files already in the directory AND files the templates will create
        # so the LLM never suggests any of these
        do_not_create = sorted(set(ctx.existing_configs + [s.filename for s in template_specs]))
        payload = {
            "project_name": ctx.project_name,
            "ecosystems": ctx.ecosystems,
            "primary_ecosystem": ctx.primary_ecosystem,
            "packages": ctx.all_packages[:30],
            "dev_packages": ctx.dev_packages[:20],
            "framework_hints": {k: v for k, v in ctx.framework_hints.items() if v},
            "python_version": ctx.python_version,
            "node_version": ctx.node_version,
            "has_docker": ctx.has_docker,
            "has_git": ctx.git_root is not None,
            "system_services": ctx.system_services,
            "DO_NOT_CREATE_these_already_exist_or_are_already_being_generated": do_not_create,
        }
        return json.dumps(payload, indent=2)

    def advise(
        self,
        ctx: ProjectContext,
        template_specs: list[ConfigFileSpec],
    ) -> list[ConfigFileSpec]:
        """Call LLM and return additional/override ConfigFileSpecs. Never raises."""
        if self.client is None:
            return []

        try:
            system_prompt = self._get_system_prompt()
            user_prompt = self._build_user_prompt(ctx, template_specs)
            raw = self._call_llm(system_prompt, user_prompt)
            return self._parse_llm_response(raw, template_specs)
        except Exception as e:
            # LLM failure is non-fatal — templates are still written
            print(f"[dim]LLM advisor skipped: {e}[/dim]", file=sys.stderr)
            return []

    def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        if self.provider in ("openai", "ollama"):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=4000,
            )
            return response.choices[0].message.content.strip()
        elif self.provider == "claude":
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                temperature=0.2,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text.strip()
        return "{}"

    def _parse_llm_response(
        self, raw: str, template_specs: list[ConfigFileSpec]
    ) -> list[ConfigFileSpec]:
        # Strip markdown code blocks
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            parts = raw.split("```")
            if len(parts) >= 3:
                raw = parts[1].strip()

        # Repair trailing commas
        raw = re.sub(r",\s*([}\]])", r"\1", raw)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        result: list[ConfigFileSpec] = []
        valid_types = {e.value for e in ConfigType}

        # Additional configs
        for item in data.get("additional_configs", []):
            ct_str = item.get("config_type", "general")
            if ct_str not in valid_types:
                ct_str = "general"
            result.append(ConfigFileSpec(
                filename=item.get("filename", ""),
                config_type=ConfigType(ct_str),
                content=item.get("content", ""),
                reason=item.get("reason", "LLM suggestion"),
                source="llm",
            ))

        # Template overrides — update existing specs in-place
        template_by_filename = {s.filename: s for s in template_specs}
        for item in data.get("template_overrides", []):
            fname = item.get("filename", "")
            if fname in template_by_filename:
                template_by_filename[fname].content = item.get("content", template_by_filename[fname].content)
                template_by_filename[fname].source = "template+llm"
                template_by_filename[fname].reason += f" (LLM: {item.get('reason', 'improved')})"

        return [s for s in result if s.filename]

    def advise_explicit(self, target: str) -> list[ConfigFileSpec] | None:
        """For explicit mode: ask LLM to generate config for a named service.
        Returns None if LLM unavailable."""
        if self.client is None:
            return None

        system_prompt = """You are a Linux system configuration expert. When given a service or tool name, generate a complete, production-ready configuration file for it.

Ask the user 3-5 essential questions needed to customize the config, then generate the full config based on their answers.

Output a JSON object:
{
  "questions": [
    {"key": "workgroup", "prompt": "Workgroup name?", "default": "WORKGROUP"}
  ],
  "config_filename": "smb.conf",
  "config_path": "/etc/samba/smb.conf",
  "config_type": "service"
}

Then, after receiving answers, output:
{
  "filename": "/etc/samba/smb.conf",
  "config_type": "service",
  "content": "... full config ...",
  "reason": "Samba configuration"
}"""

        try:
            raw = self._call_llm(system_prompt, f"Generate configuration for: {target}")
            raw = re.sub(r",\s*([}\]])", r"\1", raw)
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                parts = raw.split("```")
                if len(parts) >= 3:
                    raw = parts[1].strip()

            data = json.loads(raw)

            # If LLM returned questions, handle the Q&A flow
            if "questions" in data:
                answers = {}
                print()
                for q in data.get("questions", []):
                    key = q.get("key", "value")
                    prompt_text = q.get("prompt", key)
                    default = q.get("default", "")
                    try:
                        prompt_display = f"  {prompt_text}"
                        if default:
                            prompt_display += f" [{default}]"
                        prompt_display += ": "
                        answer = input(prompt_display).strip()
                        answers[key] = answer if answer else default
                    except (EOFError, KeyboardInterrupt):
                        return None

                # Second LLM call with answers
                answers_str = json.dumps(answers, indent=2)
                final_prompt = f"Generate complete configuration for {target} with these settings:\n{answers_str}"
                raw2 = self._call_llm(system_prompt, final_prompt)
                raw2 = re.sub(r",\s*([}\]])", r"\1", raw2)
                if "```json" in raw2:
                    raw2 = raw2.split("```json")[1].split("```")[0].strip()
                elif "```" in raw2:
                    parts = raw2.split("```")
                    if len(parts) >= 3:
                        raw2 = parts[1].strip()
                data = json.loads(raw2)

            if "filename" in data and "content" in data:
                ct_str = data.get("config_type", "service")
                if ct_str not in {e.value for e in ConfigType}:
                    ct_str = "service"
                config_path = data.get("filename") or KNOWN_SERVICES.get(target, (target, f"/etc/{target}/{target}.conf"))[1]
                return [ConfigFileSpec(
                    filename=config_path,
                    config_type=ConfigType(ct_str),
                    content=data.get("content", ""),
                    reason=data.get("reason", f"Configuration for {target}"),
                    source="llm",
                )]
        except Exception as e:
            print(f"LLM error for explicit target: {e}", file=sys.stderr)

        # Fallback: use template if available
        if target in KNOWN_SERVICES:
            lib = TemplateLibrary()
            content = lib._render_service_config(target)
            if content:
                _, config_path = KNOWN_SERVICES[target]
                return [ConfigFileSpec(
                    filename=config_path,
                    config_type=ConfigType.SERVICE,
                    content=content,
                    reason=f"Default configuration for {target}",
                    source="template",
                )]
        return None


# ─── ProjectConfigurator ──────────────────────────────────────────────────────

class ProjectConfigurator:
    """Orchestrates the full configure pipeline."""

    def __init__(self, api_key: str, provider: str):
        self.api_key = api_key
        self.provider = provider
        self._advisor = ConfigLLMAdvisor(api_key, provider)

    def run(
        self,
        project_dir: Path,
        target: str | None = None,
        dry_run: bool = False,
        force: bool = False,
        only: str | None = None,
    ) -> int:
        from helix.branding import cx_header, cx_print, console

        # Validate --only filter
        if only:
            valid_types = {e.value for e in ConfigType}
            if only not in valid_types:
                cx_print(f"Invalid type '{only}'. Valid types: {', '.join(sorted(valid_types))}", "error")
                return 1

        # ── Explicit mode: user named a specific service ──────────────────
        if target:
            return self._run_explicit(target, dry_run, force)

        # ── Auto-detect mode ──────────────────────────────────────────────
        cx_print(f"Scanning: {project_dir}", "info")

        # Stage 1: analyze
        cx_print("Analyzing project...", "thinking")
        analyzer = ProjectAnalyzer()
        ctx = analyzer.analyze(project_dir)

        if not ctx.ecosystems and not ctx.system_services:
            cx_print("No known project files or installed services detected.", "warning")
            cx_print("Generating general configs only (.gitignore, .editorconfig).", "info")

        # Stage 2: templates
        cx_print("Generating config templates...", "thinking")
        lib = TemplateLibrary()
        specs = lib.get_configs_for_context(ctx)

        # Stage 3: LLM (additive, non-blocking)
        if specs or ctx.system_services:
            cx_print("Consulting LLM for additional suggestions...", "thinking")
            llm_specs = self._advisor.advise(ctx, specs)
            specs.extend(llm_specs)

        # Stage 4: filter by --only
        if only:
            filter_type = ConfigType(only)
            specs = [s for s in specs if s.config_type == filter_type]

        if not specs:
            cx_print("No config files to generate.", "info")
            return 0

        # Stage 5: dry-run — show preview table and exit
        if dry_run:
            self._show_dry_run(specs, force)
            cx_print("Dry run — no files written.", "info")
            return 0

        # Stage 6: interactive file selection
        chosen = self._show_and_select(specs, force)
        if chosen is None:
            cx_print("Configuration cancelled.", "info")
            return 0
        if not chosen:
            cx_print("No files selected.", "info")
            return 0

        # Stage 7: write
        return self._write_configs(chosen, project_dir, force)

    def _run_explicit(self, target: str, dry_run: bool, force: bool) -> int:
        from helix.branding import cx_print, console

        cx_print(f"Configuring: {target}", "thinking")

        # Try template first
        lib = TemplateLibrary()
        template_content = lib._render_service_config(target)

        if target in KNOWN_SERVICES:
            _, config_path = KNOWN_SERVICES[target]
        else:
            config_path = f"/etc/{target}/{target}.conf"

        already_exists = Path(config_path).exists()

        if template_content and not already_exists:
            specs = [ConfigFileSpec(
                filename=config_path,
                config_type=ConfigType.SERVICE,
                content=template_content,
                reason=f"Default configuration for {target}",
                source="template",
            )]
        else:
            # Use LLM for interactive Q&A
            cx_print(f"Generating configuration for {target} with LLM...", "thinking")
            llm_specs = self._advisor.advise_explicit(target)
            if llm_specs is None:
                cx_print(f"Could not generate configuration for '{target}'.", "error")
                cx_print(f"Try: helix configure (auto-detect mode) or check if '{target}' is installed.", "info")
                return 1
            specs = llm_specs

        if dry_run:
            self._show_dry_run(specs, force)
            cx_print("Dry run — no files written.", "info")
            return 0

        chosen = self._show_and_select(specs, force)
        if chosen is None:
            cx_print("Configuration cancelled.", "info")
            return 0
        if not chosen:
            cx_print("No files selected.", "info")
            return 0

        return self._write_configs(chosen, Path.cwd(), force)

    def _show_dry_run(self, specs: list[ConfigFileSpec], force: bool) -> None:
        """Print a read-only preview table for --dry-run mode."""
        from rich.table import Table
        from rich import box
        from helix.branding import console

        console.print()
        console.print("[bold cyan]━━━ Config Files to Generate (Dry Run) ━━━[/bold cyan]")
        console.print()

        table = Table(show_header=True, header_style="bold cyan", box=box.ROUNDED, padding=(0, 1))
        table.add_column("Status", style="bold", width=8)
        table.add_column("File", style="cyan")
        table.add_column("Type", style="dim", width=10)
        table.add_column("Reason")
        table.add_column("Source", style="dim", width=12)

        create_count = 0
        skip_count = 0
        for spec in specs:
            if spec.already_exists and not force:
                table.add_row("[yellow]skip[/yellow]", f"[dim]{spec.filename}[/dim]",
                              spec.config_type.value, f"[dim]{spec.reason}[/dim]", spec.source)
                skip_count += 1
            else:
                table.add_row("[green]create[/green]", spec.filename,
                              spec.config_type.value, spec.reason, spec.source)
                create_count += 1

        console.print(table)
        console.print()
        parts = []
        if create_count:
            parts.append(f"[green]{create_count} to create[/green]")
        if skip_count:
            parts.append(f"[yellow]{skip_count} skipped (already exist — use --force)[/yellow]")
        if parts:
            console.print("  " + "  •  ".join(parts))
        console.print()

    def _show_and_select(self, specs: list[ConfigFileSpec], force: bool) -> list[ConfigFileSpec] | None:
        """Interactive file selection. Returns chosen subset, or None if cancelled."""
        from rich.table import Table
        from rich import box
        from helix.branding import console

        # Build the selectable list: skip already-existing files unless --force
        selectable: list[ConfigFileSpec] = []
        skipped: list[ConfigFileSpec] = []
        for spec in specs:
            if spec.already_exists and not force:
                skipped.append(spec)
            else:
                selectable.append(spec)

        if not selectable:
            console.print()
            cx_print = self._cx_print
            cx_print("All detected configs already exist. Use --force to overwrite.", "warning")
            return []

        # Start with all files selected (True = will be created)
        selected = [True] * len(selectable)

        while True:
            console.print()
            console.print("[bold cyan]━━━ Select Config Files to Generate ━━━[/bold cyan]")
            console.print()
            console.print(
                "  [bold]Space[/bold] or number to toggle  •  "
                "[bold]a[/bold] select all  •  "
                "[bold]n[/bold] deselect all  •  "
                "[bold]p[/bold] preview  •  "
                "[bold]e[/bold] edit  •  "
                "[bold]Enter[/bold] to confirm  •  "
                "[bold]q[/bold] to cancel"
            )
            console.print()

            table = Table(show_header=True, header_style="bold cyan", box=box.ROUNDED, padding=(0, 1))
            table.add_column("#", style="dim", width=4)
            table.add_column("", width=3)   # checkbox
            table.add_column("File", style="cyan")
            table.add_column("Type", style="dim", width=10)
            table.add_column("Reason")
            table.add_column("Source", style="dim", width=12)

            for i, (spec, sel) in enumerate(zip(selectable, selected), 1):
                checkbox = "[green]✓[/green]" if sel else "[dim]○[/dim]"
                file_style = spec.filename if sel else f"[dim]{spec.filename}[/dim]"
                table.add_row(
                    str(i),
                    checkbox,
                    file_style,
                    spec.config_type.value,
                    spec.reason if sel else f"[dim]{spec.reason}[/dim]",
                    spec.source,
                )

            if skipped:
                for spec in skipped:
                    table.add_row(
                        "-",
                        "[yellow]–[/yellow]",
                        f"[dim]{spec.filename}[/dim]",
                        spec.config_type.value,
                        "[dim]already exists (use --force)[/dim]",
                        spec.source,
                    )

            console.print(table)
            console.print()

            chosen_count = sum(selected)
            console.print(f"  [green]{chosen_count}[/green] of [cyan]{len(selectable)}[/cyan] selected")
            console.print()

            try:
                raw = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return None

            if raw == "" or raw == "y":
                # Confirm
                break
            elif raw == "q":
                return None
            elif raw == "a":
                selected = [True] * len(selectable)
            elif raw == "n":
                selected = [False] * len(selectable)
            elif raw == "p":
                self._preview_file(selectable)
            elif raw == "e":
                self._edit_file(selectable)
            else:
                # Try to parse as number or comma-separated numbers to toggle
                try:
                    indices = [int(x.strip()) - 1 for x in raw.replace(",", " ").split() if x.strip()]
                    for idx in indices:
                        if 0 <= idx < len(selected):
                            selected[idx] = not selected[idx]
                except ValueError:
                    console.print("  [dim]Enter a number (e.g. 1 3), 'a', 'n', 'p', 'e', or Enter to confirm.[/dim]")

        return [spec for spec, sel in zip(selectable, selected) if sel]

    def _preview_file(self, specs: list[ConfigFileSpec]) -> None:
        """Show content of a specific file."""
        print()
        for i, spec in enumerate(specs, 1):
            print(f"  {i}. {spec.filename}")
        print()
        try:
            idx_str = input("  Preview which file? (number): ").strip()
            idx = int(idx_str) - 1
            if 0 <= idx < len(specs):
                spec = specs[idx]
                print(f"\n{'─' * 60}")
                print(f"  {spec.filename}")
                print(f"{'─' * 60}")
                print(spec.content)
                print(f"{'─' * 60}\n")
        except (ValueError, EOFError, KeyboardInterrupt):
            pass

    def _edit_file(self, specs: list[ConfigFileSpec]) -> None:
        """Open a file in nano for editing before writing."""
        print()
        for i, spec in enumerate(specs, 1):
            print(f"  {i}. {spec.filename}")
        print()
        try:
            idx_str = input("  Edit which file? (number): ").strip()
            idx = int(idx_str) - 1
            if 0 <= idx < len(specs):
                spec = specs[idx]
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=Path(spec.filename).suffix or ".conf",
                    delete=False,
                    prefix="helix_config_",
                ) as f:
                    tmp_path = f.name
                    f.write(spec.content)
                try:
                    subprocess.run(["nano", tmp_path])
                    with open(tmp_path) as f:
                        spec.content = f.read()
                except FileNotFoundError:
                    print("  nano not found. Keeping original content.")
                finally:
                    os.unlink(tmp_path)
        except (ValueError, EOFError, KeyboardInterrupt):
            pass

    @staticmethod
    def _cx_print(message: str, status: str = "info") -> None:
        """Minimal cx_print without importing branding at class level."""
        from helix.branding import cx_print
        cx_print(message, status)

    def _write_configs(
        self, specs: list[ConfigFileSpec], project_dir: Path, force: bool
    ) -> int:
        from helix.branding import cx_print

        failures: list[str] = []

        for spec in specs:
            if spec.already_exists and not force:
                cx_print(f"Skipped (already exists): {spec.filename}", "warning")
                continue
            try:
                self._write_single(spec, project_dir)
                cx_print(f"Created: {spec.filename}", "success")
            except PermissionError:
                cx_print(f"Permission denied: {spec.filename} (try sudo?)", "error")
                failures.append(spec.filename)
            except OSError as e:
                cx_print(f"Failed to write {spec.filename}: {e}", "error")
                failures.append(spec.filename)

        if failures:
            cx_print(f"{len(failures)} file(s) could not be written.", "error")
            return 1

        from helix.branding import console
        console.print()
        cx_print("Configuration complete.", "success")
        return 0

    def _write_single(self, spec: ConfigFileSpec, project_dir: Path) -> None:
        """Write a single config file. Raises OSError / PermissionError on failure."""
        # Absolute paths (system service configs) — write directly
        if spec.filename.startswith("/"):
            target = Path(spec.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(spec.content, encoding="utf-8")
        else:
            # Relative paths — write inside project dir
            target = project_dir / spec.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(spec.content, encoding="utf-8")
