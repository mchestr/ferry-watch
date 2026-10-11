/** A WebSocket stand-in the tests drive by hand: serverSend, serverClose and so on. */
export class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  static instances: FakeWebSocket[] = [];

  static get latest(): FakeWebSocket {
    const socket = FakeWebSocket.instances.at(-1);
    if (!socket) throw new Error("No WebSocket has been created");
    return socket;
  }

  static reset(): void {
    FakeWebSocket.instances = [];
  }

  readonly url: string;
  readyState = FakeWebSocket.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  closeCalls = 0;

  constructor(url: string | URL) {
    this.url = url.toString();
    FakeWebSocket.instances.push(this);
  }

  close(): void {
    this.closeCalls += 1;
    this.readyState = FakeWebSocket.CLOSED;
  }

  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  /** Delivers a message; objects are sent as JSON. */
  serverSend(message: unknown): void {
    const data = typeof message === "string" ? message : JSON.stringify(message);
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  serverError(): void {
    this.onerror?.(new Event("error"));
  }

  serverClose(code = 1006): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close", { code }));
  }
}
