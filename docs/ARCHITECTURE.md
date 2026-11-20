# Architecture

Claude Desktop runs the normal Code session with its own login and provider. The plugin adds `/lark-link` and polls a Python bridge on loopback. The Python service uses the Feishu/Lark SDK's outbound WebSocket for messages and card actions and calls the platform's messaging API for replies. It has no model-provider credentials or public callback endpoint.

## Trust boundaries

- The bridge uses the app-scoped owner Open ID in its `.env` and accepts commands only from that user's private bot chat. Actions must match a bridge-issued card in that exact chat. Event IDs and timestamps limit duplicate or delayed execution.
- The plugin and bridge share a separate secret in `plugin/.env`. Local requests are signed with an endpoint-specific HMAC, timestamp and nonce. The hook verifies the response is signed for its own request before using any decision. This assumes the OS account and the app are trusted; software with access to the local secret is inside that trust boundary.
- Registration checks the current working directory against exact, resolved local workspaces. Automatic enrollment requires the bridge's `allow_auto_connect` gate and `desktop_connection_mode` set to `auto` in the bridge config; any other value, or none, means manual. The plugin keeps its local bridge under `~/.claude-lark-link` and never reads Desktop's own plugin settings. Manual enrollment uses a short-lived one-time code. Polling tickets also bind to the workspace and Claude session ID.
- A submitted remote task is matched to a claimed delivery, and later events must match that Code turn. The bridge does not read transcript files, search other projects or import chat history. It sends bounded and filtered excerpts; automated filtering cannot identify all private data.
- Approvals are specific to the current request and use the standard Claude hooks. Unsupported or partially hidden operations require Desktop for any affirmative decision. Expired, replaced, closed and unbound requests lose their ability to resolve the matching approval.

State is ephemeral except for config and hashed auto-pause keys. A new process accepts automatic registrations again without reconnecting explicitly paused sessions. In a single bridge instance, the long-connection SDK runs in a worker thread and transfers event handling to the HTTP service's asyncio loop.
