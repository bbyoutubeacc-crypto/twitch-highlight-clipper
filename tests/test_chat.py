from clipper.chat import emotes_from_tags, parse_irc_line, parse_privmsg
from clipper.utils import parse_channel

import pytest


def test_parse_privmsg_with_tags():
    line = (
        "@badge-info=;display-name=CoolUser;emotes=25:0-4,12-16;user-id=123 "
        ":cooluser!cooluser@cooluser.tmi.twitch.tv PRIVMSG #somechannel :Kappa hello Kappa"
    )
    msg = parse_privmsg(line, now=100.0)
    assert msg is not None
    assert msg.user == "cooluser"
    assert msg.display_name == "CoolUser"
    assert msg.text == "Kappa hello Kappa"
    assert msg.emotes == ["Kappa", "Kappa"]
    assert msg.timestamp == 100.0


def test_parse_action_and_escaped_tags():
    line = "@display-name=A\\sB :ab!ab@ab.tmi.twitch.tv PRIVMSG #x :\x01ACTION tanzt\x01"
    msg = parse_privmsg(line)
    assert msg.text == "tanzt"
    assert msg.display_name == "A B"


def test_non_privmsg():
    assert parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome") is None
    tags, prefix, cmd, params, trailing = parse_irc_line("PING :tmi.twitch.tv")
    assert cmd == "PING" and trailing == "tmi.twitch.tv"


def test_emotes_unicode_positions():
    text = "😂 Kappa"
    assert emotes_from_tags(text, "25:2-6") == ["Kappa"]


@pytest.mark.parametrize(
    "link,expected",
    [
        ("https://twitch.tv/Kanalname", "kanalname"),
        ("https://www.twitch.tv/some_user/", "some_user"),
        ("m.twitch.tv/abc123?referrer=raid", "abc123"),
        ("twitch.tv/xqc/videos", "xqc"),
        ("  shroud  ", "shroud"),
    ],
)
def test_parse_channel(link, expected):
    assert parse_channel(link) == expected


@pytest.mark.parametrize("bad", ["", "https://twitch.tv/", "https://twitch.tv/a b", "twitch.tv/directory"])
def test_parse_channel_invalid(bad):
    with pytest.raises(ValueError):
        parse_channel(bad)


def test_client_reconnects_after_disconnect():
    """Fake-IRC-Server trennt nach der ersten Nachricht – Client muss neu verbinden."""
    import socket
    import threading
    import time

    from clipper.chat import TwitchChatClient
    from clipper.config import ChatConfig

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    joins = []

    def serve():
        for round_no in range(2):
            conn, _ = srv.accept()
            f = conn.makefile("rb")
            while True:
                line = f.readline().decode()
                if not line:
                    break
                if line.startswith("JOIN"):
                    joins.append(line.strip())
                    conn.sendall(b":j!j@j JOIN #kanal\r\n")
                    conn.sendall(f":a!a@a PRIVMSG #kanal :hallo {round_no}\r\n".encode())
                    break
            if round_no == 1:
                time.sleep(1)
            f.close()
            conn.shutdown(socket.SHUT_RDWR)  # Verbindungsabbruch simulieren
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    received = []
    client = TwitchChatClient(
        "kanal", received.append,
        ChatConfig(host="127.0.0.1", port=srv.getsockname()[1], use_ssl=False, reconnect_max_delay=1),
    )
    client.start()
    deadline = time.time() + 10
    while time.time() < deadline and len(received) < 2:
        time.sleep(0.1)
    client.stop()
    assert [m.text for m in received] == ["hallo 0", "hallo 1"]
    assert len(joins) == 2 and joins[0] == "JOIN #kanal"
    assert client.reconnects >= 1
