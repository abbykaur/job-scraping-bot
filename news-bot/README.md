# AI news bot

A weekly batch of AI news for the AFG server, curated by mods.

| When (Pacific) | What happens |
|---|---|
| Monday | The bot posts about 8 of the week's AI stories as drafts in **#news-queue**, mixed across sources. |
| Mon–Fri | Mods react ✅ to include a draft or ❌ to leave it out. To edit one, reply to it (see below). Post `more: <topic>` for 3 extra drafts. |
| Friday, 10 AM | Every ✅ draft becomes its own post in the **#ai-news** forum, tagged with its topic and **Weekly Roundup**. Drafts without a ✅ are closed. |

The bot checks the queue every 2 hours on weekdays, so a reply or request takes up to 2 hours to show up. The bot reacts ✏️ when it applies an edit and 👀 when it picks up a request.

## For mods

- **Include / leave out:** react ✅ or ❌ on the draft. If a draft has both, ❌ wins. Reactions are read on Friday, so you can change your mind until then.
- **Edit a draft:** reply to it with any of these lines:
  ```
  title: Better headline
  blurb: One or two sentences on why it matters.
  tag: Research
  ```
  A reply with no `title:` / `blurb:` / `tag:` lines replaces the blurb. `tag: none` removes the tag.
- **More stories:** `more: AI chips` searches Google News for the past week. Plain `more` pulls the next stories from the regular feeds.

## Setup

1. **Create the bot.** In the [Discord Developer Portal](https://discord.com/developers/applications), click **New Application**, then on the **Bot** page:
   - **Reset Token** and copy it.
   - Turn on **Message Content Intent**. The bot needs it to read edit replies and `more` requests.
   - Or reuse the jobs bot by inviting it to AFG. Its token works here too, but it still needs Message Content Intent turned on.
2. **Invite it to AFG.** On the **OAuth2 → URL Generator** page, pick the `bot` scope and these permissions: View Channels, Send Messages, Send Messages in Threads, Embed Links, Add Reactions, Read Message History. Open the generated URL and pick AFG.
3. **Channels.** #news-queue should be visible to mods and the bot only. #ai-news has to be a forum channel.
4. **Channel IDs.** In Discord, turn on Settings → Advanced → Developer Mode. Then right-click each channel and choose **Copy Channel ID**.
5. **Repo secrets.** From the repo folder:
   ```
   gh secret set NEWS_DISCORD_TOKEN
   gh secret set NEWS_QUEUE_CHANNEL_ID --body <#news-queue id>
   gh secret set NEWS_FORUM_CHANNEL_ID --body <#ai-news id>
   ```
   `gh secret set NEWS_DISCORD_TOKEN` asks you to paste the token, so it never ends up in your shell history. Until these are set, the workflow's runs are skipped instead of failing.

**Run it now:** in the repo's **Actions** tab, open **ai-news** and click **Run workflow**. Leave `force` on `none` for a normal run, which does whatever today's schedule calls for. Pick `draft` to post this week's candidates now, or `roundup` to post the ✅ drafts to #ai-news now. Forcing `roundup` closes every open draft, the same way Friday's run does.

**Local runs:** put the three values in `news-bot/.env`, then run `python news-bot/news.py`. Local runs post to the real channels, and they save state to your copy of `state.json`. The Actions runs then won't know what was already drafted.

## Customizing

These are all near the top of `news.py`:

| Setting | What it controls |
|---|---|
| `TAG_KEYWORDS` | Which forum tag each story gets. Tag names must match the #ai-news forum's tags exactly. |
| `FEEDS` | Which RSS feeds the Monday drafts come from. |
| `WEEKLY_CANDIDATES` | How many drafts Monday posts. |
| `ROUNDUP_DAY`, `ROUNDUP_HOUR` | When approved stories post to #ai-news. |
