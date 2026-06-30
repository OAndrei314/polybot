/**
 * Paste this in Brave/Chrome DevTools console while logged into x.com.
 * It extracts your session credentials and copies them as a JSON entry
 * ready to paste into accounts.json.
 *
 * How:
 *   1. Go to https://x.com (must be logged in)
 *   2. Press F12 → Console tab
 *   3. Paste this entire script and hit Enter
 *   4. It prints + copies the JSON block to your clipboard
 *   5. Paste it into accounts.json (inside the [ ] array)
 *
 * The bearer token below is Twitter's own web-client app token —
 * it's the same for every browser session and is public knowledge
 * (embedded in Twitter's JS bundle, used by every open-source scraper).
 * If it stops working, grab a fresh one from the Network tab:
 *   Any request to x.com/i/api/ → Request Headers → authorization
 */
(function () {
  const jar = {};
  document.cookie.split("; ").forEach((c) => {
    const eq = c.indexOf("=");
    if (eq > 0) jar[c.slice(0, eq).trim()] = c.slice(eq + 1);
  });

  const authToken = jar["auth_token"] || "";
  const ct0 = jar["ct0"] || "";

  if (!authToken) {
    console.error("❌  auth_token not found — are you logged into x.com?");
    return;
  }

  const entry = {
    label: "account_" + Math.random().toString(36).slice(2, 6),
    bearer:
      "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I7ssbmGA%3DzHXrVbpyVgm6tSLgnYAA5JuvJ6JjQPxA2IG5",
    cookie: document.cookie,
    csrf: ct0,
  };

  const json = JSON.stringify(entry, null, 2);

  console.log("%c✅  Credentials extracted:", "color:#4CAF50;font-weight:bold");
  console.log(json);

  if (typeof copy === "function") {
    copy(json);
    console.log("%c📋  Copied to clipboard — paste into accounts.json", "color:#4CAF50");
  } else {
    console.log("Select and copy the JSON above manually.");
  }

  return entry;
})();
