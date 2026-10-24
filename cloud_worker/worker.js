// Pitwall Engineer - OPTIONAL online fallback. A Cloudflare Worker that forwards chat questions to Workers AI.
// YOU deploy it once (free Cloudflare account). Drivers then need NO account, key or install.
// Free plan allowance (about 10,000 "neurons" a day, shared by everyone) is small: it is a fallback, not the main brain.
// Deploy:   cd cloud_worker  ->  npx wrangler login  ->  npx wrangler deploy
// Then put the printed https://....workers.dev address into config.py as LLM_ONLINE_URL.

const MODEL = "@cf/meta/llama-3.2-3b-instruct";
const MAX_MESSAGES = 12, MAX_CHARS = 8000, MAX_TOKENS = 120;
const ROLES = new Set(["system", "user", "assistant"]);

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return json({ error: "POST only" }, 405);
    let body;
    try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }

    const list = Array.isArray(body.messages) ? body.messages.slice(-MAX_MESSAGES) : [];
    const messages = list
      .filter(m => m && ROLES.has(m.role) && typeof m.content === "string")
      .map(m => ({ role: m.role, content: m.content.slice(0, MAX_CHARS) }));
    if (!messages.length || messages.reduce((n, m) => n + m.content.length, 0) > MAX_CHARS)
      return json({ error: "bad messages" }, 400);

    const max_tokens = Math.min(Number(body.max_tokens) || 80, MAX_TOKENS);
    try {
      const result = await env.AI.run(MODEL, { messages, max_tokens });
      // OpenAI-compatible shape, which is what local_llm.py reads
      return json({ choices: [{ message: { role: "assistant", content: result.response || "" } }] });
    } catch (e) {
      return json({ error: "upstream failed" }, 502);
    }
  },
};
