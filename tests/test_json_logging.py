"""Test: Bugbot comment — 'Root handlers get silently overwritten'.

The concern: logging.root.handlers = [_handler] in main.py removes any
pre-existing handlers. We test whether a handler added BEFORE importing
orchestrator.main survives the import.

Since main.py is the process entry point (not a library), module-level
logging setup intentionally owns root handlers. This test verifies the
actual impact.
"""

import logging


def test_preexisting_root_handler_survives_main_import():
    """Bugbot: root handlers get silently overwritten by main.py import."""
    # Add a pre-existing handler before main.py import
    sentinel = logging.StreamHandler()
    sentinel.set_name("sentinel")
    logging.root.addHandler(sentinel)

    try:
        # main.py is already imported (module-level code already ran),
        # so we simulate re-running the setup logic
        import importlib

        import orchestrator.main as main_mod

        importlib.reload(main_mod)

        handler_names = [h.name for h in logging.root.handlers]
        assert "sentinel" in handler_names, f"Pre-existing handler was removed. Current handlers: {handler_names}"
    finally:
        # Cleanup: remove sentinel regardless of outcome
        logging.root.handlers = [h for h in logging.root.handlers if h.name != "sentinel"]


def test_main_import_reload_does_not_stack_handlers():
    """Reloading orchestrator.main should not add duplicate root handlers."""
    import importlib

    import orchestrator.main as main_mod

    original_handlers = list(logging.root.handlers)
    try:
        logging.root.handlers = []

        # First reload initializes module logging config for this isolated root state.
        importlib.reload(main_mod)
        first_count = len(logging.root.handlers)

        # Second reload should keep handler count stable.
        importlib.reload(main_mod)
        second_count = len(logging.root.handlers)

        assert first_count == 1, f"Expected one root handler after initial reload, got {first_count}"
        assert second_count == first_count, (
            f"Reloading orchestrator.main stacked root handlers (first={first_count}, second={second_count})"
        )
    finally:
        logging.root.handlers = original_handlers
