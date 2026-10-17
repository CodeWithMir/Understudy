"use client";
/* 8.0 live events: WebSocket to the backend (first message authenticates), with a polling fallback - e.g. on Cloud Run
 * behind the Next.js proxy, which doesn't forward WebSocket upgrades. */
import { useEffect, useRef, useState } from "react";
import { getApiBase } from "./api";

export interface LiveEventMsg { id?: string; type: string; at: number; data?: Record<string, unknown> }

function wsUrl(path: string): string {
  const base = getApiBase();
  return base.replace(/^http/, "ws") + path;
}

/** Subscribes to live events. `poll` is called every `pollMs` while the socket isn't connected (and once on start). */
export function useLiveEvents(opts: {
  path: "/api/admin/ws" | "/api/wizard/ws"; token: string | null | undefined; enabled: boolean;
  onEvent: (e: LiveEventMsg) => void; poll: () => void; pollMs?: number;
}): "live" | "polling" | "off" {
  const { path, token, enabled, pollMs = 5000 } = opts;
  const cb = useRef(opts);
  useEffect(() => { cb.current = opts; });
  const [mode, setMode] = useState<"live" | "polling" | "off">("off");
  useEffect(() => {
    if (!enabled) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let live = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const pollTimer = setInterval(() => { if (!live) cb.current.poll(); }, pollMs);
    const connect = () => {
      try { ws = new WebSocket(wsUrl(path)); } catch { setMode("polling"); return; }
      ws.onopen = () => ws?.send(JSON.stringify({ token: token || "" }));
      ws.onmessage = (m) => {
        let e: LiveEventMsg;
        try { e = JSON.parse(m.data as string); } catch { return; }
        if (e.type === "hello") { live = true; setMode("live"); cb.current.poll(); return; }
        if (e.type === "ping") return;
        cb.current.onEvent(e);
      };
      ws.onclose = () => {
        live = false;
        if (closed) return;
        setMode("polling");
        retry = setTimeout(connect, 15000);
      };
      ws.onerror = () => { try { ws?.close(); } catch { /* ignore */ } };
    };
    setTimeout(() => { if (!closed) { setMode("polling"); cb.current.poll(); connect(); } }, 0);
    return () => {
      closed = true;
      clearInterval(pollTimer);
      if (retry) clearTimeout(retry);
      try { ws?.close(); } catch { /* ignore */ }
    };
  }, [path, token, enabled, pollMs]);
  return enabled ? mode : "off";
}
