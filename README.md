# Deltachat ⇆ Telegram Bridge Bot

A small bot that relays a Delta Chat group to a Telegram group, both ways.

## Why this exists

Delta Chat runs on email relays, so it keeps working on connections where most
messengers give up. Telegram is where everyone else already is. This bot sits
between the two: one Delta Chat account of its own, one Telegram group, and
messages crossing in both directions with the sender's name, replies and media
intact.

It was written for a Delta Chat group on an Iranian chatmail relay, with the
operator on Telegram behind a SOCKS5 proxy. There is nothing Iran specific in
the code, though. Any Delta Chat account and any Telegram bot will do.

## What it does

- Relays Delta Chat messages into the Telegram group, prefixed with the sender:
  `[Ali]:` for text, `[Ali] [تصویر]:` for media with a caption. That label is the
  Persian word for the media type
- Moves media both ways: photos, videos, GIFs, voice notes, audio, stickers,
  documents
- Keeps replies attached to the right message. Quote something in Telegram and
  it arrives as a quote in Delta Chat, and the other way round
- Only Telegram IDs listed in `TG_ADMIN_IDS` can send anything into Delta Chat.
  Everyone else in the Telegram group is a spectator
- Every message type has its own allow/block switch, see `IGNORE_*` in the
  config
- Telegram traffic can go through a SOCKS5 or HTTP proxy while the Delta Chat
  side connects directly

## How it works

```
   Delta Chat group
        |  direct, no proxy
   bridge account (deltachat-rpc-client + the Delta Chat core)
        |  through a SOCKS5 proxy if you set one
   your Telegram bot
        |
   admins in the Telegram group
```

The bot owns a Delta Chat account. On the first run it configures that account,
joins the group through your invite link, and from then on it just listens.
Under the hood it drives `deltachat-rpc-server`, the Delta Chat core, over
JSON-RPC on stdin/stdout. The Python side is only glue between that and
python-telegram-bot.

Two things are worth knowing before you run it. The core is a separate binary
that has to be installed (step 3 below), and this is a bridge, not an archive:
whatever arrives while it was offline shows up when it starts, but the group's
older history stays where it is.

## Requirements

- Python 3.10 or newer
- A Delta Chat account you are willing to dedicate to the bot, plus its IMAP and
  SMTP settings. A chatmail account works fine
