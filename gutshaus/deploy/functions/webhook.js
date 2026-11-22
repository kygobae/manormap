// Lemon Squeezy webhook: remember which dossier an order bought.
// Env: LS_SIGNING_SECRET (secret), ORDERS (KV namespace), DOSSIERS (R2 bucket).

async function hmacHex(secret, body) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

export async function onRequestPost({ request, env }) {
  const body = await request.text();
  const sig = request.headers.get("X-Signature") || "";
  if (!env.LS_SIGNING_SECRET || !safeEqual(await hmacHex(env.LS_SIGNING_SECRET, body), sig)) {
    return new Response("bad signature", { status: 401 });
  }
  const p = JSON.parse(body);
  const event = p.meta?.event_name;
  const a = p.data?.attributes || {};
  const order = a.identifier;
  const dossier = String(p.meta?.custom_data?.dossier || "").toUpperCase();
  if (!order) return new Response("no order", { status: 400 });

  if (event === "order_created") {
    if (!/^GF-[0-9A-F]{8}$/.test(dossier)) {
      await env.ORDERS.put(`problem:${order}`, JSON.stringify({ reason: "no dossier id", email: a.user_email, at: Date.now() }));
      return new Response("ok (no dossier id)");
    }
    await env.ORDERS.put(`o:${order}`, JSON.stringify({ dossier, email: a.user_email, status: a.status, at: Date.now() }));
    if (!(await env.DOSSIERS.head(`dossiers/${dossier}.pdf`))) {
      await env.ORDERS.put(`missing:${dossier}`, JSON.stringify({ order, email: a.user_email, at: Date.now() }));
    }
  } else if (event === "order_refunded") {
    await env.ORDERS.delete(`o:${order}`);
  }
  return new Response("ok");
}
