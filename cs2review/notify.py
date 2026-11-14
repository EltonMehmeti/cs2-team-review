"""Post text to the team's Discord channel through a webhook (Phase 4).

The webhook URL comes from the DISCORD_WEBHOOK_URL environment variable
(Discord: channel settings > Integrations > Webhooks > Copy Webhook URL).
It is a secret: anyone with it can post to your channel. Never put it in
code, config.toml or the database.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DISCORD_LIMIT = 2000  # characters per Discord message


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split text into chunks of at most `limit` characters, at line breaks when possible."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single very long line: cut it hard
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return [c.rstrip("\n") for c in chunks if c.strip()]


def post_to_discord(text: str, webhook_url: str | None = None, opener=urllib.request.urlopen) -> int:
    """Post `text` (split into several messages if needed). Returns the number of messages sent."""
    url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set. Put the channel's webhook URL in that environment variable.")
    sent = 0
    for chunk in split_message(text):
        body = json.dumps({
            "content": chunk,
            "allowed_mentions": {"parse": []},  # text from a model must never ping @everyone
        }).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "DiscordBot (cs2review, 0.1)"},
        )
        try:
            with opener(request, timeout=15) as response:
                if response.status not in (200, 204):
                    raise RuntimeError(f"Discord answered {response.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Discord refused the message: {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Discord: {exc.reason}") from exc
        sent += 1
    return sent
