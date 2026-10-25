// Client di esempio della Rizzo PII Private API (Node.js >= 18: fetch, FormData e Blob globali).
//
//   RIZZO_PII_URL=http://rizzo-pii-api:5005 RIZZO_PII_API_KEY=... node client.mjs ingresso.pdf uscita.pdf
import { readFile, writeFile } from "node:fs/promises";

const BASE = (process.env.RIZZO_PII_URL ?? "http://rizzo-pii-api:5005").replace(/\/$/, "");
const KEY = process.env.RIZZO_PII_API_KEY; // mai nel codice: variabile d'ambiente / secret
const TIMEOUT_MS = 330_000;                // >= RIZZO_API_REQUEST_TIMEOUT

async function apiError(res) {
  const { error } = await res.json();      // {code, message, request_id}
  return new Error(`${res.status} ${error.code}: ${error.message} (${error.request_id})`);
}

export async function anonymizePdf(pdfBuffer, options) {
  const form = new FormData();
  form.append("file", new Blob([pdfBuffer], { type: "application/pdf" }), "documento.pdf");
  form.append("options", JSON.stringify(options));
  const res = await fetch(`${BASE}/api/v1/anonymize`, {
    method: "POST",
    headers: { Authorization: `Bearer ${KEY}` },
    body: form,
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) throw await apiError(res);
  const stats = Object.fromEntries([...res.headers].filter(([k]) => k.startsWith("x-pii-")));
  return { pdf: Buffer.from(await res.arrayBuffer()), stats };
}

export async function anonymizeText(text, options = {}) {
  const res = await fetch(`${BASE}/api/v1/anonymize-text`, {
    method: "POST",
    headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ text, options }),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()).anonymized_text;
}

const [src, dst] = process.argv.slice(2);
const { pdf, stats } = await anonymizePdf(await readFile(src), {
  preserve_letterhead: true, letterhead_mode: "automatic", letterhead_pages: "all",
});
await writeFile(dst, pdf);
console.log(stats);
console.log(await anonymizeText("Pratica di Giulietta Bramante, nata a Ravenna."));
