"""Ableton Live へ AbletonOSC 経由でリアルタイム配置する。

前提: Live に AbletonOSC (https://github.com/ideoforms/AbletonOSC) を Control Surface として
インストールし、ポート 11000 で待ち受けていること。

- MIDI トラック / クリップ / ノート: OSC で直接作成 (実装済み)
- オーディオサンプル: Live API はファイル読み込みを公開していないため、
  サンプルは out_dir/samples にコピーし、Live のブラウザからドラッグしてもらう (FileBridge と同じ)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ..models import ProjectPlan
from .file_export import FileBridge

Sender = Callable[[str, list[Any]], None]


class AbletonOscBridge:
    name = "ableton"

    def __init__(self, out_dir: str | Path, *, host: str = "127.0.0.1", port: int = 11000,
                 sender: Optional[Sender] = None, start_track_index: Optional[int] = None) -> None:
        self.out_dir = Path(out_dir)
        self.host, self.port = host, port
        self._sender = sender
        self.start_track_index = start_track_index
        self.sent: list[tuple[str, list[Any]]] = []

    def _send(self, address: str, args: list[Any]) -> None:
        self.sent.append((address, args))
        if self._sender is None:
            from pythonosc.udp_client import SimpleUDPClient

            self._sender = SimpleUDPClient(self.host, self.port).send_message
        self._sender(address, args)

    def _num_tracks(self) -> int:
        """Live に現在のトラック数を問い合わせる (新規トラックを末尾に追加するため)。"""
        if self.start_track_index is not None:
            return self.start_track_index
        if self._sender is not None:
            return 0  # テスト用 sender が差し込まれている場合
        import threading

        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer

        result: dict[str, int] = {}
        done = threading.Event()

        def on_reply(_addr: str, *args: Any) -> None:
            result["n"] = int(args[0])
            done.set()

        dispatcher = Dispatcher()
        dispatcher.map("/live/song/get/num_tracks", on_reply)
        server = ThreadingOSCUDPServer((self.host, self.port + 1), dispatcher)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self._send("/live/song/get/num_tracks", [])
            if not done.wait(timeout=2.0):
                raise TimeoutError("AbletonOSC から応答がありません (Live と AbletonOSC が起動しているか確認)")
        finally:
            server.shutdown()
        return result["n"]

    def realize(self, plan: ProjectPlan) -> list[Path]:
        self._send("/live/song/set/tempo", [float(plan.bpm)])
        track_index = self._num_tracks()
        for track in plan.tracks:
            if track.kind != "midi":
                continue
            idx = track_index
            self._send("/live/song/create_midi_track", [idx])
            self._send("/live/track/set/name", [idx, track.name])
            for slot, clip in enumerate(track.midi_clips):
                self._send("/live/clip_slot/create_clip", [idx, slot, float(clip.length_beats)])
                self._send("/live/clip/set/name", [idx, slot, clip.name])
                for n in clip.notes:
                    self._send(
                        "/live/clip/add/notes",
                        [idx, slot, n.pitch, float(n.start_beat), float(n.duration_beats), n.velocity, 0],
                    )
            track_index += 1
        # オーディオは汎用出力にフォールバック
        return FileBridge(self.out_dir).realize(plan)
