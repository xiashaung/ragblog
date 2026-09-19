#!/usr/bin/env python
"""Entry point for running the FastAPI application."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        reload=True,
        host="0.0.0.0",
        port=8011,
    )
