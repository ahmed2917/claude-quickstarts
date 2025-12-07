import asyncio
import base64
import json
import logging
import os
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import aiosqlite
import httpx
from anthropic import Anthropic, APIError, APIResponseValidationError, APIStatusError
from anthropic.types.beta import (
    BetaContentBlockParam,
    BetaMessageParam,
    BetaTextBlockParam,
    BetaToolUseBlockParam,
)
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uuid

from computer_use_demo.loop import (
    APIProvider,
    sampling_loop,
    ToolResult,
)
from computer_use_demo.tools import ToolCollection

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = "chat_history.db"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                session_id TEXT,
                message_json TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()
    yield

app = FastAPI(lifespan=lifespan)

# Mount static files
app.mount("/static", StaticFiles(directory="computer_use_demo/static"), name="static")

@app.get("/")
async def root():
    return FileResponse("computer_use_demo/static/index.html")

@app.post("/sessions")
async def create_session():
    session_id = str(uuid.uuid4())
    return {"session_id": session_id}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@dataclass
class Session:
    socket: WebSocket
    task: asyncio.Task | None = None
    messages: list[BetaMessageParam] = field(default_factory=list)
    tools: dict[str, ToolResult] = field(default_factory=dict)
    
class SessionManager:
    def __init__(self):
        self.active_sessions: dict[str, Session] = {}

    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_sessions[session_id] = Session(socket=websocket)

    def disconnect(self, session_id: str):
        if session_id in self.active_sessions:
            session = self.active_sessions[session_id]
            if session.task:
                session.task.cancel()
            del self.active_sessions[session_id]

    async def get_session(self, session_id: str) -> Session | None:
        return self.active_sessions.get(session_id)

session_manager = SessionManager()

async def save_message_to_db(session_id: str, message: Any):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO chat_history (session_id, message_json) VALUES (?, ?)",
                (session_id, json.dumps(message)),
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Failed to save message to DB: {e}")

async def loop_wrapper(session_id: str, user_message: str):
    session = await session_manager.get_session(session_id)
    if not session:
        logger.error(f"Session {session_id} not found in loop_wrapper")
        return

    websocket = session.socket
    messages = session.messages
    
    # Add user message
    messages.append(
        {
            "role": "user",
            "content": [BetaTextBlockParam(type="text", text=user_message)],
        }
    )
    await save_message_to_db(session_id, messages[-1])


    def output_callback(content_block: BetaContentBlockParam):
        try:
             # loop.py calls this synchronously. We must schedule the async send.
             asyncio.create_task(websocket.send_json({
                "type": "output",
                "content": content_block
            }))
        except Exception:
            logger.error("Failed to send output_callback", exc_info=True)


    def tool_output_callback(result: ToolResult, tool_id: str):
         try:
            # We construct a serializable version of ToolResult
            data = {
                "output": result.output,
                "error": result.error,
                "base64_image": result.base64_image,
                "system": result.system,
                "tool_id": tool_id
            }
            asyncio.create_task(websocket.send_json({
                "type": "tool_output",
                "content": data
            }))
         except Exception:
            logger.error("Failed to send tool_output_callback", exc_info=True)

    def api_response_callback(request: httpx.Request, response: httpx.Response | object | None, error: Exception | None):
        if error:
            error_msg = f"API Error: {error}"
            if isinstance(response, httpx.Response):
                try:
                    error_msg += f"\nResponse: {response.text}"
                except Exception:
                    pass
            logger.error(error_msg)
            asyncio.create_task(websocket.send_json({
                "type": "error",
                "content": error_msg
            }))
        else:
             # Log successful responses if needed
             pass

    try:
        # Default configuration matching Streamlit defaults
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        # In a real app, you might want to pass this from the client or validate it
        
        if not api_key:
             await websocket.send_json({"type": "error", "content": "API Key not found in environment."})
             return

        # Ensure we are in a valid container environment or provide feedback
        # The sampling loop expects certain system properties (check loop.py functionality)
        
        # Run the loop
        new_messages = await sampling_loop(
            model="claude-3-5-sonnet-20241022", # Defaulting to Sonnet 3.5
            provider=APIProvider.ANTHROPIC,
            system_prompt_suffix="",
            messages=messages,
            output_callback=output_callback,
            tool_output_callback=tool_output_callback,
            api_response_callback=api_response_callback,
            api_key=api_key,
            tool_version="computer_use_20250124", 
            # We might want to make these configurable via WS init message
        )
        
        session.messages = new_messages
        # Save updated history (simplified, typically you'd append)
        # But sampling_loop returns the full list with new messages appended.
        # We can just save the new ones, but for simplicity let's rely on the per-turn saves above?
        # Actually sampling_loop appends to 'messages' in-place AND returns it.
        # We already saved the User message. The Assistant messages are streamed via output_callback.
        # However, the final list structure (with proper turn history) is important.
        # Let's verify if we need to save the final state to DB.
        # Ideally we save each assistant message as it completes or the whole block.
        # The Streamlit app didn't explicitly save history to disk, only in memory.
        # We are required to save message history to DB.
        
        # Save just the new assistant messages/tool results effectively
        # Since we are modifying session.messages in place, we can dump the last few?
        # Or just dump the whole session for now? 
        # A simple approach for "Chat history" table:
        # Just store the final full conversation blob for the session if we want "snapshot"
        # Or append individual messages. 
        # Given the requirements: "When the loop finishes or updates, save the message history to the DB."
        # saving the full JSON list is safest for recovery.
        
        async with aiosqlite.connect(DB_PATH) as db:
             await db.execute(
                "INSERT INTO chat_history (session_id, message_json) VALUES (?, ?)",
                (session_id, json.dumps(session.messages)),
            )
             await db.commit()

    except Exception as e:
        logger.error(f"Error in sampling_loop: {e}", exc_info=True)
        await websocket.send_json({"type": "error", "content": str(e)})


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await session_manager.connect(session_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message_data = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = message_data.get("type")
            
            if msg_type == "message":
                user_content = message_data.get("content", "") # Expecting string for simplicity
                # Cancel previous task if running? 
                # The user might interrupt. 
                # For now, let's just spawn a new task. 
                # Ideally we should prevent overlapping runs or queue them.
                # Computer Use loop usually takes control until it needs input.
                
                # Check active task
                session = await session_manager.get_session(session_id)
                if session and session.task and not session.task.done():
                    # If we want to support interruption (user stopped), we cancel
                    # But if it's just a new message, strictly speaking we should await.
                    # The prompt says: "When the loop finishes or updates..."
                    pass 

                # We wrap the loop in a task
                task = asyncio.create_task(loop_wrapper(session_id, user_content))
                if session:
                    session.task = task
                    
    except WebSocketDisconnect:
        session_manager.disconnect(session_id)
