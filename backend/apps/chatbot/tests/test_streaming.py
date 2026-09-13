"""
Regression tests for the WebSocket streaming fixes.

Covers three bugs found by driving the real backend with a WebSocket test
client (see PR discussion / commit history for the live traces that
uncovered them):

1. Tool output leaking into the token stream.
   ``ChatAgentOrchestrator.astream()`` iterates LangGraph's
   ``stream_mode="messages"`` output and used to yield *any* chunk's
   ``.content`` unconditionally. When the agent calls a tool, the same
   stream also emits the ``ToolMessage`` produced by the tool node — its
   ``.content`` is the tool's raw return value, not part of the
   assistant's answer. Live proof: asking the agent to use the
   ``calculator`` tool streamed ``"2541594"`` (the tool's raw output) as
   the very first "token", glued onto the front of the real answer.
   Fixed by only yielding content from ``AIMessageChunk`` instances.

2. ``SummarizationMiddleware`` blocking the event loop.
   The middleware only defined a sync ``before_model()``, which calls
   ``SummarizationService.compress_messages()`` -> ``ChatOpenAI.invoke()``
   (a blocking network call). LangGraph's ``RunnableCallable.ainvoke()``
   has no thread-offload fallback for middleware nodes — with no async
   hook defined, it calls the sync function directly on the running event
   loop. Live proof: once a session's history passed the summarization
   threshold, a concurrent plain HTTP prober measured 1.4-1.7s latency
   spikes on *every* request while the summarization call ran — the
   whole ASGI process froze, not just the one session. Fixed by adding
   ``SummarizationMiddleware.abefore_model`` (and
   ``SummarizationService.agenerate_summary`` / ``acompress_messages``,
   both using ``model.ainvoke()``) so the async graph path never blocks.

3. Rebuilding the LangGraph agent on every single message.
   ``ChatConsumer`` used to call ``AgentService.astream()`` per message,
   which reconstructs the whole ``create_agent`` graph (tool-loading DB
   query, system prompt build, graph compile) on every turn. Fixed by
   building one ``ChatAgentOrchestrator`` in ``connect()`` and reusing it
   for every message on that connection — mirrors the reference
   ``ClientChatbotService`` pooling pattern.

Testing strategy (per TESTING.md rules):
    - NO external calls — ChatOpenAI / create_agent / the checkpointer
      pool are always mocked; no real OpenAI request is ever made.
    - ChatbotTestMixin factory helpers for user/session creation.
    - Async test methods (``async def test_...``) are supported natively
      by Django's TestCase since Django 4.1 — no pytest-asyncio needed,
      consistent with the rest of this suite (plain ``manage.py test``).

Run:
    cd backend
    python manage.py test chatbot.tests.test_streaming \
        --settings=config.settings.test -v 2

    # Fast re-run
    python manage.py test chatbot.tests.test_streaming \
        --settings=config.settings.test -v 2 --keepdb
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from chatbot.consumers.chat_consumer import ChatConsumer
from chatbot.routing import websocket_urlpatterns
from chatbot.services.agent_service import (
    ChatAgentOrchestrator,
    SummarizationMiddleware,
    _extract_text_delta,
)
from chatbot.services.summarization_service import SummarizationService
from chatbot.tests._mixins import ChatbotTestMixin


# ---------------------------------------------------------------------------
# _extract_text_delta — content-block normalisation
# ---------------------------------------------------------------------------


class TestExtractTextDelta(TestCase):
    """``_extract_text_delta`` must handle both str and content-block list
    shapes of ``AIMessageChunk.content`` without raising."""

    def test_string_passthrough(self):
        self.assertEqual(_extract_text_delta("hello"), "hello")

    def test_none_returns_empty_string(self):
        self.assertEqual(_extract_text_delta(None), "")

    def test_empty_list_returns_empty_string(self):
        self.assertEqual(_extract_text_delta([]), "")

    def test_list_of_text_blocks_joined(self):
        content = [{"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}]
        self.assertEqual(_extract_text_delta(content), "Hello")

    def test_list_with_non_text_blocks_ignored(self):
        content = [
            {"type": "text", "text": "kept"},
            {"type": "image_url", "image_url": "http://example.com/x.png"},
        ]
        self.assertEqual(_extract_text_delta(content), "kept")

    def test_list_of_plain_strings_joined(self):
        self.assertEqual(_extract_text_delta(["a", "b", "c"]), "abc")


# ---------------------------------------------------------------------------
# ChatAgentOrchestrator.astream() — tool-message leak fix
# ---------------------------------------------------------------------------


async def _fake_message_stream(events):
    """Async generator standing in for ``agent.astream(stream_mode="messages")``.

    ``events`` is a list of (chunk, metadata) tuples, matching what
    LangGraph actually yields for that stream mode.
    """
    for chunk, metadata in events:
        yield chunk, metadata


class TestAstreamFiltersNonAIMessages(ChatbotTestMixin, TestCase):
    """Regression tests for Bug 1: tool output leaking into the token stream."""

    def setUp(self):
        self.user = self.create_user()
        self.preference = self.create_preference(self.user)
        self.session = self.create_session(self.user)

    async def _build_orchestrator(self, mock_agent):
        """Construct a ChatAgentOrchestrator with all I/O mocked out.

        Runs the (synchronous, ORM-touching) constructor in a worker
        thread via database_sync_to_async — exactly how production code
        builds it from async contexts (see AgentService.astream and
        ChatConsumer.connect) — so this doesn't trip Django's
        SynchronousOnlyOperation guard.
        """
        with patch(
            "chatbot.services.agent_service.create_agent", return_value=mock_agent
        ), patch(
            "chatbot.services.agent_service.get_checkpointer", return_value=MagicMock()
        ), patch(
            "chatbot.services.agent_service.load_tools_for_user", return_value=[]
        ):
            return await database_sync_to_async(ChatAgentOrchestrator)(self.session)

    async def test_tool_message_content_is_not_yielded(self):
        """The exact scenario proven live: a calculator tool call must not
        leak its raw return value ahead of the assistant's real answer."""
        mock_agent = MagicMock()
        mock_agent.astream.return_value = _fake_message_stream(
            [
                # First LLM call: decides to call the tool (no content yet)
                (AIMessageChunk(content=""), {}),
                # Tool node executes and emits a ToolMessage — content is
                # the tool's raw return value, NOT assistant text.
                (
                    ToolMessage(content="2541594", tool_call_id="call_1"),
                    {"langgraph_node": "tools"},
                ),
                # Second LLM call: the actual natural-language answer
                (AIMessageChunk(content="The"), {}),
                (AIMessageChunk(content=" result"), {}),
                (AIMessageChunk(content=" is 2,541,594."), {}),
            ]
        )

        orch = await self._build_orchestrator(mock_agent)
        chunks = [c async for c in orch.astream("What is 8734 * 291?")]

        self.assertNotIn("2541594", chunks)
        self.assertEqual("".join(chunks), "The result is 2,541,594.")

    async def test_ai_message_chunks_are_yielded_in_order(self):
        """Plain (no-tool) streaming still yields every AI token, in order."""
        mock_agent = MagicMock()
        mock_agent.astream.return_value = _fake_message_stream(
            [
                (AIMessageChunk(content="Hel"), {}),
                (AIMessageChunk(content="lo"), {}),
                (AIMessageChunk(content="!"), {}),
            ]
        )

        orch = await self._build_orchestrator(mock_agent)
        chunks = [c async for c in orch.astream("Hi")]

        self.assertEqual(chunks, ["Hel", "lo", "!"])

    async def test_content_block_list_chunks_are_normalised_to_text(self):
        """AIMessageChunk.content as a v1 content-block list must not crash
        the accumulator (str + list would raise TypeError)."""
        mock_agent = MagicMock()
        mock_agent.astream.return_value = _fake_message_stream(
            [
                (AIMessageChunk(content=[{"type": "text", "text": "Block"}]), {}),
                (AIMessageChunk(content=" text"), {}),
            ]
        )

        orch = await self._build_orchestrator(mock_agent)
        chunks = [c async for c in orch.astream("Hi")]

        self.assertEqual("".join(chunks), "Block text")

    async def test_empty_content_chunks_are_skipped(self):
        """Tool-call-only AIMessageChunks (empty content) yield nothing."""
        mock_agent = MagicMock()
        mock_agent.astream.return_value = _fake_message_stream(
            [
                (AIMessageChunk(content=""), {}),
                (AIMessageChunk(content="42"), {}),
            ]
        )

        orch = await self._build_orchestrator(mock_agent)
        chunks = [c async for c in orch.astream("2 + 40?")]

        self.assertEqual(chunks, ["42"])


