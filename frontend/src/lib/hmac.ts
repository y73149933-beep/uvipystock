/**
 * HMAC-SHA256 signing for REST API authentication.
 *
 * The backend stores `secret_hash = sha256(raw_secret)` and uses that hash
 * as the HMAC key. So the client must compute:
 *   signing_key = sha256(raw_secret)
 *   signature  = hmac_sha256(signing_key, payload)
 *
 * Uses the pure-JS `js-sha256` package: Web Crypto (SubtleCrypto) is unavailable
 * in insecure contexts (plain http:// over a bare IP), which would otherwise
 * break every HMAC-signed request in the browser.
 */

import { sha256 } from "js-sha256";

async function sha256Hex(message: string): Promise<string> {
  return sha256(message);
}

async function hmacSha256Hex(key: string, message: string): Promise<string> {
  return sha256.hmac(key, message);
}

/**
 * Compute the HMAC signature for a REST request.
 *
 * @param rawSecret  The raw API secret (as shown at key creation time)
 * @param method     HTTP method (GET, POST, PUT, DELETE)
 * @param path       Request path including query string (e.g. "/api/v1/orders?limit=10")
 * @param timestamp  Unix seconds
 * @param body       Raw request body string (empty for GET)
 * @returns          Hex-encoded HMAC-256 signature
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