"""
Helix Linux Branding Module

Provides consistent visual branding across all Helix CLI output.
Uses Rich library for cross-platform terminal styling.

Enhanced with rich output formatting (Issue #242):
- Color-coded status messages
- Formatted boxes and panels
- Progress spinners and bars
- Consistent visual language
"""

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# Brand colors
HELIX_CYAN = "cyan"
HELIX_DARK = "dark_cyan"
HELIX_SUCCESS = "green"
HELIX_WARNING = "yellow"
HELIX_ERROR = "red"
HELIX_INFO = "blue"
HELIX_MUTED = "dim"

# ASCII Logo - matches the CX circular logo
LOGO_LARGE = """
[bold cyan]   ██████╗██╗  ██╗[/bold cyan]
[bold cyan]  ██╔════╝╚██╗██╔╝[/bold cyan]
[bold cyan]  ██║      ╚███╔╝ [/bold cyan]
[bold cyan]  ██║      ██╔██╗ [/bold cyan]
[bold cyan]  ╚██████╗██╔╝ ██╗[/bold cyan]
[bold cyan]   ╚═════╝╚═╝  ╚═╝[/bold cyan]
"""

LOGO_SMALL = """[bold cyan]╔═╗─┐ ┬[/bold cyan]
[bold cyan]║  ┌┴┬┘[/bold cyan]
[bold cyan]╚═╝┴ └─[/bold cyan]"""

# Version info
VERSION = "0.1.0"


def show_banner(show_version: bool = True):
    """
    Display the full Helix banner.
    Called on first run or with --version flag.
    """
    content = LOGO_LARGE + "\n"
    content += "[dim]HelixLinux[/dim] [white]• AI-Powered Package Manager[/white]"

    if show_version:
        content += f"\n[dim]v{VERSION}[/dim]"

    console.print(Panel(content, border_style="cyan", padding=(0, 2)))


def cx_print(message: str, status: str = "info"):
    """
    Print a message with the HX badge prefix.
    Like Claude's orange icon, but for Helix.

    Args:
        message: The message to display
        status: One of "info", "success", "warning", "error", "thinking"
    """
    badge = "[bold white on dark_cyan] HX [/bold white on dark_cyan]"

    status_icons = {
        "info": "[dim]│[/dim]",
        "success": "[green]✓[/green]",
        "warning": "[yellow]⚠[/yellow]",
        "error": "[red]✗[/red]",
        "thinking": "[cyan]⠋[/cyan]",  # Spinner frame
    }

    icon = status_icons.get(status, status_icons["info"])
    console.print(f"{badge} {icon} {message}")


def cx_step(step_num: int, total: int, message: str):
    """
    Print a numbered step with the HX badge.

    Example: HX │ [1/4] Updating package lists...
    """
    badge = "[bold white on dark_cyan] HX [/bold white on dark_cyan]"
    console.print(f"{badge} [dim]│[/dim] [{step_num}/{total}] {message}")


def cx_header(title: str):
    """
    Print a section header.
    """
    console.print()
    console.print(f"[bold cyan]━━━ {title} ━━━[/bold cyan]")
    console.print()


def cx_table_header():
    """
    Returns styled header for package tables.
    """
    return (
        "[bold cyan]Package[/bold cyan]",
        "[bold cyan]Version[/bold cyan]",
        "[bold cyan]Action[/bold cyan]",
    )


def show_welcome():
    """
    First-run welcome message.
    """
    show_banner()
    console.print()
    cx_print("Welcome to Helix! Let's get you set up.", "success")
    cx_print("Run [bold]helix wizard[/bold] to configure your API key.", "info")
    console.print()


def show_goodbye():
    """
    Exit message.
    """
    console.print()
    cx_print("Done! Run [bold]helix --help[/bold] for more commands.", "info")
    console.print()


# ============================================
# Rich Output Formatting (Issue #242)
# ============================================


def cx_box(
    content: str,
    title: str | None = None,
    subtitle: str | None = None,
    status: str = "info",
) -> None:
    """
    Print content in a styled box/panel.

    Args:
        content: Content to display
        title: Optional box title
        subtitle: Optional box subtitle
        status: Style - "info", "success", "warning", "error"
    """
    border_colors = {
        "info": HELIX_CYAN,
        "success": HELIX_SUCCESS,
        "warning": HELIX_WARNING,
        "error": HELIX_ERROR,
    }
    border_style = border_colors.get(status, HELIX_CYAN)

    panel = Panel(
        content,
        title=f"[bold]{title}[/bold]" if title else None,
        subtitle=f"[dim]{subtitle}[/dim]" if subtitle else None,
        border_style=border_style,
        padding=(1, 2),
        box=box.ROUNDED,
    )
    console.print(panel)


