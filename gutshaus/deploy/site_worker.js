// The static site's Worker: serves the files in ./public and takes feedback at POST /api/feedback.
// Copied to <out-root>/site/worker.js by `gutshaus publish` when feedback is configured (FEEDBACK_D1_ID and
// TURNSTILE_SITE_KEY in gutshaus.json; the Turnstile secret is a Worker secret: `npx wrangler secret put
// TURNSTILE_SECRET` inside <out-root>/site).
//
// Bots are kept out by, in this order: a hidden field people never fill in (silently accepted and dropped), a minimum
// time between opening the form and sending it, Cloudflare Turnstile, and at most PER_DAY messages per visitor and
// day. Visitors are counted by a salted hash of their IP address that changes every day; the address itself is
// never stored.

const KINDS = ["error", "idea", "owner", "sell", "other"];
const PER_DAY = 5;
const MIN_MS = 3000;
const ACTION = "feedback";                    // the widget's action: a token from another form or site does not count

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/feedback") return feedback(request, env);
    return env.ASSETS.fetch(request);
  },
};

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

async function sha256(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function feedback(request, env) {
  if (request.method !== "POST") return json({ ok: false, error: "method" }, 405);
  const origin = request.headers.get("Origin");
  if (origin && new URL(origin).host !== new URL(request.url).host) return json({ ok: false, error: "origin" }, 403);
  let b;
  try {
    b = await request.json();
  } catch {
    return json({ ok: false, error: "format" }, 400);
  }
  if (b.website) return json({ ok: true });                       // the hidden field: a bot; pretend all went well
  const kind = KINDS.includes(b.kind) ? b.kind : null;
  let text = String(b.text || "").trim().slice(0, 3000);
  const site = /^GF-[0-9A-F]{8}$/.test(b.site || "") ? b.site : "";
  const email = String(b.email || "").trim().slice(0, 200);
  const lang = ["de", "pl", "en"].includes(b.lang) ? b.lang : "";
  if (!kind || text.length < 5) return json({ ok: false, error: "text" }, 400);
  if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return json({ ok: false, error: "email" }, 400);
  if ((kind === "owner" || kind === "sell") && !email) return json({ ok: false, error: "email" }, 400);   // we must be able to ask for proof
  if (kind === "sell") {
    // an owner's listing: published only after we have checked ownership, and only with this consent
    if (b.consent !== true) return json({ ok: false, error: "consent" }, 400);
    const price = String(b.price || "").trim().slice(0, 80);
    const contact = String(b.contact || "").trim().slice(0, 200);
    text += `\n\n[listing] price: ${price || "-"} | public contact: ${contact || "-"} | consent to publish: yes`;
  }
  if (!(Date.now() - Number(b.t || 0) >= MIN_MS)) return json({ ok: false, error: "fast" }, 400);

  const ip = request.headers.get("CF-Connecting-IP") || "";
  const token = b.token;
  const hosts = String(env.TURNSTILE_HOSTNAMES || "").split(",").map((h) => h.trim()).filter(Boolean);
  if (!env.TURNSTILE_SECRET || !hosts.length) {
    console.log("turnstile: not configured", { secret: !!env.TURNSTILE_SECRET, hosts: hosts.length });
    return json({ ok: false, error: "captcha" }, 403);
  }
  if (typeof token !== "string" || !token || token.length > 2048) return json({ ok: false, error: "captcha" }, 403);
  let check;
  try {
    const r = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST", signal: AbortSignal.timeout(10_000),
      body: new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: token, remoteip: ip }),
    });
    if (!r.ok) return json({ ok: false, error: "captcha" }, 403);
    check = await r.json();
  } catch {
    return json({ ok: false, error: "captcha" }, 403);
  }
  // single-use tokens (siteverify rejects a replay), issued by our widget on our own domain
  if (check.success !== true || check.action !== ACTION || !hosts.includes(check.hostname)) {
    // no personal data: Cloudflare's error codes and what the token was issued for (see `npx wrangler tail`)
    console.log("turnstile: rejected", { codes: check["error-codes"], action: check.action, hostname: check.hostname });
    return json({ ok: false, error: "captcha" }, 403);
  }

  const now = new Date().toISOString();
  const day = now.slice(0, 10);
  const iph = (await sha256(`${ip}|${day}|${env.TURNSTILE_SECRET || ""}`)).slice(0, 20);
  const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM feedback WHERE iph = ?1 AND ts >= ?2").bind(iph, day).first();
  if ((row && row.n) >= PER_DAY) return json({ ok: false, error: "limit" }, 429);
  await env.DB.prepare("INSERT INTO feedback (ts, kind, site, text, email, lang, iph) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)")
    .bind(now, kind, site, text, email, lang, iph).run();
  // the privacy notice promises deletion after 12 months
  await env.DB.prepare("DELETE FROM feedback WHERE ts < ?1").bind(new Date(Date.now() - 365 * 864e5).toISOString()).run();
  return json({ ok: true });
}
