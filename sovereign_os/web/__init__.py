"""
Web UI: FastAPI dashboard for Sovereign-OS (alternative to TUI).
"""

from sovereign_os.web.app import create_app, run_web_ui

__all__ = ["create_app", "run_web_ui"]
