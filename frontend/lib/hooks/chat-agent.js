// lib/hooks/chat-agent.js
// TanStack Query hooks for chat agent messaging + WebSocket streaming.
// Uses react-use-websocket for robust connection management with auto-reconnect.
//
// Streaming protocol (server → client):
//   stream_start → token (×N) → message → done
//   error frames may carry error_code: "STREAM_BUSY" — see handleIncomingMessage.
//
// Incoming frames are processed via react-use-websocket's `onMessage` option
// (fires synchronously per native WebSocket message) rather than a
// `lastJsonMessage` + useEffect pair. react-use-websocket wraps its internal
// `setLastMessage` in `flushSync`, so no message is ever silently dropped —
// but that also means every single token forces its own synchronous React
// commit before this hook's own throttle ever gets a say. Doing the
// token-accumulation and throttling directly in `onMessage`, outside of
// React's render cycle, lets the throttle actually cap re-renders at ~60fps
// under fast token rates (100+ tokens/sec observed against the real backend)
// instead of forcing one commit per token regardless.
//
// The hook uses throttled chunk updates (60fps max) to prevent React
// render storms when the LLM emits tokens faster than the browser can paint.

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, useCallback } from "react";
import useWebSocket, { ReadyState } from "react-use-websocket";
import { keys } from "@/lib/query-keys";
import { getWsToken, buildWsUrl } from "@/lib/ws";

async function proxyFetch(path, options = {}) {
  const res = await fetch(`/api/proxy/chatbot/${path.replace(/^\//, "")}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(data?.message || data?.detail || `API ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/** Load chat history for a session. */
export function useChatHistory(sessionId) {
  return useQuery({
    queryKey: keys.chatHistory(sessionId),
    queryFn: () => proxyFetch(`chat-agent/history/${sessionId}/`),
    enabled: !!sessionId && sessionId !== "new" && sessionId !== "undefined",
  });
}

/** Send a message via HTTP (fallback when WebSocket is unavailable). */
export function useSendMessage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ message, session_id, system_prompt }) =>
      proxyFetch("chat-agent/send/", {
        method: "POST",
        body: JSON.stringify({ message, session_id, system_prompt }),
      }),
    onSuccess: (_data, { session_id }) =>
      qc.invalidateQueries({ queryKey: keys.chatHistory(session_id) }),
  });
}

// Safety-net: if the server never sends "done"/"message"/"error" (e.g. a
// connection that dies without a clean WS close frame), force the UI out of
// the "streaming" state instead of leaving the spinner stuck forever.
const STREAM_WATCHDOG_MS = 30000;

// react-use-websocket only exposes the numeric ReadyState — map it to the
// human-readable labels this hook has always returned as `connectionStatus`.
const CONNECTION_STATUS_LABELS = {
  [ReadyState.UNINSTANTIATED]: "Uninstantiated",
  [ReadyState.CONNECTING]: "Connecting",
  [ReadyState.OPEN]: "Open",
  [ReadyState.CLOSING]: "Closing",
  [ReadyState.CLOSED]: "Closed",
};

/**
 * Custom hook that manages a WebSocket connection for real-time chat
 * using react-use-websocket for robust auto-reconnect and lifecycle management.
 *
 * Implements throttled streaming (60fps max) to prevent React render depth
 * errors when the LLM emits tokens faster than the browser can paint.
 *
 * @param {string} sessionId - Chat session UUID.
 * @returns {{ sendMessage, streamingContent, isStreaming, isThinking, error, connectionStatus }}
 */
