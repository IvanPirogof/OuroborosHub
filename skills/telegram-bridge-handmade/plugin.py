"""Telegram duplex bridge extension.

Incoming: a background polling thread calls Telegram getUpdates and injects
each message into the Ouroboros chat via Host Service POST /chat/inject.
Documents (PDF), audio, and voice are downloaded via getFile to the skill's
state dir (incoming/telegram/) and a notification is injected.

Outgoing: whenever the agent replies, the host publishes chat.outbound on the
event bus. We are subscribed and forward the text to Telegram via Bot API
sendMessage (chat TELEGRAM_CHAT_ID), split into <=4096-char segments.

Outgoing files (v1.3.0): the bridge registers a namespaced ``send_file`` tool so
the agent can deliver a produced artifact (xlsx, pdf, lecture markdown, patch)
to the owner chat as a real Telegram document via Bot API sendDocument. Before
this, that needed a hand-written side script calling sendDocument directly,
which bypassed the reviewed skill. See the design notes above ``_send_document``.

Registration is fail-closed: a missing TELEGRAM_BOT_TOKEN, a getMe failure, or
a missing/non-numeric TELEGRAM_CHAT_ID raises ExtensionRegistrationError, so
the bridge surfaces as a load error instead of silently staying offline.

Runs inside the server process (in-process extension), so it survives task
boundaries and restarts together with the server (owner requirement).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.request
from collections import deque

from ouroboros.contracts.plugin_api import ExtensionRegistrationError

POLL_INTERVAL_SEC = 2.0
TG_API_BASE = "https://api.telegram.org/bot"
_MAX_SEGMENT = 4096
SLASH_RE = re.compile(r"^\s*/[A-Za-z]")
# Slash commands can also arrive embedded after a label ("[Telegram] @user: /help").
# The previous pattern r".*:\s*/[A-Za-z]" matched ANY colon, so ordinary text like
# "Смотри: /home/rdp/report.xlsx" or "path: /etc/hosts" was silently dropped as a
# command. Only command-SHAPED tails count now: one command word, optionally a few
# whitespace-separated args, and no further path separators. Note the sender label is
# carried in a separate payload field, so it never appears in this text — the pattern
# guards the case where the label is already part of the message body. The host service
# still rejects reserved commands, so this remains defense-in-depth.
_EMBEDDED_SLASH_RE = re.compile(r"^[^:]{0,80}:\s*/[A-Za-z][\w-]{0,20}(?:\s+[^\s/\\]+){0,3}$")
# Local injection rate limit: at most MAX_INJECT_PER_WINDOW injects in the
# trailing window before we drop excess traffic (defense-in-depth; the host
# service has its own guard, but a network transport must backpressure too).
MAX_INJECT_PER_WINDOW = 10
INJECT_WINDOW_SEC = 10.0
_inject_timestamps = deque()

# Thread safety for _state accessed from main thread, poller thread, and event handlers.
_state_lock = threading.RLock()

_state = {}


def _get_state(key: str, default=None):
    """Thread-safe state read."""
    with _state_lock:
        return _state.get(key, default)


def _set_state(key: str, value) -> None:
    """Thread-safe state write."""
    with _state_lock:
        _state[key] = value


def _log(api, level: str, message: str) -> None:
    try:
        api.log(level, message)
    except Exception:
        pass


def _emit_lifecycle(api, event: str, detail: str = "") -> None:
    """Record a durable lifecycle transition in the skill log.

    completion_notification (Skill Review Checklist item 16): the bridge is a
    long-running background process, so start/stop/error transitions are
    persisted through api.log (durable on-disk skill log), not a non-durable
    WS broadcast.
    """
    _log(api, "info", f"lifecycle:{event}" + (f" — {detail}" if detail else ""))


def _load_token(api) -> str:
    try:
        return str(
            api.get_settings(["TELEGRAM_BOT_TOKEN"]).get("TELEGRAM_BOT_TOKEN") or ""
        ).strip()
    except Exception:
        return ""


def _host_service_url(api) -> str:
    # The host service port is exported to env (config.apply_settings_to_env) and
    # this in-process extension shares the server process environment.
    try:
        port = int(os.environ.get("OUROBOROS_HOST_SERVICE_PORT") or 8767)
    except (TypeError, ValueError):
        port = 8767
    return f"http://127.0.0.1:{port}"


def _hs_post(api, path: str, payload: dict) -> dict:
    token = api.get_skill_token().use_in_request()
    req = urllib.request.Request(
        f"{_host_service_url(api)}{path}",
        data=__import__("json").dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Skill-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return __import__("json").loads(resp.read().decode("utf-8"))


def _inject_allow() -> bool:
    """Return True if injecting another message stays under the local rate cap.

    Guarded by _state_lock because _inject_timestamps is read/modified from the
    poller thread and the event-handler threads concurrently (Skill Review
    Checklist: benign data race on the rate-limit deque — worst case previously
    a slightly exceeded cap or a corrupted window).
    """
    now = time.monotonic()
    with _state_lock:
        while _inject_timestamps and now - _inject_timestamps[0] > INJECT_WINDOW_SEC:
            _inject_timestamps.popleft()
        if len(_inject_timestamps) >= MAX_INJECT_PER_WINDOW:
            return False
        _inject_timestamps.append(now)
        return True


def _inject_to_chat(api, text: str, sender_label: str) -> None:
    if _is_blocked_command(text):
        return
    if not _inject_allow():
        _log(api, "warning", "inject rate limit hit; dropping message")
        return
    payload = {
        "text": text,
        "chat_id": 1,
        "user_id": 1,
        "sender_label": sender_label,
        "transport": {"telegram": True},
    }
    try:
        _hs_post(api, "/chat/inject", payload)
    except Exception as exc:
        _log(api, "warning", f"inject failed: {exc}")


def _is_blocked_command(text: str) -> bool:
    """Reject slash-command-shaped input before injection. Defense-in-depth:
    the host service also rejects reserved commands, but a network transport
    must not let owner-like slash input reach the chat at all."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if SLASH_RE.match(stripped):
        return True
    # Also reject messages that carry an embedded leading slash after a
    # formatted source prefix (e.g. "[Telegram] @user: /help").
    if _EMBEDDED_SLASH_RE.match(stripped):
        return True
    return False


