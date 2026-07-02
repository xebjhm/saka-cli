"""Best-effort hardware detection + a local-vs-cloud LLM backend suggestion.

No hard dependency on `psutil` or any GPU SDK: `detect_hardware()` probes RAM via
`os.sysconf` (Linux/macOS) or `ctypes`+`GlobalMemoryStatusEx` (Windows), and GPU/VRAM via
`nvidia-smi` (any platform that has it) or Apple Silicon unified-memory detection on
macOS. Every probe is individually best-effort and the whole function is safe to call
unconditionally -- it never raises, it just reports `None` for what it couldn't detect.

`suggest_llm_backend()` is pure logic over the resulting dict (or a hand-made one, for
tests / "what-if" queries): it maps VRAM/RAM tiers to a recommended backend, model, and
tier label, per the local-hardware research baked into the tier thresholds below.
"""

from __future__ import annotations

import platform
import subprocess
from typing import Any

import structlog

logger = structlog.get_logger()

_NVIDIA_SMI_TIMEOUT = 5.0


def detect_hardware() -> dict[str, Any]:
    """Best-effort, cross-platform hardware probe. Never raises.

    Returns a dict with `ram_gb` (float|None), `gpu` (str|None), `vram_gb` (float|None),
    and `platform` (str, from `platform.system()`). Each individual probe already guards
    its own failures, but every call is wrapped again here so a probe raising something
    unexpected still can't take down a caller that just wants a best-effort suggestion.
    """
    hw: dict[str, Any] = {
        "ram_gb": _safe_call(_detect_ram_gb),
        "gpu": None,
        "vram_gb": None,
        "platform": platform.system(),
    }

    gpu, vram_gb = _safe_call(_detect_nvidia_gpu) or (None, None)
    if gpu is None and _safe_call(_is_apple_silicon):
        # Apple Silicon has no discrete VRAM -- unified memory is shared with the CPU and
        # usable as "VRAM" for a locally-run model, so treat it as such (report vram_gb
        # approximately equal to system RAM).
        gpu = "Apple Silicon"
        vram_gb = hw["ram_gb"]

    hw["gpu"] = gpu
    hw["vram_gb"] = vram_gb

    logger.debug("hardware.detect_hardware.done", **hw)
    return hw


def _safe_call(fn):
    """Run `fn()`, returning `None` (or `(None, None)`-safe by caller) on any exception."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- best-effort probe, must never propagate
        logger.debug(
            "hardware.probe_failed",
            probe=getattr(fn, "__name__", repr(fn)),
            error=str(exc),
        )
        return None


def _detect_ram_gb() -> float | None:
    """Total physical RAM in GiB, or `None` if it can't be determined."""
    try:
        import os

        if (
            hasattr(os, "sysconf")
            and "SC_PAGE_SIZE" in os.sysconf_names
            and "SC_PHYS_PAGES" in os.sysconf_names
        ):
            page_size = os.sysconf("SC_PAGE_SIZE")
            phys_pages = os.sysconf("SC_PHYS_PAGES")
            if page_size > 0 and phys_pages > 0:
                return round((page_size * phys_pages) / (1024**3), 2)
    except (ValueError, OSError, AttributeError) as exc:
        logger.debug("hardware.detect_ram_gb.sysconf_failed", error=str(exc))

    if platform.system() == "Windows":
        return _detect_ram_gb_windows()
    return None


def _detect_ram_gb_windows() -> float | None:
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return round(stat.ullTotalPhys / (1024**3), 2)
    except (OSError, ValueError, AttributeError) as exc:
        logger.debug("hardware.detect_ram_gb.windows_failed", error=str(exc))
    return None


def _detect_nvidia_gpu() -> tuple[str | None, float | None]:
    """`(gpu_name, vram_gb)` for the first NVIDIA GPU reported by `nvidia-smi`, or `(None, None)`."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_NVIDIA_SMI_TIMEOUT,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            first_line = result.stdout.strip().splitlines()[0]
            name, _, vram_mib = first_line.partition(",")
            name = name.strip()
            vram_mib_str = vram_mib.strip()
            if name and vram_mib_str:
                return name, round(float(vram_mib_str) / 1024, 2)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.debug("hardware.detect_nvidia_gpu.failed", error=str(exc))
    return None, None


def _is_apple_silicon() -> bool:
    try:
        if platform.system() != "Darwin":
            return False
        machine = platform.machine().lower()
        processor = (platform.processor() or "").lower()
        return machine in ("arm64", "aarch64") or "apple" in processor
    except OSError as exc:
        logger.debug("hardware.is_apple_silicon.failed", error=str(exc))
        return False


def suggest_llm_backend(hw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recommend a local-vs-cloud LLM backend (and, for local, a model) given `hw`.

    `hw` defaults to a fresh `detect_hardware()` call. Returns
    `{"recommended": "local"|"cloud", "local_model": str|None, "tier": str, "reason": str}`.
    """
    if hw is None:
        hw = detect_hardware()

    vram_gb = hw.get("vram_gb")
    ram_gb = hw.get("ram_gb")
    gpu = hw.get("gpu")

    suggestion: dict[str, Any]
    if vram_gb is not None and vram_gb >= 24:
        suggestion = {
            "recommended": "local",
            "local_model": "qwen3:32b",
            "tier": "T2",
            "reason": (
                "GPU reports >=24GB VRAM, enough headroom for a 32B-class model with strong "
                "local tool-calling accuracy -- local is recommended, cloud is optional."
            ),
        }
    elif vram_gb is not None and vram_gb >= 10:
        suggestion = {
            "recommended": "local",
            "local_model": "qwen2.5:14b",
            "tier": "T1",
            "reason": (
                "GPU reports 10-24GB VRAM, enough for Qwen2.5-14B, which benchmarks close to "
                "GPT-4 on local tool-selection tasks -- local is recommended."
            ),
        }
    elif vram_gb is not None and vram_gb >= 6:
        suggestion = {
            "recommended": "local",
            "local_model": "qwen2.5:7b",
            "tier": "T1-small",
            "reason": (
                "GPU reports 6-10GB VRAM, borderline for a 7-9B local model (e.g. qwen2.5:7b "
                "or nemotron-nano-9b-jp) -- local is usable but optional; cloud (Gemini Flash) "
                "is also a fine choice at this tier."
            ),
        }
    elif gpu == "Apple Silicon" and ram_gb is not None and ram_gb >= 16:
        suggestion = {
            "recommended": "local",
            "local_model": "qwen2.5:14b",
            "tier": "T1",
            "reason": (
                "Apple Silicon with >=16GB unified memory can comfortably run Qwen2.5-14B "
                "locally, since the GPU shares the system's unified memory pool -- local is "
                "recommended."
            ),
        }
    else:
        suggestion = {
            "recommended": "cloud",
            "local_model": None,
            "tier": "cloud",
            "reason": (
                "No capable local GPU detected (or hardware is unknown/insufficient) -- "
                "cloud (Gemini Flash via the OpenAI-compatible endpoint) is recommended."
            ),
        }

    logger.info("hardware.suggest_llm_backend", hardware=hw, **suggestion)
    return suggestion
