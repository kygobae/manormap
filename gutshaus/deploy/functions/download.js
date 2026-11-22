// /download?o=<order identifier>  (set as the product's "Button link" in Lemon Squeezy:
//   https://<your-site>/download?o=[order_identifier] )
const page = (title, text, refresh) => new Response(
  `<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
  (refresh ? `<meta http-equiv="refresh" content="${refresh}">` : "") +
  `<title>${title}</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:560px;margin:60px auto;padding:0 18px;color:#1d2321}h1{font-family:Georgia,serif;color:#7a4b2a}</style></head>` +
  `<body><h1>${title}</h1><p>${text}</p></body></html>`,
  { status: 200, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });

export async function onRequestGet({ request, env }) {
  const o = new URL(request.url).searchParams.get("o") || "";
  if (!/^[0-9a-f-]{20,64}$/i.test(o)) return page("Link ungültig", "Bitte verwenden Sie den Link aus Ihrer Bestellbestätigung.");
  const rec = await env.ORDERS.get(`o:${o}`, "json");
  if (!rec) {
    // the webhook usually arrives within seconds of the payment
    return page("Einen Moment …", "Ihre Zahlung wird bestätigt. Diese Seite lädt sich automatisch neu. / Trwa potwierdzanie płatności. / Confirming your payment.", 5);
  }
  const obj = await env.DOSSIERS.get(`dossiers/${rec.dossier}.pdf`);
  if (!obj) {
    return page("Dossier in Vorbereitung",
      `Ihr Dossier ${rec.dossier} wird gerade erstellt und kommt innerhalb eines Werktags per E-Mail an ${rec.email}. Entschuldigen Sie die Verzögerung.`);
  }
  return new Response(obj.body, {
    headers: {
      "content-type": "application/pdf",
      "content-disposition": `attachment; filename="Gutshaus-Dossier-${rec.dossier}.pdf"`,
      "cache-control": "private, no-store",
    },
  });
}