def _split_segments(text: str, limit: int = _MAX_SEGMENT):
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    out = []
    while text:
        take = text[:limit]
        nl = take.rfind("\n")
        sp = take.rfind(" ")
        split_at = nl if (nl > 0 and nl > limit - 300) else sp
        if split_at > 0:
            out.append(take[:split_at])
            text = text[split_at:].lstrip()
        else:
            out.append(take)
            text = text[limit:]
    return out


def _send_request(url: str, payload: dict) -> dict:
    data = __import__("json").dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return __import__("json").loads(resp.read().decode("utf-8"))


def _get_incoming_dir(api) -> str:
    """Get the incoming directory under the skill's state dir (write confinement).

    Write-path confinement (Skill Review Checklist item 4): all file writes
    must stay inside the dedicated skill state dir. api.get_state_dir() is part
    of the frozen PluginAPI contract and always returns the per-skill state
    directory; there is no raw relative-path fallback.
    """
    state_dir = api.get_state_dir()
    incoming_dir = os.path.join(state_dir, "incoming", "telegram")
    os.makedirs(incoming_dir, exist_ok=True)
    return incoming_dir


def _download_file(api, file_id: str, filename: str) -> str:
    """Download a file from Telegram using getFile API and save to skill's incoming dir."""
    token = _load_token(api)
    if not token:
        return ""
    try:
        # Get file path
        req = urllib.request.Request(
            f"{TG_API_BASE}{token}/getFile?file_id={file_id}", method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            _log(api, "warning", f"getFile failed: {str(data)[:160]}")
            return ""
        file_path = data["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        
        # Get incoming directory from skill's state dir
        incoming_dir = _get_incoming_dir(api)
        
        # Generate safe filename
        safe_name = re.sub(r"[^\w\-.]", "_", filename)
        save_path = os.path.join(incoming_dir, f"{int(time.time())}_{safe_name}")
        
        # Download file
        with urllib.request.urlopen(file_url, timeout=30) as resp:
            content = resp.read()
        
        with open(save_path, "wb") as f:
            f.write(content)
        
        _log(api, "info", f"downloaded file: {save_path} ({len(content)} bytes)")
        
        # Cleanup old files (keep last 50 files)
        _cleanup_old_files(api, incoming_dir, keep=50)
        
        return save_path
    except Exception as exc:
        _log(api, "warning", f"download file error: {exc}")
        return ""


def _cleanup_old_files(api, directory: str, keep: int = 50) -> None:
    """Remove oldest files in directory, keeping only the most recent `keep` files."""
    try:
        files = []
        for entry in os.scandir(directory):
            if entry.is_file():
                files.append((entry.stat().st_mtime, entry.path))
        if len(files) > keep:
            files.sort(key=lambda x: x[0])
            for _, path in files[:-keep]:
                try:
                    os.remove(path)
                    _log(api, "info", f"cleaned up old file: {path}")
                except Exception:
                    pass
    except Exception as exc:
        _log(api, "warning", f"cleanup error: {exc}")


# ---------------------------------------------------------------------------
# MTProto (Telethon) fallback — files larger than the Bot API 20 MB getFile cap.
# Bots can use MTProto with the same bot token; download limit is up to 2 GB.
# Requires TELEGRAM_API_ID / TELEGRAM_API_HASH in settings. When missing, large
# files are reported to the chat instead of silently failing.
BOT_API_FILE_LIMIT = 20 * 1024 * 1024

_mt_lock = threading.Lock()
_mt_client = None
_mt_loop = None


def _mt_credentials(api):
    try:
        creds = api.get_settings(["TELEGRAM_API_ID", "TELEGRAM_API_HASH"])
    except Exception:
        return 0, ""
    try:
        api_id = int(str(creds.get("TELEGRAM_API_ID") or "").strip() or 0)
    except (TypeError, ValueError):
        api_id = 0
    api_hash = str(creds.get("TELEGRAM_API_HASH") or "").strip()
    return api_id, api_hash


def _mt_start(api):
    """Start (once per process) a persistent Telethon client on a dedicated loop."""
    global _mt_client, _mt_loop
    with _mt_lock:
        if _mt_client is not None:
            return _mt_client
        try:
            from telethon import TelegramClient
        except ImportError:
            _log(api, "warning", "telethon not installed — MTProto downloads disabled")
            return None
        api_id, api_hash = _mt_credentials(api)
        if not api_id or not api_hash:
            _log(
                api,
                "warning",
                "TELEGRAM_API_ID/TELEGRAM_API_HASH not set — MTProto downloads disabled",
            )
            return None
        session_path = os.path.join(api.get_state_dir(), "mtproto")
        _mt_loop = asyncio.new_event_loop()
        threading.Thread(
            target=_mt_loop.run_forever,
            daemon=True,
            name="telegram-bridge-handmade-mtproto",
        ).start()
        try:
            token = _load_token(api)
            client = TelegramClient(session_path, api_id, api_hash, loop=_mt_loop)

            async def _bootstrap():
                # Telethon's client.start() is synchronous in 1.44 and cannot be
                # submitted via run_coroutine_threadsafe. Drive the async
                # connect + sign_in path directly on the dedicated loop instead.
                await client.connect()
                if not await client.is_user_authorized():
                    await client.sign_in(bot_token=token)
                return client

            asyncio.run_coroutine_threadsafe(_bootstrap(), _mt_loop).result(timeout=60)
            _mt_client = client
            _log(api, "info", "MTProto client started")
            return client
        except Exception as mt_exc:
            _log(api, "warning", f"MTProto client start failed: {mt_exc}")
            # Cleanup the orphaned event loop thread
            if _mt_loop is not None and _mt_loop.is_running():
                _mt_loop.call_soon_threadsafe(_mt_loop.stop)
            return None


def _mt_shutdown(api) -> None:
    """Tear down the persistent MTProto client and its loop on unload.

    _mt_start() keeps one Telethon client and one asyncio loop alive for the
    whole server process. Nothing used to stop them: _stop() only set the poller
    stop flag, so every extension disable/reload left a connected Telegram
    session and a running loop behind — a leak that compounds in the long-lived
    server process. Called from on_unload, bounded so it cannot wedge unload.
    """
    global _mt_client, _mt_loop
    with _mt_lock:
        client, loop = _mt_client, _mt_loop
        _mt_client = None
        _mt_loop = None
    if client is not None and loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(client.disconnect(), loop).result(timeout=15)
            _log(api, "info", "MTProto client disconnected")
        except Exception as exc:
            _log(api, "warning", f"MTProto client disconnect failed: {exc}")
    if loop is not None and loop.is_running():
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass


def _mtproto_download(api, message_id: int) -> str:
    """Download media from a specific message via MTProto into the incoming dir.

    Uses GetMessagesRequest (not get_messages / GetHistoryRequest) because
    bot tokens cannot call GetHistoryRequest — that method raises
    BotMethodInvalidError. GetMessagesRequest with explicit message IDs
    works for bots and allows downloading files >20 MB (up to 2 GB).

    Write confinement (checklist item 4): Telethon saves directly under
    api.get_state_dir()/incoming/telegram/, same as the Bot API path.
    """
    client = _mt_start(api)
    if client is None:
        return ""

    async def _work():
        from telethon.tl.functions.messages import GetMessagesRequest
        from telethon.tl.types import InputMessageID

        result = await client(GetMessagesRequest(id=[InputMessageID(message_id)]))
        if not result or not result.messages:
            return ""
        msg = result.messages[0]
        if not msg or not msg.media:
            return ""
        return await client.download_media(msg, file=_get_incoming_dir(api)) or ""

    try:
        path = asyncio.run_coroutine_threadsafe(_work(), _mt_loop).result(timeout=1800)
    except Exception as exc:
        _log(api, "warning", f"MTProto download error: {exc}")
        return ""
    if path:
        try:
            size = os.path.getsize(path)
            _log(api, "info", f"MTProto downloaded: {path} ({size} bytes)")
            _cleanup_old_files(api, _get_incoming_dir(api), keep=50)
        except OSError:
            pass
    return path


def _mt_download_and_notify(api, chat_id: int, message_id: int, file_name: str,
                            file_size: int, emoji: str, kind: str,
                            sender_label: str) -> None:
    """Worker-thread body: MTProto download + chat notification.

    Catches everything so a daemon-thread crash never goes silent.
    """
    try:
        save_path = _mtproto_download(api, message_id)
        if not save_path:
            _inject_to_chat(
                api,
                f"{emoji} {kind.capitalize()} {file_name} ({file_size} bytes) exceeds "
                f"the 20 MB Bot API limit and MTProto download failed "
                f"(check TELEGRAM_API_ID/TELEGRAM_API_HASH settings).",
                sender_label,
            )
            return
        _inject_to_chat(
            api,
            f"{emoji} {kind.capitalize()} received: {file_name} "
            f"({file_size} bytes)\nSaved to: {save_path}",
            sender_label,
        )
    except Exception as exc:
        try:
            _log(api, "error", f"MTProto worker thread crashed: {exc}")
        except Exception:
            pass
        try:
            _inject_to_chat(
                api,
                f"⚠️ {kind.capitalize()} {file_name} ({file_size} bytes) — "
                f"MTProto download error: {exc}",
                sender_label,
            )
        except Exception:
            pass


def _handle_media_file(api, chat_id: int, message_id: int, file_id, file_name: str,
                       file_size: int, mime_type: str, emoji: str, kind: str,
                       sender_label: str) -> None:
    """Route one media attachment: Bot API when small, MTProto above 20 MB.

    Large transfers run on their own daemon thread so the getUpdates poller
    keeps processing incoming messages during long downloads.
    """
    _log(
        api,
        "info",
        f"received {kind}: {file_name} ({mime_type}, {file_size} bytes)",
    )
    if file_size and file_size > BOT_API_FILE_LIMIT:
        threading.Thread(
            target=_mt_download_and_notify,
            args=(api, chat_id, message_id, file_name, file_size, emoji, kind,
                  sender_label),
            daemon=True,
            name=f"telegram-bridge-mtproto-{message_id}",
        ).start()
        return
    save_path = ""
    if file_id:
        save_path = _download_file(api, file_id, file_name)
    if save_path:
        _inject_to_chat(
            api,
            f"{emoji} {kind.capitalize()} received: {file_name} ({mime_type}, "
            f"{file_size} bytes)\nSaved to: {save_path}",
            sender_label,
        )
    elif file_id:
        _inject_to_chat(
            api,
            f"⚠️ {kind.capitalize()} {file_name} ({file_size} bytes) — получен, "
            f"но не удалось скачать (ошибка Bot API).",
            sender_label,
        )


def _send_to_telegram(api, text: str) -> None:
    token = _load_token(api)
    if not token:
        return
    chat_id = _get_state("chat_id")
    if not chat_id:
        _log(api, "warning", "chat_id not configured — dropping outbound message")
        return
    for segment in _split_segments(text):
        if not segment:
            continue
        url = f"{TG_API_BASE}{token}/sendMessage"
        try:
            body = _send_request(url, {"chat_id": chat_id, "text": segment})
            if not body.get("ok"):
                _log(api, "warning", f"sendMessage rejected: {str(body)[:160]}")
        except Exception as exc:
            _log(api, "warning", f"sendMessage error: {exc}")


def _load_chat_id(api) -> int:
    """Read the owner chat id from settings. Fail-closed when missing/non-numeric."""
    try:
        raw = str(api.get_settings(["TELEGRAM_CHAT_ID"]).get("TELEGRAM_CHAT_ID") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        raise ExtensionRegistrationError(
            "TELEGRAM_CHAT_ID is empty — cannot route owner traffic (fail-closed)"
        )
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ExtensionRegistrationError(f"TELEGRAM_CHAT_ID is not numeric: {raw!r}")


def _send_chat_action(api, action: str) -> None:
    token = _load_token(api)
    if not token or not action:
        return
    chat_id = _get_state("chat_id")
    if not chat_id:
        return
    try:
        _send_request(
            f"{TG_API_BASE}{token}/sendChatAction",
            {"chat_id": chat_id, "action": action},
        )
    except Exception:
        pass


def _incoming_loop(api, token: str) -> None:
    offset = 0
    # NOTE: token is re-read every poll iteration below. Baking api_base here
    # once stranded the poller on a revoked token after rotation (401 loop).

    _emit_lifecycle(api, "started", "incoming poller started")
    
    while not _get_state("stop"):
        try:
            current_token = _load_token(api) or token
            api_base = f"{TG_API_BASE}{current_token}"
            url = f"{api_base}/getUpdates?offset={offset}&timeout=15"
            with urllib.request.urlopen(url, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                time.sleep(10)
                continue
            for upd in data.get("result", []):
                update_id = int(upd.get("update_id") or 0)
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    # Source minimization: only accept traffic from the owner chat id.
                    # Any other chat/user reaching the bot is dropped here, before
                    # text is evaluated or injected.
                    owner_chat_id = _get_state("chat_id")
                    if not owner_chat_id:
                        _log(api, "warning", "chat_id not configured — dropping incoming update")
                        offset = max(offset, update_id + 1)
                        continue
                    try:
                        incoming_chat_id = int(msg.get("chat", {}).get("id") or 0)
                    except (TypeError, ValueError):
                        incoming_chat_id = 0
                    if incoming_chat_id != owner_chat_id:
                        _log(
                            api,
                            "warning",
                            f"dropping message from non-owner chat {incoming_chat_id} "
                            f"(owner={owner_chat_id})",
                        )
                        offset = max(offset, update_id + 1)
                        continue
                    
                    from_user = (
                        msg.get("from", {}).get("username")
                        or msg.get("from", {}).get("first_name")
                        or "unknown"
                    )
                    
                    # Handle text/caption
                    text = str(msg.get("text") or msg.get("caption") or "").strip()
                    if text:
                        if _is_blocked_command(text):
                            _log(api, "warning", f"blocked dangerous command from @{from_user}")
                        else:
                            _inject_to_chat(api, text, f"[Telegram] @{from_user}")
                            _log(api, "info", f"injected [{update_id}] @{from_user}: {text[:80]}")
                    
                    # Handle document (PDF, etc.)
                    document = msg.get("document")
                    if document:
                        _handle_media_file(
                            api, incoming_chat_id, msg.get("message_id") or 0,
                            document.get("file_id"),
                            document.get("file_name") or "document",
                            document.get("file_size") or 0,
                            document.get("mime_type") or "unknown",
                            "📎", "document", f"[Telegram] @{from_user}",
                        )

                    # Handle audio
                    audio = msg.get("audio")
                    if audio:
                        _handle_media_file(
                            api, incoming_chat_id, msg.get("message_id") or 0,
                            audio.get("file_id"),
                            audio.get("file_name") or "audio",
                            audio.get("file_size") or 0,
                            audio.get("mime_type") or "unknown",
                            "🎵", "audio", f"[Telegram] @{from_user}",
                        )

                    # Handle voice messages
                    voice = msg.get("voice")
                    if voice:
                        _handle_media_file(
                            api, incoming_chat_id, msg.get("message_id") or 0,
                            voice.get("file_id"),
                            f"voice_{update_id}.ogg",
                            voice.get("file_size") or 0,
                            voice.get("mime_type") or "audio/ogg",
                            "🎤", "voice", f"[Telegram] @{from_user}",
                        )

                    # Handle photos (largest size for best quality)
                    photo = msg.get("photo")
                    if photo:
                        biggest = photo[-1] if isinstance(photo, list) and photo else {}
                        _handle_media_file(
                            api, incoming_chat_id, msg.get("message_id") or 0,
                            biggest.get("file_id"),
                            f"photo_{update_id}.jpg",
                            biggest.get("file_size") or 0,
                            "image/jpeg",
                            "🖼️", "photo", f"[Telegram] @{from_user}",
                        )

                    # Handle video files
                    video = msg.get("video")
                    if video:
                        _handle_media_file(
                            api, incoming_chat_id, msg.get("message_id") or 0,
                            video.get("file_id"),
                            video.get("file_name") or f"video_{update_id}.mp4",
                            video.get("file_size") or 0,
                            video.get("mime_type") or "video/mp4",
                            "🎬", "video", f"[Telegram] @{from_user}",
                        )
                    
                offset = max(offset, update_id + 1)
            time.sleep(POLL_INTERVAL_SEC)
        except Exception as exc:
            _log(api, "warning", f"poll loop error: {exc}")
            _emit_lifecycle(api, "error", f"poll loop error: {exc}")
            time.sleep(5)
    
    _emit_lifecycle(api, "stopped", "incoming poller stopped")


def _send_photo_to_telegram(api, image_b64: str, caption: str = "", mime: str = "image/png") -> None:
    """Send a base64-encoded image to the owner chat via Bot API sendPhoto.

    Multipart/form-data is built by hand (stdlib only) so the bridge keeps its
    zero-third-party-dependency footprint. Triggered by the chat.photo event
    that the agent's send_photo tool publishes, mirroring photo delivery to
    Telegram exactly like outbound text.
    """
    if not image_b64:
        return
    token = _load_token(api)
    if not token:
        return
    chat_id = _get_state("chat_id")
    if not chat_id:
        _log(api, "warning", "chat_id not configured — dropping outbound photo")
        return
    import base64
    try:
        photo_bytes = base64.b64decode(image_b64)
    except Exception as exc:
        _log(api, "warning", f"photo base64 decode error: {exc}")
        return
    if not photo_bytes:
        _log(api, "warning", "photo payload decoded to empty bytes — dropping")
        return
    boundary = "----OuroborosBridge" + str(int(time.time() * 1000))
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
        f"{chat_id}\r\n".encode("utf-8")
    ]
    if caption:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n"
            f"{caption}\r\n".encode("utf-8")
        )
    filename = f"photo_{int(time.time())}.jpg"
    content_type = str(mime or "image/jpeg")
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = b"\r\n--" + boundary.encode("utf-8") + b"--\r\n"
    body = b"".join(parts) + head + photo_bytes + tail
    req = urllib.request.Request(
        f"{TG_API_BASE}{token}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        if not out.get("ok"):
            _log(api, "warning", f"sendPhoto rejected: {str(out)[:160]}")
    except Exception as exc:
        _log(api, "warning", f"sendPhoto error: {exc}")


def _on_outbound(api, payload) -> None:
    """Handler for chat.outbound — mirror agent text replies to Telegram.

    This handler is required by the `chat.outbound` subscription in
    register(); it mirrors outgoing agent text into Telegram via sendMessage
    (split into <=4096-char segments). Telegram-transport echoes are skipped so
    content that arrived from Telegram is not re-broadcast back (no echo loop),
    mirroring the transport guard in _on_photo.
    """
    if _get_state("stop"):
        return
    try:
        transport = payload.get("transport") or {}
        if isinstance(transport, dict) and transport.get("telegram"):
            return None
        text = str(payload.get("text") or "").strip()
        if not text:
            return None
        _send_to_telegram(api, text)
    except Exception as exc:
        _log(api, "warning", f"outbound handler error: {exc}")
    return None


def _on_photo(api, payload) -> None:
    """Handler for chat.photo — mirror agent-delivered photos to Telegram."""
    if _get_state("stop"):
        return
    try:
        image_b64 = str(payload.get("image_base64") or "").strip()
        if not image_b64:
            return None
        caption = str(payload.get("caption") or "").strip()
        mime = str(payload.get("mime") or "image/jpeg")
        # Skip re-broadcasts of Telegram-originated media to avoid an echo loop.
        transport = payload.get("transport") or {}
        if isinstance(transport, dict) and transport.get("telegram"):
            return None
        _send_photo_to_telegram(api, image_b64, caption, mime)
    except Exception as exc:
        _log(api, "warning", f"photo outbound handler error: {exc}")
    return None


def _on_typing(api, payload) -> None:
    if _get_state("stop"):
        return
    try:
        action = str(payload.get("action") or "").strip()
        if action:
            _send_chat_action(api, action)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Outgoing documents (agent -> Telegram). v1.3.0.
#
# The bridge mirrored text (chat.outbound) and photos (chat.photo) but had no
# way to deliver an arbitrary file the agent produced (xlsx, pdf, lecture
# markdown, patch). That used to need a hand-written side script calling Bot API
# sendDocument directly, which bypassed the reviewed skill entirely. This turns
# the capability into a reviewed, namespaced tool.
#
# Deliberate design decisions (rationale also mirrored in SKILL.md):
# - Tool only, never an HTTP route. A route would make "upload any local path to
#   Telegram" reachable from the browser/API surface, i.e. a generic
#   exfiltration primitive. The tool is callable only from inside the agent loop
#   and its destination is hard-bound to the owner's TELEGRAM_CHAT_ID.
# - The read path is caller-provided (the agent must be able to send files it
#   just wrote under task_drive/artifact_store/user_files), so skill-dir
#   confinement does not apply here. Instead there is an explicit sensitive-path
#   denylist so an injected instruction cannot talk the bridge into uploading
#   owner secrets, session files, or control-plane state.
# - Bot API sendDocument caps uploads at 50 MB. Oversized files fail loudly with
#   an actionable message; they are never truncated or silently dropped.
BOT_API_DOCUMENT_LIMIT = 50 * 1024 * 1024
_MAX_CAPTION_LEN = 1024

_SENSITIVE_SEND_BASENAMES = frozenset({
    "settings.json", "auth_token.json", "grants.json", "review.json",
    "enabled.json", "credentials.json", "service-account.json",
    "secrets.json", "secrets.yaml", "secrets.yml",
    ".git-credentials", ".netrc", ".npmrc", ".pypirc",
})
_SENSITIVE_SEND_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".session", ".env",
)
_SENSITIVE_SEND_PATH_FRAGMENTS = (
    "/data/state/", "/.ssh/", "/.aws/", "/.gnupg/",
)


def _is_sensitive_send_path(path: str) -> bool:
    """True when the path looks like owner secrets or control-plane state."""
    text = str(path or "").replace("\\", "/")
    base = os.path.basename(text).lower()
    if not base:
        return True
    if base in _SENSITIVE_SEND_BASENAMES:
        return True
    if base.startswith(".env") or base.startswith("id_rsa"):
        return True
    if base.startswith("authorized_keys"):
        return True
    if base.endswith(_SENSITIVE_SEND_SUFFIXES):
        return True
    return any(fragment in text for fragment in _SENSITIVE_SEND_PATH_FRAGMENTS)


def _safe_document_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\-.]", "_", str(name or "").strip()) or "file"
    return cleaned[:120]


def _build_multipart_document(chat_id, path: str, filename: str, caption: str):
    """Build a stdlib multipart/form-data body for Bot API sendDocument."""
    name = _safe_document_name(filename or os.path.basename(path))
    with open(path, "rb") as handle:
        payload = handle.read()
    boundary = "----OuroborosBridgeDoc" + str(int(time.time() * 1000))
    head = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
        f"{chat_id}\r\n"
    ]
    if caption:
        head.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n"
            f"{caption}\r\n"
        )
    head.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
    )
    pre = "".join(head).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return pre + payload + tail, boundary, name