def cx_status_box(
    title: str,
    items: list[tuple[str, str, str]],
) -> None:
    """
    Print a status box with aligned key-value pairs.

    Example output:
    ┌─────────────────────────────────────────┐
    │  HELIX ML SCHEDULER                    │
    ├─────────────────────────────────────────┤
    │  Status:    Active                      │
    │  Uptime:    0.5 seconds                 │
    └─────────────────────────────────────────┘

    Args:
        title: Box title
        items: List of (label, value, status) tuples
               status: "success", "warning", "error", "info", "default"
    """
    style_colors = {
        "success": HELIX_SUCCESS,
        "warning": HELIX_WARNING,
        "error": HELIX_ERROR,
        "info": HELIX_CYAN,
        "default": "white",
    }

    max_label_len = max(len(item[0]) for item in items) if items else 0
    lines = []

    for label, value, status in items:
        color = style_colors.get(status, "white")
        padded_label = label.ljust(max_label_len)
        lines.append(f"  [dim]{padded_label}:[/dim]  [{color}]{value}[/{color}]")

    content = "\n".join(lines)
    panel = Panel(
        content,
        title=f"[bold cyan]{title}[/bold cyan]",
        border_style=HELIX_CYAN,
        padding=(1, 2),
        box=box.ROUNDED,
    )
    console.print(panel)


def cx_table(
    headers: list[str],
    rows: list[list[str]],
    title: str | None = None,
    row_styles: list[str] | None = None,
) -> None:
    """
    Print a formatted table with Helix styling.

    Args:
        headers: Column header names
        rows: List of rows (each row is a list of cell values)
        title: Optional table title
        row_styles: Optional list of styles for each row
    """
    table = Table(
        title=f"[bold cyan]{title}[/bold cyan]" if title else None,
        show_header=True,
        header_style="bold cyan",
        border_style=HELIX_CYAN,
        box=box.ROUNDED,
        padding=(0, 1),
    )

    for header in headers:
        table.add_column(header, style="cyan")

    for i, row in enumerate(rows):
        style = row_styles[i] if row_styles and i < len(row_styles) else None
        table.add_row(*row, style=style)

    console.print(table)


def cx_package_table(
    packages: list[tuple[str, str, str]],
    title: str = "Packages",
) -> None:
    """
    Print a formatted package table.

    Args:
        packages: List of (name, version, action) tuples
        title: Table title
    """
    table = Table(
        title=f"[bold cyan]{title}[/bold cyan]",
        show_header=True,
        header_style="bold cyan",
        border_style=HELIX_CYAN,
        box=box.ROUNDED,
        padding=(0, 1),
    )

    table.add_column("Package", style="cyan", no_wrap=True)
    table.add_column("Version", style="white")
    table.add_column("Action", style="green")

    for name, version, action in packages:
        # Color-code actions
        if "install" in action.lower():
            action_styled = f"[green]{action}[/green]"
        elif "remove" in action.lower() or "uninstall" in action.lower():
            action_styled = f"[red]{action}[/red]"
        elif "update" in action.lower() or "upgrade" in action.lower():
            action_styled = f"[yellow]{action}[/yellow]"
        else:
            action_styled = action
        table.add_row(name, version, action_styled)

    console.print(table)


def cx_divider(title: str | None = None) -> None:
    """
    Print a horizontal divider with optional title.

    Args:
        title: Optional section title
    """
    if title:
        console.print(f"\n[bold cyan]━━━ {title} ━━━[/bold cyan]\n")
    else:
        console.print(f"[{HELIX_CYAN}]{'━' * 50}[/{HELIX_CYAN}]")


def cx_success(message: str) -> None:
    """Print a success message with checkmark."""
    console.print(f"[{HELIX_SUCCESS}]✓[/{HELIX_SUCCESS}] {message}")


def cx_error(message: str) -> None:
    """Print an error message with X."""
    console.print(f"[{HELIX_ERROR}]✗[/{HELIX_ERROR}] [{HELIX_ERROR}]{message}[/{HELIX_ERROR}]")


def cx_warning(message: str) -> None:
    """Print a warning message with warning icon."""
    console.print(
        f"[{HELIX_WARNING}]⚠[/{HELIX_WARNING}] [{HELIX_WARNING}]{message}[/{HELIX_WARNING}]"
    )


def cx_info(message: str) -> None:
    """Print an info message with info icon."""
    console.print(f"[{HELIX_INFO}]ℹ[/{HELIX_INFO}] {message}")


def cx_spinner_message(message: str) -> None:
    """Print a message with spinner icon (static, for logs)."""
    console.print(f"[{HELIX_CYAN}]⠋[/{HELIX_CYAN}] {message}")


# Demo
if __name__ == "__main__":
    # Full banner
    show_banner()
    print()

    # Status box demo (Issue #242 format)
    cx_status_box(
        "HELIX ML SCHEDULER",
        [
            ("Status", "Active", "success"),
            ("Uptime", "0.5 seconds", "default"),
            ("CPU Usage", "12%", "info"),
            ("Memory", "256 MB", "warning"),
        ],
    )
    print()

    # Package table demo
    cx_package_table(
        [
            ("docker.io", "24.0.5", "Install"),
            ("docker-compose", "2.20.2", "Install"),
            ("nginx", "1.24.0", "Update"),
        ],
        title="Installation Plan",
    )
    print()

    # Simulated operation flow
    cx_divider("Installation Progress")
    cx_step(1, 4, "Updating package lists...")
    cx_step(2, 4, "Installing docker.io...")
    cx_step(3, 4, "Installing docker-compose...")
    cx_step(4, 4, "Configuring services...")
    print()

    # Status messages
    cx_success("Package installed successfully")
    cx_warning("Disk space running low")
    cx_error("Installation failed")
    cx_info("Checking dependencies...")
    print()

    # Box demo
    cx_box(
        "Installation completed!\nAll packages are now available.",
        title="Success",
        status="success",
    )
