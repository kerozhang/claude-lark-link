from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from hmac import digest
from pathlib import Path
from runpy import run_path
from time import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from claude_lark_link.api import create_app
from claude_lark_link.cards import DESKTOP_ACTION, DESKTOP_SESSIONS_ACTION
from claude_lark_link.config import BridgeConfig, initialize, load_config
from claude_lark_link.inbox import DesktopInbox
from claude_lark_link.protocol import REMOTE_TASK_PREFIX, SIGNATURE_HEADER, remote_task_fingerprint
from claude_lark_link.transport import LarkTransport

OWNER = "ou_testuser"
CHAT = "oc_testchat"


@pytest.fixture
def config(tmp_path: Path) -> BridgeConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return BridgeConfig(app_id="cli_test", app_secret="fixture", open_id=OWNER,
                        workspaces={"demo": workspace}, hook_token="ab" * 32, data_dir=tmp_path)


@pytest.fixture
async def setup(config: BridgeConfig):
    cards = []

    async def send(chat, card):
        message_id = f"om_card{len(cards) + 1}"
        cards.append((chat, message_id, card))
        return message_id

    im = NS(send_interactive=AsyncMock(side_effect=send), update_interactive=AsyncMock())
    app = create_app(config, im=im, connect=False)
    bridge = app.state.bridge
    transport = LarkTransport(bridge, asyncio.get_running_loop())
    async with app.router.lifespan_context(app):
        yield app, bridge, transport, im, cards
        for turn in bridge.turns.values():
            if turn.worker:
                turn.worker.cancel()


def event(config: BridgeConfig, payload, *, created: int | None = None, app_id: str | None = None):
    return NS(header=NS(create_time=created if created is not None else int(time() * 1000), event_id=uuid4().hex,
                        app_id=app_id or config.app_id), event=payload)


def incoming(config: BridgeConfig, text: str, *, owner: str = OWNER, kind: str = "p2p", **kwargs):
    return event(config, NS(sender=NS(sender_id=NS(open_id=owner), sender_type="user"),
                            message=NS(chat_id=CHAT, message_id="om_" + uuid4().hex, chat_type=kind,
                                       message_type="text", content=json.dumps({"text": text}))), **kwargs)


def click(config, message, request, choice, *, actor=OWNER, chat=CHAT, form=None):
    action = NS(value={"action": DESKTOP_ACTION, "request": request, "decision": choice}, form_value=form)
    return event(config, NS(operator=NS(open_id=actor), context=NS(open_chat_id=chat, open_message_id=message), action=action))


def target(config: BridgeConfig) -> dict:
    return {"session": str(uuid4()), "cwd": str(config.workspaces["demo"])}


def encoded(config, path, payload, *, when=None, key=None):
    body = json.dumps({**payload, "nonce": uuid4().hex, "at": int(time()) if when is None else when},
                      ensure_ascii=False).encode()
    signature = digest(key or bytes.fromhex(config.hook_token), path.encode() + b":" + body, "sha256").hex()
    return body, {"content-type": "application/json", SIGNATURE_HEADER: signature}


