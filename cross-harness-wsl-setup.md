# cross-harness WSL2 セットアップ指示書

あなた（**WSL2 内で動作する Claude Code**）は、**WSL2 上の Ubuntu-24.04** に個人用ツール
`cross-harness` を導入するセットアップ担当です。この指示書を **上から順に** 実行してください。各ステップには
「実行コマンド」「合格条件」「失敗時の対応」があります。合格条件を満たさないまま次へ進まないこと。

対象トポロジ: Claude（親・オーケストレータ）→ Codex（子・実行）の一方向・一段のみ。
このマシンは native Linux なので、macOS 専用の Seatbelt サンドボックス（Claude 書き込みロールの封じ込め）は
`platform_not_darwin` として**無効になりますが、これは想定内**です（既定では書き込み実行は Codex 側で、
Linux の Landlock/seccomp 実サンドボックスが効きます）。これは `doctor` の出力ではなく、書き込みロールの
委任ごとに `sandbox_exec` の理由として run メタデータへ記録される文字列です。

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

## 1. 事前確認 — 合格ライン5点（NG が1つでもあれば停止して報告）

```sh
cd ~
pwd                                   # 1) /home/<user> であること（/mnt/... はNG）
whoami                                # 2) 一般ユーザー（root はNG。プロンプト末尾が $ で # でない）
grep VERSION= /etc/os-release         # 3) Ubuntu 24.04 (Noble)
uname -r                              # 4) 文字列に "WSL2" を含む（例: 6.x...-microsoft-standard-WSL2）
test -n "${WSL_DISTRO_NAME:-}"         # 5) 実行主体が WSL2 内のシェルであること
```

- **合格条件**: 5項目すべて OK。`pwd` は `/home/<user>` 配下であり、実行主体は WSL2 内のシェルであること。
- **失敗時**: どれがNGかを報告して停止。特に `uname -r` に `WSL2` が無い（WSL1）場合、
  Codex の実サンドボックスが効かないため、人間に「Windows側で `wsl --set-version Ubuntu-24.04 2`」を依頼する。
  Windows 側 Claude Code の Git Bash では `pwd` が `/c/Users/...` となり不合格であるため、WSL2 内のシェルからやり直す。

---

## 1-1. 対象プロジェクトの配置方針

対象プロジェクトは WSL ネイティブFS の `$HOME` 配下に置く。`delegate` の `--cwd` に `/mnt/c` 配下のパスを
渡さない。

```sh
cd ~
pwd                                   # /home/<user> 配下であること
```

- **合格条件**: clone と以後の作業対象が `$HOME` 配下にあり、`delegate --cwd` に `/mnt/c/...` を渡さない。
- **失敗時の対応**: Windows 側にプロジェクトがある場合も、その clone を流用せず、WSL 側に作業用 clone を作成する。
  両者の同期は push/pull で行い、Windows 側 clone を install 元にしない。

> **注記**: このリポジトリには `.gitattributes` がない。Windows の既定 `core.autocrlf=true` で clone すると
> `bin/cross-harness` の shebang が CRLF になり、install はそれをコピーするため
> `~/.local/bin/cross-harness` が恒久的に壊れる。Windows 側の既存 clone は install 元に流用しないこと。

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
あなた自身が Claude Code なので通常は導入済み。Windows 側の実行ファイルを検出して偽に合格しないよう、
WSL 内の `claude` であることも確認する。
```sh
claude_path="$(command -v claude)" || exit 1
printf '%s\n' "$claude_path"
case "$claude_path" in /mnt/*|*.exe) exit 1 ;; esac
claude --version
```
- **合格条件**: バージョンが表示され、`command -v claude` の解決先が `/mnt/` 配下でも `.exe` でもない。
  Windows 側の Claude Code は不合格とする。
