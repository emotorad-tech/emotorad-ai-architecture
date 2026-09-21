"""Video evidence: frames and narration. Claude has no video block, so a clip is
sampled into stills and its audio transcribed. Shared by the production agent
loop and the playground; the playground did this first, and these are its
functions moved, not rewritten."""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional

# Claude has no video block: the Messages API takes images and PDFs. A model
# cannot watch an mp4, so "support video" means sampling frames and sending
# those as images, labelled honestly as frames rather than passed off as the
# video itself. The customer's original file is kept whole for the human who
# picks up the ticket — they can watch it, and it is the evidence.
VIDEO_TYPES = ("mp4", "mov", "webm", "m4v")
# Eight is a compromise. Each frame costs roughly as much as a photo, so a video
# is already the most expensive thing in a conversation; too few and a two-second
# flicker on a battery indicator falls between samples.
VIDEO_FRAMES = 8
# Long edge. Above this the extra pixels buy no accuracy and cost tokens.
FRAME_MAX_EDGE = 1024


# Speech-to-text for the video's audio track. Frames answer "what does it look
# like"; the customer's own narration answers "what am I meant to be noticing" —
# and on a video of a motor, that sentence is usually the entire diagnosis
# ("listen, it wheezes when I start it"). Whisper transcribes *speech*: it will
# not characterise a mechanical noise, and nothing here should imply it can.
#
# Local and offline by design, so prompt tuning needs no extra credentials. For
# production, AWS Transcribe is the architecturally consistent choice — CLAUDE.md
# keeps this traffic inside Emotorad's AWS boundary — and transcribe_audio is
# the single seam to swap.
#
# "base" is what has been tested here. Indian-accented Hinglish, Hindi and
# Marathi are noticeably better on "small" or "medium"; set the env var to
# upgrade, at the cost of a larger one-off model download.
WHISPER_MODEL = os.environ.get("EMOTORAD_WHISPER_MODEL", "base")
_whisper_cache: Dict[str, Any] = {}


def ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def extract_audio(data: bytes, suffix: str) -> Optional[bytes]:
    """The video's audio track as 16kHz mono WAV, or None if it has none.

    16kHz mono is what speech models want; anything richer is discarded on the
    way in, so sending more is wasted decode time.
    """
    import subprocess
    import tempfile

    exe = ffmpeg_exe()
    if not exe:
        return None
    try:
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "in%s" % (suffix or ".mp4"))
            target = os.path.join(folder, "out.wav")
            with open(source, "wb") as handle:
                handle.write(data)
            result = subprocess.run(
                [exe, "-y", "-i", source, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", target],
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0 or not os.path.exists(target):
                return None  # silent clip, or no audio stream at all
            data = open(target, "rb").read()
            # A WAV header alone is ~44 bytes; anything near that is not audio.
            return data if len(data) > 1024 else None
    except Exception:
        return None


def transcribe_audio(wav: bytes) -> Optional[Dict[str, Any]]:
    """Speech in the clip, as text plus the language it was spoken in.

    Language is reported rather than assumed: the testing strategy requires
    results per language and never averaged, and a Hinglish transcript that
    silently scored as English would hide exactly the weakness that matters.
    """
    import tempfile

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    try:
        model = _whisper_cache.get(WHISPER_MODEL)
        if model is None:
            # First call downloads the model; later ones are cheap.
            model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            _whisper_cache[WHISPER_MODEL] = model
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as handle:
            handle.write(wav)
            handle.flush()
            segments, info = model.transcribe(handle.name, beam_size=1)
            text = " ".join(segment.text.strip() for segment in segments).strip()
        if not text:
            return None
        return {
            "text": text,
            "language": info.language,
            "language_probability": round(float(info.language_probability), 2),
        }
    except Exception:
        return None


def is_video(mime: str, name: str = "") -> bool:
    mime = (mime or "").lower()
    name = (name or "").lower()
    return mime.startswith("video/") or name.rsplit(".", 1)[-1] in VIDEO_TYPES


def extract_frames(data: bytes, suffix: str, count: int = VIDEO_FRAMES) -> List[str]:
    """Evenly spaced frames from a video, as base64 JPEGs.

    Evenly spaced rather than the first N: a customer filming a battery indicator
    holds the camera still for a while and the informative moment is usually in
    the middle, so the opening second tells you nothing.

    Returns [] on any failure — a codec we cannot read, a corrupt upload, a
    missing decoder. The caller says so plainly rather than the model silently
    receiving nothing and assuming it has seen the video.
    """
    import io
    import tempfile

    try:
        import imageio
        from PIL import Image
    except ImportError:
        return []

    import gc
    import warnings

    frames: List[str] = []
    reader = None
    failed = False
    # A corrupt clip makes ffmpeg exit immediately with an error, and
    # imageio_ffmpeg's own cleanup only closes the subprocess's stdin/stdout
    # pipes when the process is still running (it checks `process.poll() is
    # None`) — so on that path `reader` is never even bound (get_reader()
    # raised before returning) and those pipes are only reachable from the
    # failed call, for the GC to notice and warn about at some unrelated
    # later line, on some unrelated later test. The warning is suppressed
    # for this whole attempt because it is reporting a close we were never
    # in a position to make ourselves; gc.collect() below still finalizes
    # the pipes deterministically, here, rather than leaving that to chance.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix or ".mp4", delete=True) as handle:
                handle.write(data)
                handle.flush()
                # reader is bound before any of this can raise, so the finally
                # below closes it however we leave this block.
                reader = imageio.get_reader(handle.name, "ffmpeg")
                meta = reader.get_meta_data()
                # Estimated from duration × fps rather than count_frames(),
                # which decodes the whole file to answer. Seeking to an index
                # past the end raises, and that is caught per frame below.
                total = int((meta.get("duration") or 0) * (meta.get("fps") or 0)) or 0
                step = max(total // count, 1) if total else 1
                for i in range(count):
                    try:
                        frame = reader.get_data(i * step)
                    except (IndexError, StopIteration, RuntimeError):
                        break
                    image = Image.fromarray(frame)
                    image.thumbnail((FRAME_MAX_EDGE, FRAME_MAX_EDGE))
                    buffer = io.BytesIO()
                    image.convert("RGB").save(buffer, format="JPEG", quality=80)
                    frames.append(base64.b64encode(buffer.getvalue()).decode("utf-8"))
        except Exception:
            failed = True
        finally:
            if reader is not None:
                reader.close()
        gc.collect()
    if failed:
        return []
    return frames
