# Claude Lark Link

在飞书或 Lark 的机器人私聊中选择本机 Claude Desktop Code 会话、发送任务、查看状态和 Markdown 结果，并通过卡片处理本轮审批与提问。Claude Desktop 继续使用现有登录、订阅或网关。桥接服务使用 Python 和飞书/Lark 应用凭据。

另见 [架构和信任边界](docs/ARCHITECTURE.md) 与 [功能约束](docs/FEATURE.md)。

## 本机安装

你需要 Python 3.11 或更新版本、支持原生插件模块的 Claude Desktop Code，以及自己的飞书或 Lark 自建应用。确保 Desktop 启动的进程能在 `PATH` 中找到 `python`。桥接服务可以使用独立的虚拟环境。

### 1. 安装 Python 桥接服务

```bash
python -m pip install "git+https://github.com/kerozhang/claude-lark-link.git"
```

```bash
claude-lark-link init
```

命令在 `~/.claude-lark-link/` 建立配置。其中 `.env` 填写 `LARK_APP_ID`、`LARK_APP_SECRET` 和 `LARK_OWNER_OPEN_ID`。Open ID 和应用对应，可从同一应用收到的私聊事件的 `sender.sender_id.open_id` 获取。`plugin/.env` 保存自动生成的本机签名密钥。配置文件示例：

```json
{
  "domain": "feishu",
  "workspaces": {"demo": "C:/code/demo"},
  "port": 8765,
  "api_prefix": "/api/v1",
  "allow_auto_connect": false,
  "desktop_connection_mode": "manual"
}
```

`domain` 可选 `feishu` 或 `lark`。`workspaces` 中每项对应一个已存在的本机目录。Desktop 会话的工作目录必须与其中一项相符。子目录和独立工作树可各自列入配置。

在开发者后台启用机器人能力和以应用身份发送消息的权限，为 `im.message.receive_v1` 开启接收单聊消息的权限。将事件和回调均配置为长连接，添加 `im.message.receive_v1` 和 `card.action.trigger`，并发布应用版本。订阅使用长连接，桥接仅监听 `127.0.0.1`。如果平台保存订阅时要求已有长连接，先启动桥接，再返回控制台完成保存。

```bash
claude-lark-link check
```

```bash
claude-lark-link serve
```

健康接口为 `http://127.0.0.1:8765/api/v1/healthz`。其中 `lark_connected` 表示 SDK 当前持有 WebSocket 连接；`stats` 给出自动登记、轮询、领取和未就绪的次数，便于排查任务没有被 Desktop 领取的情况。

### 2. 从 GitHub 安装 Desktop 插件

在 Claude Desktop 的 Code 会话中运行：

```text
/plugin install lark-link --marketplace kerozhang/claude-lark-link
```

也可以在 Code 插件管理中添加 GitHub Marketplace `kerozhang/claude-lark-link`，再安装 `lark-link`。在目标工作区打开或重新打开 Code 会话，让它加载插件。

## 连接与指令

| 连接方式 | 如何使用 |
| --- | --- |
| `manual`，默认 | 在目标 Desktop 会话输入 `/lark-link`，再在两分钟内向机器人私聊发送 `/bind 配对码`。桥接重启后重新配对。 |
| `auto` | 在 `~/.claude-lark-link/config.json` 写入 `"desktop_connection_mode": "auto"`，并将 `allow_auto_connect` 设为 `true`，然后重启桥接。插件为已打开且加载它的授权 Desktop 会话登记，私聊发送 `/sessions` 查看。 |

连接方式只由桥接配置的 `desktop_connection_mode` 决定，未写入或值无效时按 `manual` 处理；新打开或重新打开的 Code 会话会采用最新值。自动连接仍须同时开启 `allow_auto_connect` 并匹配授权目录。

| 飞书私聊指令 | 操作 |
| --- | --- |
| `/sessions` | 查看在线会话，在卡片上设置当前会话或定向发送。 |
| `/send 任务内容` | 发到当前会话。原卡片依次更新排队、领取、执行和结束状态，结果另发。 |
| `/model` | 读取当前模型，并在受支持的会话显示切换控件。网关宿主管理的会话回报内部模型 ID，切换在 Desktop 菜单操作。 |
| `/unbind [会话ID]` | 断开指定或全部会话。自动连接的暂停状态会保留到你在目标 Desktop 会话输入 `/lark-link`。 |

自动连接只登记当前打开且加载插件的授权会话，保留已有的当前会话选择。普通 Desktop 聊天不会同步到飞书。仅连接允许将远程任务及其结果、审批详情发送到飞书的工作区；自动脱敏不能识别所有敏感内容。远程任务的审批针对当前请求，卡片过期、关掉原请求或切换轮次即失效。无法完整呈现的授权请求仍可在 Desktop 处理。任务队列和配对保存在内存中，桥接重启后清空。

## English quick start

Install the Python bridge from GitHub with the pip command above. Run `claude-lark-link init`, enter your app credentials and app-scoped owner Open ID in the generated `.env`, and list exact local workspace directories in `config.json`. Enable the bot and its private message permissions. Subscribe to `im.message.receive_v1` and `card.action.trigger` using the platform's long-connection option. Start the bridge with `claude-lark-link serve` and install `lark-link` from the GitHub marketplace `kerozhang/claude-lark-link` in Claude Desktop Code.

Manual pairing is the default. Run `/lark-link` in the target Desktop session and send `/bind <code>` in the bot's private chat. To connect automatically, set `allow_auto_connect` to `true` and `desktop_connection_mode` to `auto` in the bridge config, then restart the bridge. Closed sessions and conversation history are outside the scope of discovery. Use `/sessions`, `/send <task>`, `/model` and `/unbind` from the configured private chat. Host-managed gateway models are changed in Desktop.

## 许可证 / License

[MIT License](LICENSE)
