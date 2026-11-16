const STORAGE = "file-arrival-operator";
const REQUEST = "file-arrival-sign-in";

function randomString(bytes = 32) {
  const random = crypto.getRandomValues(new Uint8Array(bytes));
  return btoa(String.fromCharCode(...random)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function accessToken() {
  try {
    const session = JSON.parse(sessionStorage.getItem(STORAGE));
    return session?.expiresAt > Date.now() ? session.token : null;
  } catch {
    return null;
  }
}

export async function beginSignIn(config) {
  if (config.localPreview) {
    const response = await fetch("/local/session", { method: "POST" });
    if (!response.ok) throw new Error("Local operator sign-in failed.");
    const session = await response.json();
    sessionStorage.setItem(STORAGE, JSON.stringify(session));
    return;
  }
  const verifier = randomString(48);
  const state = randomString();
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const challenge = btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  sessionStorage.setItem(REQUEST, JSON.stringify({ verifier, state }));
  const url = new URL("/oauth2/authorize", config.authUrl);
  url.search = new URLSearchParams({
    client_id: config.clientId, redirect_uri: config.redirectUri, response_type: "code",
    scope: "file-arrival/upload", code_challenge_method: "S256", code_challenge: challenge, state,
  });
  location.assign(url);
}

export async function finishSignIn(config) {
  const query = new URLSearchParams(location.search);
  if (!query.has("code") && !query.has("error")) return;
  history.replaceState(null, "", location.pathname + location.hash);
  const request = JSON.parse(sessionStorage.getItem(REQUEST));
  sessionStorage.removeItem(REQUEST);
  if (query.has("error") || !request || query.get("state") !== request.state || config.localPreview) {
    throw new Error("Sign-in could not be verified. Try again.");
  }
  const response = await fetch(new URL("/oauth2/token", config.authUrl), {
    method: "POST",
    body: new URLSearchParams({
      grant_type: "authorization_code", client_id: config.clientId, redirect_uri: config.redirectUri,
      code: query.get("code"), code_verifier: request.verifier,
    }),
  });
  if (!response.ok) throw new Error("Sign-in expired. Try again.");
  const { access_token, expires_in } = await response.json();
  sessionStorage.setItem(STORAGE, JSON.stringify({ token: access_token, expiresAt: Date.now() + expires_in * 1000 }));
}

export function signOut(config) {
  sessionStorage.removeItem(STORAGE);
  sessionStorage.removeItem(REQUEST);
  if (config.localPreview) {
    location.reload();
    return;
  }
  const url = new URL("/logout", config.authUrl);
  url.search = new URLSearchParams({ client_id: config.clientId, logout_uri: config.redirectUri });
  location.assign(url);
}