async def test_loopback_signature_timestamp_and_replay(config, setup):
    app, *_ = setup
    url = f"http://127.0.0.1:{config.port}"
    endpoint = config.api_prefix + "/inbox"
    body, headers = encoded(config, "inbox", {**target(config), "op": "pair"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=url) as client:
        assert (await client.post(endpoint, content=body, headers={"content-type": "application/json"})).status_code == 403
        assert (await client.post(endpoint, content=body, headers={**headers, "origin": "https://example.invalid"})).status_code == 403
        assert (await client.post(endpoint, content=body, headers={**headers, "host": "example.invalid"})).status_code == 403
        assert (await client.post(endpoint, content=body, headers={**headers, "x-forwarded-for": "10.0.0.1"})).status_code == 403
        assert (await client.post(config.api_prefix + "/hook", content=body, headers=headers)).status_code == 403
        stale, stale_headers = encoded(config, "inbox", {}, when=int(time()) - 60)
        assert (await client.post(endpoint, content=stale, headers=stale_headers)).status_code == 403
        other, other_headers = encoded(config, "inbox", {}, key=b"x" * 32)
        assert (await client.post(endpoint, content=other, headers=other_headers)).status_code == 403
        response = await client.post(endpoint, content=body, headers=headers)
        assert response.status_code == 200
        reply = response.json()
        assert len(json.loads(reply["result"])["code"]) == 16
        assert reply["proof"] == digest(bytes.fromhex(config.hook_token), b"inbox-result:"
                                        + headers[SIGNATURE_HEADER].encode() + reply["result"].encode(), "sha256").hex()
        assert (await client.post(endpoint, content=body, headers=headers)).status_code == 409
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.0.2.4", 123)), base_url=url) as client:
        assert (await client.post(endpoint, content=body, headers=headers)).status_code == 403


async def test_auto_is_opt_in_and_only_attaches_for_verified_private_chat(config, setup, tmp_path):
    _, bridge, transport, _, cards = setup
    first, second = target(config), target(config)
    assert bridge.local_inbox({**first, "op": "auto"}) == {"reason": "auto_disabled"}
    bridge.config = replace(config, allow_auto_connect=True)
    assert bridge.local_inbox({**first, "cwd": str(tmp_path), "op": "auto"}) == {"reason": "workspace"}
    assert bridge.local_inbox({**first, "op": "auto"})["auto"]
    for invalid in (incoming(config, "/sessions", owner="ou_other"), incoming(config, "/sessions", kind="group"),
                    incoming(config, "/sessions", app_id="cli_other"),
                    incoming(config, "/sessions", created=int(time() * 1000) - 130000)):
        await transport.on_message(invalid)
    assert cards == []
    listed = incoming(config, "/sessions")
    await transport.on_message(listed)
    await transport.on_message(listed)
    assert len(cards) == 1
    assert "当前（自动）" in json.dumps(cards[0][2], ensure_ascii=False)
    assert bridge.local_inbox({**second, "op": "auto"})["auto"]
    paired, current, automatic = bridge.inbox.paired(CHAT)
    assert len(paired) == len(automatic) == 2
    assert current == bridge.session_key(first)
    await transport.on_message(incoming(config, f"/unbind {current[1][:8]}"))
    assert bridge.local_inbox({**first, "op": "auto"}) == {"paused": True}
    assert DesktopInbox(config.data_dir / "paused.json").auto_enroll(*current) == {"paused": True}
    assert bridge.local_inbox({**first, "op": "resume"})["auto"]


async def bind(config, bridge, transport):
    who = target(config)
    pair = bridge.local_inbox({**who, "op": "pair"})
    await transport.on_message(incoming(config, f"/bind {pair['code']}"))
    return who, pair


async def start_turn(config, bridge, transport, who, pair):
    await transport.on_message(incoming(config, "/send Check the project"))
    reply = bridge.local_inbox({**who, "op": "poll", "ticket": pair["ticket"], "ready": True})
    assert reply["task"] == "Check the project"
    turn_id = str(uuid4())
    await bridge.hook({**who, "event": "UserPromptSubmit", "turn_id": turn_id,
                       "delivery": remote_task_fingerprint(REMOTE_TASK_PREFIX + reply["task"])})
    return {**who, "turn_id": turn_id}


async def test_manual_pairing_and_only_remote_turn_updates_original_card(config, setup):
    _, bridge, transport, im, cards = setup
    who, pair = await bind(config, bridge, transport)
    assert "已关联" in json.dumps(cards[-1][2], ensure_ascii=False)
    await transport.on_message(incoming(config, f"/bind {pair['code']}"))
    assert "已用过" in json.dumps(cards[-1][2], ensure_ascii=False)
    impostor = bridge.local_inbox({**target(config), "op": "poll", "ticket": pair["ticket"], "ready": True})
    assert not impostor["connected"]
    before = len(cards)
    local = {**who, "turn_id": str(uuid4())}
    await bridge.hook({**local, "event": "UserPromptSubmit"})
    await bridge.hook({**local, "event": "Stop", "answer": "LOCAL ONLY"})
    assert len(cards) == before
    remote = await start_turn(config, bridge, transport, who, pair)
    queued = cards[-1][1]
    turn = bridge.turns[bridge.session_key(who)]
    await bridge.hook({**remote, "event": "PostToolUse", "tool": "Read"})
    await bridge.hook({**remote, "event": "Stop", "answer": "# Summary\nSee [code](src/demo.py) and https://example.invalid/private"})
    await bridge.hook({**remote, "event": "Stop", "answer": "duplicate"})
    await turn.worker
    await asyncio.sleep(0)
    patches = [(call.args[0], json.dumps(call.args[1], ensure_ascii=False)) for call in im.update_interactive.await_args_list]
    assert all(message == queued for message, _ in patches)
    assert any("Desktop 已领取" in card for _, card in patches)
    assert any("本轮结束" in card and "读取文件" in card for _, card in patches)
    result = json.dumps(cards[-1][2], ensure_ascii=False)
    assert "**Summary**" in result and "src/demo.py" in result and "example.invalid" not in result
    assert len(cards) == before + 2


@pytest.mark.parametrize("oversized", [False, True])
async def test_result_pages_keep_the_final_choices(config, setup, monkeypatch, oversized):
    app, bridge, transport, _, cards = setup
    who, pair = await bind(config, bridge, transport)
    remote = await start_turn(config, bridge, transport, who, pair)
    turn = bridge.turns[bridge.session_key(who)]
    before = len(cards)
    line = "内容：" + "测" * 100 + "\n"
    opening = "# 分析\n\n```text\n"
    middle = "只在未截断时出现的中间内容\n"
    ending = "```\n\n请选择：\n1. 继续处理\n2. 保留现状\n\nhttps://example.invalid/private"
    section = line * (100 if oversized else 46)
    answer = opening + section + middle + section + ending

    native_event = run_path(str(Path(__file__).resolve().parents[1] / "plugins/claude-lark-link/hooks/forward.py"))["native_event"]
    hook_payload = {}
    monkeypatch.setitem(native_event.__globals__, "bridge_request", lambda _root, payload, **_: hook_payload.update(payload))
    native_event({"hook_event_name": "Stop", "session_id": remote["session"], "cwd": remote["cwd"],
                  "prompt_id": remote["turn_id"], "last_assistant_message": answer}, config.data_dir)
    assert hook_payload["answer_truncated"] is oversized
    body, headers = encoded(config, "hook", hook_payload)
    assert 24576 < len(body) < 65536
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://127.0.0.1:{config.port}") as client:
        response = await client.post(config.api_prefix + "/hook", content=body, headers=headers)
    assert response.status_code == 200
    await asyncio.gather(turn.worker, *bridge.background)

    results = [card for _, _, card in cards[before:]]
    assert len(results) > 1
    for index, card in enumerate(results, 1):
        assert card["header"]["subtitle"]["content"].endswith(f" · {index}/{len(results)}")
        assert len(json.dumps(card, ensure_ascii=False).encode()) < 30000
        markdown = next(element["content"] for element in card["body"]["elements"] if element["tag"] == "markdown")
        assert sum(line.startswith("```") for line in markdown.splitlines()) % 2 == 0
    combined = json.dumps(results, ensure_ascii=False)
    last = json.dumps(results[-1], ensure_ascii=False)
    assert "**分析**" in combined
    assert "请选择" in last and "1. 继续处理" in last and "2. 保留现状" in last
    assert "example.invalid" not in combined
    assert ("中间内容已省略" in combined) is oversized
    assert (middle.strip() in combined) is not oversized
    if not oversized:
        assert combined.count("内容：") == 92


async def wait_pending(bridge) -> tuple[str, object]:
    for _ in range(20):
        await asyncio.sleep(0)
        matches = [(request, review) for request, review in bridge.reviews.items() if review.message]
        if matches:
            return matches[0]
    pytest.fail("review card never sent")


async def test_questions_and_permission_callbacks_bind_actor_chat_and_message(config, setup):
    _, bridge, transport, _, _ = setup
    who, pair = await bind(config, bridge, transport)
    remote = await start_turn(config, bridge, transport, who, pair)
    question = {"question": "Which format?", "options": [{"label": "Text"}, {"label": "JSON"}]}
    review = asyncio.create_task(bridge.hook({**remote, "event": "PermissionRequest", "tool": "AskUserQuestion",
                                              "questions": [question]}))
    request, pending = await wait_pending(bridge)
    for invalid in (click(config, pending.message, request, "answer", actor="ou_other", form={"pick_0": "1"}),
                    click(config, pending.message, request, "answer", chat="oc_group", form={"pick_0": "1"}),
                    click(config, "om_forged", request, "answer", form={"pick_0": "1"}),
                    click(config, pending.message, request, "answer", form={"pick_0": "4"}),
                    click(config, pending.message, request, "allow")):
        assert (await transport.on_card(invalid))["toast"]["type"] == "error"
    valid = click(config, pending.message, request, "answer", form={"pick_0": "1"})
    assert (await transport.on_card(valid))["toast"]["type"] == "success"
    assert await review == {"Which format?": "JSON"}
    assert (await transport.on_card(valid))["toast"]["type"] == "error"

    review = asyncio.create_task(bridge.hook({**remote, "event": "PermissionRequest", "tool": "Bash",
                                              "review": 'echo "example"', "complete": True}))
    request, pending = await wait_pending(bridge)
    assert (await transport.on_card(click(config, pending.message, request, "allow")))["toast"]["type"] == "success"
    assert await review == "allow"


async def test_incomplete_review_and_unbind_fail_closed(config, setup):
    _, bridge, transport, _, cards = setup
    who, pair = await bind(config, bridge, transport)
    remote = await start_turn(config, bridge, transport, who, pair)
    review = asyncio.create_task(bridge.hook({**remote, "event": "PermissionRequest", "tool": "Bash",
                                              "review": 'cmd --token fixture-hidden', "complete": True}))
    request, pending = await wait_pending(bridge)
    assert "fixture-hidden" not in json.dumps(cards[-1][2])
    assert (await transport.on_card(click(config, pending.message, request, "allow")))["toast"]["type"] == "error"
    await transport.on_message(incoming(config, "/unbind"))
    assert await review is None
    assert (await transport.on_card(click(config, pending.message, request, "deny")))["toast"]["type"] == "error"


async def test_desktop_rejection_updates_original_card_without_retry(config, setup):
    _, bridge, transport, im, cards = setup
    who, pair = await bind(config, bridge, transport)
    await transport.on_message(incoming(config, "/send Check the project"))
    poll = {**who, "op": "poll", "ticket": pair["ticket"], "ready": True}
    reply = bridge.local_inbox(poll)
    reject = {**who, "op": "reject", "ticket": pair["ticket"], "delivery_id": reply["delivery_id"]}
    bridge.local_inbox(reject)
    bridge.local_inbox(reject)
    assert "task" not in bridge.local_inbox(poll)
    for _ in range(10):
        await asyncio.sleep(0)
    patches = [json.dumps(call.args[1], ensure_ascii=False) for call in im.update_interactive.await_args_list]
    assert sum("Desktop 未接受任务" in card for card in patches) == 1
    assert im.update_interactive.call_args.args[0] == cards[-1][1]


async def test_mcp_empty_optional_answer_is_signed_for_the_current_hook(config, setup):
    app, bridge, transport, _, _ = setup
    who, pair = await bind(config, bridge, transport)
    remote = await start_turn(config, bridge, transport, who, pair)
    schema = {"type": "object", "properties": {"note": {"type": "string"}}}
    body, headers = encoded(config, "hook", {**remote, "event": "Elicitation", "schema": schema,
                                            "message": "Add an optional note", "server": "demo"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://127.0.0.1:{config.port}") as client:
        waiting = asyncio.create_task(client.post(config.api_prefix + "/hook", content=body, headers=headers))
        request, pending = await wait_pending(bridge)
        invalid = click(config, pending.message, request, "answer", form={"field_0": ["invalid"]})
        assert (await transport.on_card(invalid))["toast"]["type"] == "error"
        valid = click(config, pending.message, request, "answer", form={})
        assert (await transport.on_card(valid))["toast"]["type"] == "success"
        result = (await waiting).json()
        proof = digest(bytes.fromhex(config.hook_token), b"answers:" + headers[SIGNATURE_HEADER].encode() + b"{}", "sha256").hex()
        assert result == {"result": {}, "proof": proof}


async def test_sensitive_file_preview_never_leaves_desktop(config, setup):
    _, bridge, transport, _, cards = setup
    who, pair = await bind(config, bridge, transport)
    remote = await start_turn(config, bridge, transport, who, pair)
    review = asyncio.create_task(bridge.hook({**remote, "event": "PermissionRequest", "tool": "Write", "complete": True,
                                              "path": ".env", "review": "fixture content must stay local"}))
    request, pending = await wait_pending(bridge)
    assert "fixture content must stay local" not in json.dumps(cards[-1][2])
    assert (await transport.on_card(click(config, pending.message, request, "allow")))["toast"]["type"] == "error"
    assert (await transport.on_card(click(config, pending.message, request, "deny")))["toast"]["type"] == "success"
    assert await review == "deny"


async def test_sessions_card_accepts_microsecond_and_string_callbacks(config, setup):
    _, bridge, transport, _, cards = setup
    bridge.config = replace(config, allow_auto_connect=True)
    who = target(config)
    ticket = bridge.local_inbox({**who, "op": "auto"})["ticket"]
    await transport.on_message(incoming(config, "/sessions"))
    message = cards[-1][1]
    sid = bridge.session_key(who)[1]

    def press(created, *, value=None, form=None):
        action = NS(value=value or {"action": DESKTOP_SESSIONS_ACTION, "request": "list", "decision": "send"},
                    form_value=form)
        payload = NS(operator=NS(open_id=OWNER), context=NS(open_chat_id=CHAT, open_message_id=message), action=action)
        return event(config, payload, created=created)

    now_ms = int(time() * 1000)
    assert (await transport.on_card(press(now_ms, form=json.dumps({"session": sid, "task": "111"}))))["toast"]["type"] == "success"
    for _ in range(5):
        await asyncio.sleep(0)
    claimed = bridge.local_inbox({**who, "op": "poll", "ticket": ticket, "ready": True, "model_control": True})
    assert claimed["task"] == "111"
    assert bridge.stats["claim"] == 1
    refresh = {"action": DESKTOP_SESSIONS_ACTION, "request": "list", "decision": "recent"}
    assert (await transport.on_card(press(now_ms * 1000, value=refresh)))["toast"]["type"] == "success"
    assert (await transport.on_card(press(now_ms - 200_000, value=refresh)))["toast"]["type"] == "error"


def test_init_keeps_secrets_in_separate_env_files(tmp_path):
    initialize(tmp_path)
    secret = tmp_path / "plugin" / ".env"
    before = secret.read_bytes()
    initialize(tmp_path)
    assert secret.read_bytes() == before
    assert b"LARK_APP_SECRET" not in before
    assert b"HOOK_TOKEN" not in (tmp_path / "config.json").read_bytes()
    assert not load_config(tmp_path).allow_auto_connect
