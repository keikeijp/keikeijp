# 22. home — Home Assistant の家の状態を Jev で判定して通知

元ネタ: [AboveColin/HA-Jev](https://github.com/AboveColin/HA-Jev)

> **安全用途には使わない。** 鍵・警報・コンロ・ガス・煙/CO 検知・医療機器には絶対に使わないこと。
> このアプリが呼べる HA サービスは `notify.*` だけで、ルール検証で他のサービスは拒否される。

## 目的

「洗濯が終わった?」「キッチンの電気がつけっぱなし?」のような、センサー値をルールで書くのが面倒な日常判断を
Jev の `noul` に任せて通知する。Jev は各ルールが列挙した entity の state・主要属性・直近履歴だけを見る。

## ルール (YAML / JSON)

```json
{"rules": [
  {"name": "laundry_done",
   "entities": ["sensor.washer_power", "binary_sensor.washer_door"],
   "question": "Has the washing machine finished its cycle (power dropped to idle after running)?",
   "history_hours": 2, "threshold": 0.8, "cooldown_minutes": 120,
   "notify": {"service": "notify.mobile_app_phone", "message": "洗濯が終わったよ ({probability})"}}
]}
```

- `entities`: Jev に見せる entity (これ以外は送らない)
- `history_hours`: 直近履歴を最大 24 点まで添える (電力の推移などに)
- `threshold` (既定 0.8) / `cooldown_minutes` (既定 60): 通知の閾値と再通知の間隔
- `notify.service`: `notify.*` のみ

## 使い方

```bash
# オフライン (HA の状態を JSON で)
jevlab home --rules rules.json --states states.json --dry-run

# 実機 (長期アクセストークン)
export HA_URL=http://homeassistant.local:8123 HA_TOKEN=...
jevlab home --rules rules.yaml --dry-run                 # まず表示だけ
jevlab home --rules rules.yaml --memory memory.json --loop --interval 300   # 5 分毎に常駐
```

`--memory` に最終通知時刻を保存して cooldown を効かせる。

## 追加依存

なし (YAML ルールを使うなら `pip install pyyaml`)。

## 課金・外部送信

Jev にはルールごとの小さな state (entity_id、state、一部属性、直近履歴) が送られる。
ルール数 × 実行間隔 が呼び出し回数。

## 制限

- Jev は「今この瞬間の state + 短い履歴」しか見ない。長期の傾向は HA 側の統計センサーにしてから渡す
- 判定が難しいルールは `threshold` を上げるか、`question` に判断基準 (数値の目安) を書く
