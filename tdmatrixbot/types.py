from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional
from livekit import rtc


# Matrix types


@dataclass
class MatrixClientHomeserver:
    base_url: str


@dataclass
class MatrixClientRtcFocus:
    type: str
    livekit_service_url: Optional[str] = None


@dataclass
class MatrixClientConfig:
    homeserver: MatrixClientHomeserver
    rtc_foci: List[MatrixClientRtcFocus] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "MatrixClientConfig":
        return cls(
            homeserver=MatrixClientHomeserver(**data["m.homeserver"]),
            rtc_foci=[
                MatrixClientRtcFocus(**f)
                for f in data.get("org.matrix.msc4143.rtc_foci", [])
            ],
        )

    def to_dict(self) -> dict:
        return {
            "m.homeserver": {"base_url": self.homeserver.base_url},
            "org.matrix.msc4143.rtc_foci": [
                {k: v for k, v in f.__dict__.items() if v is not None}
                for f in self.rtc_foci
            ],
        }

    @property
    def rtc_foci_preferred(self):
        preferred_type = "livekit"
        preferred = next(
            (rtc_foci for rtc_foci in self.rtc_foci if rtc_foci.type == preferred_type),
            None,
        )
        fallback = next(
            (rtc_foci for rtc_foci in self.rtc_foci if rtc_foci.type != preferred_type),
            None,
        )
        return preferred if preferred else fallback


@dataclass
class MatrixServerConfig:
    server: str


@dataclass
class MatrixDiscovery:
    client: MatrixClientConfig
    server: Optional[MatrixServerConfig] = None


@dataclass
class MatrixOpenIDToken:
    access_token: str
    token_type: str
    matrix_server_name: str
    expires_in: int


@dataclass
class MatrixRTCToken:
    url: str
    jwt: str


@dataclass
class MatrixRoom:
    id: str
    name: str
    topic: str | None = None
    alias: str | None = None
    members: List[str] = field(default_factory=List)

    @staticmethod
    def from_state_events(state_events: List[Dict[str, Any]]):
        event_room_name = next(
            (e for e in state_events if e.get("type") == "m.room.name"), None
        )
        if not event_room_name:
            raise Exception('unable to find "m.room.name" state-event')

        return MatrixRoom(
            event_room_name["room_id"],
            event_room_name["content"]["name"],
            topic=next(
                (
                    e["content"]["topic"]
                    for e in state_events
                    if e.get("type") == "m.room.topic"
                ),
                None,
            ),
            alias=next(
                (
                    e["content"]["alias"]
                    for e in state_events
                    if e.get("type") == "m.room.canonical_alias"
                ),
                None,
            ),
            members=[
                e["user_id"]
                for e in state_events
                if e.get("type") == "m.room.member"
                and e.get("content", {}).get("membership") == "join"
            ],
        )


@dataclass
class MatrixCallRTC:
    room: rtc.Room
    audio_source: rtc.AudioSource
    audio_track: rtc.LocalAudioTrack


@dataclass
class MatrixCall:
    room: MatrixRoom
    rtc: MatrixCallRTC


# Bot types


@dataclass
class BotCommand:
    name: str
    handler: Callable[
        [
            MatrixRoom,
            Dict[str, Any],  # event
            str,  # command_value
        ],
        Awaitable,
    ]
    help_description: str | None = None
    dependencies: List[Callable[[], Awaitable[bool]]] | None = None
