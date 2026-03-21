# Helix Linux

AI-powered Linux command interpreter and package manager. Describe what you need in plain English instead of memorizing commands.

---

## Setup

```bash
# Clone and enter the repo
git clone https://github.com/helixlinux/helix.git
cd helix

# Create virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## Configure AI Provider

Run the interactive wizard:

```bash
helix wizard
```

This will prompt you to choose a provider (OpenAI, Claude, or Ollama) and enter your API key. The key is saved to `~/.helix/.env` and your project `.env`.

Or configure manually by copying `.env.example` to `.env` and setting:

| Provider | Variables |
|----------|-----------|
| **Ollama** (free, local) | `HELIX_PROVIDER=ollama` then run `python scripts/setup_ollama.py` |
| **OpenAI** | `HELIX_PROVIDER=openai` and `OPENAI_API_KEY=your-key` |
| **Claude** | `HELIX_PROVIDER=claude` and `ANTHROPIC_API_KEY=your-key` |

---

## Commands

### ask
Ask a question about your system or Linux in general.
```bash
helix ask "How do I check disk usage?"
```

### install
Install software using natural language.
```bash
helix install nginx --dry-run       # Preview (default)
helix install nginx --execute       # Actually install
helix install nginx --parallel      # Parallel execution
helix install nginx --sandbox --execute  # Test in Docker first, then install
```

### stack
Manage pre-built package collections (`ml`, `ml-cpu`, `webdev`, `devops`, `data`).
```bash
helix stack --list                  # List available stacks
helix stack --describe webdev       # Show stack details
helix stack webdev --dry-run        # Preview
helix stack webdev --execute        # Install
```

### uninstall
Remove installed software. The LLM resolves the correct package manager (apt, pip, etc.).
```bash
helix uninstall nginx                # Dry-run by default (shows commands)
helix uninstall nginx --execute      # Actually remove the package
```

### history
View installation history. Every install/rollback is tracked in a local SQLite database.
```bash
helix history                       # List recent installations
helix history --limit 5             # Show last 5 records
helix history --status success      # Filter by status (success/failed)
helix history <id>                  # Show details of a specific installation
helix history --clear               # Delete all history
```

### rollback
Undo a previous installation. Compares before/after package snapshots and restores the previous state.
```bash
helix rollback <id> --dry-run       # Preview what would be rolled back
helix rollback <id>                 # Execute the rollback
```

### resolve
Resolve package dependency conflicts or diagnose installation errors.
```bash
helix resolve nginx --tree          # Show dependency tree
helix resolve docker --auto-remove-conflicts  # Handle conflicts
helix resolve --error "Unable to locate package nginx"  # Diagnose error
helix resolve --error-file error.log --json   # Parse error from file
```

### sandbox
Manage Docker sandbox environments for safe package testing.
```bash
helix sandbox status                # Show Docker availability and active sandboxes
helix sandbox cleanup               # Remove all sandbox containers
```

### daemon
Manage the helixd background daemon.
```bash
helix daemon install --execute      # Install and enable daemon service
helix daemon uninstall --execute    # Stop and remove daemon service
helix daemon config                 # Show daemon configuration
helix daemon ping                   # Test daemon connectivity
helix daemon shutdown               # Request daemon shutdown
```

### wizard
Interactive setup wizard for API key and provider configuration.
```bash
helix wizard                        # Configure provider and API key
```

### config
View current configuration.
```bash
helix config show
```

### Global options
```bash
helix --version
helix --help
```
