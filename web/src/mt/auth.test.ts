import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  FLOW_KEY,
  SignInFailed,
  challengeFor,
  completeSignIn,
  isSignedOut,
  markSignedOut,
  safeReturn,
  saveToken,
  signInUrl,
  token,
} from "./auth";

const CONFIG = {
  authorization_endpoint: "https://auth.chestr.dev/api/oidc/authorization",
  client_id: "waitlist",
  redirect_uri: "https://waitlist.chestr.dev/auth/callback",
  scopes: "openid profile email groups",
};

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PKCE", () => {
  it("is base64url(SHA-256(verifier)), unpadded (S256)", async () => {
    // Computed independently: base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
    expect(await challengeFor("dBjftJeZ4CVP-mJ92K9I3GZ4sFZ3pbTAXkUAmPWVYrw")).toBe(
      "Io7zFFcUlmKLvY7TD2J9EqW_P3EoSqV82fkqln9B_Vw",
    );
  });
});

describe("starting a sign-in", () => {
  it("sends the whole window to the provider with a fresh state, nonce and challenge", async () => {
    markSignedOut();
    const url = new URL(await signInUrl(CONFIG, "/c/3f9c0a1b2c3d4e5f"));
    expect(url.origin + url.pathname).toBe(CONFIG.authorization_endpoint);
    const p = url.searchParams;
    expect(p.get("response_type")).toBe("code");
    expect(p.get("response_mode")).toBe("fragment");
    expect(p.get("code_challenge_method")).toBe("S256");
    const flow = JSON.parse(sessionStorage.getItem(FLOW_KEY) ?? "{}") as Record<string, string>;
    expect(p.get("state")).toBe(flow.state);
    expect(p.get("nonce")).toBe(flow.nonce);
    expect(p.get("code_challenge")).toBe(await challengeFor(flow.verifier ?? ""));
    expect(flow.verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(flow.returnTo).toBe("/c/3f9c0a1b2c3d4e5f");
    expect(isSignedOut()).toBe(false); // pressing the button clears the signed-out flag
  });

  it("only ever returns to a path on this site", () => {
    expect(safeReturn("/c/abc")).toBe("/c/abc");
    expect(safeReturn("//evil.example/")).toBe("/");
    expect(safeReturn("https://evil.example/")).toBe("/");
    expect(safeReturn("/\\evil.example")).toBe("/");
  });
});

describe("the callback", () => {
  async function started(): Promise<URLSearchParams> {
    return new URL(await signInUrl(CONFIG, "/settings")).searchParams;
  }

  const ok = (body: object, status = 200) =>
    vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(body), { status }));

  it("exchanges the code and keeps the token", async () => {
    const p = await started();
    const post = ok({ token: "t-1", user: "u" });
    const back = await completeSignIn(`#code=abc&state=${p.get("state") ?? ""}`, post);
    expect(back).toBe("/settings");
    expect(token()).toBe("t-1");
    const [path, init] = post.mock.calls[0] ?? [];
    expect(path).toBe("/auth/session");
    const sent = JSON.parse(String(init?.body)) as Record<string, string>;
    expect(sent.code).toBe("abc");
    expect(sent.nonce).toBe(p.get("nonce"));
    expect(await challengeFor(sent.code_verifier ?? "")).toBe(p.get("code_challenge"));
    expect(sessionStorage.getItem(FLOW_KEY)).toBeNull(); // used up
  });

  it("sends the current token when re-verifying, so it's rotated out", async () => {
    saveToken("old");
    const p = await started();
    const post = ok({ token: "new" });
    await completeSignIn(`#code=abc&state=${p.get("state") ?? ""}`, post);
    const init = post.mock.calls[0]?.[1];
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer old");
    expect(token()).toBe("new");
  });

  it("refuses a callback this page didn't start", async () => {
    await started();
    await expect(completeSignIn("#code=abc&state=someone-elses", ok({ token: "x" }))).rejects.toThrow(SignInFailed);
    expect(token()).toBeNull();
  });

  it("explains a refusal", async () => {
    await started();
    await expect(completeSignIn("#error=access_denied", ok({}))).rejects.toThrow("Sign-in was refused");
    const p = await started();
    const refused = ok({ error: "Your access is being set up" }, 403);
    await expect(completeSignIn(`#code=a&state=${p.get("state") ?? ""}`, refused)).rejects.toThrow(
      "Your access is being set up",
    );
  });
});