# ---------------------------------------------------------------------------
# SummarizationMiddleware — async (non-blocking) path
# ---------------------------------------------------------------------------


class TestSummarizationMiddlewareAsyncPath(ChatbotTestMixin, TestCase):
    """Regression tests for Bug 2: SummarizationMiddleware blocking the
    event loop when invoked from the async (WebSocket) graph path."""

    def setUp(self):
        self.user = self.create_user()
        self.preference = self.create_preference(self.user)
        self.session = self.create_session(self.user)

    def test_abefore_model_is_overridden(self):
        """LangGraph's create_agent only wires the async node — and thus
        avoids the blocking sync fallback — when abefore_model is actually
        overridden. This assertion is the direct regression guard for the
        root cause: langchain.agents.factory checks exactly this
        (`m.__class__.abefore_model is not AgentMiddleware.abefore_model`)
        to decide whether an async-safe hook exists.
        """
        self.assertIsNot(
            SummarizationMiddleware.abefore_model, AgentMiddleware.abefore_model
        )

    @patch("chatbot.services.agent_service.SummarizationService")
    async def test_abefore_model_skips_when_disabled(self, MockSummary):
        MockSummary.get_session_config.return_value = {"enabled": False}
        mw = SummarizationMiddleware(self.session)

        state = {"messages": [HumanMessage(content="Hi")]}
        result = await mw.abefore_model(state, runtime=None)

        self.assertEqual(result, state)
        MockSummary.should_summarize.assert_not_called()

    @patch("chatbot.services.agent_service.SummarizationService")
    async def test_abefore_model_skips_below_threshold(self, MockSummary):
        MockSummary.get_session_config.return_value = {
            "enabled": True,
            "threshold": 384,
        }
        MockSummary.should_summarize.return_value = False
        mw = SummarizationMiddleware(self.session)

        state = {"messages": [HumanMessage(content="Hi")]}
        result = await mw.abefore_model(state, runtime=None)

        self.assertEqual(result, state)
        MockSummary.acompress_messages.assert_not_called()

    @patch("chatbot.services.agent_service.SummarizationService")
    async def test_abefore_model_uses_async_compression_never_sync(self, MockSummary):
        """The core fix: above threshold, the async hook must call
        acompress_messages (awaited, non-blocking) and must NEVER call the
        blocking sync compress_messages."""
        MockSummary.get_session_config.return_value = {
            "enabled": True,
            "threshold": 10,
            "keep_recent": 2,
            "model_name": "gpt-4o-mini",
            "max_summary_tokens": 128,
            "style": "concise",
        }
        MockSummary.should_summarize.return_value = True
        MockSummary.acompress_messages = AsyncMock(
            return_value=[
                SystemMessage(content="[Conversation Summary]\nTest summary"),
                AIMessage(content="Recent message"),
            ]
        )

        mw = SummarizationMiddleware(self.session)
        state = {"messages": [HumanMessage(content="Long message")] * 20}
        result = await mw.abefore_model(state, runtime=None)

        MockSummary.acompress_messages.assert_awaited_once()
        MockSummary.compress_messages.assert_not_called()
        self.assertEqual(len(result["messages"]), 2)


