import time
import urllib.parse
import httpx
import urllib
from typing import Any, Dict, List
from markdown import markdown
from tdmatrixbot.types import MatrixClientConfig, MatrixOpenIDToken


ENDPOINT_WHOAMI = "/_matrix/client/v3/account/whoami"
ENDPOINT_SYNC = "/_matrix/client/v3/sync"
ENDPOINT_JOINED_ROOMS = "/_matrix/client/v3/joined_rooms"

ENDPOINT_ROOM_STATE = "/_matrix/client/v3/rooms/{room_id}/state"
ENDPOINT_SEND_MESSAGE = (
    "/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"
)
ENDPOINT_USER_PROFILE = "/_matrix/client/v3/profile/{user_id}"
ENDPOINT_OPENID_REQUEST_TOKEN = "/_matrix/client/v3/user/{user_id}/openid/request_token"


class HomeserverHandler:
    def __init__(self, config: MatrixClientConfig, token: str) -> None:
        self.config = config
        self.token = token

        self.client = httpx.AsyncClient(
            base_url=self.config.homeserver.base_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(10.0, connect=5.0),  # total=10s, connect=5s
        )

    # Helpers
    def _unique_txn_id(self):
        return f"tdmatrixbot_{int(time.time() * 1000)}"

    # Public
    async def close(self):
        await self.client.aclose()

    async def whoami(self) -> Dict[str, Any]:
        """Fetch the bot's own user ID from /whoami."""
        response = await self.client.get(ENDPOINT_WHOAMI)
        response.raise_for_status()
        return response.json()

    async def sync(self, since: str | None = None, timeout_ms: int = 30_000) -> dict:
        """
        Long-poll /sync to receive new events.
        `since` is the `next_batch` token from the previous sync.
        """
        params: Dict[str, str | int] = {"timeout": timeout_ms}
        if since:
            params["since"] = since

        response = await self.client.get(
            ENDPOINT_SYNC,
            params=params,
            timeout=httpx.Timeout(
                timeout_ms / 1000 + 10
            ),  # slightly longer than server timeout
        )
        response.raise_for_status()
        return response.json()

    # Messaging
    async def send_message(
        self,
        room_id: str,
        body: str,
        formatted_body: str | None = None,
        msgtype: str = "m.text",
    ) -> dict:
        payload = {"msgtype": msgtype, "body": body}
        if formatted_body:
            payload["format"] = "org.matrix.custom.html"
            payload["formatted_body"] = formatted_body

        response = await self.client.put(
            ENDPOINT_SEND_MESSAGE.format(room_id=room_id, txn_id=self._unique_txn_id()),
            json=payload,
        )

        response.raise_for_status()
        return response.json()

    async def send_message_reply(
        self,
        room_id: str,
        reply_body: str,
        reply_to_event: dict,
        msgtype: str = "m.text",
    ) -> dict:
        """
        Send a reply to a specific event.
        `reply_to_event` is the raw event dict received from /sync.
        """
        event_id = reply_to_event["event_id"]
        sender = reply_to_event["sender"]
        original_body = reply_to_event.get("content", {}).get("body", "")

        reply_body = markdown(reply_body)

        fallback_body = "\n".join(
            f"> <{sender}> {line}" for line in original_body.splitlines()
        )
        full_body = f"{fallback_body}\n\n{reply_body}"

        formatted_fallback = (
            "<mx-reply>"
            f'<blockquote><a href="https://matrix.to/#/{room_id}/{event_id}">In reply to</a> '
            f'<a href="https://matrix.to/#/{sender}">{sender}</a><br>{original_body}</blockquote>'
            "</mx-reply>"
        )
        formatted_body = f"{formatted_fallback}{reply_body}"

        response = await self.client.put(
            ENDPOINT_SEND_MESSAGE.format(room_id=room_id, txn_id=self._unique_txn_id()),
            json={
                "msgtype": msgtype,
                "body": full_body,
                "format": "org.matrix.custom.html",
                "formatted_body": formatted_body,
                "m.relates_to": {"m.in_reply_to": {"event_id": event_id}},
            },
        )
        response.raise_for_status()
        return response.json()

    # Rooms
    async def joined_room_ids(self) -> List[Any]:
        response = await self.client.get(ENDPOINT_JOINED_ROOMS)
        response.raise_for_status()
        return response.json().get("joined_rooms", [])

    async def room_state(self, room_id: str) -> list:
        """Fetch the full state of a room."""
        response = await self.client.get(ENDPOINT_ROOM_STATE.format(room_id=room_id))
        response.raise_for_status()
        return response.json()

    async def room_active_call(self, room_id: str):
        state_events = await self.room_state(room_id)

        for event in state_events:
            if event.get("type") != "org.matrix.msc3401.call.member":
                continue

            content = event.get("content")
            if not content:
                continue

            return content

        return None

    # Users
    async def user_openid_token(
        self, user_id: str, timeout_ms: int = 30_000
    ) -> MatrixOpenIDToken:
        r = await self.client.post(
            ENDPOINT_OPENID_REQUEST_TOKEN.format(
                user_id=urllib.parse.quote_plus(user_id)
            ),
            timeout=httpx.Timeout(timeout_ms / 1000 + 10),
            json={},
        )
        r.raise_for_status()
        data = r.json()
        return MatrixOpenIDToken(**data)

    async def user_profile(self, user_id: str) -> dict:
        """Fetch a user's display name and avatar."""
        response = await self.client.get(ENDPOINT_USER_PROFILE.format(user_id=user_id))
        response.raise_for_status()
        return response.json()

    async def user_join_call(
        self,
        user_id: str,
        device_id: str,
        room_id: str,
        foci_preferred: List[Dict[str, str]],
    ):
        state_key = f"_{user_id}_{device_id}_m.call"
        rtc_foci = foci_preferred[0]

        payload = {
            "application": "m.call",
            "call_id": "",
            "scope": "m.room",
            "device_id": device_id,
            "expires": 7200000,  # 2 hours in ms
            "focus_active": {
                "type": rtc_foci["type"],
                "focus_selection": "oldest_membership",
            },
            "foci_preferred": [rtc_foci],
            "m.call.intent": "video",  # or "audio"
        }

        response = await self.client.put(
            f"{ENDPOINT_ROOM_STATE.format(room_id=room_id)}/org.matrix.msc3401.call.member/{state_key}",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def user_leave_call(self, user_id: str, device_id: str, room_id: str):
        state_key = f"_{user_id}_{device_id}_m.call"
        response = await self.client.put(
            f"{ENDPOINT_ROOM_STATE.format(room_id=room_id)}/org.matrix.msc3401.call.member/{state_key}",
            json={},  # Empty content = left the call
        )
        response.raise_for_status()
        return response.json()
