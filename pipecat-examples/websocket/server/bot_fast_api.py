
import os
import sys
import time
import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
    AssistantTurnStoppedMessage,
)
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService, InputParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


SYSTEM_INSTRUCTION = """
You are Gemini Chatbot, a friendly and helpful AI assistant.
Your responses will be converted to audio so avoid special characters.
Keep answers short (1-2 sentences).
"""


LiveTranscriptCallback = Callable[[str, str], Awaitable[None]]
OutputStartCallback = Callable[[str], Awaitable[None]]


class GeminiLiveLLMServiceWithLiveTranscripts(GeminiLiveLLMService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._live_user_transcript = ""
        self._live_bot_transcript = ""
        self._live_user_cb: Optional[LiveTranscriptCallback] = None
        self._live_bot_cb: Optional[LiveTranscriptCallback] = None
        self._output_start_cb: Optional[OutputStartCallback] = None
        self._last_user_emit = 0.0
        self._last_bot_emit = 0.0
        self._output_started_for_turn = False

    def set_live_transcript_callbacks(
        self,
        *,
        user_cb: Optional[LiveTranscriptCallback],
        bot_cb: Optional[LiveTranscriptCallback],
    ) -> None:
        self._live_user_cb = user_cb
        self._live_bot_cb = bot_cb

    def set_output_start_callback(self, cb: Optional[OutputStartCallback]) -> None:
        self._output_start_cb = cb

    def reset_live_user_transcript(self) -> None:
        self._live_user_transcript = ""

    def reset_live_bot_transcript(self) -> None:
        self._live_bot_transcript = ""

    def consume_live_user_transcript(self) -> str:
        text = self._live_user_transcript.strip()
        self._live_user_transcript = ""
        return text

    def _emit_interval_secs(self) -> float:
        raw = (os.getenv("LIVE_TRANSCRIPT_THROTTLE_MS") or "").strip() or "100"
        try:
            ms = max(0, int(raw))
        except ValueError:
            ms = 100
        return ms / 1000.0

    async def _maybe_emit_user_partial(self) -> None:
        cb = self._live_user_cb
        if not cb:
            return

        now = time.monotonic()
        interval = self._emit_interval_secs()
        if interval and (now - self._last_user_emit) < interval:
            return

        self._last_user_emit = now
        await cb("user_transcript_partial", self._live_user_transcript.strip())

    async def _maybe_emit_bot_partial(self) -> None:
        cb = self._live_bot_cb
        if not cb:
            return

        now = time.monotonic()
        interval = self._emit_interval_secs()
        if interval and (now - self._last_bot_emit) < interval:
            return

        self._last_bot_emit = now
        await cb("bot_transcript_partial", self._live_bot_transcript.strip())

    async def _handle_msg_input_transcription(self, message):  # type: ignore[override]
        try:
            text = message.server_content.input_transcription.text if message.server_content else None
            if text:
                # New user input implies we're in a new turn; allow output-start detection again.
                self._output_started_for_turn = False
                if text.startswith(" ") and not self._live_user_transcript:
                    text = text.lstrip()
                self._live_user_transcript += text
                logger.debug(f"live user partial chunk: {text!r}")
                await self._maybe_emit_user_partial()
        except Exception:
            logger.exception("Failed emitting live user transcription")

        await super()._handle_msg_input_transcription(message)

    async def _handle_msg_output_transcription(self, message):  # type: ignore[override]
        try:
            text = message.server_content.output_transcription.text if message.server_content else None
            if text:
                # Flush the user's transcript before the bot starts speaking so the client UI
                # shows "user -> bot" ordering (the universal aggregator turn-stopped can lag
                # behind Gemini Live output).
                if not self._output_started_for_turn:
                    self._output_started_for_turn = True
                    cb = self._output_start_cb
                    if cb:
                        user_text = self.consume_live_user_transcript()
                        if user_text:
                            await cb(user_text)

                self._live_bot_transcript += text
                logger.debug(f"live bot partial chunk: {text!r}")
                await self._maybe_emit_bot_partial()
        except Exception:
            logger.exception("Failed emitting live bot transcription")

        await super()._handle_msg_output_transcription(message)


async def run_bot(websocket_client):

    # -----------------------------
    # WebSocket Transport
    # -----------------------------
    ws_transport = FastAPIWebsocketTransport(
        websocket=websocket_client,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            # Required so the browser client receives RTVI protocol messages
            # (bot-ready, user/bot transcription events, server-message, etc.).
            serializer=ProtobufFrameSerializer(
                params=FrameSerializer.InputParams(ignore_rtvi_messages=False)
            ),
        ),
    )

    # -----------------------------
    # Gemini Live LLM
    # -----------------------------
    gemini_language = (os.getenv("GEMINI_LANGUAGE") or "").strip() or "en-US"
    gemini_model = (os.getenv("GEMINI_MODEL") or "").strip() or "models/gemini-2.5-flash-native-audio-preview-12-2025"
    voice_id = (os.getenv("GEMINI_VOICE_ID") or "").strip() or "Puck"

    try:
        llm_params = InputParams(language=gemini_language)
    except Exception:
        logger.warning(
            f"Invalid GEMINI_LANGUAGE={gemini_language!r}; falling back to default Gemini language."
        )
        llm_params = InputParams()

    llm = GeminiLiveLLMServiceWithLiveTranscripts(
        api_key=os.getenv("GOOGLE_API_KEY"),
        model=gemini_model,
        voice_id=voice_id,
        system_instruction=SYSTEM_INSTRUCTION,
        params=llm_params,
    )

    # -----------------------------
    # Conversation Context
    # -----------------------------
    context = LLMContext(
        [
            {
                "role": "user",
                "content": "Start by greeting the user warmly and introducing yourself.",
            }
        ]
    )

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    # -----------------------------
    # Pipeline
    # -----------------------------
    pipeline = Pipeline(
        [
            ws_transport.input(),
            user_aggregator,
            llm,
            ws_transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    def _iso_timestamp(raw_ts: object | None) -> str:
        if isinstance(raw_ts, datetime):
            return raw_ts.astimezone(timezone.utc).isoformat()
        if isinstance(raw_ts, str) and raw_ts.strip():
            return raw_ts.strip()
        return datetime.now(timezone.utc).isoformat()

    live_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=50)

    async def _live_sender() -> None:
        while True:
            msg_type, text = await live_queue.get()
            try:
                await task.rtvi.send_server_message(
                    {
                        "type": msg_type,
                        "text": text,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
            except Exception:
                logger.exception(f"Failed sending server message {msg_type!r}")

    live_sender_task = asyncio.create_task(_live_sender())

    last_user_final_text = ""

    async def _emit_user_final(text: str, *, timestamp: object | None = None) -> None:
        nonlocal last_user_final_text
        transcript = (text or "").strip()
        if not transcript or transcript == last_user_final_text:
            return

        last_user_final_text = transcript

        try:
            # Keep the "live" line updated and then clear it when final lands.
            await task.rtvi.send_server_message(
                {"type": "user_transcript_partial", "text": transcript, "timestamp": _iso_timestamp(timestamp)}
            )
            await task.rtvi.send_server_message(
                {"type": "user_transcript_partial", "text": "", "timestamp": _iso_timestamp(timestamp)}
            )
            await task.rtvi.send_server_message(
                {"type": "user_transcript", "text": transcript, "timestamp": _iso_timestamp(timestamp)}
            )
        except Exception:
            logger.exception("Failed sending user transcript messages")

    # Ensure user transcript appears before bot output.
    llm.set_output_start_callback(lambda user_text: _emit_user_final(user_text))

    if (os.getenv("ENABLE_LIVE_TRANSCRIPTION") or "").strip() != "0":

        async def _send_live_message(msg_type: str, text: str) -> None:
            try:
                live_queue.put_nowait((msg_type, text))
            except asyncio.QueueFull:
                pass

        llm.set_live_transcript_callbacks(user_cb=_send_live_message, bot_cb=_send_live_message)

    # -----------------------------
    # RTVI CLIENT READY
    # -----------------------------
    @task.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await rtvi.set_bot_ready()
        await task.rtvi.send_server_message(
            {
                "type": "server_info",
                "text": "client_ready",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        await task.queue_frames([LLMRunFrame()])

    # -----------------------------
    # USER TRANSCRIPT EVENT
    # -----------------------------
    # @user_aggregator.event_handler("on_user_turn_stopped")
    # async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):

    #     transcript = message.content.strip()

    #     if not transcript:
    #         return

    #     logger.info(f"User transcript: {transcript}")

    #     await task.rtvi.send_server_message(
    #         {
    #             "type": "user_transcript",
    #             "text": transcript,
    #         }
    #     )


    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):

        transcript = message.content.strip()

        # Ignore empty or punctuation transcripts
        if not transcript or transcript in [".", ",", "?", "!"]:
            return

        logger.info(f"User transcript: {transcript}")
        llm.reset_live_user_transcript()
        await _emit_user_final(transcript, timestamp=message.timestamp)

    # -----------------------------
    # BOT TRANSCRIPT EVENT
    # -----------------------------
    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage):

        transcript = message.content.strip()

        if not transcript:
            return

        logger.info(f"Bot transcript: {transcript}")

        # Send final transcripts as partial fallback to keep live UI alive
        await task.rtvi.send_server_message({"type": "bot_transcript_partial", "text": transcript, "timestamp": _iso_timestamp(message.timestamp)})

        llm.reset_live_bot_transcript()
        await task.rtvi.send_server_message({"type": "bot_transcript_partial", "text": "", "timestamp": _iso_timestamp(message.timestamp)})

        await task.rtvi.send_server_message(
            {
                "type": "bot_transcript",
                "text": transcript,
                "timestamp": _iso_timestamp(message.timestamp),
            }
        )

    # -----------------------------
    # CLIENT CONNECTED
    # -----------------------------
    @ws_transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):

        logger.info("Client connected")

    # -----------------------------
    # CLIENT DISCONNECTED
    # -----------------------------
    @ws_transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):

        logger.info("Client disconnected")

        await task.cancel()
        live_sender_task.cancel()

    runner = PipelineRunner(handle_sigint=False)

    await runner.run(task)
