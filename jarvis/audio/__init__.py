"""Audio I/O for jarvisd: mic capture, wake word, VAD, playback and the PipeWire echo canceller."""

SAMPLE_RATE = 16000        # capture / wake word / VAD / Whisper
FRAME_SAMPLES = 1280       # 80 ms, what openWakeWord wants per call
