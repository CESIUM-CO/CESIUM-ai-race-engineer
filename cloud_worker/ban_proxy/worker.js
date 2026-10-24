// Cesium ban list proxy. Holds the Discord bot token as a SECRET and returns only the pinned message text.
// The desktop app never sees the token.
export default {
  async fetch(request, env) {
    if (request.method !== "GET") return new Response("GET only", { status: 405 });
    const url = `https://discord.com/api/v10/channels/${env.BAN_CHANNEL_ID}/messages/${env.PINNED_MESSAGE_ID}`;
    try {
      const r = await fetch(url, {
        headers: { Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`, "User-Agent": "DiscordBot (cesium-ban-proxy, 1.0)" },
      });
      if (!r.ok) return new Response(JSON.stringify({ error: "upstream " + r.status }), { status: 502 });
      const msg = await r.json();
      return new Response(JSON.stringify({ content: String(msg.content || "") }), {
        headers: { "content-type": "application/json", "cache-control": "public, max-age=300" },
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: "upstream failed" }), { status: 502 });
    }
  },
};
