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

Copy the example and edit `.env`:

```bash
cp .env.example .env
```

Set one of the following in `.env`:

**Ollama (free, local):**
```
HELIX_PROVIDER=ollama
```
Then run: `python scripts/setup_ollama.py`

**OpenAI:**
```
HELIX_PROVIDER=openai
OPENAI_API_KEY=your-key-here
```

**Claude (Anthropic):**
```
HELIX_PROVIDER=claude
ANTHROPIC_API_KEY=your-key-here
```

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

### config
View current configuration.
```bash
helix config show
```

### Global options
```bash
helix --version
helix --verbose
helix --help
```