def _send_document(api, path: str, caption: str = "", filename: str = "") -> str:
    """Send one local file to the owner chat via Bot API sendDocument.

    Returns a human-readable status string (tool handlers return text, never
    raise, so the agent gets an actionable message instead of a stack trace).
    """
    raw_path = os.path.expanduser(str(path or "").strip())
    if not raw_path:
        return "⚠️ send_file: 'path' is required."
    # Resolve symlinks BEFORE the denylist check. abspath() does not follow
    # links, so a path like report.xlsx -> data/settings.json used to sail
    # through every basename/suffix/fragment test and upload owner secrets.
    # Checking both the caller-supplied form and the fully resolved target
    # closes that bypass (and neutralises the '..'-spelled variant too).
    resolved = os.path.realpath(raw_path)
    if _is_sensitive_send_path(raw_path) or _is_sensitive_send_path(resolved):
        return (
            "⚠️ send_file refused: this path (or its symlink target) looks like owner "
            "secrets or control-plane state (settings, tokens, MTProto session, "
            "credentials). The bridge will not upload it."
        )
    if not os.path.isfile(resolved):
        return f"⚠️ send_file: not a regular file: {resolved}"
    try:
        size = os.path.getsize(resolved)
    except OSError as exc:
        return f"⚠️ send_file: cannot stat {resolved}: {exc}"
    if size <= 0:
        return f"⚠️ send_file: refusing to send an empty file: {resolved}"
    if size > BOT_API_DOCUMENT_LIMIT:
        return (
            f"⚠️ send_file: {os.path.basename(resolved)} is {size} bytes, above the "
            f"Bot API sendDocument upload cap ({BOT_API_DOCUMENT_LIMIT} bytes = 50 MB). "
            "The bridge has no MTProto upload path yet — reduce the file, split it, "
            "or send a link instead."
        )
    token = _load_token(api)
    if not token:
        return "⚠️ send_file: TELEGRAM_BOT_TOKEN is not configured."
    chat_id = _get_state("chat_id")
    if not chat_id:
        return "⚠️ send_file: chat_id is not configured — nowhere to send."
    note = ""
    text = str(caption or "").strip()
    if len(text) > _MAX_CAPTION_LEN:
        note = f"\n⚠️ caption truncated from {len(text)} to {_MAX_CAPTION_LEN} chars."
        text = text[:_MAX_CAPTION_LEN]
    body, boundary, sent_name = _build_multipart_document(chat_id, resolved, filename, text)
    req = urllib.request.Request(
        f"{TG_API_BASE}{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _log(api, "warning", f"sendDocument error: {exc}")
        return f"⚠️ send_file: sendDocument failed: {exc}"
    if not out.get("ok"):
        description = str(out.get("description") or out)[:200]
        _log(api, "warning", f"sendDocument rejected: {description}")
        return f"⚠️ send_file: Telegram rejected the upload: {description}"
    message_id = (out.get("result") or {}).get("message_id")
    _log(api, "info", f"sendDocument ok: {sent_name} ({size} bytes) message_id={message_id}")
    return (
        f"✅ Sent {sent_name} ({size} bytes) to Telegram chat {chat_id} "
        f"(message_id={message_id}).{note}"
    )


def _make_send_file_handler(api):
    """Bind the PluginAPI into an async, timeout-bounded send_file handler.

    Async on purpose: the extension dispatcher runs async handlers on a bounded
    helper thread with ``timeout_sec``, so a slow upload cannot wedge the worker
    the way an unbounded synchronous handler would.
    """

    async def _tool_send_file(ctx=None, path: str = "", caption: str = "",
                              filename: str = "") -> str:
        return await asyncio.to_thread(_send_document, api, path, caption, filename)

    return _tool_send_file


def register(api):
    token = _load_token(api)
    if not token:
        raise ExtensionRegistrationError(
            "TELEGRAM_BOT_TOKEN is empty — bridge cannot start (fail-closed)"
        )

    try:
        req = urllib.request.Request(f"{TG_API_BASE}{token}/getMe", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            me = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise ExtensionRegistrationError(f"getMe error: {exc}")
    if not me.get("ok"):
        raise ExtensionRegistrationError(f"getMe failed: {str(me)[:160]}")
    _log(api, "info", f"bot online: {me.get('result', {}).get('username', '?')}")

    _set_state("api", api)
    _set_state("stop", False)
    _set_state("chat_id", _load_chat_id(api))

    api.subscribe_event("chat.outbound", lambda p: _on_outbound(api, p))
    api.subscribe_event("chat.typing", lambda p: _on_typing(api, p))
    api.subscribe_event("chat.photo", lambda p: _on_photo(api, p))

    # Outgoing files (v1.3.0): let the agent deliver a produced artifact to the
    # owner chat as a real Telegram document. Destination is fixed to the owner
    # chat at registration time; see the design notes above _send_document.
    api.register_tool(
        "send_file",
        _make_send_file_handler(api),
        description=(
            "Send a local file from this machine to the owner's Telegram chat through "
            "the live Telegram bridge. Use it to deliver a produced artifact (xlsx, pdf, "
            "markdown lecture, patch, any document) so it arrives in Telegram as a real "
            "attachment instead of only appearing in the Web UI. Max 50 MB (Bot API "
            "sendDocument cap). The destination is fixed to the owner chat; the tool "
            "refuses paths that look like secrets or control-plane state."
        ),
        schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path of the local file to send.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption for the file (max 1024 chars).",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Optional filename shown in Telegram; defaults to the "
                        "basename of path."
                    ),
                },
            },
            "required": ["path"],
        },
        timeout_sec=300,
    )

    thread = threading.Thread(
        target=_incoming_loop,
        args=(api, token),
        daemon=True,
        name="telegram-bridge-handmade-incoming",
    )
    thread.start()
    _set_state("thread", thread)
    _emit_lifecycle(api, "registered", "telegram-bridge-handmade registered")
    api.log("info", "telegram-bridge-handmade registered (incoming poller + outbound mirror)")

    def _stop():
        _set_state("stop", True)
        _emit_lifecycle(api, "unloading", "on_unload called")
        # Release the persistent MTProto client and its loop. Without this every
        # disable/reload leaves a connected Telegram session running inside the
        # server process. The stop flag is set first so the poller leaves its
        # loop before the client is disconnected underneath it.
        _mt_shutdown(api)

    api.on_unload(_stop)


__all__ = ["register"]