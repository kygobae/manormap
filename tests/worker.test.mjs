// node tests/worker.test.mjs : the site Worker's feedback endpoint (owners' listings) with a fake D1 and Turnstile
import assert from "node:assert/strict";
import worker from "../gutshaus/deploy/site_worker.js";

const rows = [];
const env = {
  TURNSTILE_SECRET: "t", TURNSTILE_HOSTNAMES: "manormap.eu",
  ASSETS: { fetch: async () => new Response("static") },
  DB: { prepare: sql => ({ bind: (...a) => ({
    first: async () => ({ n: 0 }),
    run: async () => { if (sql.startsWith("INSERT")) rows.push(a); return {}; },
  }) }) },
};
globalThis.fetch = async () => new Response(JSON.stringify({ success: true, action: "feedback", hostname: "manormap.eu" }));

const send = body => worker.fetch(new Request("https://manormap.eu/api/feedback", { method: "POST",
  headers: { "content-type": "application/json", Origin: "https://manormap.eu" },
  body: JSON.stringify({ kind: "sell", site: "GF-1A2B3C4D", text: "Dwór z parkiem, 2 ha", email: "o@example.org",
                         lang: "pl", t: Date.now() - 10_000, token: "tok", price: "900 000 zł", contact: "+48 600 000 000",
                         ...body }) }), env);

let r = await send({ consent: false });                                // no consent: nothing stored
assert.equal(r.status, 400);
assert.equal((await r.json()).error, "consent");
r = await send({ consent: true, email: "" });                           // we must be able to ask for proof
assert.equal((await r.json()).error, "email");
assert.equal(rows.length, 0);
r = await send({ consent: true });
assert.equal(r.status, 200);
assert.equal(rows.length, 1);
const [, kind, site, text, email] = rows[0];
assert.equal(kind, "sell");
assert.equal(site, "GF-1A2B3C4D");
assert.equal(email, "o@example.org");
assert.match(text, /^Dwór z parkiem, 2 ha\n\n\[listing\] price: 900 000 zł \| public contact: \+48 600 000 000 \| consent to publish: yes$/);
console.log("worker ok");
