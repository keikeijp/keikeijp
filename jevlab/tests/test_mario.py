from jevlab.apps import mario
from jevlab.apps.mario import GameState, MiniMario, SMB_RAM, apply_danger_policy, controller, decode_smb_ram, play, reflex_action
from jevlab.core import Jev, MockBackend, ScriptedBackend


def test_walking_into_hole_loses_life_and_respawns():
    sim = MiniMario(width=40, holes=[6, 7], enemies=[], blocks=[])
    for _ in range(10):
        sim.step("right")
        if "fell" in sim.events:
            break
    assert "fell" in sim.events
    assert sim.lives == 2
    assert sim.x == 5 and sim.y == 0 and sim.on_ground  # 直前の安全な地面に戻る


def test_jump_right_clears_hole_and_lands():
    sim = MiniMario(width=40, holes=[6, 7, 8], enemies=[], blocks=[])
    for _ in range(3):
        sim.step("right")
    assert sim.x == 5 and sim.on_ground
    state, _, _ = sim.step("jump_right")
    assert not state.on_ground and sim.y == 3
    ys = []
    for _ in range(6):
        sim.step("jump_right")
        ys.append(sim.y)
    assert ys == [5, 6, 6, 5, 3, 0]  # ジャンプ弧
    assert sim.x == 12 and sim.on_ground and sim.lives == 3


def test_stomp_and_block_bonk():
    sim = MiniMario(width=40, holes=[], enemies=[8], blocks=[(2, 3)])
    state, reward, _ = sim.step("jump")  # 真上のブロックに頭をぶつける
    assert "bonk" in sim.events and sim.y == 2
    # goomba は左へ歩き、Mario は右へ跳ぶ: 7 ステップ目に x=9 で着地と同時に踏む
    sim = MiniMario(width=40, holes=[], enemies=[16], blocks=[])
    rewards = [sim.step("jump_right")[1] for _ in range(7)]
    assert "stomp" in sim.events and rewards[-1] >= 100
    assert sim.score == 100 and sim.lives == 3 and sim.x == 9
    # 歩いて正面から当たると 1 ミス
    sim = MiniMario(width=40, holes=[], enemies=[8], blocks=[])
    for _ in range(4):
        sim.step("right")
        if "hit" in sim.events:
            break
    assert "hit" in sim.events and sim.lives == 2 and sim.x == 5


def test_observe_is_relative_and_goal_ends_game():
    sim = MiniMario(width=12, holes=[5], enemies=[9], blocks=[(7, 3)])
    state = sim.observe()
    assert state.holes == [3] and state.enemies[0] == {"dx": 7, "dy": 0, "kind": "goomba"}
    assert state.blocks == [{"dx": 5, "dy": 3}]
    assert "nearest_hole_dx" in state.to_jev()
    sim = MiniMario(width=6, holes=[], enemies=[], blocks=[])
    for _ in range(3):
        _, _, done = sim.step("run_right")
    assert done and sim.won


def test_controller_runs_with_scripted_backend_and_danger_policy():
    backend = ScriptedBackend(default={"action": "run_right", "danger": 0})
    jev = Jev(backend)
    sim = MiniMario(width=60, holes=[], enemies=[], blocks=[])
    result = play(jev, sim, steps=50, jev_every=3)
    assert result["won"] and result["steps"] == 29
    assert result["jev_calls"] <= 11  # jev_every=3 なので毎ステップは聞かない
    assert all(entry["action"] == "run_right" for entry in result["log"])
    # imminent なら run_right は jump_right に落ち、毎ステップ聞き直す
    assert apply_danger_policy("run_right", 2, 5) == ("jump_right", 1)
    assert apply_danger_policy("run_right", 1, 5) == ("right", 2)
    assert apply_danger_policy("run_right", 0, 5) == ("run_right", 5)
    # 確信度が低いときは reflex に落ちる
    low = ScriptedBackend([{"action": {"left": 0.3, "right": 0.3, "jump": 0.1, "jump_right": 0.1, "run_right": 0.1, "wait": 0.1}, "danger": 0}])
    out = controller(Jev(low), GameState(5, 0, True, holes=[2]), min_confidence=0.5)
    assert out["source"] == "reflex" and out["action"] == "jump_right"
    assert reflex_action(GameState(5, 0, True)) == "run_right"


def test_decode_smb_ram_from_fake_snapshot():
    ram = bytearray(0x800)
    ram[SMB_RAM["player_page"]] = 1
    ram[SMB_RAM["player_screen_x"]] = 0x20  # x = 256 + 32 = 288px → タイル 18
    ram[SMB_RAM["player_y"]] = 0xB0 - 32  # 地面から 2 タイル上
    ram[SMB_RAM["player_float"]] = 1
    ram[SMB_RAM["player_vx"]] = 0xFE  # -2
    ram[SMB_RAM["player_vy"]] = 0x03  # 下向き 3 → vy=-3
    ram[SMB_RAM["enemy_drawn"] + 1] = 1
    ram[SMB_RAM["enemy_type"] + 1] = 0x06  # goomba
    ram[SMB_RAM["enemy_page"] + 1] = 1
    ram[SMB_RAM["enemy_screen_x"] + 1] = 0x60  # x = 352px → タイル 22
    ram[SMB_RAM["enemy_y"] + 1] = 0xB0
    ram[SMB_RAM["lives"]] = 2
    ram[SMB_RAM["score"] : SMB_RAM["score"] + 6] = bytes([0, 0, 1, 2, 0, 0])
    ram[SMB_RAM["time"] : SMB_RAM["time"] + 3] = bytes([3, 5, 7])
    state = decode_smb_ram(ram)
    assert (state.mario_x, state.mario_y, state.on_ground) == (18, 2, False)
    assert state.velocity == {"vx": -2, "vy": -3}
    assert state.enemies == [{"dx": 4, "dy": -2, "kind": "goomba"}]
    assert state.score == 1200 and state.time_left == 357 and state.lives == 3


def test_main_play_mock(capsys):
    assert mario.main(["play", "--steps", "30", "--backend", "mock", "--render", "--jev-every", "2"]) == 0
    out = capsys.readouterr().out
    assert "M" in out and '"steps"' in out
    assert Jev(MockBackend()).decide(MiniMario().observe().to_jev(), mario.QUESTIONS).choice("action").choice in mario.ACTIONS
