"""Loopback-only UI preview. No lifecycle, DB, credentials, or trading routes.

Run from v2.3/server: venv/bin/python -m scripts.preview_blueprint --port 8765
"""
from __future__ import annotations

import argparse

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

from app.api.architecture_blueprint import blueprint_asset, blueprint_page


def create_preview() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_api_route("/dashboard/blueprint", blueprint_page, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/dashboard/blueprint/assets/{asset_name}", blueprint_asset, methods=["GET"])

    @app.get("/dashboard", response_class=HTMLResponse)
    @app.get("/dashboard/review", response_class=HTMLResponse)
    def preview_home():
        return '<html lang="zh-CN"><meta charset="utf-8"><p>这是隔离的页面预览，不连接生产数据库或券商。</p><a href="/dashboard/blueprint">打开三盘新架构</a></html>'

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(create_preview(), host="127.0.0.1", port=args.port, log_level="warning")
