# Olyphants Telegram Mini App (UI prototype)

Static UI prototype. PAPER-only and deliberately disconnected from exchange, robot, and order execution.

## Files
- `index.html`: responsive Mini App UI. Includes Telegram WebApp SDK bootstrap (`ready()` and `expand()`).

## Publish with HTTPS
Telegram Mini Apps require an HTTPS URL reachable by Telegram. Publish this folder to a static host (for example GitHub Pages or Cloudflare Pages), then copy the published HTTPS URL.

## Attach to the existing bot
1. Open the official `@BotFather` chat.
2. Select the existing Olyphants bot (`Olyphants_bot`) — do not create a new bot.
3. Use `/setmenubutton` and select that bot.
4. Set button title to `Olyphants` (or `Dashboard`).
5. Paste the published HTTPS URL.
6. Open the existing bot chat and tap its Menu button to test.

BotFather's menu button only opens the web page. This prototype does not read robot status, send Telegram commands, connect to Termux, or place trades. Do not expose bot tokens in this folder or in browser JavaScript. Backend integration should use a separately secured HTTPS API and Telegram `initData` validation.

## Local preview
Open `index.html` in a browser. The Telegram-specific banner appears only when Telegram's WebApp object is available.
