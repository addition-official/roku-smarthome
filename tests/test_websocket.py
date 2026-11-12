"""Command delivery over a real websocket, against a local fake Roku server."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.protocol import State

from roku_smarthome import CommandFailed, Device, RokuSmartHome, RokuUnavailable, SessionExpired

from conftest import make_leaf


class FakeRoku:
    """A websocket server on localhost that records what it receives.

    reject_with   HTTP status to refuse the handshake with (e.g. 403)
    chatter       messages to send to each new client, like Roku's init burst
    reply         how to answer each command, like real Roku: "ok" sends
                  send_command_resp, any other string is sent back as an error,
                  None sends nothing
    reply_delay   seconds before replying
    process_delay like real Roku, a command only takes effect (lands in
                  `processed`) if the connection is still open this long after
                  it arrives; closing right after sending loses it
    """

    def __init__(self):
        self.reject_with = None
        self.chatter = 0
        self.received = []
        self.processed = []
        self.custom_handler = None  # replaces the default behavior if set
        self.reply = "ok"
        self.reply_delay = 0.0
        self.process_delay = 0.3
        self.connections = 0
        self.headers = []
        self._clients = set()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start())
        self._ready.set()
        self._loop.run_forever()

    async def _start(self):
        self._server = await serve(self._handler, "127.0.0.1", 0,
                                   process_request=self._process_request)
        self.url = f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    def _process_request(self, connection, request):
        self.headers.append(request.headers)
        if self.reject_with:
            return Response(self.reject_with, "Rejected", Headers(), b"")
        return None

    async def _handler(self, ws):
        if self.custom_handler:
            return await self.custom_handler(ws)
        self.connections += 1
        self._clients.add(ws)
        try:
            for i in range(self.chatter):
                await ws.send(json.dumps({"type": "init", "n": i}))
            async for message in ws:
                msg = json.loads(message)
                self.received.append(msg)
                asyncio.get_running_loop().create_task(self._process(ws, msg))
                asyncio.get_running_loop().create_task(self._reply(ws, msg))
        finally:
            self._clients.discard(ws)

    async def _reply(self, ws, msg):
        if self.reply is None:
            return
        await asyncio.sleep(self.reply_delay)
        if self.reply == "ok":
            answer = {"id": msg["id"], "type": "send_command_resp"}
        else:
            answer = {"id": msg["id"], "type": "send_command", "error": self.reply}
        try:
            # unrelated push message first, as Roku does
            await ws.send(json.dumps({"type": "leaf_state_report", "payload": []}))
            await ws.send(json.dumps(answer))
        except Exception:
            pass

    async def _process(self, ws, msg):
        await asyncio.sleep(self.process_delay)
        if ws.state is State.OPEN:
            self.processed.append(msg)

    def wait_for_processed(self, n, timeout=5):
        deadline = time.monotonic() + timeout
        while len(self.processed) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.processed

    def push(self, message: dict):
        """Send a message to every connected client, like Roku's state reports."""
        async def go():
            for ws in list(self._clients):
                await ws.send(json.dumps(message))
        asyncio.run_coroutine_threadsafe(go(), self._loop).result(5)

    def wait_for_clients(self, n=1, timeout=5):
        deadline = time.monotonic() + timeout
        while len(self._clients) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        return len(self._clients)

    def drop_all_connections(self):
        """Close every client connection from the server side."""
        async def go():
            for ws in list(self._clients):
                await ws.close()
        asyncio.run_coroutine_threadsafe(go(), self._loop).result(5)

    def wait_for_messages(self, n, timeout=5):
        deadline = time.monotonic() + timeout
        while len(self.received) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.received

    def stop(self):
        async def go():
            self._server.close()
            await self._server.wait_closed()
        asyncio.run_coroutine_threadsafe(go(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture
def server():
    s = FakeRoku()
    yield s
    s.stop()


@pytest.fixture
def make_client(cookie_file, server):
    clients = []

    def make(**kwargs):
        rk = RokuSmartHome(cookie_file, timeout=3, **kwargs)
        rk.import_cookie_header("ks.session=abc123; __cf_bm=stale")
        rk.socket_url = server.url
        clients.append(rk)
        return rk, Device(rk, make_leaf())

    yield make
    for rk in clients:
        rk.close()


def test_command_message_format_and_headers(make_client, server):
    rk, bulb = make_client()
    bulb.set_brightness(40)
    [msg] = server.wait_for_messages(1)
    assert msg["type"] == "send_command"
    assert msg["payload"] == {
        "leafId": "leaf-1",
        "leafType": "device",
        "command": {"command": "brightness", "parameters": {"level": 40}},
    }
    assert server.headers[0]["Cookie"] == "ks.session=abc123"
    assert server.headers[0]["Origin"] == "https://my.roku.com"


def test_commands_reuse_one_connection(make_client, server):
    rk, bulb = make_client()
    start = time.monotonic()
    for level in range(10, 60, 10):
        bulb.set_brightness(level)
    elapsed = time.monotonic() - start
    msgs = server.wait_for_messages(5)
    assert [m["payload"]["command"]["parameters"]["level"] for m in msgs] == [10, 20, 30, 40, 50]
    assert server.connections == 1 and rk._connects == 1
    # the old behavior waited 0.5 s per command
    assert elapsed < 1.5


def test_reconnects_after_server_drops_connection(make_client, server):
    rk, bulb = make_client()
    bulb.turn_on()
    server.wait_for_messages(1)
    server.drop_all_connections()
    time.sleep(0.2)
    bulb.turn_off()
    msgs = server.wait_for_messages(2)
    assert [m["payload"]["command"]["parameters"] for m in msgs] == [
        {"power": "on"}, {"power": "off"}]
    assert server.connections == 2


def test_resends_when_connection_died_unnoticed(make_client, server):
    # Simulate a connection that died without the client noticing yet:
    # the first send fails, so the client must reconnect and resend.
    rk, bulb = make_client()
    bulb.turn_on()
    server.wait_for_messages(1)
    stale = rk._ws

    async def dead_send(data):
        raise ConnectionClosedError(None, None)
    stale.send = dead_send
    bulb.turn_off()
    msgs = server.wait_for_messages(2)
    assert [m["payload"]["command"]["parameters"] for m in msgs] == [
        {"power": "on"}, {"power": "off"}]
    assert rk._connects == 2 and rk._ws is not stale


def test_gives_up_if_fresh_connection_also_fails(make_client, server, monkeypatch):
    rk, bulb = make_client()
    real_open = rk._ws_open

    async def open_broken():
        ws = await real_open()

        async def dead_send(data):
            raise ConnectionClosedError(None, None)
        ws.send = dead_send
        return ws
    monkeypatch.setattr(rk, "_ws_open", open_broken)
    with pytest.raises(RokuUnavailable):
        bulb.turn_on()
    assert rk._connects == 1  # a brand-new connection failing is not retried


def test_server_chatter_does_not_break_connection(make_client, server):
    # Roku sends several messages on connect. More than websockets' receive
    # queue (16) must not stall the connection.
    server.chatter = 100
    rk, bulb = make_client()
    bulb.turn_on()
    time.sleep(0.3)
    # keep-alive pings must still be answered, or the library drops the link
    async def ping():
        pong = await rk._ws.ping()
        await asyncio.wait_for(pong, timeout=2)
    asyncio.run_coroutine_threadsafe(ping(), rk._loop).result(5)
    bulb.turn_off()
    assert len(server.wait_for_messages(2)) == 2
    assert server.connections == 1


def test_idle_timeout_closes_and_next_command_reopens(make_client, server):
    rk, bulb = make_client(idle_timeout=0.2)
    bulb.turn_on()
    server.wait_for_messages(1)
    time.sleep(1)  # idle timeout, plus the 0.5 s wait after the last command
    assert rk._ws is None
    bulb.turn_off()
    assert len(server.wait_for_messages(2)) == 2
    assert server.connections == 2


def test_close_then_reuse(make_client, server):
    rk, bulb = make_client()
    bulb.turn_on()
    rk.close()
    rk.close()  # safe twice
    bulb.turn_off()
    assert len(server.wait_for_messages(2)) == 2


def test_context_manager_closes(cookie_file, server):
    with RokuSmartHome(cookie_file, timeout=3) as rk:
        rk.import_cookie_header("ks.session=abc")
        rk.socket_url = server.url
        Device(rk, make_leaf()).turn_on()
        assert rk._ws is not None
    assert rk._ws is None
    assert len(server.wait_for_messages(1)) == 1


def test_non_persistent_mode_connects_per_command(make_client, server):
    rk, bulb = make_client(persistent=False, command_wait=0.05)
    bulb.turn_on()
    bulb.turn_off()
    assert len(server.wait_for_messages(2)) == 2
    assert server.connections == 2


@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("status,error", [(403, SessionExpired), (401, SessionExpired),
                                          (500, RokuUnavailable)])
def test_handshake_rejection(make_client, server, persistent, status, error):
    server.reject_with = status
    rk, bulb = make_client(persistent=persistent)
    with pytest.raises(error):
        bulb.turn_on()


def test_unreachable_server(make_client, server):
    rk, bulb = make_client()
    rk.socket_url = "ws://127.0.0.1:1"
    with pytest.raises(RokuUnavailable):
        bulb.turn_on()


def test_commands_from_many_threads(make_client, server):
    rk, bulb = make_client()
    errors = []

    def worker(level):
        try:
            Device(rk, make_leaf()).set_brightness(level)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(server.wait_for_messages(20)) == 20
    assert server.connections == 1


def test_command_is_delivered_when_script_exits_immediately(server, cookie_file):
    # A script that sends one command and exits without calling close()
    # must still get the command to Roku.
    import subprocess
    import sys
    import textwrap
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from conftest import make_leaf
        from roku_smarthome import RokuSmartHome, Device
        rk = RokuSmartHome({str(cookie_file)!r})
        rk.import_cookie_header("ks.session=abc")
        rk.socket_url = {server.url!r}
        Device(rk, make_leaf()).turn_on()
    """)
    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)
    msgs = server.wait_for_processed(1)
    assert msgs and msgs[0]["payload"]["command"]["parameters"] == {"power": "on"}


def test_close_right_after_command_still_processed(make_client, server):
    # What the CLI does: one command, then close() straight away.
    # Roku drops a command if the connection closes too soon after it.
    rk, bulb = make_client()
    bulb.turn_on()
    rk.close()
    assert len(server.wait_for_processed(1)) == 1


def test_last_of_a_burst_is_processed_before_close(make_client, server):
    # The failed hardware test: off, 1 s, on, then exit. The "on" was lost.
    rk, bulb = make_client()
    bulb.turn_off()
    time.sleep(0.5)
    bulb.turn_on()
    rk.close()
    msgs = server.wait_for_processed(2)
    assert [m["payload"]["command"]["parameters"] for m in msgs] == [
        {"power": "off"}, {"power": "on"}]


def test_idle_close_does_not_cut_off_last_command(make_client, server):
    rk, bulb = make_client(idle_timeout=0.05)
    bulb.turn_on()
    time.sleep(1)
    assert rk._ws is None
    assert len(server.wait_for_processed(1)) == 1


def test_cli_command_is_processed(server, cookie_file, monkeypatch):
    from roku_smarthome import cli
    rk = RokuSmartHome(cookie_file, timeout=3)
    rk.import_cookie_header("ks.session=abc")
    rk.socket_url = server.url
    rk._fetch_leaves = lambda: [make_leaf()]
    monkeypatch.setattr(cli, "RokuSmartHome", lambda cookies=None: rk)
    cli.main(["on", "bedroom"])
    assert len(server.wait_for_processed(1)) == 1



def test_server_closing_after_delivered_command_is_not_an_error(make_client, server):
    # Non-persistent mode lingers after sending. If Roku closes the connection
    # normally during that time, the command was delivered: no exception, or
    # a caller that retries would send it twice (and a toggle would undo itself).
    async def close_after_first_message(ws):
        server.connections += 1
        async for message in ws:
            server.received.append(json.loads(message))
            await ws.close()
    server.custom_handler = close_after_first_message
    rk, bulb = make_client(persistent=False, command_wait=1, confirm=False)
    bulb.turn_on()
    assert len(server.wait_for_messages(1)) == 1


def test_persistent_server_close_after_command_is_not_an_error(make_client, server):
    async def close_after_first_message(ws):
        server.connections += 1
        async for message in ws:
            server.received.append(json.loads(message))
            await ws.close()
    server.custom_handler = close_after_first_message
    rk, bulb = make_client(confirm=False)
    bulb.turn_on()          # delivered, then the server closes
    time.sleep(0.2)
    bulb.turn_off()         # must reconnect, not fail
    assert len(server.wait_for_messages(2)) == 2



# ----- confirmation of commands (Roku's replies) -----

@pytest.mark.parametrize("persistent", [True, False])
def test_confirmed_command_returns_roku_reply(make_client, server, persistent):
    rk, bulb = make_client(persistent=persistent)
    reply = bulb.send("power", {"power": "on"})
    assert reply["type"] == "send_command_resp"
    assert reply["id"] == server.received[0]["id"]


@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("error", ["failed to execute command", "forbidden request"])
def test_roku_error_raises_command_failed(make_client, server, persistent, error):
    server.reply = error
    rk, bulb = make_client(persistent=persistent)
    bulb.power = "off"
    with pytest.raises(CommandFailed) as exc:
        bulb.turn_on()
    assert error in str(exc.value)
    assert exc.value.reply["error"] == error
    assert bulb.power == "off"  # local state not updated on failure


@pytest.mark.parametrize("persistent", [True, False])
def test_no_reply_times_out(make_client, server, persistent):
    server.reply = None
    rk, bulb = make_client(persistent=persistent, reply_timeout=0.3)
    start = time.monotonic()
    with pytest.raises(RokuUnavailable) as exc:
        bulb.turn_on()
    assert "did not confirm" in str(exc.value)
    assert time.monotonic() - start < 3


@pytest.mark.parametrize("persistent", [True, False])
def test_connection_closed_before_reply(make_client, server, persistent):
    async def close_without_reply(ws):
        server.connections += 1
        async for message in ws:
            server.received.append(json.loads(message))
            await ws.close()
    server.custom_handler = close_without_reply
    rk, bulb = make_client(persistent=persistent)
    with pytest.raises(RokuUnavailable) as exc:
        bulb.turn_on()
    assert "before Roku confirmed" in str(exc.value)


def test_slow_reply_still_confirms(make_client, server):
    server.reply_delay = 1.0  # the bad-value reply took 1.8 s on real Roku
    rk, bulb = make_client()
    assert bulb.send("power", {"power": "on"})["type"] == "send_command_resp"


def test_replies_are_matched_to_the_right_command(make_client, server):
    # Many threads at once: each must get the reply to its own command.
    rk, _ = make_client()
    results, errors = {}, []

    def worker(level):
        try:
            results[level] = Device(rk, make_leaf()).send("brightness", {"level": level})
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    sent_ids = {m["payload"]["command"]["parameters"]["level"]: m["id"] for m in server.received}
    assert {lvl: r["id"] for lvl, r in results.items()} == sent_ids


def test_confirm_can_be_turned_off_per_command(make_client, server):
    server.reply = None  # would time out if we waited
    rk, bulb = make_client(reply_timeout=0.3)
    assert bulb.send("power", {"power": "on"}, confirm=False) is None


def test_confirmed_command_still_waits_before_close(make_client, server):
    # Roku's reply can come before the device has acted (on real hardware the
    # state update arrived 0.7 s after the reply), so closing still waits.
    server.process_delay = 0.4
    rk, bulb = make_client()
    bulb.turn_on()          # reply arrives immediately
    rk.close()
    assert len(server.wait_for_processed(1)) == 1


@pytest.mark.parametrize("persistent", [True, False])
def test_reply_before_processing_does_not_lose_command(make_client, server, persistent):
    server.process_delay = 0.4
    rk, bulb = make_client(persistent=persistent)
    bulb.turn_on()
    rk.close()
    assert len(server.wait_for_processed(1)) == 1


def test_cli_reports_command_failed(server, cookie_file, monkeypatch, capsys):
    from roku_smarthome import cli
    server.reply = "failed to execute command"
    rk = RokuSmartHome(cookie_file, timeout=3)
    rk.import_cookie_header("ks.session=abc")
    rk.socket_url = server.url
    rk._fetch_leaves = lambda: [make_leaf()]
    monkeypatch.setattr(cli, "RokuSmartHome", lambda cookies=None: rk)
    with pytest.raises(SystemExit) as exc:
        cli.main(["on", "bedroom"])
    assert exc.value.code == cli.EXIT_COMMAND_FAILED
    assert "COMMAND FAILED" in capsys.readouterr().err
