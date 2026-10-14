/**
 * The dashboard's live socket on ferry-web: authenticates with its first message (a browser can't put a
 * header on an upgrade, and the token never goes in a URL), then receives the user's own state on every
 * change. A 4401 close means the session no longer works: the next API call says why.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { parseJson } from "@/lib/api";
import { backoffDelay, socketUrl } from "@/lib/live";
import { MY_STATE_KEY } from "./api";
import { token } from "./auth";
import { LiveMessageSchema } from "./schema";

export const CLOSE_UNAUTHORIZED = 4401;

export function useMyLive(): boolean {
  const qc = useQueryClient();
  const [live, setLive] = useState(false);

  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;

    function connect() {
      if (stopped) return;
      const current = token();
      if (!current) return;
      const ws = new WebSocket(socketUrl());
      socket = ws;
      ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: current }));
      ws.onmessage = (event: MessageEvent<unknown>) => {
        const message = LiveMessageSchema.safeParse(typeof event.data === "string" ? parseJson(event.data) : undefined);
        if (!message.success) return;
        failures = 0;
        setLive(true);
        qc.setQueryData(MY_STATE_KEY, message.data.state);
      };
      ws.onclose = (event) => {
        if (ws !== socket || stopped) return;
        setLive(false);
        socket = null;
        if (event.code === CLOSE_UNAUTHORIZED) void qc.invalidateQueries({ queryKey: MY_STATE_KEY });
        failures += 1;
        timer = setTimeout(connect, backoffDelay(failures - 1));
      };
    }

    connect();
    return () => {
      stopped = true;
      clearTimeout(timer);
      const ws = socket;
      socket = null;
      ws?.close();
    };
  }, [qc]);

  return live;
}
