import { useEffect, useRef, useState, useCallback } from "react";
import { WS_URL } from "../api";

const MAX_BACKOFF_MS = 15_000;

/**
 * Connects to the backend's /ws/live event feed (Phase 5a) and re-connects
 * automatically with exponential backoff. Consumers pass an onEvent callback
 * that receives every parsed { type, data } envelope as it arrives.
 */
export default function useWebSocket(onEvent) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);
  const backoffRef = useRef(1000);
  const stoppedRef = useRef(false);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const connect = useCallback(() => {
    if (stoppedRef.current) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      backoffRef.current = 1000;
    };

    ws.onmessage = (evt) => {
      let parsed;
      try {
        parsed = JSON.parse(evt.data);
      } catch {
        return;
      }
      onEventRef.current?.(parsed);
    };

    ws.onclose = () => {
      setConnected(false);
      if (stoppedRef.current) return;
      const delay = backoffRef.current;
      backoffRef.current = Math.min(delay * 1.7, MAX_BACKOFF_MS);
      setTimeout(connect, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, []);

  useEffect(() => {
    stoppedRef.current = false;
    connect();
    return () => {
      stoppedRef.current = true;
      wsRef.current?.close();
    };
  }, [connect]);

  return { connected };
}
