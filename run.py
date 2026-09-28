#!/usr/bin/env python
"""Entry point for running the FastAPI application.

本地开发入口；生产用 scripts/start.sh（多 worker、无 reload）。
监听地址/端口/reload 都可以用环境变量覆盖，不必再改代码：

    APP_PORT=9000 python run.py
    APP_RELOAD=false python run.py
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8011")),
        reload=os.getenv("APP_RELOAD", "true").lower() in ("1", "true", "yes"),
    )
