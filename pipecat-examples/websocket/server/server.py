
#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Dict

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware

# Load environment variables
load_dotenv(override=True)

from bot_fast_api import run_bot
from bot_websocket_server import run_bot_websocket_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles FastAPI startup and shutdown."""
    print("Starting FastAPI Pipecat server...")
    yield
    print("Shutting down server...")


# Initialize FastAPI app
app = FastAPI(lifespan=lifespan)

# Enable CORS for frontend client
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# WebSocket Endpoint
# -----------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connection accepted")

    try:
        await run_bot(websocket)

    except Exception as e:
        print(f"Exception in run_bot: {e}")


# -----------------------------
# Client Connect Endpoint
# -----------------------------
@app.post("/connect")
async def bot_connect(request: Request) -> Dict[Any, Any]:

    server_mode = os.getenv("WEBSOCKET_SERVER", "fast_api")

    if server_mode == "websocket_server":
        ws_url = "ws://localhost:8765"

    else:
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or "localhost:7860"
        )

        proto = (
            request.headers.get("x-forwarded-proto")
            or request.url.scheme
            or "http"
        )

        ws_proto = "wss" if proto == "https" else "ws"

        ws_url = f"{ws_proto}://{host}/ws"

    return {"ws_url": ws_url}


# -----------------------------
# Run Server
# -----------------------------
async def main():

    server_mode = os.getenv("WEBSOCKET_SERVER", "fast_api")

    tasks = []

    try:

        if server_mode == "websocket_server":
            tasks.append(run_bot_websocket_server())

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=7860,
            log_level="info",
        )

        server = uvicorn.Server(config)

        tasks.append(server.serve())

        await asyncio.gather(*tasks)

    except asyncio.CancelledError:
        print("Server shutdown detected.")


if __name__ == "__main__":
    asyncio.run(main())