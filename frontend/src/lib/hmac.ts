/**
 * HMAC-SHA256 signing for REST API authentication.
 *
 * The backend stores `secret_hash = sha256(raw_secret)` and uses that hash
 * as the HMAC key. So the client must compute:
 *   signing_key = sha256(raw_secret)
 *   signature  = hmac_sha256(signing_key, payload)
 *
 * We use the Web Crypto API (SubtleCrypto) which is available in all modern
 * browsers and in Node.js 18+.
 */

async function sha256Hex(message: string): Promise<string> {
  const msgBuffer = new TextEncoder().encode(message);
  const hashBuffer = await crypto.subtle.digest("SHA-256", msgBuffer);
  return bufferToHex(hashBuffer);
}

async function hmacSha256Hex(key: string, message: string): Promise<string> {
  const keyBuffer = new TextEncoder().encode(key);
  const msgBuffer = new TextEncoder().encode(message);

  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    keyBuffer,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );

  const sigBuffer = await crypto.subtle.sign("HMAC", cryptoKey, msgBuffer);
  return bufferToHex(sigBuffer);
}

function bufferToHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Compute the HMAC signature for a REST request.
 *
 * @param rawSecret  The raw API secret (as shown at key creation time)
 * @param method     HTTP method (GET, POST, PUT, DELETE)
 * @param path       Request path including query string (e.g. "/api/v1/orders?limit=10")
 * @param timestamp  Unix seconds
 * @param body       Raw request body string (empty for GET)
 * @returns          Hex-encoded HMAC-SHA256 signature
 */
export async function computeSignature(
  rawSecret: string,
  method: string,
  path: string,
  timestamp: number,
  body: string,
): Promise<string> {
  // The signing key is sha256(raw_secret) — matches backend's hash_api_secret()
  const signingKey = await sha256Hex(rawSecret);
  const payload = `${method.toUpperCase()}\n${path}\n${timestamp}\n${body}`;
  return hmacSha256Hex(signingKey, payload);
}

/**
 * Compute the HMAC signature for a WebSocket handshake.
 * Payload: "WS_HANDSHAKE\n{timestamp}\n{api_key}"
 */
export async function computeWsSignature(
  rawSecret: string,
  apiKey: string,
  timestamp: number,
): Promise<string> {
  const signingKey = await sha256Hex(rawSecret);
  const payload = `WS_HANDSHAKE\n${timestamp}\n${apiKey}`;
  return hmacSha256Hex(signingKey, payload);
}

/**
 * Build the auth headers for a REST request.
 *
 * @returns Object with X-API-Key, X-Timestamp, X-Signature headers
 */
export async function buildAuthHeaders(
  apiKey: string,
  rawSecret: string,
  method: string,
  path: string,
  body: string = "",
): Promise<Record<string, string>> {
  const timestamp = Math.floor(Date.now() / 1000);
  const signature = await computeSignature(rawSecret, method, path, timestamp, body);
  return {
    "X-API-Key": apiKey,
    "X-Timestamp": String(timestamp),
    "X-Signature": signature,
    "Content-Type": "application/json",
  };
}
