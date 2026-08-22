"""Backend package initialization."""

from .environment import load_project_environment


# Python initializes this package before importing backend.app.main or services.
load_project_environment()
