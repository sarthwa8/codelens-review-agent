import { useEffect, useRef, useState } from "react";
import { api } from "./api";

export type StreamPhase =
  | "connecting"
  | "queued"
  | "streaming"
  | "following"
  | "retrying"
  | "reconnecting"
  | "complete"
  | "skipped"
  | "failed";

export interface StreamState {
  text: string;
  phase: StreamPhase;
  cacheHit: boolean;
  message: string | null;
  deltas: number; // incremental chunks received — makes the live arrival visible/measurable
  firstTokenMs: number | null;
}

const INITIAL: StreamState = {
  text: "",
  phase: "connecting",
  cacheHit: false,
  message: null,
  deltas: 0,
  firstTokenMs: null,
};

/**
 * Subscribes to a review's SSE stream.
 *
 * Deltas accumulate in refs and are committed to React state at most once per animation frame:
 * the text still visibly grows token by token, but a fast model can't force hundreds of renders a
 * second. Server "done"/"failed" events close the EventSource explicitly — otherwise the browser
 * would treat the closed response as a dropped connection and reconnect forever.
 */
export function useReviewStream(reviewId: number): StreamState {
  const [state, setState] = useState<StreamState>(INITIAL);
  const pending = useRef({ text: "", deltas: 0, firstTokenMs: null as number | null });
  const frame = useRef(0);

  useEffect(() => {
    setState(INITIAL);
    pending.current = { text: "", deltas: 0, firstTokenMs: null };
    const openedAt = performance.now();
    const source = new EventSource(api.streamUrl(reviewId));

    const flush = () => {
      frame.current = 0;
      const { text, deltas, firstTokenMs } = pending.current;
      if (!deltas) return;
      pending.current = { text: "", deltas: 0, firstTokenMs: null };
      setState((s) => ({
        ...s,
        text: s.text + text,
        deltas: s.deltas + deltas,
        firstTokenMs: s.firstTokenMs ?? firstTokenMs,
        phase: s.phase === "following" ? "following" : "streaming",
      }));
    };
    const discardPending = () => {
      if (frame.current) cancelAnimationFrame(frame.current);
      frame.current = 0;
      pending.current = { text: "", deltas: 0, firstTokenMs: null };
    };
    const on = <T,>(event: string, handler: (data: T) => void) =>
      source.addEventListener(event, (e) => handler(JSON.parse((e as MessageEvent).data) as T));

    on<{ attempt: number }>("reset", () => {
      discardPending();
      setState((s) => ({ ...s, text: "", message: null }));
    });
    on<{ status: StreamPhase; cache_hit?: boolean }>("status", (d) =>
      setState((s) => ({ ...s, phase: d.status, cacheHit: d.cache_hit ?? s.cacheHit })),
    );
    on<{ text: string }>("delta", (d) => {
      const p = pending.current;
      p.text += d.text;
      p.deltas += 1;
      p.firstTokenMs ??= Math.round(performance.now() - openedAt);
      if (!frame.current) frame.current = requestAnimationFrame(flush);
    });
    on<{ text: string; cache_hit: boolean }>("snapshot", (d) => {
      discardPending();
      setState((s) => ({ ...s, text: d.text, cacheHit: d.cache_hit }));
    });
    on<{ message: string }>("retrying", (d) =>
      setState((s) => ({ ...s, phase: "retrying", message: d.message })),
    );
    on<{ reason: string }>("skipped", (d) =>
      setState((s) => ({ ...s, phase: "skipped", message: d.reason })),
    );
    on<{ status: string; cache_hit?: boolean }>("done", (d) => {
      source.close();
      flush();
      setState((s) => ({
        ...s,
        phase: d.status === "skipped" ? "skipped" : "complete",
        cacheHit: d.cache_hit ?? s.cacheHit,
      }));
    });
    // Named "failed", not "error": "error" is EventSource's built-in connection-error event.
    on<{ message: string }>("failed", (d) => {
      source.close();
      flush();
      setState((s) => ({ ...s, phase: "failed", message: d.message }));
    });
    source.onerror = () => {
      if (source.readyState === EventSource.CONNECTING) {
        setState((s) => ({ ...s, phase: "reconnecting" }));
      }
    };

    return () => {
      discardPending();
      source.close();
    };
  }, [reviewId]);

  return state;
}
