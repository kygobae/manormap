// node tests/functions.test.mjs : Cloudflare Pages Functions (webhook + download) with fake KV/R2
import assert from "node:assert/strict";
import * as webhook from "../gutshaus/deploy/functions/webhook.js";
import * as download from "../gutshaus/deploy/functions/download.js";

const secret = "s3cret", order = "104e18a2-d755-4d4b-80c4-a6c1dcbe1c10", kv = new Map();
const env = {
  LS_SIGNING_SECRET: secret,
  ORDERS: { put: async (k, v) => kv.set(k, v), delete: async k => kv.delete(k),
            get: async (k, t) => kv.has(k) ? (t === "json" ? JSON.parse(kv.get(k)) : kv.get(k)) : null },
  DOSSIERS: { head: async k => k.endsWith("GF-C586A63B.pdf") ? {} : null,
              get: async k => k.endsWith("GF-C586A63B.pdf") ? { body: "%PDF" } : null },
};
async function sign(body) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return [...new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body)))].map(b => b.toString(16).padStart(2, "0")).join("");
}
const post = async (event, dossier, sig) => {
  const body = JSON.stringify({ meta: { event_name: event, custom_data: { dossier } },
                                data: { attributes: { identifier: order, user_email: "a@b.c", status: "paid" } } });
  return webhook.onRequestPost({ request: new Request("https://x/webhook", { method: "POST", body,
    headers: { "X-Signature": sig ?? await sign(body) } }), env });
};
const get = o => download.onRequestGet({ request: new Request(`https://x/download?o=${o}`), env });

assert.equal((await post("order_created", "gf-c586a63b", "00")).status, 401);          // forged
assert.equal((await get(order)).headers.get("content-type"), "text/html; charset=utf-8"); // webhook not yet in
assert.equal((await post("order_created", "gf-c586a63b")).status, 200);
const r = await get(order);
assert.equal(r.headers.get("content-type"), "application/pdf");
assert.match(r.headers.get("content-disposition"), /GF-C586A63B/);
assert.equal((await get("not-an-order")).status, 200);                                   // friendly error page
await post("order_created", "GF-00000000");                                             // PDF not uploaded
assert.ok(kv.has("missing:GF-00000000"));
await post("order_refunded", "GF-00000000");
assert.ok(!kv.has(`o:${order}`));
console.log("functions ok");
