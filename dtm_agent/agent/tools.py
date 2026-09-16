"""Claude に公開するツール群。すべて Session を閉じ込めたクロージャとして生成する。"""

from __future__ import annotations

import json
from typing import Optional

from anthropic import beta_tool
from pydantic import BaseModel, Field

from ..analysis import analyze_file, parse_segment
from ..daw import get_bridge
from ..models import AudioClip, MidiClip, Note, Track
from ..music import chord_pitches, scale_pitches, validate_notes
from ..reference import resolve_reference
from .session import Session


class NoteInput(BaseModel):
    pitch: int = Field(ge=0, le=127, description="MIDI ノート番号 (60=C4, 69=A4)")
    start_beat: float = Field(ge=0, description="クリップ先頭からの開始位置 (拍)")
    duration_beats: float = Field(gt=0, description="長さ (拍)")
    velocity: int = Field(default=100, ge=1, le=127)


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def build_tools(session: Session) -> list:
    @beta_tool
    def resolve_reference_url(url: str) -> str:
        """Spotify などの URL から曲名・アーティストなどのメタデータを取得する。

        音声自体は取得できないので、解析には analyze_reference_audio でローカル音声を指定する。

        Args:
            url: Spotify のトラック URL または URI
        """
        meta = resolve_reference(url)
        if meta is None:
            return _j({"error": "未対応の URL です (現在 Spotify トラック URL のみ)"})
        session.reference_meta = meta.model_dump()
        return _j(session.reference_meta)

    @beta_tool
    def analyze_reference_audio(audio_path: str, segment: str = "") -> str:
        """ローカル音声ファイルの指定区間を解析し、BPM / キー / 質感指標を返す。

        以後の search_samples は use_reference=true でこの区間に音響的に近いものを探せる。

        Args:
            audio_path: 解析する音声ファイルのパス (wav/mp3/flac など)
            segment: 区間。'1:05-1:21' や '65-81' (秒)。空なら全体
        """
        start, end = parse_segment(segment or None)
        profile = analyze_file(audio_path, start, end)
        session.reference = profile
        session.log.append(f"analyzed {audio_path} {segment}: {profile.describe()}")
        return _j({
            "summary": profile.describe(),
            "bpm": round(profile.bpm, 1),
            "key": profile.key,
            "key_confidence": round(profile.key_confidence, 2),
            "energy": round(profile.energy, 2),
            "brightness": round(profile.brightness, 2),
            "percussiveness": round(profile.percussiveness, 2),
            "tonalness": round(profile.tonalness, 2),
            "chroma": [round(c, 3) for c in profile.chroma],
        })

    @beta_tool
    def search_samples(query: str = "", use_reference: bool = True, limit: int = 8,
                       sources: str = "local,freesound") -> str:
        """サンプルを検索する。

        ローカルライブラリ (音響類似 + ファイル名キーワード) と Freesound (キーワード) を横断する。
        query は英語のキーワード (例: 'kick 808', 'pad warm analog', 'vocal chop') が有効。

        Args:
            query: 英語キーワード。空でも use_reference=true なら参照区間の音響類似で検索する
            use_reference: analyze_reference_audio の結果を類似検索に使うか
            limit: 各ソースの最大件数
            sources: 'local', 'freesound' をカンマ区切りで
        """
        wanted = {s.strip() for s in sources.split(",") if s.strip()}
        ref = session.reference if use_reference else None
        results: dict[str, list] = {}
        if "local" in wanted:
            if session.library is None:
                results["local"] = {"error": "ローカルライブラリ未設定 (--library でフォルダを指定)"}
            elif not session.library.entries:
                results["local"] = {"error": "インデックス未作成 (dtm-agent index <folder> を実行)"}
            else:
                hits = session.library.search(query=query or None, reference=ref, limit=limit)
                results["local"] = [h.model_dump(exclude_none=True) for h in hits]
        if "freesound" in wanted:
            if not session.freesound.available():
                results["freesound"] = {"error": "FREESOUND_API_KEY 未設定"}
            else:
                hits = session.freesound.search(query=query or None, reference=ref, limit=limit)
                results["freesound"] = [h.model_dump(exclude_none=True) for h in hits]
        return _j(results)

    @beta_tool
    def scale_info(key: str) -> str:
        """キーのスケール構成音 (C3-C6) とダイアトニックコード (三和音) の MIDI ノート番号を返す。

        Args:
            key: 例 'A minor', 'F# major', 'D dorian'
        """
        pitches = scale_pitches(key, 48, 84)
        chords = {f"degree_{d + 1}": chord_pitches(key, d, octave=3) for d in range(7)}
        return _j({"key": key, "scale_pitches_C3_C6": pitches, "diatonic_triads": chords})

    @beta_tool
    def set_project(title: str, bpm: float, key: str) -> str:
        """プロジェクトのタイトル / テンポ / キーを設定する。ノートを置く前に必ず呼ぶ。

        Args:
            title: 曲名 (ファイル名にも使う)
            bpm: テンポ
            key: 例 'A minor'
        """
        scale_pitches(key)  # 検証
        session.plan.title, session.plan.bpm, session.plan.key = title, float(bpm), key
        return _j({"ok": True, "title": title, "bpm": bpm, "key": key})

    @beta_tool
    def add_midi_clip(track_name: str, clip_name: str, notes: list[NoteInput], length_beats: float,
                      instrument_hint: str = "", allow_out_of_scale: bool = False) -> str:
        """MIDI トラックにクリップ (ノート列) を追加する。同名トラックがあれば追記する。

        ノートが現在のキーのスケール外なら拒否する (意図的なら allow_out_of_scale=true)。

        Args:
            track_name: トラック名 (例 'Lead', 'Bass', 'Chords')
            clip_name: クリップ名
            notes: ノート列
            length_beats: クリップ長 (拍)。4/4 なら 1 小節 = 4 拍
            instrument_hint: 音色の指示 (例 'warm analog pad')。DAW 側でユーザーが音色を選ぶ手掛かり
            allow_out_of_scale: スケール外の音を許可するか
        """
        note_models = [Note(**n.model_dump()) for n in notes]
        problems = validate_notes(note_models, session.plan.key)
        if problems and not allow_out_of_scale:
            return _j({"ok": False, "problems": problems, "hint": "scale_info で構成音を確認するか allow_out_of_scale=true"})
        if any(n.start_beat + n.duration_beats > length_beats + 1e-6 for n in note_models):
            return _j({"ok": False, "problems": ["length_beats を超えるノートがあります"]})
        track = session.plan.find_track(track_name)
        if track is None:
            track = session.plan.add_track(Track(name=track_name, kind="midi", instrument_hint=instrument_hint or None))
        elif instrument_hint and not track.instrument_hint:
            track.instrument_hint = instrument_hint
        track.midi_clips.append(MidiClip(name=clip_name, notes=note_models, length_beats=length_beats))
        return _j({"ok": True, "track": track_name, "clips": len(track.midi_clips), "notes": len(note_models),
                   "warnings": problems})

    @beta_tool
    def add_audio_clip(track_name: str, sample_path: str, start_beat: float = 0.0,
                       gain_db: float = 0.0, clip_name: str = "") -> str:
        """オーディオトラックにサンプルを配置する (search_samples の path を使う)。

        Args:
            track_name: トラック名 (例 'Drums', 'FX')
            sample_path: サンプルファイルのパス (Freesound の場合はプレビュー URL)
            start_beat: 配置位置 (拍)
            gain_db: ゲイン (dB)
            clip_name: クリップ名 (空ならファイル名)
        """
        from pathlib import Path

        track = session.plan.find_track(track_name)
        if track is None:
            track = session.plan.add_track(Track(name=track_name, kind="audio"))
        track.audio_clips.append(AudioClip(name=clip_name or Path(sample_path).stem, path=sample_path,
                                           start_beat=start_beat, gain_db=gain_db))
        return _j({"ok": True, "track": track_name, "clips": len(track.audio_clips)})

    @beta_tool
    def add_note_for_user(text: str) -> str:
        """ユーザーへの申し送り (音色の選び方、エフェクトの提案、次のステップなど) を記録する。

        Args:
            text: 申し送り内容
        """
        session.plan.notes_for_user.append(text)
        return _j({"ok": True})

    @beta_tool
    def realize_in_daw(daw: str = "") -> str:
        """設計図を DAW に反映する。'file' (汎用 MIDI 書き出し) / 'reaper' (.rpp 生成) / 'ableton' (AbletonOSC)。

        すべてのトラックを追加し終えてから最後に 1 回呼ぶ。

        Args:
            daw: 空ならセッション既定の DAW
        """
        name = daw or session.daw
        kwargs = {"host": session.ableton_host, "port": session.ableton_port} if name == "ableton" else {}
        bridge = get_bridge(name, session.out_dir, **kwargs)
        written = bridge.realize(session.plan)
        return _j({"ok": True, "daw": name, "files": [str(p) for p in written]})

    @beta_tool
    def show_plan() -> str:
        """現在の設計図 (トラック / クリップ / ノート数) を確認する。"""
        return session.plan.model_dump_json(indent=None)

    return [
        resolve_reference_url, analyze_reference_audio, search_samples, scale_info,
        set_project, add_midi_clip, add_audio_clip, add_note_for_user, realize_in_daw, show_plan,
    ]