class TestSummarizationServiceAsyncMethods(TestCase):
    """Direct tests of SummarizationService.agenerate_summary /
    acompress_messages — the real compression logic, with only the LLM
    client mocked (no real OpenAI call)."""

    async def test_agenerate_summary_empty_messages_no_model_call(self):
        with patch(
            "chatbot.services.summarization_service.ChatOpenAI"
        ) as MockLLM:
            result = await SummarizationService.agenerate_summary([])

        self.assertEqual(result, "")
        MockLLM.assert_not_called()

    async def test_agenerate_summary_uses_ainvoke_only(self):
        with patch(
            "chatbot.services.summarization_service.ChatOpenAI"
        ) as MockLLM:
            mock_model = MagicMock()
            mock_model.ainvoke = AsyncMock(
                return_value=AIMessage(content="A concise summary.")
            )
            MockLLM.return_value = mock_model

            result = await SummarizationService.agenerate_summary(
                [HumanMessage(content="Hi"), AIMessage(content="Hello!")]
            )

        self.assertEqual(result, "A concise summary.")
        mock_model.ainvoke.assert_awaited_once()
        mock_model.invoke.assert_not_called()

    async def test_acompress_messages_short_history_unchanged_no_model_call(self):
        messages = [HumanMessage(content="Hi"), AIMessage(content="Hello!")]

        with patch(
            "chatbot.services.summarization_service.ChatOpenAI"
        ) as MockLLM:
            result = await SummarizationService.acompress_messages(
                messages, keep_recent=10
            )

        self.assertEqual(result, messages)
        MockLLM.assert_not_called()

    async def test_acompress_messages_compresses_and_preserves_recent(self):
        system_msg = SystemMessage(content="You are helpful.")
        messages = [system_msg] + [
            HumanMessage(content=f"msg{i}") if i % 2 == 0 else AIMessage(content=f"reply{i}")
            for i in range(6)
        ]
        recent_expected = messages[-2:]

        with patch.object(
            SummarizationService, "agenerate_summary", new=AsyncMock(return_value="Summary text")
        ) as mock_summary:
            result = await SummarizationService.acompress_messages(
                messages, keep_recent=2
            )

        mock_summary.assert_awaited_once()
        # [original system prompt, summary message, *recent messages]
        self.assertEqual(len(result), 4)
        self.assertIs(result[0], system_msg)
        self.assertTrue(result[1].additional_kwargs.get("is_summary"))
        self.assertIn("Summary text", result[1].content)
        self.assertEqual(result[2:], recent_expected)


