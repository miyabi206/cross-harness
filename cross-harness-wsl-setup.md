# cross-harness WSL2 セットアップ指示書

あなた（Claude Code）は、**WSL2 上の Ubuntu-24.04** に個人用ツール `cross-harness` を
導入するセットアップ担当です。この指示書を **上から順に** 実行してください。各ステップには
「実行コマンド」「合格条件」「失敗時の対応」があります。合格条件を満たさないまま次へ進まないこと。

対象トポロジ: Claude（親・オーケストレータ）→ Codex（子・実行）の一方向・一段のみ。
このマシンは native Linux なので、macOS 専用の Seatbelt サンドボックス（Claude 書き込みロールの封じ込め）は
`platform_not_darwin` として**無効になりますが、これは想定内**です（既定では書き込み実行は Codex 側で、
Linux の Landlock/seccomp 実サンドボックスが効きます）。

---

## 0. 絶対ルール（全ステップ共通のガードレール）

1. **作業場所は WSL ネイティブFS（`$HOME` 配下 = ext4）のみ。`/mnt/c` や `/mnt/host` 配下では絶対に作業しない。**
   （クロスOSマウントは遅く、symlink/chmod/権限が壊れ、インストーラが失敗する）
2. **資格情報を一切運ばない/コピーしない/ログに出さない。** API キー、トークン、`auth.json`、
   `.env`、keychain 値をハーネス間・マシン間で渡さない。認証はすべて**このWSL内で新規に**行う。
3. **root で常用しない。** 一般ユーザーで作業し、`apt` などOSパッケージ導入時のみ `sudo` を使う。
4. **`*_API_KEY` を設定しない/使わない。** Codex は保存済み ChatGPT 認証、Claude はサブスク認証のみ。
   APIビリングや外部ルーター、カスタム base URL へ切り替えない。
5. **停止条件（末尾「停止条件」参照）に当たったら、勝手に回避せず停止して人間に報告する。**
6. **対話が必要な認証・信頼操作（🧑マーク）は、あなたは実行できない。** 該当ステップでは手を止め、
   人間に何をどの端末で行うかを提示し、完了を待ってから検証コマンドだけを実行する。

---

## 1. 事前確認 — 合格ライン4点（NG が1つでもあれば停止して報告）

```sh
cd ~
pwd                                   # 1) /home/<user> であること（/mnt/... はNG）
whoami                                # 2) 一般ユーザー（root はNG。プロンプト末尾が $ で # でない）
grep VERSION= /etc/os-release         # 3) Ubuntu 24.04 (Noble)
uname -r                              # 4) 文字列に "WSL2" を含む（例: 6.x...-microsoft-standard-WSL2）
```

- **合格条件**: 4項目すべて OK。
- **失敗時**: どれがNGかを報告して停止。特に `uname -r` に `WSL2` が無い（WSL1）場合、
  Codex の実サンドボックスが効かないため、人間に「Windows側で `wsl --set-version Ubuntu-24.04 2`」を依頼する。

---

## 2. 前提ツール導入

### 2-1. 基本ツール
```sh
sudo apt update
sudo apt install -y git python3 python3-venv curl
python3 --version                     # 3.11 以上（24.04 なら 3.12.x）
```
- **合格条件**: `python3 --version` が **3.11 以上**。3.10 以下なら停止して人間に報告（Ubuntuのバージョン取り違え）。

### 2-2. GitHub CLI（`gh`）
まず apt を試し、無ければ公式リポジトリを使う。
```sh
sudo apt install -y gh || {
  (type -p wget >/dev/null || sudo apt install -y wget)
  sudo mkdir -p -m 755 /etc/apt/keyrings
  wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt update && sudo apt install -y gh
}
gh --version
```
- **合格条件**: `gh --version` が表示される。

### 2-3. Node.js（nvm 経由）＋ Codex CLI
`sudo` を使わず nvm でユーザー空間に入れる（グローバル npm の権限問題を避ける）。
```sh
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install --lts
node --version && npm --version
npm install -g @openai/codex
codex --version
```
- **合格条件**: `node --version` と `codex --version` が表示される。

### 2-4. Claude Code の存在確認
あなた自身が Claude Code なので通常は導入済み。確認のみ。
```sh
claude --version
```
- **合格条件**: バージョンが表示される。表示されなければ、人間に WSL 内での Claude Code 導入を依頼して停止。

### 2-5. PATH 設定（インストーラが作る `~/.local/bin` を通す）
```sh
grep -q 'HOME/.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
export PATH="$HOME/.local/bin:$PATH"
```

---

## 3. 認証 🧑（人間が対話で実施 → あなたは検証のみ）

これらはブラウザ/デバイス認証で、あなた（非対話ツール実行）では完了できない。
**手を止め、人間に次を依頼し、完了後に検証コマンドだけ実行すること。**

