/**
 * Signing in to waitlist-web (docs/multi-tenant/design-reference.md §6.2.2): the OIDC code flow with PKCE,
 * done by the page itself. The whole window goes to the identity provider and comes back to
 * /auth/callback with the code in the URL fragment (never sent to a server, so never logged); the page
 * exchanges it for an app token, which lives in localStorage and goes in the Authorization header.
 * Nothing here sets or reads a cookie.
 */

import { myApi } from "./api";

export const TOKEN_KEY = "wl.token";
export const SIGNED_OUT_KEY = "wl.signedOut";
export const FLOW_KEY = "wl.signin";

export interface AuthConfig {
  authorization_endpoint: string;
  client_id: string;
  redirect_uri: string;
  scopes: string;
}

interface Flow {
  state: string;
  nonce: string;
  verifier: string;
  returnTo: string;
}

function read(storage: Storage, key: string): string | null {
  try {
    return storage.getItem(key);
  } catch {
    return null; // storage blocked (private mode, enterprise policy)
  }
}

function write(storage: Storage, key: string, value: string | null): void {
  try {
    if (value === null) storage.removeItem(key);
    else storage.setItem(key, value);
  } catch {
    // nothing to do: the page works for this visit
  }
}

export const token = (): string | null => read(localStorage, TOKEN_KEY);
export const saveToken = (value: string): void => write(localStorage, TOKEN_KEY, value);
export const clearToken = (): void => write(localStorage, TOKEN_KEY, null);

/** After an explicit sign-out, nothing signs in again until the button is pressed. */
export const isSignedOut = (): boolean => read(localStorage, SIGNED_OUT_KEY) === "1";
export function markSignedOut(): void {
  clearToken();
  write(localStorage, SIGNED_OUT_KEY, "1");
}

/** Signs this device out (only this one: the user's other devices stay signed in), then back to the start. */
export async function signOut(): Promise<void> {
  try {
    await myApi.logout();
  } catch {
    // Signed out here all the same: the token is dropped below.
  } finally {
    markSignedOut();
    location.assign("/");
  }
}

export function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const random = (length: number): string => base64url(crypto.getRandomValues(new Uint8Array(length)));

export async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** Only a path on this site: never somewhere else ("//evil.example", "https://…"). */
export function safeReturn(path: string): string {
  return path.startsWith("/") && !path.startsWith("//") && !path.startsWith("/\\") ? path : "/";
}

/** The provider's sign-in page for a new flow. Keeps what the callback will need in sessionStorage. */
export async function signInUrl(config: AuthConfig, returnTo: string): Promise<string> {
  const flow: Flow = { state: random(16), nonce: random(16), verifier: random(32), returnTo: safeReturn(returnTo) };
  write(sessionStorage, FLOW_KEY, JSON.stringify(flow));
  write(localStorage, SIGNED_OUT_KEY, null);
  const url = new URL(config.authorization_endpoint);
  const params: Record<string, string> = {
    response_type: "code",
    response_mode: "fragment",
    client_id: config.client_id,
    redirect_uri: config.redirect_uri,
    scope: config.scopes,
    state: flow.state,
    nonce: flow.nonce,
    code_challenge: await challengeFor(flow.verifier),
    code_challenge_method: "S256",
  };
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  return url.href;
}

export async function startSignIn(returnTo: string = location.pathname + location.search): Promise<void> {
  const response = await fetch("/auth/config", { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!response.ok) throw new Error("Sign-in is unavailable right now");
  const config = (await response.json()) as AuthConfig;
  location.assign(await signInUrl(config, returnTo));
}

export class SignInFailed extends Error {
  override readonly name = "SignInFailed";
}

/**
 * Finish a sign-in from the callback's fragment: check `state`, exchange the code, keep the token.
 * Returns the path to go back to.
 */
export async function completeSignIn(fragment: string, post: typeof fetch = fetch): Promise<string> {
  const params = new URLSearchParams(fragment.replace(/^#/, ""));
  const saved = read(sessionStorage, FLOW_KEY);
  write(sessionStorage, FLOW_KEY, null); // one use, whatever happens
  if (params.get("error")) {
    throw new SignInFailed(params.get("error") === "access_denied" ? "Sign-in was refused" : "Sign-in didn't complete");
  }
  const flow = saved ? (JSON.parse(saved) as Flow) : null;
  const code = params.get("code");
  if (!flow || !code || params.get("state") !== flow.state) {
    throw new SignInFailed("This sign-in didn't start here; try again");
  }
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json" };
  const current = token();
  if (current) headers.Authorization = `Bearer ${current}`; // re-verifying: the old token is rotated out
  const response = await post("/auth/session", {
    method: "POST",
    headers,
    body: JSON.stringify({ code, code_verifier: flow.verifier, state: flow.state, nonce: flow.nonce }),
  });
  const body = (await response.json().catch(() => ({}))) as { token?: unknown; error?: unknown };
  if (!response.ok || typeof body.token !== "string") {
    throw new SignInFailed(typeof body.error === "string" ? body.error : "Sign-in didn't complete");
  }
  saveToken(body.token);
  return safeReturn(flow.returnTo);
}
