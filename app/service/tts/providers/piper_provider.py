from __future__ import annotations

import os
import sys
import tempfile
import shlex
import select
import time
import subprocess
from typing import Iterable, Optional

from .base import SynthesisProvider


class PiperProvider(SynthesisProvider):
    name = "piper"

    def __init__(self, piper_bin: str, model_path: str):
        self.piper_bin = piper_bin
        self.model_path = model_path

    def synth_stream(
        self, text: str, *, voice: Optional[str] = None, speed: float = 1.0, fmt: str = "wav"
    ) -> Iterable[bytes]:
        """Run Piper and stream audio bytes from stdout.

        We use --output_raw for low latency PCM16 frames; however, to keep
        client simple we allow Piper to output WAV by omitting --output_raw
        when fmt == 'wav'. For now, use WAV to allow easy blob assembly.
        """
        # --- Preflight checks for clearer errors ---
        exe = os.path.expanduser(os.path.expandvars(self.piper_bin))
        model = os.path.expanduser(os.path.expandvars(self.model_path))
        if not os.path.isfile(exe):
            raise FileNotFoundError(f"Piper binary not found at {exe}")
        if not os.access(exe, os.X_OK):
            raise PermissionError(f"Piper binary not executable: {exe}")
        # If a voice path is provided and exists, treat it as the model override (for Piper)
        if voice:
            vpath = os.path.expanduser(os.path.expandvars(str(voice)))
            if os.path.isfile(vpath):
                model = vpath
        if not os.path.isfile(model):
            raise FileNotFoundError(f"Piper model not found at {model}")

        # Write WAV to a temporary file (pip CLI cannot write WAV to non-seekable stdout)
        tmp_file = tempfile.NamedTemporaryFile(prefix="piper_", suffix=".wav", delete=False)
        tmp_path = tmp_file.name
        tmp_file.close()
        # Map speed to Piper's --length_scale (inverse: larger length_scale -> slower)
        try:
            spd = float(speed)
        except Exception:
            spd = 1.0
        # Clamp sensible range
        if spd <= 0:
            spd = 1.0
        if spd < 0.25:
            spd = 0.25
        if spd > 3.0:
            spd = 3.0
        length_scale = 1.0 / spd
        cmd = (
            f"{shlex.quote(exe)} --model {shlex.quote(model)} "
            f"--length_scale {length_scale:.3f} "
            f"--output_file {shlex.quote(tmp_path)}"
        )
        env = os.environ.copy()
        # Help the bundled Piper find libraries & espeak-ng data on macOS/Linux
        piper_dir = os.path.dirname(exe)
        # Optional overrides via env
        lib_dir_override = os.getenv("PIPER_LIB_DIR")
        espeak_data_override = os.getenv("ESPEAKNG_DATA") or os.getenv("PIPER_ESPEAK_DATA")
        candidate_lib_dirs = [d for d in [lib_dir_override, piper_dir, "/opt/homebrew/lib", "/usr/local/lib"] if d]
        # Prepend candidate lib dirs for dynamic loader
        if os.name == "posix":
            # macOS uses DYLD_LIBRARY_PATH; Linux uses LD_LIBRARY_PATH
            if sys.platform == "darwin":  # type: ignore
                prior = env.get("DYLD_LIBRARY_PATH", "")
                env["DYLD_LIBRARY_PATH"] = ":".join([*candidate_lib_dirs, prior]) if prior else ":".join(candidate_lib_dirs)
            else:
                prior = env.get("LD_LIBRARY_PATH", "")
                env["LD_LIBRARY_PATH"] = ":".join([*candidate_lib_dirs, prior]) if prior else ":".join(candidate_lib_dirs)
        # espeak-ng data (voices, phonemes)
        if espeak_data_override:
            env["ESPEAKNG_DATA"] = espeak_data_override
        else:
            guess_candidates = [
                os.path.join(piper_dir, "espeak-ng-data"),
                "/opt/homebrew/share/espeak-ng",
                "/usr/local/share/espeak-ng",
            ]
            for gd in guess_candidates:
                if os.path.isdir(gd):
                    env["ESPEAKNG_DATA"] = gd
                    break
        # Piper supports SSML-like rate tags; for now we ignore speed, or we could wrap text
        proc = subprocess.Popen(
            shlex.split(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
        )
        assert proc.stdin is not None
        # Write newline-terminated input and flush to ensure Piper starts synthesis
        data = (text if text.endswith("\n") else text + "\n").encode("utf-8")
        proc.stdin.write(data)
        try:
            proc.stdin.flush()
        except Exception:
            pass
        proc.stdin.close()
        # If process exited early, surface a helpful error
        try:
            code = proc.poll()
            if code is not None and code != 0:
                err = proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else ""
                hint = ""
                if "Library not loaded" in err or "image not found" in err:
                    hint = (
                        "Piper failed to load dynamic libraries. On macOS, set DYLD_LIBRARY_PATH or PIPER_LIB_DIR to include libespeak-ng/libonnxruntime, "
                        "and set ESPEAKNG_DATA if needed. Try installing espeak-ng via Homebrew or bundling libs next to the binary."
                    )
                raise RuntimeError(f"Piper exited with code {code}. stderr: {err}\n{hint}")
        except Exception:
            pass
        # Wait for Piper to finish writing the WAV file
        retcode = proc.wait()
        stderr_out = ""
        try:
            if proc.stderr:
                stderr_out = proc.stderr.read().decode("utf-8", errors="ignore")
        except Exception:
            stderr_out = ""
        if retcode != 0:
            raise RuntimeError(f"Piper exited with code {retcode}. stderr: {stderr_out}")
        # Stream the generated file in chunks
        CHUNK = 64 * 1024
        produced_any = False
        try:
            with open(tmp_path, "rb") as f:
                while True:
                    buf = f.read(CHUNK)
                    if not buf:
                        break
                    produced_any = True
                    yield buf
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        # Drain stderr for logs (non-blocking best effort)
        # Ensure some audio was produced; if not, raise a helpful error
        if not produced_any:
            hint = []
            if "Library not loaded" in stderr_out or "image not found" in stderr_out:
                hint.append(
                    "Piper couldn't load dynamic libraries. On macOS with Apple Silicon, use the arm64 Piper and install espeak-ng/onnxruntime/libsndfile via Homebrew under /opt/homebrew. "
                    "Set PIPER_LIB_DIR=/opt/homebrew/lib and ESPEAKNG_DATA if needed."
                )
            if "Permission denied" in stderr_out:
                hint.append("Remove quarantine and add execute bit: xattr -dr com.apple.quarantine /path/to/piper && chmod +x /path/to/piper")
            msg = f"Piper did not produce audio. stderr: {stderr_out.strip()[:800]}"
            if hint:
                msg += "\n" + " ".join(hint)
            raise RuntimeError(msg)

    def get_word_marks(self, text: str, *, voice: Optional[str] = None, speed: float = 1.0) -> list[dict]:
        """Piper CLI currently not wired for word timing marks in this app.

        Return an empty list to signal unsupported capability.
        """
        return []