依頼内容（人間向け）:
1. `gh auth login` を実行 → GitHub.com / HTTPS / ブラウザ認証で **miyabi206** にログイン
2. `codex login` を実行 → ChatGPT サブスク認証（`*_API_KEY` は使わない）
3. `claude` を起動しサブスク認証（未ログインなら）

人間の完了後、あなたが検証:
```sh
gh auth status                        # Logged in to github.com（miyabi206）
codex login status                    # ChatGPT 認証済みであること
claude auth status                    # 認証済みサブスクリプションであること
```
- **合格条件**: 3つとも「認証済み」。
- **失敗/不明時**: 停止して報告（**API キーへ切り替えたり、認証を自動化したりしない**）。

---

## 4. リポジトリ取得（プライベート）

```sh
cd ~
gh repo clone miyabi206/cross-harness
cd ~/cross-harness
git log --oneline -1                  # 最新コミットが取得できていること
```
- **合格条件**: `~/cross-harness` に clone され、`git log` が出る。作業ディレクトリが `/mnt/...` でないこと。
- **失敗時**: private リポジトリへのアクセス権（miyabi206 でのログイン）を確認。別アカウントなら collaborator 追加が必要 → 人間に報告して停止。

---

## 5. cross-harness インストール

まず**非破壊チェック**を通し、問題なければ本導入する。

```sh
cd ~/cross-harness
# 5-1. 設定の妥当性チェック（非破壊）
./bin/cross-harness validate --config config/default.toml
# 5-2. 既存設定のバックアップ目録（非破壊）
./bin/cross-harness inventory --output docs/inventory.md --backup .local/backups/pre-install
# 5-3. ドライラン（非破壊。何が起きるかの確認）
./bin/cross-harness install --dry-run
```
- **合格条件**: 3つともエラーなく完了。ドライランの計画に不審な破壊操作がないこと。
- **失敗時**: 出力を添えて停止・報告。

```sh
# 5-4. 本導入
./bin/cross-harness install
# 5-5. 健全性チェック
~/.local/bin/cross-harness doctor
```
- **合格条件**: `doctor` が問題を報告しない（グリーン）。
- **注意**: `platform_not_darwin`（Seatbelt 無効）は**想定内で問題なし**。それ以外の失敗は報告。
- **symlink 作成失敗**が出たら、作業場所が `/mnt/...` でないか（ステップ0違反）を最優先で疑う。

---

## 6. Codex フックの信頼付け 🧑（人間が Codex 上で実施 → あなたは確認コマンドのみ）

これは Codex ネイティブのセキュリティ境界で、インストーラでは自動化されない。

依頼内容（人間向け）:
1. `codex` を起動し `/hooks` を開く
2. ユーザーレベルの**再帰ガード（recursion guard）フック**の定義を目視確認し、その**正確な定義を trust** する

人間の完了後、あなたが実行:
```sh
~/.local/bin/cross-harness trust codex-hook --confirmed-after-review
~/.local/bin/cross-harness doctor
```
- **合格条件**: `doctor` がグリーン。
- **失敗時**: 停止して報告（**信頼をスキップしたり `--force` 等で迂回しない**）。

---

## 7. 検証

```sh
cd ~/cross-harness
scripts/test.sh
scripts/e2e.sh
```
- **合格条件**: 両方成功。
- **失敗時**: 失敗したテスト名と出力を添えて報告（成功したと偽らない）。

---

## 8. 完了報告フォーマット（最後に人間へ）

以下を埋めて報告すること:

- 事前確認4点: pwd / whoami / os-release / uname → 各結果
- 導入ツール版: python3 / gh / node / codex / claude
- 認証状態: gh / codex / claude（各「認証済み」か）
- clone: コミットハッシュ
- install: `doctor` 結果（グリーン/警告内容）※`platform_not_darwin` は正常
- codex-hook trust: 実施済みか、`doctor` 再確認結果
- test.sh / e2e.sh: 成否
- 未完了・要フォロー事項（あれば）

---

## 停止条件（いずれかに該当したら、勝手に回避せず停止して人間に報告）

- 事前確認4点のいずれかがNG（特に WSL2 でない／root／`/mnt` 配下）
- Python が 3.11 未満
- 認証状態が不明・失敗・レート制限
- private リポジトリへアクセスできない（アカウント不一致）
- `install` / `doctor` が `platform_not_darwin` **以外**の失敗を出す
- symlink 作成失敗、権限エラー（＝ネイティブFS外の疑い）
- 再帰検出、リトライ予算の枯渇
- 既存のユーザー変更・未コミット作業を壊しそうな操作が必要になったとき
  （reset/clean/overwrite はしない。保全を最優先）

> 迷ったら止めて人間に聞く。これは fail-closed 設計であり、
> 「とりあえず先に進める」より「安全に閉じる」を常に優先する。
