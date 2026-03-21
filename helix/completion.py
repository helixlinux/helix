# PYTHON_ARGCOMPLETE_OK
"""Shell tab-completion helpers for Helix CLI."""

STACK_NAMES = ["ml", "ml-cpu", "webdev", "devops", "data"]


def stack_name_completer(prefix, parsed_args, **kwargs):
    """Complete stack names for `helix stack <TAB>`."""
    return [s for s in STACK_NAMES if s.startswith(prefix)]


def no_complete(prefix, parsed_args, **kwargs):
    """Suppress completion for free-form arguments (software names, questions, IDs)."""
    return []
