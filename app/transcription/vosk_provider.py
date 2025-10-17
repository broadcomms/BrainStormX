"""Vosk offline transcription streaming provider.

Implements the TranscriptionProvider protocol using the Vosk
speech recognition engine. This runs fully locally (CPU) with
no external network calls, allowing development and testing
without incurring cloud costs.

Model Loading Strategy:
 - The model path is resolved from env var VOSK_MODEL_PATH
   (expected to point to a downloaded Vosk model directory).
 - Model is loaded lazily on first provider instance to avoid
   import/startup cost when feature is disabled.

Streaming Strategy:
 - Chunks written via write() are fed to a background thread
   that invokes recognizer.AcceptWaveform or PartialResult
   depending on Vosk's internal buffering.
 - Final results are emitted when AcceptWaveform returns True;
   partials when PartialResult contains text.

Thread <-> Async Bridge:
 - An asyncio.Queue[ProviderEvent] is used so the async aresults
   iterator can yield events produced by a worker thread.

Limitations / Simplifications:
 - No word-level timestamps unless model returns them (some
   models require --words parameter during creation).
 - Confidence values are not normalized; omitted for now.
 - End-of-stream finalization ensures any remaining partial is
   flushed as a final if non-empty.
"""
from __future__ import annotations
import asyncio
import json
import os
import threading
import queue
from typing import AsyncIterator, Optional
import logging

try:
    from vosk import Model, KaldiRecognizer  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    Model = None  # type: ignore
    KaldiRecognizer = None  # type: ignore

from .provider import (
    TranscriptionProvider, ProviderConfig,
    TranscriptPartialEvent, TranscriptFinalEvent, ProviderEvent
)

_MODEL_SINGLETON: dict[str, object] = {
    'path': None,
    'model': None,
    'sample_rate': None,
}


def _load_model(sample_rate: int) -> Optional[Model]:  # type: ignore[override]
    if Model is None:
        return None
    model_path = os.getenv('VOSK_MODEL_PATH')
    if not model_path or not os.path.isdir(model_path):
        return None
    # Basic singleton reuse (ignore differing sample rates for now)
    if _MODEL_SINGLETON.get('model') is None:
        _MODEL_SINGLETON['model'] = Model(model_path)  # type: ignore[assignment]
        _MODEL_SINGLETON['path'] = model_path  # type: ignore[assignment]
        _MODEL_SINGLETON['sample_rate'] = sample_rate  # type: ignore[assignment]
    return _MODEL_SINGLETON['model']  # type: ignore


