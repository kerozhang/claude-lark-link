# Features and boundaries

| Capability | Behavior |
| --- | --- |
| Manual | Pair an open Claude Desktop Code session with a short-lived private-chat code. Manual is the default. |
| Auto | Register open, plugin-enabled sessions in an explicitly allowed workspace when `desktop_connection_mode` is `auto` in the bridge config. Never scans closed sessions or changes the user's current selection on arrival. |
| Tasks | Select an online session from `/sessions` or send to the current one. Progress updates the original card; the Markdown result arrives separately. |
| Approvals | For supported operations, show one request with allow-once or deny. If details are hidden or the operation is unsupported, approval stays on Desktop. |
| Input | Answer supported Claude questions and simple MCP forms from the original private chat. Other formats use Desktop. |
| Model | Read the session model and change it when the Code runtime supports a session-only switch. For host-managed gateways, show the internal ID and point to the Desktop picker. |
| Disconnect | Use `/unbind` in Feishu/Lark. For automatic connections, the pause survives bridge restarts until `/lark-link` runs in that Desktop session. |

The bridge forwards only remotely submitted task turns. Do not enable it for workspaces whose contents may not be shared with Feishu or Lark. Automated tests use mocked Feishu/Lark events; live end-to-end platform compatibility has not been verified.
