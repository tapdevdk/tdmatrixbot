import asyncio
import logging
import httpx
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List

from tdmatrixbot.homeserver import HomeserverHandler
from tdmatrixbot.rtc import RTCHandler
from tdmatrixbot.types import (
    BotCommand,
    MatrixCall,
    MatrixCallRTC,
    MatrixClientConfig,
    MatrixDiscovery,
    MatrixRoom,
)

logger = logging.getLogger("tdmatrixbot")

URL_HOMESERVER_DISCOVERY = "https://{host}/.well-known/matrix/client"


class MatrixBot:
    _commands: Dict[str, BotCommand] = {}
    _room_names: dict[str, str] = {}

    def __init__(
        self,
        host: str,
        token: str,
        data_dir: Path,
        cmd_prefix: str = "!",
    ):
        self.host = host
        self.token = token
        self.data_dir = data_dir
        self.batch_file = self.data_dir / ".next_batch"
        self.cmd_prefix = cmd_prefix

        # NOTE: will be set through discovery in run()
        self.user_id = "@tdmatrixbot:matrix.example.com"
        self.device_id = "TDMATRIXBOT"

    # Privates
    def _discovery(self, host: str) -> MatrixDiscovery:
        resp = httpx.get(URL_HOMESERVER_DISCOVERY.format(host=host))
        resp.raise_for_status()
        return MatrixDiscovery(client=MatrixClientConfig.from_dict(resp.json()))

    def _load_next_batch(self) -> str | None:
        if self.batch_file.exists():
            return self.batch_file.read_text().strip() or None
        return None

    def _save_next_batch(self, token):
        if self.batch_file.exists() and isinstance(token, str):
            self.batch_file.write_text(token)

    # Public
    def command(
        self,
        command: str,
        help_description: str | None = None,
        dependencies: List[Callable[[], Awaitable[bool]]] | None = None,
    ):
        def decorator(
            command_handler: Callable[
                [MatrixRoom, Dict[str, Any], str], Awaitable[Any]
            ],
        ):
            self._commands[command] = BotCommand(
                name=command,
                help_description=help_description,
                handler=command_handler,
                dependencies=dependencies,
            )

        return decorator

    async def join_call(
        self, room: MatrixRoom, foci_preferred: List[Dict[str, str]]
    ) -> MatrixCall:
        openid_token = await self.homeserver.user_openid_token(self.user_id)
        rtc_token = await self.rtc.get_token(openid_token, room.id, self.device_id)

        # Announce bot is joining the call in Matrix
        join_resp = await self.homeserver.user_join_call(
            self.user_id, self.device_id, room.id, foci_preferred
        )

        rtc_room, rtc_audio_source, rtc_audio_track = await self.rtc.join_room(
            rtc_token
        )

        joined_call = MatrixCall(
            room=room,
            rtc=MatrixCallRTC(
                room=rtc_room,
                audio_source=rtc_audio_source,
                audio_track=rtc_audio_track,
            ),
        )

        return joined_call

    async def leave_call(self, room_id: str):
        return await self.homeserver.user_leave_call(
            self.user_id, self.device_id, room_id
        )

    async def disconnect_from_all_calls(self):
        """
        Find all rooms where the bot has an active call.member state and clear them.
        """
        room_ids = await self.homeserver.joined_room_ids()
        disconnected = []
        for room_id in room_ids:
            try:
                state_events = await self.homeserver.room_state(room_id)
                for event in state_events:
                    event_type = event.get("type")
                    if event_type != "org.matrix.msc3401.call.member":
                        continue

                    state_key = event.get("state_key", "")
                    if state_key.startswith(f"_{self.user_id}_"):
                        await self.leave_call(room_id)
                        disconnected.append(room_id)
                        logger.info(f"Disconnected from call in {room_id}")

            except httpx.HTTPStatusError:
                # No call.member state in this room, skip
                continue

    async def run(self):
        # Run discovery
        logger.info("Running discovery ...")
        discovery = self._discovery(self.host)
        rtc_foci_preferred = discovery.client.rtc_foci_preferred
        if not rtc_foci_preferred:
            raise ValueError(
                '"org.matrix.msc4143.rtc_foci" is missing, and is required by TDMatrixBot'
            )

        self.homeserver = HomeserverHandler(discovery.client, self.token)
        self.rtc = RTCHandler(rtc_foci_preferred)

        whoami = await self.homeserver.whoami()
        self.user_id = whoami["user_id"]
        self.device_id = whoami["device_id"]
        logger.debug(f"Bot user ID: {self.user_id}")
        logger.debug(f"Bot device ID: {self.device_id}")

        # Get the first batch
        next_batch = self._load_next_batch()
        if next_batch is None:
            logger.info("No sync token found — catching up to present...")
            data = await self.homeserver.sync(timeout_ms=0)
            next_batch = data.get("next_batch")
            self._save_next_batch(next_batch)

        logger.info("Bot starting...")
        try:
            while True:
                data = await self.homeserver.sync(since=next_batch, timeout_ms=30_000)
                next_batch = data.get("next_batch")
                self._save_next_batch(next_batch)

                # Walk joined rooms for new timeline events
                rooms: Dict = data.get("rooms", {}).get("join", {})
                for room_id, room_data in rooms.items():
                    events = room_data.get("timeline", {}).get("events", [])
                    for event in events:
                        # Verify event
                        if event.get("type") != "m.room.message":
                            continue

                        body = event.get("content", {}).get("body", "")
                        if not isinstance(body, str):
                            continue

                        body_parts = body.split(" ")
                        cmd_str = body_parts[0]
                        if not cmd_str.startswith(self.cmd_prefix):
                            continue

                        # TODO: Make some "auth" so only specify senders are allowed to use the bot
                        sender = event.get("sender", "unknown")

                        # Get room from state
                        room_state_events = await self.homeserver.room_state(room_id)
                        room = MatrixRoom.from_state_events(room_state_events)
                        logger.info(f"[{room.name}] {sender}: {body}")

                        # Command handling
                        cmd_name = cmd_str.removeprefix(self.cmd_prefix)
                        cmd_value = " ".join(body_parts[1:])
                        cmd = self._commands.get(cmd_name)
                        if cmd is None:
                            continue

                        try:
                            await cmd.handler(room, event, cmd_value)
                        except Exception as e:
                            await self.homeserver.send_message(
                                room.id, f"COMMAND ERROR:\n{e}"
                            )

        except asyncio.CancelledError:
            logger.info("Bot shutting down...")
        finally:
            await self.homeserver.close()
