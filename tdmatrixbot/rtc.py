import logging
import asyncio
import subprocess
import time
import httpx
from dataclasses import asdict
from pathlib import Path
from livekit import rtc

from tdmatrixbot.types import MatrixClientRtcFocus, MatrixOpenIDToken, MatrixRTCToken

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48_000  # Hz — Livekit standard
CHANNELS = 1  # mono
CHUNK_MS = 20  # ms per frame — 20ms is the standard for WebRTC


class RTCHandler:
    def __init__(self, rtc_foci: MatrixClientRtcFocus):
        if not rtc_foci.livekit_service_url:
            raise ValueError("MatrixClientRtcFocus.livekit_service_url cannot be None")

        self.type = rtc_foci.type
        self.service_url = rtc_foci.livekit_service_url

        self.client = httpx.AsyncClient(
            base_url=self.service_url,
            timeout=httpx.Timeout(10.0, connect=5.0),  # total=10s, connect=5s
        )

    async def get_token(
        self, openid_token: MatrixOpenIDToken, room_id: str, device_id: str
    ):
        response = await self.client.post(
            "/sfu/get",
            json={
                "room": room_id,
                "openid_token": asdict(openid_token),
                "device_id": device_id,
            },
        )
        response.raise_for_status()
        data = response.json()
        return MatrixRTCToken(**data)

    async def join_room(self, token: MatrixRTCToken):
        room = rtc.Room()
        audio_source = rtc.AudioSource(SAMPLE_RATE, CHANNELS)
        audio_track = rtc.LocalAudioTrack.create_audio_track("bot-audio", audio_source)
        options = rtc.RoomOptions(auto_subscribe=False)

        await room.connect(token.url, token.jwt, options=options)
        await room.local_participant.publish_track(
            audio_track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )

        return room, audio_source, audio_track

    async def stream_silence(self, audio_source: rtc.AudioSource):
        silent_frame = rtc.AudioFrame(
            data=b"\x00" * (SAMPLE_RATE // 50 * 2),  # 20ms of silence, 16-bit samples
            sample_rate=SAMPLE_RATE,
            num_channels=CHANNELS,
            samples_per_channel=SAMPLE_RATE // 50,
        )
        await audio_source.capture_frame(silent_frame)

    async def stream_audio_file(self, audio_source: rtc.AudioSource, audio_file: Path):
        def decode_audio() -> bytes:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    str(audio_file),
                    "-f",
                    "s16le",  # output format: signed 16-bit little-endian PCM
                    "-ar",
                    str(SAMPLE_RATE),  # resample Hz
                    "-ac",
                    str(CHANNELS),  # mix
                    "-",  # output to stdout instead of a file
                ],
                stdout=subprocess.PIPE,  # capture stdout (the raw PCM bytes)
                stderr=subprocess.DEVNULL,  # suppress ffmpeg's noisy progress output
            )
            return result.stdout

        print(f"Decoding {audio_file.name}...")
        raw = await asyncio.get_event_loop().run_in_executor(None, decode_audio)

        samples_per_chunk = int(SAMPLE_RATE * CHUNK_MS / 1000)
        bytes_per_chunk = samples_per_chunk * 2
        total_samples = len(raw) // 2

        print(f"Streaming {audio_file.name} ({total_samples / SAMPLE_RATE:.1f}s)...")

        frames_sent = 0
        start_time = time.perf_counter()

        for offset in range(0, len(raw), bytes_per_chunk):
            chunk_bytes = raw[offset : offset + bytes_per_chunk]

            if len(chunk_bytes) < bytes_per_chunk:
                chunk_bytes += b"\x00" * (bytes_per_chunk - len(chunk_bytes))

            frame = rtc.AudioFrame(
                data=chunk_bytes,
                sample_rate=SAMPLE_RATE,
                num_channels=CHANNELS,
                samples_per_channel=samples_per_chunk,
            )
            await audio_source.capture_frame(frame)
            frames_sent += 1

            if frames_sent % 50 == 0:
                print(
                    f"Sent {frames_sent} frames ({frames_sent * CHUNK_MS / 1000:.1f}s)"
                )

            next_frame_time = start_time + (frames_sent * CHUNK_MS / 1000)
            sleep_time = next_frame_time - time.perf_counter()
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