export function useChatSocket(sessionId) {
  const qc = useQueryClient();
  const [token, setToken] = useState(null);
  const [streamingContent, setStreamingContent] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [isThinking, setIsThinking] = useState(false);
  const [error, setError] = useState(null);
  const pendingMessageRef = useRef(null);

  // Throttling refs — prevent React render storms from rapid token chunks.
  // The LLM can emit tokens faster than 60fps; we batch them to one update per frame.
  const chunkUpdateTimeoutRef = useRef(null);
  const pendingChunkContentRef = useRef("");
  const lastChunkUpdateRef = useRef(0);
  const MIN_CHUNK_DELAY = 16; // ~60fps

  const watchdogRef = useRef(null);

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current) {
      clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
  }, []);

  const armWatchdog = useCallback(() => {
    clearWatchdog();
    watchdogRef.current = setTimeout(() => {
      setIsStreaming(false);
      setIsThinking(false);
      setError("Response timed out. Please try again.");
    }, STREAM_WATCHDOG_MS);
  }, [clearWatchdog]);

  // Perform the actual state update with the latest accumulated content.
  const performChunkUpdate = useCallback(() => {
    lastChunkUpdateRef.current = Date.now();
    setStreamingContent(pendingChunkContentRef.current);
  }, []);

  // Throttled update — batches rapid chunks into a single state update per frame.
  const throttledChunkUpdate = useCallback(
    (newContent) => {
      if (chunkUpdateTimeoutRef.current) {
        clearTimeout(chunkUpdateTimeoutRef.current);
      }
      pendingChunkContentRef.current = newContent;

      const now = Date.now();
      const elapsed = now - lastChunkUpdateRef.current;

      if (elapsed >= MIN_CHUNK_DELAY) {
        performChunkUpdate();
      } else {
        chunkUpdateTimeoutRef.current = setTimeout(() => {
          performChunkUpdate();
        }, MIN_CHUNK_DELAY - elapsed);
      }
    },
    [performChunkUpdate],
  );

  // Fetch a short-lived WS token when the session changes.
  // Skip if sessionId is missing or "new" (not a real session yet).
  useEffect(() => {
    if (!sessionId || sessionId === "new" || sessionId === "undefined") return;
    let cancelled = false;
    getWsToken()
      .then((t) => {
        if (!cancelled) setToken(t);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Build the WS URL once we have a token.
  const wsUrl = token ? buildWsUrl(sessionId, token) : null;

  // Processes exactly one WS frame. Passed directly as react-use-websocket's
  // `onMessage` option, which fires per native WebSocket "message" event —
  // not gated behind a `lastJsonMessage` state update, so the throttle above
  // is the only thing deciding how often React actually re-renders.
  const handleIncomingMessage = useCallback(
    (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }

      switch (msg.type) {
        case "stream_start":
          // Generation has started — show thinking indicator, clear old content.
          armWatchdog();
          setIsThinking(true);
          setIsStreaming(true);
          setStreamingContent("");
          pendingChunkContentRef.current = "";
          lastChunkUpdateRef.current = 0;
          if (chunkUpdateTimeoutRef.current) {
            clearTimeout(chunkUpdateTimeoutRef.current);
            chunkUpdateTimeoutRef.current = null;
          }
          break;

        case "token":
          // Streaming chunk — accumulate with throttled updates.
          setIsThinking(false);
          throttledChunkUpdate(pendingChunkContentRef.current + msg.content);
          break;

        case "message":
          // Final complete response — flush any pending throttled content.
          clearWatchdog();
          if (chunkUpdateTimeoutRef.current) {
            clearTimeout(chunkUpdateTimeoutRef.current);
            chunkUpdateTimeoutRef.current = null;
          }
          setStreamingContent(msg.content || pendingChunkContentRef.current);
          // Invalidate history so the query refetches from the checkpointer.
          qc.invalidateQueries({ queryKey: keys.chatHistory(sessionId) });
          break;

        case "done":
          // Stream finished — reset all streaming state.
          clearWatchdog();
          setIsStreaming(false);
          setIsThinking(false);
          if (chunkUpdateTimeoutRef.current) {
            clearTimeout(chunkUpdateTimeoutRef.current);
            chunkUpdateTimeoutRef.current = null;
          }
          setStreamingContent("");
          pendingChunkContentRef.current = "";
          qc.invalidateQueries({ queryKey: keys.chatHistory(sessionId) });
          break;

        case "error":
          // STREAM_BUSY means THIS message was rejected because a previous
          // one is still streaming on the connection — that other stream is
          // still running fine and must not be wiped out from under it.
          if (msg.error_code === "STREAM_BUSY") {
            setError(msg.content || "Please wait for the current response to finish.");
            break;
          }
          clearWatchdog();
          setError(msg.content || "Server error");
          setIsStreaming(false);
          setIsThinking(false);
          setStreamingContent("");
          pendingChunkContentRef.current = "";
          if (chunkUpdateTimeoutRef.current) {
            clearTimeout(chunkUpdateTimeoutRef.current);
            chunkUpdateTimeoutRef.current = null;
          }
          break;

        default:
          break;
      }
    },
    [armWatchdog, clearWatchdog, throttledChunkUpdate, qc, sessionId],
  );

  // react-use-websocket manages connection lifecycle, auto-reconnect, etc.
  // NOTE: the package's WebSocketHook return type only has `readyState`
  // (numeric — see ReadyState) — there is no `connectionStatus` field on it.
  // Destructuring `connectionStatus` here would silently be `undefined`
  // forever, which used to make sendMessage's `readyState === ReadyState.OPEN`
  // gate (previously written as `connectionStatus === ReadyState.OPEN`)
  // always false: every message fell through to the "queue for onOpen"
  // branch, and since onOpen only fires once per connection, any message
  // sent after the very first one was silently dropped and never reached
  // the server. `connectionStatus` is now derived below instead.
  const { sendJsonMessage, readyState } = useWebSocket(
    wsUrl,
    {
      share: false,
      retryOnError: true,
      reconnectAttempts: 5,
      reconnectInterval: 2000,
      shouldReconnect: () => true,
      onOpen: () => {
        // Send any pending message that was queued before the socket opened.
        if (pendingMessageRef.current) {
          sendJsonMessage({ message: pendingMessageRef.current });
          pendingMessageRef.current = null;
        }
      },
      onMessage: handleIncomingMessage,
      onClose: () => {
        clearWatchdog();
        setIsStreaming(false);
        setIsThinking(false);
      },
      onError: () => {
        clearWatchdog();
        setError("WebSocket connection error");
        setIsStreaming(false);
        setIsThinking(false);
      },
    },
    !!wsUrl, // connect only when we have a URL
  );

  // Clean up any pending timers on unmount.
  useEffect(() => {
    return () => {
      if (chunkUpdateTimeoutRef.current) {
        clearTimeout(chunkUpdateTimeoutRef.current);
      }
      clearWatchdog();
    };
  }, [clearWatchdog]);

  // Send a message — queues if the socket isn't open yet.
  const sendMessage = useCallback(
    (message) => {
      setError(null);
      setStreamingContent("");
      pendingChunkContentRef.current = "";
      setIsStreaming(true);
      setIsThinking(true);
      // Arm the watchdog here too — covers the case where the server never
      // responds at all (not even stream_start), not just a mid-stream stall.
      armWatchdog();
      if (readyState === ReadyState.OPEN) {
        sendJsonMessage({ message });
      } else {
        // Queue the message — it'll be sent in onOpen.
        pendingMessageRef.current = message;
      }
    },
    [readyState, sendJsonMessage, armWatchdog],
  );

  // Human-readable connection status, derived from the numeric readyState —
  // matches the reference widget's own readyState -> label mapping.
  const connectionStatus = CONNECTION_STATUS_LABELS[readyState] ?? "Uninstantiated";

  return {
    sendMessage,
    streamingContent,
    isStreaming,
    isThinking,
    error,
    connectionStatus,
  };
}
