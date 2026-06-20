# -*- coding: utf-8 -*-
"""路由层包。"""
from src.api.routers import system as system  # noqa: F401
from src.api.routers import tasks as tasks  # noqa: F401

__all__ = ["system", "tasks"]
