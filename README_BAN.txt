HWID + BAN CHECK UPDATE
=======================
1. Close the engineer.
2. Copy ALL files of this folder into your project folder AI_RACE_ENGINNER_3.12\ and say "Replace".
   Keep cesium.ico, .venv\, models\, data\, ac_app\ exactly as they are.
   NOTE: config.py here is your current one plus ONE new line (BAN_LIST_URL). If you have your own edited
   config.py, do NOT replace it: just add the BAN_LIST_URL line near USE_CLOUD.
3. Rotate your Discord bot token (it was pasted in a chat): Developer Portal > Bot > Reset Token.
4. Deploy the ban proxy (token is typed at a prompt, never saved in a file):
      cd cloud_worker\ban_proxy
      npx wrangler login
      npx wrangler secret put DISCORD_BOT_TOKEN
      npx wrangler deploy
5. Put the printed https://cesium-ban-list.YOURNAME.workers.dev address into config.py as BAN_LIST_URL.
6. Test: python bug_reporter.py --hwid   (prints your own hash)
   To ban: paste a hash from a #bug-reports embed on its own line in the pinned message.

FOLDER LAYOUT
  AI_RACE_ENGINNER_3.12\
  |-- bug_reporter.py              CHANGED
  |-- config.py                    CHANGED (one line)
  |-- README_BAN.txt               NEW
  `-- cloud_worker\
      |-- worker.js                (your existing AI fallback, unchanged)
      `-- ban_proxy\
          |-- worker.js            NEW
          `-- wrangler.toml        NEW
