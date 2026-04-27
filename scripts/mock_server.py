"""QMT Pipeline 临时 Mock Server（用于联测占位）。

API 合约严格按《API接口文档（纯股）》实现 stub。等搭档真实策略上线后，
保持同样的接口签名替换业务逻辑即可，本端 settings.yaml 不需要改。

跑法（云服务器，绑 0.0.0.0 才会暴露公网）:

    pip install -r scripts/requirements-mock.txt
    uvicorn scripts.mock_server:app --host 0.0.0.0 --port 8000

后台跑:

    nohup uvicorn scripts.mock_server:app --host 0.0.0.0 --port 8000 \\
        > mock_server.log 2>&1 &

详细部署见 docs/manual_tests/aliyun_mock_deploy.md。
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("mock_server")

app = FastAPI(title="QMT Pipeline Mock Server")

# 跟搭档约定的测试 API Key；上线时替换或改读环境变量
EXPECTED_API_KEY = "TEST_KEY_123"

# 内存状态：记录收到的请求，方便 _health / 排障
_state = {
    "market_data": [],
    "trade_results": [],
}


def _check_auth(authorization: str | None) -> None:
    if authorization != f"Bearer {EXPECTED_API_KEY}":
        logger.warning("auth failed: %s", authorization)
        raise HTTPException(
            status_code=401,
            detail={"code": 1001, "message": "auth failed"},
        )


@app.post("/market-data")
async def push_market(body: dict, authorization: str = Header(None)):
    _check_auth(authorization)
    n = len(body.get("stocks", []))
    logger.info(
        "POST /market-data trade_date=%s stocks=%d",
        body.get("trade_date"), n,
    )
    _state["market_data"].append(body)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "trade_date": body["trade_date"],
            "received_count": n,
            "strategy_triggered": True,
        },
    }


@app.get("/signals")
async def get_signals(date: str, authorization: str = Header(None)):
    _check_auth(authorization)
    logger.info("GET /signals date=%s", date)
    # Stub 信号：买茅台 100 股，方便整条链路通起来。
    # 真实策略上线后这里换成读信号队列。
    sigs = [
        {
            "signal_id": f"mock-{date}-001",
            "symbol": "600519.SH",
            "direction": "BUY",
            "quantity": 100,
            "order_type": "LIMIT",
            "limit_price": 1500.0,
            "price_offset": 0.005,
            "strategy_id": "mock_strategy",
            "signal_time": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "valid_date": date,
        }
    ]
    return {
        "code": 0,
        "message": "ok",
        "data": {"date": date, "signals": sigs},
    }


@app.post("/trade-result")
async def trade_result(body: dict, authorization: str = Header(None)):
    _check_auth(authorization)
    n = len(body.get("results", []))
    logger.info(
        "POST /trade-result trade_date=%s stage=%s results=%d",
        body.get("trade_date"), body.get("stage"), n,
    )
    _state["trade_results"].append(body)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "trade_date": body["trade_date"],
            "stage": body["stage"],
            "matched_count": n,
            "unmatched_signal_ids": [],
        },
    }


@app.get("/_health")
async def health():
    """非 API 文档定义的接口，仅用于联测时检查 mock 是否还活着。"""
    return {
        "ok": True,
        "stored": {
            "market_data": len(_state["market_data"]),
            "trade_results": len(_state["trade_results"]),
        },
    }