- A bot token from [@BotFather](https://t.me/BotFather)
- A proxy, if the machine running the bot cannot reach Telegram directly

## Setup

### 1. Get the code

```bash
git clone https://github.com/Arshnme/Deltachat-Telegram-Bridge-Bot.git
cd Deltachat-Telegram-Bridge-Bot
```

### 2. Make a virtualenv

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
```

### 3. Install the Delta Chat core

The Python library is a client. It needs `deltachat-rpc-server` next to it, and
the bot looks for that binary in three places, in this order:

1. the path in `DC_RPC_SERVER_PATH`, if you set one
2. next to the Python interpreter of your virtualenv, so
   `venv/bin/deltachat-rpc-server`
3. anywhere in `PATH`

That second spot is why the easy way works so well:

```bash
pip install "deltachat-rpc-server>=2.69,<3"
```

This drops the binary into `venv/bin/` and you are done. It is already listed in
`requirements.txt`, so the next step installs it too. Check it with:

```bash
deltachat-rpc-server --version
```

If you would rather install it system wide, grab the Linux build from the
releases page ([github.com/chatmail/core/releases](https://github.com/chatmail/core/releases) —
the core lives in the `chatmail/core` repo now, it used to be
`deltachat/deltachat-core-rust`):

```bash
tar xzf deltachat-rpc-server-*.tar.gz
sudo mv deltachat-rpc-server /usr/local/bin/
sudo chmod +x /usr/local/bin/deltachat-rpc-server
deltachat-rpc-server --version
```

Any other location is fine too, just point at it from the config:

```python
DC_RPC_SERVER_PATH = "/opt/deltachat-telegram-bridge/bin/deltachat-rpc-server"
```

The core has nothing to do with Python. The bot starts it as a child process and
talks to it over stdin/stdout, and hands it `DC_ACCOUNTS_DIR` as the folder for
its database, keys and downloaded files.

### 4. Install the Python packages

```bash
pip install -r requirements.txt
```

### 5. Write the config

```bash
cp config.example.py config.py
nano config.py
```

| Setting | What it is |
|---|---|
| `TG_BOT_TOKEN` | the token from @BotFather |
| `TG_CHAT_ID` | the Telegram group to relay into. Group IDs are negative, like `-1001234567890` |
| `TG_ADMIN_IDS` | the numeric IDs allowed to send messages into Delta Chat |
| `PROXY_HOST`, `PROXY_PORT` | proxy for Telegram. Leave `PROXY_HOST` empty to connect directly |
| `PROXY_TYPE`, `PROXY_USER`, `PROXY_PASS` | `socks5` or `http`, and credentials if the proxy wants them |
| `DC_EMAIL`, `DC_PASSWORD` | the Delta Chat account. Used once, to configure it |
| `DC_IMAP_HOST`, `DC_SMTP_HOST` | mail servers for that account, ports are next to them |
| `DC_INVITE_LINK` | group invite link from Delta Chat. Only needed the first time |
| `DC_GROUP_NAME_HINT` | part of the group's name, used to find it in the chat list |
| `DC_DISPLAY_NAME` | the name the bridge shows in Delta Chat |
| `DC_RPC_SERVER_PATH` | path to the core binary. Leave empty to autodetect |
| `IGNORE_*` | one switch per message type. `True` means "don't relay it" |

`config.py` is in `.gitignore`. Still, treat it like a password file, because
that is what it is.

If you are upgrading from an older copy of this bot, start from the fresh
`config.example.py` and move your values in. Two keys (`DC_RPC_SERVER_PATH` and
`DC_DISPLAY_NAME`) did not exist before.

### 6. Find the Telegram group ID

Add the bot to the group, send a message there, then open:

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Copy the number after `"chat": {"id":` into `TG_CHAT_ID`.

### 7. Run it

```bash
python bot.py
```

The first run creates and configures the Delta Chat account and joins the group
if it is not in it yet. Later runs skip straight to relaying.

```
2026-11-12 10:00:01 - bridge - INFO - پروکسی تلگرام: socks5://127.0.0.1:1070
2026-11-12 10:00:03 - bridge - INFO - اکانت موجود بارگذاری شد.
2026-11-12 10:00:04 - bridge - INFO - گروه «My Group» پیدا شد.
2026-11-12 10:00:04 - bridge - INFO - ربات پل آماده است.
```

The log lines are in Persian, same as the labels the bot puts on relayed
messages. The code and the config comments are Persian too. The README is in
English because you are reading it.

## Running it as a service

Create a system user for it and give it the folder:

```bash
sudo useradd --system --home /opt/deltachat-telegram-bridge --shell /usr/sbin/nologin bridge
sudo mkdir -p /opt/deltachat-telegram-bridge
sudo cp -r . /opt/deltachat-telegram-bridge/
sudo chown -R bridge:bridge /opt/deltachat-telegram-bridge
```

Then the unit file:

```bash
sudo cp deploy/deltachat-telegram-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deltachat-telegram-bridge
```

Logs and status:

```bash
systemctl status deltachat-telegram-bridge
journalctl -u deltachat-telegram-bridge -f
```

`ExecStart` points straight at the virtualenv's Python, so the core next to it
gets found without depending on the system `PATH`. If the service cannot start,
check `journalctl` first. Permission problems on `dc_data/` show up there as an
account error from the core.

## Using it

Reply to a relayed message in Telegram and your answer lands as a reply in Delta
Chat. Write a normal message and it goes as a normal message. Send a photo or a
file and it arrives as one, caption included.

Only the IDs in `TG_ADMIN_IDS` are let through. Everyone else can read the
Telegram group but nothing they write reaches Delta Chat.

## Things it does not do

- No history. Messages from before the bot first joined are not replayed
- Locations arrive as a `[Ali] [لوکیشن]` notice without coordinates, because
  the core does not expose them in a message snapshot
- Stickers and voice notes that Telegram refuses go out as plain documents
  instead of failing
- Telegram caps bot downloads at 20 MB by default, so bigger files coming from
  Telegram are dropped. The log says so
- A caption longer than 1024 characters is sent as a separate message right
  under the media
- One Delta Chat group per bot instance

## When something breaks

**`FileNotFoundError: دلتاچتکور پیدا نشد`**
The core is not installed. Run `pip install deltachat-rpc-server` inside the
virtualenv, or set `DC_RPC_SERVER_PATH`.

**`فایل config.py پیدا نشد`**
You skipped step 5. `cp config.example.py config.py` and fill it in.

**The bot cannot reach Telegram**
Set the proxy in `config.py`. To check the proxy itself is alive:

```bash
curl -x socks5://127.0.0.1:1070 https://api.telegram.org
```

**It will not join the Delta Chat group**
Invite links expire. Take a fresh one from Delta Chat. If the account is already
in the group, no link is needed at all, and `DC_GROUP_NAME_HINT` just has to
match part of the group name.

**No messages come through**
Look at the log. If the core could not configure the account, the reason is
there. Do not delete `dc_data/` to "fix" it, that folder is the account:
database, keys and all.

**The same message shows up twice**
Two copies of the bot are running. Check `systemctl status` and your Python
processes.

## Files

```
bot.py                                      the bot
config.example.py                           copy this to config.py
requirements.txt                            deps, core included
deploy/deltachat-telegram-bridge.service    systemd unit
dc_data/                                    Delta Chat account data (created at runtime, not committed)
media_tmp/                                  scratch space for Telegram uploads (created at runtime)
```

## License

MIT