- **失敗時の対応**: Windows 側の `claude` が解決された場合、WSL 内へ Claude Code を導入または PATH を修正するよう
  人間に依頼して停止する。

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
  Windows 側 clone を install 元として流用していないこと。
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
- **合格条件**: `doctor` の9項目（configuration、independent Codex CLI、independent Claude CLI、
  API-key environment、Codex ChatGPT auth、Claude subscription auth、Claude charter、Codex charter、
  Codex hook trust）がすべて PASS。`independent Codex CLI` は `codex` の解決先が
  `/.vscode/extensions/` 配下であれば FAIL となるため、その状態も不合格とする。
- **失敗時の対応**: `doctor` の FAIL はすべて停止条件である。FAIL の項目を添えて停止・報告する。
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
- **合格条件**: `doctor` の9項目がすべて PASS。
- **失敗時**: 停止して報告（**信頼をスキップしたり `--force` 等で迂回しない**）。

---

## 7. 検証

### 7-1. テスト依存の導入
テストは pytest で実行される。pytest は開発依存でありランタイムには不要なため、
`install` では導入されない。**検証の前に一度だけ導入する。**

ステップ2-1 で `python3-venv` を導入済みなので、追加ツールなしで次を実行できる。
`scripts/test.sh` が優先して使う `.venv` をリポジトリ直下に作る。

```sh
cd ~/cross-harness
python3 -m venv .venv
.venv/bin/python -m pip install pytest
.venv/bin/python -c 'import pytest; print(pytest.__version__)'
```

uv を導入済みなら `uv sync --group dev` でも同じ結果になる。

- **合格条件**: 最終行がバージョンを表示する（9.1.1 以上）。
- **失敗時の対応**: ネットワーク到達性を確認して報告する。`sudo pip install` でシステムの
  Python へ入れてはならない。`scripts/test.sh` が参照するのは `.venv/bin/python` である。

### 7-2. 実行
```sh
cd ~/cross-harness
scripts/test.sh
scripts/e2e.sh
```
- **合格条件**: 両方成功。
- **失敗時**: 失敗したテスト名と出力を添えて報告（成功したと偽らない）。
  `pytest is required to run tests` で停止した場合は 7-1 が未実施なので 7-1 に戻る。

`scripts/e2e.sh` は tracked file の `docs/e2e-results.md` を書き換える副作用がある。実行後はリポジトリが
dirty になり、直後の書き込み委任が `dirty_worktree` で停止しうることを確認する。また、同スクリプト内の
`claude auth status` は失敗しても `not verified by this run` に落ち、スクリプト自体は PASS する。したがって、
e2e の成功は Claude 認証済みの証明ではない。認証はステップ3の検証結果で判断する。

---

## 8. 完了報告フォーマット（最後に人間へ）

以下を埋めて報告すること:

- 事前確認5点: pwd / whoami / os-release / uname / WSL 内シェル → 各結果
- 導入ツール版: python3 / gh / node / codex / claude
- 認証状態: gh / codex / claude（各「認証済み」か）
- clone: コミットハッシュ
- install: `doctor` 結果（9項目すべて PASS か）
- codex-hook trust: 実施済みか、`doctor` 再確認結果
- テスト依存: `.venv` 作成と pytest 導入の可否、表示された pytest バージョン
- test.sh / e2e.sh: 成否
- 未完了・要フォロー事項（あれば）

---

## 停止条件（いずれかに該当したら、勝手に回避せず停止して人間に報告）

- 事前確認5点のいずれかがNG（特に WSL2 でない／root／`/mnt` 配下／Windows 側シェル）
- Python が 3.11 未満
- 認証状態が不明・失敗・レート制限
- private リポジトリへアクセスできない（アカウント不一致）
- `install` が失敗した、または `doctor` の9項目のいずれかが FAIL
- symlink 作成失敗、権限エラー（＝ネイティブFS外の疑い）
- 再帰検出、リトライ予算の枯渇
- 既存のユーザー変更・未コミット作業を壊しそうな操作が必要になったとき
  （reset/clean/overwrite はしない。保全を最優先）

> 迷ったら止めて人間に聞く。これは fail-closed 設計であり、
> 「とりあえず先に進める」より「安全に閉じる」を常に優先する。
