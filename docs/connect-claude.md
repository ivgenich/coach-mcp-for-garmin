# Connect Claude

Add the server to Claude once; the connector then works in Claude on the web, in the desktop and
mobile apps, and in Claude Code signed in with the same Claude account. Steps checked November
2026 against Anthropic's
[Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom/add-unlisted.md).

## claude.ai (Free, Pro, Max and Ultra plans)

In a browser; the mobile apps pick the connector up by themselves.

1. **Customize → Connectors → Add → Add custom connector**
2. Name: `Garmin`. URL: the **connector URL** your deploy printed
   (`https://<your-host>/mcp`), exactly as printed, with no trailing slash.
   Click **Continue**.
3. Authentication: **Sign in now**. OAuth client: **Register automatically** (keep them
   if Claude marks them **Detected**). Click **Add**, then **Connect** if Claude shows it.
   These settings can't be changed later; to change them, remove the connector and add it
   again.
4. A sign-in page on your server opens. Check that it names your server
   (`<your-host>`) and that you started this sign-in just now, type the owner secret and click **Approve**. A password manager can fill it in; the user name
   is `owner`. Type the secret only when the address bar shows your server: a password
   manager offers it only on the address you saved it for, not on a look-alike page.
5. You are back in claude.ai, and Claude can use the Garmin tools.

The sign-in page works once, for 10 minutes, and three wrong secrets cancel it; start
again from claude.ai. Approve only a sign-in you started yourself: if someone sends you a
link to this page, click **Deny**.

One sign-in serves claude.ai on the web, the desktop and mobile apps, cloud routines, and
Claude Code logged in with the same claude.ai account. If Claude later shows **Reconnect**
for the connector, or a **Connect** card in a chat, sign in again the same way.

## Team and Enterprise plans

Only an Owner can add a custom connector; on Enterprise, so can a member whose custom role
includes managing the organization's libraries. The connector is added for the whole
organization:

1. Go to **Organization settings → Connectors**, choose **Add**, then **Custom**. If Claude asks
   for the connector type, choose **Web**.
2. Enter the name and the connector URL as above, then **Continue** if the dialog shows it.
3. Authentication: **Sign in now**. OAuth client: **Register automatically**. Click **Add**.
4. Then, as a member: **Customize → Connectors**, find the connector with the **Custom** label,
   click **Connect** and approve on your server's page with the owner secret.

Other members see the connector too, but none of them can finish a sign-in without your owner
secret. Chats in a work organization follow its settings, so a personal plan is the better place
for your health data.

## Desktop and mobile apps

Add the connector on claude.ai in a browser, or in the desktop app (**Customize** in the sidebar,
then **Connectors**); in the desktop app the sign-in page opens in your browser. The mobile apps
then use it too. In a chat, **+ → Connectors** shows whether it is on. If you have two Garmin
connectors (a test server, for example), turn on only one of them per chat there.

## Claude Code

Logged in to Claude Code with the same claude.ai account, you already have the connector.
To connect Claude Code on its own instead:

```bash
claude mcp add --transport http garmin https://<your-host>/mcp
```

Then run `/mcp` in a Claude Code session and sign in to `garmin`. Your browser opens the
same sign-in page, which warns that the sign-in returns to a program on this computer
(`localhost:<port>`); approve it with the owner secret. A local entry for the same URL
takes the place of the claude.ai connector in Claude Code.

Where no browser is at hand to sign in (CI, for example), turn on the optional static token:
`./scripts/deploy.sh --rotate-token` prints it once. Without deploy.sh (Docker, or Railway in the
dashboard): see [Rotate the static token](maintenance.md#rotate-the-static-token). Then:

```bash
claude mcp add --transport http garmin https://<your-host>/mcp --header "Authorization: Bearer <token>"
```

The token works without the sign-in page, so guard it like the owner secret: your shell
history and Claude Code's settings keep a copy. With a header set, a wrong token is a failed
connection; Claude Code does not fall back to the sign-in page.

To disconnect Claude Code later, run `claude mcp logout garmin`, then
`claude mcp remove garmin`: the logout asks the server to revoke the sign-in.

## Try it

- "How am I today?"
- "Pull my latest activity from Garmin and summarize it"
- "What was the training effect of my last run?"
- "How did I sleep last night?"
- "Compare my last two runs"

## Disconnect or remove

On claude.ai or in the desktop app, open **Customize → Connectors** and select the connector:

- **Disconnect** signs Claude out of your server. The connector stays and shows **Connect**.
- **Remove** (a button, or in the three-dot menu) takes the connector off your account. On Team and
  Enterprise, an Owner removes it in **Organization settings → Connectors**.
- **Tool permissions** set each tool to **Always allow**, **Needs approval** or **Blocked**.

To keep Claude from using the server in one chat only, turn it off under **+ → Connectors** in
that chat. Removing the connector does not delete your server: see
[Uninstall and revoke](../README.md#uninstall-and-revoke).
