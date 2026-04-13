"""
Hermes：Traffic-Lab 战略大脑入口。
逻辑按架构手册拆分为 brain / runner / scoring / negative_pool / memory / verify / metrics / client。
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from routes import register_routes

logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, (os.environ.get("LOG_LEVEL") or "INFO").upper(), logging.INFO))

app = FastAPI(title="Hermes", version="0.1.0", description="Traffic-Lab strategic orchestrator (TAVC)")
register_routes(app)
