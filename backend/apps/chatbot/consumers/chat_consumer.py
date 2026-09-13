"""
Chat WebSocket Consumer — Async Streaming with LangGraph Agent

Handles real-time chat communication over WebSocket with **true async
streaming** of LLM tokens.  Builds one ``ChatAgentOrchestrator`` per
connection (in ``connect()``) and reuses it for every message, calling
LangGraph's ``astream(stream_mode="messages")`` to yield individual AI
token chunks as they arrive — no rebuilding the agent per message, no
synchronous agent calls blocking the event loop.

Protocol:
    CONNECT  → authenticate via JWT token in query string
    SEND     → {"message": "Hello!"}
    RECEIVE  → {"type": "stream_start"}               (generation started)
               {"type": "token", "content": "..."}     (streaming chunk)
               {"type": "message", "content": "..."}   (final full response)
               {"type": "error", "content": "..."}      (errors)
               {"type": "done"}                         (stream complete)

Usage (frontend):
    const ws = new WebSocket(
        `ws://localhost:8000/ws/chat/${sessionId}/?token=${accessToken}`
    );
    ws.send(JSON.stringify({message: "Hello!"}));

    ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type === "token")      appendToUI(data.content);
        if (data.type === "done")       markComplete();
        if (data.type === "error")      showError(data.content);
    };
"""

import json
import logging
import time

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model

from chatbot.models import ChatSession

User = get_user_model()
logger = logging.getLogger(__name__)


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket consumer for real-time chat with the LangGraph agent.

    Authentication:
        Pass JWT access token as ``?token=xxx`` query parameter.

    Messages (client → server):
        {"message": "Hello!"}

    Messages (server → client):
        {"type": "stream_start"}                    — generation started
        {"type": "token",    "content": "..."}      — streaming chunk
        {"type": "message",  "content": "..."}       — complete AI response
        {"type": "error",    "content": "..."}       — error description
        {"type": "done"}                             — stream finished
    """

    # Guards against overlapping streams on a single connection (see
    # receive_json). Set as a class attribute so it's always defined even
    # if a message somehow arrives before connect() finishes.
    _streaming_active = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self):
        """Authenticate and accept the WebSocket connection."""
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"chat_{self.session_id}"

        # Authenticate user from JWT token in query string
        self.user = await self._authenticate()
        if not self.user or not self.user.is_authenticated:
            logger.warning("WebSocket auth failed for session %s", self.session_id)
            await self.close(code=4001)
            return

        # Accept must come before any send_json calls
        await self.accept()

        # Verify the session belongs to this user
        self.session = await self._get_session()
        if not self.session:
            await self.send_json({"type": "error", "content": "Session not found"})
            await self.close(code=4004)
            return

        # Initialise the async checkpointer pool on first connection
        # (idempotent — subsequent calls return the existing singleton)
        from chatbot.services.agent_service import AgentService, get_async_checkpointer

        async_checkpointer = await get_async_checkpointer()

        # Build ONE orchestrator for the lifetime of this connection and
        # reuse it for every message (see _astream_agent). Building it
        # per-message would recompile the create_agent graph, re-run the
        # tool-loading DB query, and rebuild the system prompt on every
        # single turn — matching the reference service's pooling pattern
        # ("eliminates the cold-start latency that affected every message").
        try:
            self.orchestrator = await database_sync_to_async(
                AgentService.create_orchestrator
            )(session=self.session, async_checkpointer=async_checkpointer)
        except Exception:
            logger.exception(
                "Failed to build agent orchestrator for session %s", self.session_id
            )
            await self.send_json(
                {"type": "error", "content": "Failed to initialise the agent"}
            )
            await self.close(code=4005)
            return

        # Join the channel group
        await self.channel_layer.group_add(self.group_name, self.channel_name)

        self._streaming_active = False

        logger.info(
            "WebSocket connected: user=%s, session=%s",
            self.user.email,
            self.session_id,
        )

    async def disconnect(self, close_code):
        """Leave the channel group on disconnect."""
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        logger.info(
            "WebSocket disconnected: user=%s, code=%s",
            getattr(self.user, "email", "anonymous"),
            close_code,
        )

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def receive_json(self, content, **kwargs):
        """
        Handle incoming JSON messages from the client.

        Expected format: {"message": "Hello!"}
        """
        message = content.get("message", "").strip()
        if not message:
            await self.send_json({"type": "error", "content": "Empty message"})
            return

        # Guard against overlapping streams on this connection. Channels
        # already serialises receive_json() calls per-connection (the next
        # WS frame isn't read until this call returns), but a client can
        # still queue several messages before the first finishes — reject
        # the extras with a clear message instead of silently running them
        # back-to-back against the same LangGraph thread_id.
        if self._streaming_active:
            await self.send_json(
                {
                    "type": "error",
                    "content": "Please wait for the current response to finish before sending another message.",
                    # Lets the client tell "your other message is still fine,
                    # this one was just dropped" apart from a fatal stream
                    # error — it must NOT clear the in-progress response.
                    "error_code": "STREAM_BUSY",
                }
            )
            return
        self._streaming_active = True

        logger.info(
            "WS message received: user=%s, session=%s, len=%d",
            self.user.email,
            self.session_id,
            len(message),
        )

        try:
            # Signal that streaming is starting
            await self.send_json(
                {
                    "type": "stream_start",
                    "timestamp": time.time(),
                }
            )

            # Stream tokens from the agent directly (no thread blocking)
            accumulated_response = ""
            async for chunk in self._astream_agent(message):
                accumulated_response += chunk
                await self.send_json({"type": "token", "content": chunk})

            # Send the final complete response
            await self.send_json(
                {
                    "type": "message",
                    "content": accumulated_response,
                }
            )

            # Signal completion
            await self.send_json({"type": "done"})

        except Exception as e:
            logger.exception("Agent error in WebSocket: %s", e)
            await self.send_json(
                {
                    "type": "error",
                    "content": f"Agent error: {e}",
                }
            )
        finally:
            self._streaming_active = False

    # ------------------------------------------------------------------
    # Channel group handler (for future broadcast support)
    # ------------------------------------------------------------------

    async def chat_message(self, event):
        """Handle messages from the channel group (broadcast)."""
        await self.send_json(event["data"])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _astream_agent(self, message: str):
        """
        Async generator that yields content chunks from the agent.

        Streams from ``self.orchestrator`` — built once in ``connect()`` and
        reused for every message on this connection — via LangGraph's
        ``astream(stream_mode="messages")`` for real-time token streaming.
        """
        async for chunk in self.orchestrator.astream(message):
            yield chunk

    async def _authenticate(self):
        """
        Authenticate the WebSocket connection via JWT token.

        Checks for ``?token=xxx`` in the query string.
        """
        from rest_framework_simplejwt.tokens import AccessToken
        from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

        query_string = self.scope.get("query_string", b"").decode()
        params = dict(
            pair.split("=", 1) for pair in query_string.split("&") if "=" in pair
        )

        token = params.get("token")
        if not token:
            return None

        try:
            access_token = AccessToken(token)
            user_id = access_token["user_id"]
            return await database_sync_to_async(User.objects.get)(id=user_id)
        except (InvalidToken, TokenError, User.DoesNotExist) as e:
            logger.warning("JWT auth failed: %s", e)
            return None

    async def _get_session(self):
        """Get the chat session, verifying ownership."""
        try:
            session = await database_sync_to_async(ChatSession.objects.get)(
                id=self.session_id, user=self.user
            )
            return session
        except ChatSession.DoesNotExist:
            return None
