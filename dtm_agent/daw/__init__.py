from .base import DawBridge
from .file_export import FileBridge
from .reaper import ReaperProjectBridge
from .ableton import AbletonOscBridge

__all__ = ["DawBridge", "FileBridge", "ReaperProjectBridge", "AbletonOscBridge"]


def get_bridge(name: str, out_dir, **kwargs) -> DawBridge:
    name = name.lower()
    if name in ("file", "midi"):
        return FileBridge(out_dir)
    if name == "reaper":
        return ReaperProjectBridge(out_dir)
    if name == "ableton":
        return AbletonOscBridge(out_dir, **kwargs)
    raise ValueError(f"unknown DAW bridge: {name} (file / reaper / ableton)")
