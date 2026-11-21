# axis-vapix-configurator

複数台の Axis ネットワークカメラへ、VAPIX 経由で共通設定を一括適用する Python スクリプト。
サブネットが異なるカメラには、自分の IP を変更せず一時 IP（TemporaryIP）を追加して到達します。
設計の詳細は [docs/DESIGN.md](docs/DESIGN.md) を参照してください。

## 動作環境

- Windows（TemporaryIP を使う場合。管理者権限が必要）
- Python 3.10 以上
- 依存ライブラリなし（Python 標準ライブラリのみ）

## 使い方

対象は `--ip` で複数指定します。各値系オプションは **1 個なら全台共通、台数分なら台別、
未指定ならその処理をしない**、それ以外の個数はエラーになります（実行前に全て検証します）。

```bat
rem 3台まとめて：Hz は全台共通 60、パスワードは台別、IP は台別に変更
python axis_vapix_configurator.py --ip 192.168.0.90 192.168.0.91 192.168.1.90 ^
  --user root --pass passA passB passC ^
  --hz 60 --ntp 192.168.10.5 ^
  --new-ip 10.0.0.11 10.0.0.12 10.0.0.13 --new-pass newpass
```

```bat
rem IP 保持の初期化（Restore）。単独実行専用で、他の変更オプションとは併用できません
python axis_vapix_configurator.py --ip 192.168.0.90 --pass passA --restore
```

```bat
rem 実行内容の確認だけ（変更リクエストは送らない）
python axis_vapix_configurator.py --ip 192.168.0.90 --pass passA --hz 60 --dry-run

rem 診断：各対象のパラメータ全件を表示し、params_<IP>.txt に保存する
python axis_vapix_configurator.py --ip 192.168.0.90 --pass passA --dump-params
```

主なオプション：

| オプション | 内容 |
|---|---|
| `--ip` | 対象カメラの変更前 IP（必須、複数指定） |
| `--user` / `--pass` | アクセス用ユーザー／パスワード（既定ユーザー root） |
| `--hz` | 電源周波数 50 / 60 |
| `--new-ip` / `--new-mask` / `--new-gateway` | 変更後の IP／マスク／ゲートウェイ（マスク・ゲートウェイは任意） |
| `--new-pass` | 変更後のパスワード |
| `--ntp` | NTP サーバーの IP/ホスト名、または `manual`（マニュアル時刻へ切替） |
| `--restore` | IP 保持の初期化（単独実行専用） |
| `--interface` | TemporaryIP を追加する NIC 名（未指定なら自動選択） |
| `--dry-run` / `--dump-params` / `--timeout` | 実行内容の表示のみ／パラメータ診断／HTTP タイムアウト秒 |

## TemporaryIP

対象と同じサブネットに自分の IP がある場合は何もしません。到達できない対象は
/24 ネットワーク単位でグループ化し、そのネットワーク向けの一時 IP（候補は .250 から、
コンフリクト確認済み）を 1 つ追加して処理し、終了時・例外時を問わず必ず削除します。
強制終了に備え、追加した IP は状態ファイルに記録され、次回起動時に残存分を掃除します。

## バージョン規則

初期バージョンは 0.9.0.0。バグ修正では末端を +1、機能追加では下から2番目を +1 します。

## 注意事項

- TemporaryIP の追加には管理者権限が必要です。管理者として実行してください。
- `--restore`（IP 保持の初期化）はユーザー／パスワードを保持しません。
  初期化後は管理者アカウントの作り直しが必要になるため、単独で実行してください。
- ログはスクリプトのあるフォルダの `axis_vapix_configurator.log` に追記されます
  （パスワードは記録されません）。
- **実機検証状況**：Hz のパラメータ名は実行時にパラメータ一覧から特定する方式ですが、
  実機での一意な特定は未検証です。また、マニュアル時刻切替で使う
  `date.cgi` による時刻セットも要実機検証です。