class VoskStreamingProvider(TranscriptionProvider):
    @classmethod
    def is_available(cls) -> bool:
        return Model is not None

    def __init__(self):
        # Async queue for events consumed by aresults()
        self._queue: 'asyncio.Queue[ProviderEvent]' = asyncio.Queue()
        # Underlying Vosk recognizer (initialized in open_stream)
        self._recognizer: Optional[KaldiRecognizer] = None  # type: ignore
        self._opened = False
        self._worker_thread: Optional[threading.Thread] = None
        # Thread-safe audio buffer (event-loop producer -> worker thread consumer)
        self._input_queue: 'queue.Queue[Optional[bytes]]' = queue.Queue()
        self._stop_event = threading.Event()
        self._session_id: Optional[str] = None
        self._config: Optional[ProviderConfig] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def open_stream(self, session_id: str, config: ProviderConfig) -> None:  # type: ignore[override]
        if Model is None or KaldiRecognizer is None:
            raise RuntimeError('Vosk not installed. Install with: pip install vosk')
        model = _load_model(config.sample_rate_hz)
        if model is None:
            raise RuntimeError('Vosk model not found. Set VOSK_MODEL_PATH to a valid model directory')
        logging.getLogger(__name__).info(
            "Opening Vosk stream session_id=%s model_path=%s sample_rate=%s", session_id, _MODEL_SINGLETON.get('path'), config.sample_rate_hz
        )
        self._recognizer = KaldiRecognizer(model, config.sample_rate_hz)  # type: ignore[arg-type]
        # Enable word timestamps
        try:
            self._recognizer.SetWords(True)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._opened = True
        self._session_id = session_id
        self._config = config
        self._loop = asyncio.get_running_loop()
        # Start worker thread to consume audio from input queue
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def _worker_loop(self):
        log = logging.getLogger(__name__)
        print(f"[DEBUG] Vosk worker thread starting for session {self._session_id}")
        chunk_count = 0
        while not self._stop_event.is_set():
            try:
                try:
                    # Block briefly waiting for audio
                    chunk = self._input_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if chunk is None:
                    print(f"[DEBUG] Received stop sentinel in worker thread")
                    break
                if not self._recognizer:
                    print(f"[DEBUG] No recognizer available in worker thread")
                    continue
                
                chunk_count += 1
                print(f"[DEBUG] Processing chunk #{chunk_count} ({len(chunk)} bytes)")
                accepted = self._recognizer.AcceptWaveform(chunk)  # type: ignore[attr-defined]
                print(f"[DEBUG] Vosk AcceptWaveform returned: {accepted}")
                
                if accepted:
                    res = self._recognizer.Result()  # type: ignore[attr-defined]
                    print(f"[DEBUG] Vosk Result: {res}")
                    words_list = None
                    text = ''
                    try:
                        data = json.loads(res)
                        text = data.get('text', '').strip()
                        raw_words = data.get('result') or []
                        if isinstance(raw_words, list):
                            words_list = []
                            for w in raw_words:
                                if not isinstance(w, dict):
                                    continue
                                words_list.append({
                                    'word': w.get('word'),
                                    'start': w.get('start'),
                                    'end': w.get('end'),
                                    'confidence': w.get('conf'),
                                })
                    except Exception:
                        log.debug('Failed to parse Vosk result JSON', exc_info=True)
                    if text and self._loop:
                        print(f"[DEBUG] Emitting final event: '{text}'")
                        evt = TranscriptFinalEvent(text=text, words=words_list)
                        asyncio.run_coroutine_threadsafe(self._queue.put(evt), self._loop)
                    elif text:
                        print(f"[DEBUG] Final text but no event loop: '{text}'")
                    else:
                        print(f"[DEBUG] No text in final result")
                else:
                    pres = self._recognizer.PartialResult()  # type: ignore[attr-defined]
                    print(f"[DEBUG] Vosk PartialResult: {pres}")
                    try:
                        pdata = json.loads(pres)
                        ptext = pdata.get('partial', '').strip()
                    except Exception:
                        ptext = ''
                    if ptext and self._loop:
                        print(f"[DEBUG] Emitting partial event: '{ptext}'")
                        evt = TranscriptPartialEvent(text=ptext, is_final=False)
                        asyncio.run_coroutine_threadsafe(self._queue.put(evt), self._loop)
                    elif ptext:
                        print(f"[DEBUG] Partial text but no event loop: '{ptext}'")
                    else:
                        print(f"[DEBUG] No text in partial result")
            except Exception:
                log.exception('Error in Vosk worker loop')
                continue
        # Flush final after stop
        try:
            if self._recognizer and self._loop:
                fres = self._recognizer.FinalResult()  # type: ignore[attr-defined]
                data = json.loads(fres)
                ftext = data.get('text', '').strip()
                if ftext:
                    words_list = None
                    raw_words = data.get('result') or []
                    if isinstance(raw_words, list):
                        words_list = []
                        for w in raw_words:
                            if not isinstance(w, dict):
                                continue
                            words_list.append({
                                'word': w.get('word'),
                                'start': w.get('start'),
                                'end': w.get('end'),
                                'confidence': w.get('conf'),
                            })
                    asyncio.run_coroutine_threadsafe(self._queue.put(TranscriptFinalEvent(text=ftext, words=words_list)), self._loop)
        except Exception:
            log.debug('Final flush failed', exc_info=True)

    async def write(self, chunk: bytes) -> None:  # type: ignore[override]
        if not self._opened:
            raise RuntimeError('Stream not opened')
        # Non-blocking put; if queue is very large we could drop or block
        print(f"[DEBUG] Vosk write() called with {len(chunk)} bytes")
        try:
            self._input_queue.put_nowait(chunk)
            print(f"[DEBUG] Audio chunk queued for processing (queue size: {self._input_queue.qsize()})")
        except Exception as e:
            # Fallback blocking put
            print(f"[DEBUG] Queue full, using blocking put: {e}")
            self._input_queue.put(chunk)
            print(f"[DEBUG] Audio chunk queued via blocking put")

    async def aresults(self) -> AsyncIterator[ProviderEvent]:  # type: ignore[override]
        while True:
            evt = await self._queue.get()
            if evt is None:  # sentinel -> graceful termination
                break
            yield evt

    async def close(self) -> None:  # type: ignore[override]
        if not self._opened:
            return
        self._opened = False
        try:
            self._input_queue.put_nowait(None)
        except Exception:
            self._input_queue.put(None)
        self._stop_event.set()
        # Worker thread will flush final
        # No explicit join needed (daemon), but we can attempt short join
        if self._worker_thread and self._worker_thread.is_alive():
            try:
                self._worker_thread.join(timeout=0.5)
            except Exception:
                pass
        # Push sentinel so consumer loop in gateway can exit promptly
        try:
            await self._queue.put(None)  # type: ignore[arg-type]
        except Exception:
            pass
        await asyncio.sleep(0)  # yield control