# ---------------------------------------------------------------------------
# ChatConsumer — orchestrator reuse (Bug 3) + streaming guard
# ---------------------------------------------------------------------------


class _FakeOrchestrator:
    """Stands in for ChatAgentOrchestrator — records how many times it
    would have been constructed, and streams a fixed token list."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.call_count = 0

    async def astream(self, message):
        self.call_count += 1
        for tok in self.tokens:
            yield tok


class TestChatConsumerOrchestratorReuse(ChatbotTestMixin, TestCase):
    """Regression tests for Bug 3: rebuilding the agent on every message."""

    async def _drain_turn(self, communicator):
        """Read frames for one full stream_start..done cycle."""
        frames = []
        while True:
            frame = await communicator.receive_json_from()
            frames.append(frame)
            if frame["type"] in ("done", "error"):
                break
        return frames

    async def test_orchestrator_built_once_and_reused_across_messages(self):
        user = await database_sync_to_async(self.create_user)()
        session = await database_sync_to_async(self.create_session)(user)
        token = await database_sync_to_async(self.create_ws_access_token)(user)

        fake_orch = _FakeOrchestrator(["Hel", "lo"])

        with patch(
            "chatbot.services.agent_service.get_async_checkpointer",
            new=AsyncMock(return_value=MagicMock()),
        ), patch(
            "chatbot.services.agent_service.AgentService.create_orchestrator",
            return_value=fake_orch,
        ) as mock_create:
            application = URLRouter(websocket_urlpatterns)
            communicator = WebsocketCommunicator(
                application, f"/ws/chat/{session.id}/?token={token}"
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            await communicator.send_json_to({"message": "Hi"})
            frames_1 = await self._drain_turn(communicator)

            await communicator.send_json_to({"message": "Again"})
            frames_2 = await self._drain_turn(communicator)

            await communicator.disconnect()

        # Built exactly ONCE for the whole connection, even though two
        # messages were sent — proves the per-message rebuild bug is fixed.
        mock_create.assert_called_once()
        self.assertEqual(fake_orch.call_count, 2)

        types_1 = [f["type"] for f in frames_1]
        self.assertEqual(types_1, ["stream_start", "token", "token", "message", "done"])
        types_2 = [f["type"] for f in frames_2]
        self.assertEqual(types_2, ["stream_start", "token", "token", "message", "done"])

    async def test_connect_closes_gracefully_if_orchestrator_build_fails(self):
        user = await database_sync_to_async(self.create_user)()
        session = await database_sync_to_async(self.create_session)(user)
        token = await database_sync_to_async(self.create_ws_access_token)(user)

        with patch(
            "chatbot.services.agent_service.get_async_checkpointer",
            new=AsyncMock(return_value=MagicMock()),
        ), patch(
            "chatbot.services.agent_service.AgentService.create_orchestrator",
            side_effect=RuntimeError("boom"),
        ):
            application = URLRouter(websocket_urlpatterns)
            communicator = WebsocketCommunicator(
                application, f"/ws/chat/{session.id}/?token={token}"
            )
            connected, _ = await communicator.connect()
            # accept() happens before the orchestrator is built, so the
            # handshake itself still succeeds...
            self.assertTrue(connected)

            error_frame = await communicator.receive_json_from()
            self.assertEqual(error_frame["type"], "error")

            # ...but the consumer then closes the socket with its
            # dedicated failure code instead of leaving the client hanging.
            closed = await communicator.receive_output()
            self.assertEqual(closed["type"], "websocket.close")
            self.assertEqual(closed.get("code"), 4005)

            await communicator.disconnect()


class TestChatConsumerStreamingGuard(ChatbotTestMixin, TestCase):
    """
    Direct, low-level test of ChatConsumer's overlapping-stream guard.

    Channels' own per-connection dispatch loop already serialises
    receive_json() calls — the next WS frame isn't even read until the
    current one's handler returns — so two real frames sent back-to-back
    over a single WebsocketCommunicator can never actually exercise the
    "reject while streaming" branch: by the time the second is dispatched,
    the first has already finished and the guard flag is back to False.

    To test the guard's own mutual-exclusion logic as a defensive
    invariant (one that must hold even if that serialisation guarantee
    ever changes, e.g. if receive_json were ever refactored to spawn a
    background task), this invokes receive_json() twice concurrently via
    asyncio.create_task on a directly-constructed consumer instance,
    bypassing the ASGI dispatch loop entirely.
    """

    async def test_second_concurrent_message_rejected_while_streaming(self):
        user = await database_sync_to_async(self.create_user)()
        session = await database_sync_to_async(self.create_session)(user)

        consumer = ChatConsumer()
        consumer.user = user
        consumer.session = session
        consumer.session_id = str(session.id)

        sent = []

        async def fake_send_json(data):
            sent.append(data)

        consumer.send_json = fake_send_json

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_astream(message):
            started.set()
            await release.wait()
            yield "rest"

        consumer.orchestrator = SimpleNamespace(astream=slow_astream)

        first = asyncio.create_task(consumer.receive_json({"message": "one"}))
        await started.wait()  # first call is now mid-stream, guard flag is True

        await consumer.receive_json({"message": "two"})

        rejected = [
            d
            for d in sent
            if d.get("type") == "error" and d.get("error_code") == "STREAM_BUSY"
        ]
        self.assertEqual(len(rejected), 1, sent)

        release.set()
        await first

        # Guard is released once the first stream finishes.
        self.assertFalse(consumer._streaming_active)
