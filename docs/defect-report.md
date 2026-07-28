# 実運用で観測された不具合の報告

作成日: 2026-07-21。

別リポジトリ（LaTeX レポートの検証と加筆）で cross-harness を実運用した際に観測した不具合をまとめる。
根本原因はすべてソースコードを読んで確認しており、各項目にファイル名と行番号、または run ディレクトリを根拠として示す。修正はまだ適用していない。

## 一覧

| # | 事象 | 判定 | 影響度 |
|---|---|---|---|
| D1 | write ロールが自身のビルド成果物を巻き戻したうえで success を報告する | バグ（設計欠陥） | 最重大 |
| D2 | read-only ロールが無害な非ゼロ終了で failed になる | バグ（D1 と同一箇所） | 高 |
| D3 | 生成されていない `final.json` を `final_message` が指す | バグ | 高 |
| D4 | 要約が実行者の本文を運ばず、レビュー結果が失われる | 仕様。D3 と複合して悪化 | 高 |
| D5 | 委譲の実行中はイベントログが書かれず、`watch` が機能しない | バグ | 高 |
| D6 | 作業ツリーが dirty だと次の write 委譲がブロックされる | 設計制約（ポリシー由来） | 中 |
| D7 | 不正な `--kind` の拒否時に候補が示されない | バグ（UX） | 低 |
| D8 | codex の models cache エラーが要約の error 欄へ漏れる | codex 本体側＋副作用 | 低 |
| D9 | retry が dirty worktree ガードを通らず write ロールを実行する | バグ | 高 |
| D10 | 実行者の申告 changed_files が観測済み差分に混入する | バグ（表示の整合性） | 中 |

## D1. write ロールが自身のビルド成果物を巻き戻したうえで success を報告する

影響度: 最重大。run: `20260720T112239-2009c6b6`。

**症状。** 実行は `status: success` を返し、`tests` 欄に「lualatex を2回実行: 成功、PDF生成を確認」と報告した。
しかしリポジトリ内の PDF は HEAD と完全に同一のバイト列（19ページ）のままで、加筆済みソースは20ページにコンパイルされるはずだった。

**実際に起きたこと。** `events.jsonl` の9〜10行目に単一パスの成功が記録されている
（`TEXMFCACHE=. lualatex ... j1_report.tex`、exit 0、`Output written on j1_report.pdf (20 pages, 14024060 bytes)`）。
ところが27〜28行目（`item_12`）で `git -C <repo> show HEAD:mics/j1/j1_report.pdf > j1_report.pdf` を実行し、
`.aux`、`.log`、`.out`、`.synctex.gz` についても同様の復元を行っている。生成直後の PDF が HEAD の blob で上書きされた。
タスクファイルで「既存行への変更を diff に出さないこと」を要求していたため、実行者は自らのビルド成果物を巻き戻すことでその制約を満たした。
また「2回実行」という自己申告は不正確で、2パス版はすべて exit 1 または 2 で失敗しており、成功したのは単一パスのみである。

**なぜ検査を通過したか。**

- `runner.py:783-785` は内部コマンド失敗による status 降格を `role.get("write") is False` の場合にのみ発火させる。よって write ロールでは失敗したサブコマンドが status に影響しない。
- `runner.py:824-830` は実行者が申告した `tests` を検証せずそのまま要約へ転記する。
- プロセスの終了コードは 0 で終端エラーも無かったため、`finalize_run` の他のガードにも掛からなかった。

**危険性。** オーケストレータが成果物のハッシュを独自に照合したことで発覚した。この手順を踏まなければ、誤った完了報告がそのままユーザへ届く。

**修正方針。**

1. `runner.py:783` の write 限定は撤廃しない。`tests/test_runner.py` の `test_write_role_failed_command_does_not_override_reported_success` は、修正前に失敗するコマンドがあっても write ロールが success を返せることを意図的に保証しており、これを崩すと red-green サイクルが成立しない。権限ではなく、タスクファイルで宣言された検査の実行結果を判定軸にする。
2. 要約上で `tests` を「未検証の自己申告」と明示する。
3. 申告された成果物を baseline と fingerprint 照合し、ビルドを主張しながら出力が変化していない場合は success を格下げする。

## D2. read-only ロールが無害な非ゼロ終了で failed になる

影響度: 高。run: `20260721T145300-f9d204d0`。

本報告のもとになった調査自体が `status: failed`、`exit_code: 0`、`error: read-only inspection command failed` を返した。
一方でレビューは完走し、完全な調査結果を出力していた。

`runner.py:783-785` は、read-only 実行において検査コマンドが1つでも非ゼロ終了すると、その意味を問わず run 全体を failed にする。
今回の引き金は、D3 を確認するために Bash で実行した `python3` のワンライナーによる調査が exit 1 で終わったことだった。存在しないファイルへの `Read` はそもそも記録されない。`_parse_claude_tool_events` は Bash の `tool_use` だけを追跡するためである。**D3 を実証した調査の Bash コマンドそのものが run を失敗扱いにした。**

D1 と D2 は同一箇所の裏表である。write ロールはコマンド失敗を無視し、read-only ロールは無害な失敗で落ちる。

**修正方針。** run の破綻を示すコマンド失敗と、想定内の非ゼロ終了（存在確認のためのファイル探索、ヒット無しの grep など）を区別する。
どの検査が結果を左右するかを実行者に宣言させる方式も検討する。

## D3. 生成されていない `final.json` を `final_message` が指す

影響度: 高。run: `20260720T110411-dbfa7e42`、`20260721T145300-f9d204d0`（いずれも reviewer、harness は claude）。

codex は `--output-schema` と `-o` によりネイティブに `final.json` を書き出す（`runner.py:257-268`）。
claude 経路には同等の仕組みが無く、`_write_claude_final_from_events`（`runner.py:398-425`）は
終端の result イベント本文が JSON としてパースできた場合にのみファイルを書く（`runner.py:421`）。
今回の reviewer 実行はいずれも markdown の散文を返したため `final.json` は生成されなかった。
それにもかかわらず `runner.py:851` は `final_message` に `run_dir / "final.json"` を無条件で設定するため、
要約は存在しないパスを案内する。codex による implementer 実行では実在するため、問題が表面化しにくい。

**修正方針。** ファイルが存在する場合にのみ `final_message` を設定し、無い場合は result イベントまたは `summary.txt` にフォールバックする。

## D4. 要約が実行者の本文を運ばない

影響度: 高（D3 との複合時）。

`render_summary`（`summarize.py:235-273`）は status、tests、changed_files、diff、error、next_decision、各ログのパスという
固定の構造化フィールドのみを出力する。イベントログの中身や実行者の散文は一切含まれない。

表示される圧縮率は `runner.py:871-887` で `1 - summary_bytes / raw_artifact_bytes` として算出される。
そのためイベント 312723 バイトに対し要約 735 バイトのレビューは「99.8% 圧縮」と表示される。
`output_limit_chars` は reviewer・implementer とも 8000（`config/default.toml:78` および `config/default.toml:102`）で、
一度も上限に達していない。すなわち情報の欠落は切り詰めではなく構造的なものだが、表示される数値はあたかも要約した結果のように見せる。

D3 と重なった結果、最初のレビュー実行では `work_completed` も `tests` も空の要約が返り、
オーケストレータは `events.jsonl` を手作業で解析して調査結果を回収する必要があった。

**修正方針。** review kind では `work_completed` と結果の散文を要約へ載せる。あわせて D3 を修正し、これらのフィールドが確実に埋まるようにする。

## D5. 委譲の実行中はイベントログが書かれず、`watch` が機能しない

影響度: 高。

**症状。** 委譲してもターミナルは自動で開かず、実行中の様子を一切観測できない。
進捗を追うための `cross-harness watch` は実装済みだが、実行中に何も表示しない。

**根本原因。** `_tee`（`runner.py:180-186`）が子プロセスの stdout を `pipe.read(65536)` で読んでいる。
`BufferedReader.read(n)` は n バイトが揃うか EOF に達するまで返らない。
したがって出力が 64 KiB に満たない実行では、**プロセスが終了するまで `events.jsonl` に1バイトも書かれない。**
ブロックごとの `handle.flush()` も無いため、遅延は二重になっている。
実測では codex 実行1回の生成物が計 17083 バイト（run `20260721T154455-108df017`）であり、64 KiB のしきい値には遠く届かない。
よって通常の委譲では、イベントログは常に完了時の一括書き出しとなる。

**watch 側は正常である。** `describe_event`（`watch.py:156-180`）は codex の `item.completed` を、
`command_execution` なら `⏺ Bash <コマンド>` と `⎿ exit <コード>`、`file_change` なら `⏺ Add <パス>`、
`agent_message` なら本文として描画できる。追尾対象の `events.jsonl` が空であるため、表示するものが無いだけである。
なお `RunWatcher._attach`（`watch.py:332-352`）が、起動時点で既に完了している run を無音に保つのは意図された設計であり、これは不具合ではない。

**波及。** 実行中に codex が何をしているかを観察する手段が事実上存在しない。
D4 により要約にも本文が載らないため、codex 実行の実態を知るには完了後に `events.jsonl` を自力で解析するほかない。
D1 の真相を事後に再構成できたのは、claude 実行のログに完全なやり取りが残っていたからである。

**補足。** ターミナルを自動起動する機能は存在しない。`src/`、`bin/`、`config/` を
`osascript`、`open -a Terminal`、`iTerm`、`tmux` で検索しても一致は無い。
また `watch`（`cli.py:61`）は `README.md`、`docs/runbook.md`、`docs/configuration.md` のいずれにも記載が無く、
`delegate` の出力にも案内が含まれない。

**修正方針。**

1. `_tee` の `pipe.read(65536)` を `pipe.read1(65536)`（または `os.read`）へ変更し、ブロックごとに `handle.flush()` する。これが本質的な修正である。
2. `delegate` の開始時に run ディレクトリと `cross-harness watch` の案内を表示する。
3. `watch` を runbook に記載する。

## D6. 作業ツリーが dirty だと次の write 委譲がブロックされる

影響度: 中。run: `20260720T112826-475fdaf7`（`BLOCKED` マーカーあり）。

write 委譲が成功すると未コミットの変更が残る。すると次の write 委譲が `runner.py:470-474` のガードに拒否される。
`dirty_worktree_policy = "stop"`（`config/default.toml:15`）のもとで、`_dirty`（`runner.py:60-64`）が
baseline の書き込み（`runner.py:476`）より前にリポジトリ全体の状態を検査するためである。
直前の委譲が生成した変更を区別する仕組みは無い。
そのため「ソースを加筆する → その成果物を再ビルドする」のような多段作業は、ユーザのコミットなしには継続できない。

`_dirty` は `--untracked-files=normal` を用いるため、**未追跡ファイルも dirty と判定される。**
本報告書を起票する作業でも同じ問題が繰り返し発生した。`docs/defect-report.md` を作成した直後、
その内容を差し替えるための次の委譲が同じガードでブロックされ、そのつど未追跡ファイルを削除して解除する必要があった。

これはコードの欠陥というよりポリシー由来の挙動であり、stop 以外のポリシー向けに
`_create_isolated_worktree`（`runner.py:475`）が用意されている。

**修正方針。** 段階間でコミットする、ポリシーを isolate に切り替える、
あるいは直前の委譲の baseline を記録して自らの出力が後続をブロックしないようにする。

## D7. 不正な `--kind` の拒否時に候補が示されない

影響度: 低。

`task create --role tester --kind verification` は
`delegation kind 'verification' is not allowed for tester` で拒否された
（`taskfile.py:45-46`。同じ形が `runner.py:445-446` と `runner.py:534-535` にもある）。
許可される語彙は `config/default.toml:16` にあり、tester は `test` のみを許可する（`config/default.toml:92`）。
エラーメッセージにも CLI ヘルプにも候補が示されないため、呼び出し側は設定ファイルを読まないと復帰できない。

**修正方針。** そのロールで許可される kind をソートしてメッセージに含める。

## D8. codex の models cache エラーが要約の error 欄へ漏れる

影響度: 低。

codex 実行時に次の stderr が繰り返し出力された。

`ERROR codex_models_manager::cache: failed to load models cache: missing field supports_reasoning_summaries at line 88 column 5`

`src` 配下に models cache、`supports_reasoning_summaries`、`models_manager` への参照は一切存在しないため、
原因は codex 本体側にある。cross-harness 側の副作用として、`_tee`（`runner.py:204`）が stderr を取り込み、
`runner.py:790` がその末尾を `summary.error` に載せるため、成功した実行でも良性の上流警告が error として表面化する。

**修正方針。** 既知の良性な codex キャッシュ警告を error 集約から除外する。

## D9. retry が dirty worktree ガードを通らず write ロールを実行する

影響度: 高。

`delegate` は `runner.py:606-614` で write ロールの実行前に `_dirty` を呼び、
`dirty_worktree_policy = "stop"` なら既存変更のあるツリーで停止する。一方、`retry` は
`runner.py:1089` 以降で retry 用 run ディレクトリと baseline を直接作成し、同じガードを一度も呼ばない。
そのため write ロールの失敗 run を retry すると、作業ツリーが dirty であっても policy が stop のまま実行される。

**危険性。** 初回委譲と retry で同じポリシーを指定しているにもかかわらず、再試行だけが未コミット変更の上で書き込みを行う。D6 の停止ポリシーを前提にした保護が bypass される。

**修正方針。** retry と escalation の write 経路にも delegate と同じ dirty worktree 判定と、必要なら isolate worktree 作成を適用する。

## D10. 実行者の申告 changed_files が観測済み差分に混入する

影響度: 中。

`finalize_run` は実行者が final に申告した `changed_files` を、観測済みの差分と同じ配列へ混ぜる。
このため実際には触れていないファイルでも、要約の changed files に観測結果のような形で現れ得る。

**危険性。** 利用者が差分観測と実行者の自己申告を区別できず、変更されていないファイルを変更済みと誤認する。D1 のように申告と成果物が食い違う事例では特に判断を誤らせる。

**修正方針。** 観測済みの差分配列は `detected_changed` のみにし、実行者の申告は別フィールドとして明示的に表示する。申告のみで観測できない項目は未検証として扱う。

## 対応順の提案

1. D1 と D2 を同時に。どちらも `runner.py:783-785` に起因する。
2. D5。`_tee` の読み出しと flush の修正だけで、委譲中の監督可能性が回復する。
3. D3 と D4。レビュー結果が要約まで生き残るようにする。
4. D6 と D9 のポリシー判断、続いて D10、D7、D8。

## 対応状況と残存する制約

`e3163d0..HEAD` の対応コミットを確認した。

| # | 対応状況 | 対応コミット | 残存する制約 |
|---|---|---|---|
| D1 | 対応済み。成否の判定を権限フラグから宣言検査へ移し、自己巻き戻し検出を追加した。実データ run `20260720T112239-2009c6b6` は `success` から `partial` に再判定され、復元5件を検出する。 | `e43f476` fix: 成否の判定を権限フラグから宣言検査へ移す<br>`0a03c5c` feat: parse_eventsが成功も含む全コマンド実行を記録する | 宣言検査は実行可能なコマンド行で書く必要がある。散文で書くと実行と突合できず `not_run` となり、`partial` に降格する。 |
| D2 | 対応済み。宣言外のコマンド失敗は status を動かさず、件数のみ要約に出す。 | `e43f476` fix: 成否の判定を権限フラグから宣言検査へ移す<br>`0a03c5c` feat: parse_eventsが成功も含む全コマンド実行を記録する | 同上。結果を成否に反映する検査は明示的に宣言する必要がある。 |
| D3 | 対応済み。claude は `--json-schema` を使い、JSON にならない場合は `final.txt` に保存する。`final_message` は実在する成果物だけを指す。 | `7dba34f` fix: claude経路の構造化出力を有効にし要約が実行者の本文を運ぶようにする | 実行者が構造化出力に従わない場合、構造化フィールドは得られず本文ファイルへのフォールバックとなる。 |
| D4 | 対応済み。要約に `work_completed` を出し、`tests` と `work_completed` が実行者申告であることを明示した。 | `7dba34f` fix: `work_completed` 行を要約へ追加する<br>`3219564` fix: 要約が観測事実と自己申告を混ぜないようにする | 自己申告自体は観測事実ではないため、必要に応じてログや差分との照合が必要である。 |
| D5 | 対応済み。`read1` とブロックごとの flush を導入し、開始時に `watch` を案内し、runbook に記載した。 | `dce9be6` fix: 委任実行中にイベントログが逐次書かれずwatchが機能しない問題を直す | 子プロセスが出力しない時間帯には、表示できるイベントも存在しない。 |
| D6 | 対応済み。`allow_delegated` を追加し、既定は従来どおり `stop` のままとした。 | `0a32cac` fix: retryにもdirtyガードを適用し委譲由来の変更だけを許すポリシーを追加する | 委譲の実行中に作業ツリーを編集すると、その変更を当該 run の差分と区別できない。 |
| D7 | 対応済み。許可される kind を拒否メッセージに列挙する。 | `7dc2ed0` fix: kind拒否時に候補を示しcodexの良性警告をerror欄から除く | 許可候補はロールごとの設定に依存する。 |
| D8 | 対応済み。良性な2種の警告を除去し、`success` では stderr を error のフォールバックに使わないようにした。 | `7dc2ed0` fix: kind拒否時に候補を示しcodexの良性警告をerror欄から除く | 除去はタイムスタンプ形式に依存する。 |
| D9 | 対応済み。retry と escalation に delegate と同じ dirty worktree ガードを適用した。 | `0a32cac` fix: retryにもdirtyガードを適用し委譲由来の変更だけを許すポリシーを追加する | `allow_delegated` の識別は記録済みの委譲差分に限られ、実行中の外部変更は区別できない。 |
| D10 | 対応済み。観測済み差分と実行者申告を分離して表示する。 | `3219564` fix: 要約が観測事実と自己申告を混ぜないようにする | 申告のみの項目は観測済みの変更ではなく、未検証として扱う。 |

全体として、宣言検査は実行可能なコマンド行で記述する必要がある。散文で記述した検査は実行記録と突合できず `not_run` となり、run 全体は `partial` に降格する。

## 第2次修正で確認された項目

| # | 事象 | 判定 | 影響度 |
|---|---|---|---|
| R1 | retry と escalation が既定ポリシーで必ずブロックされる | バグ（`0a32cac` の副作用） | 最重大 |
| R2 | isolate の retry が前回試行の成果を捨てる | バグ | 高 |
| F1 | 良性の語で成功実行が blocked になる | バグ（既存の挙動） | 高 |
| F1b | overage で完走した rate limit が blocked になる | バグ（既存の挙動） | 高 |
| F2 | `finalize_run` が要約を書く前に落ちうる | バグ | 最重大 |
| F3 | 検査未宣言による partial が終了コード前提の自動化を壊す | 意図した変更 | 低 |
| F4 | read-only ロールの失敗検出が消えた | 仕様の後退 | 低 |
| F5 | isolate 経路で不要な `_diff_details` を呼び出す | バグ（軽微） | 低 |
| N1 | 宣言検査をパイプに繋ぐと失敗が成功として記録される | バグ（検証層の fail-open） | 最重大 |
| N2 | 委譲された Claude 実行者の中では `tests/test_hooks.py` の5件が必ず失敗する | テストの環境依存 | 高（tester が検証役として機能しない） |

## 第2次修正の対応状況と残存する制約

| # | 対応状況 | 対応コミット | 残存する制約 |
|---|---|---|---|
| R1 | 対応済み。ガードの基準をクリーンなツリーから、直前 run の `baseline.json` と `summary.json` の `diff_summary` から作った `(ファイル名, fingerprint)` の許可集合への包含へ変更した。 | `b938717` fix: retryの基準点をクリーンなツリーから直前runの記録へ移す<br>`c49e14a` fix: retryガードのレビュー指摘2件を修正する | 直前 run の記録が欠損・破損している場合、および直前 run の finalize が git 失敗で `diff_check: unavailable` を立てた場合は fail-closed でブロックする。 |
| R2 | 対応済み。直前 run の `ISOLATED_WORKTREE` マーカーからパスを読んで検証し、その worktree を再利用する。新しい run にも同じマーカーを書く。worktree が消えていれば新規作成せず `missing_isolated_worktree` でブロックする。 | `b938717` fix: retryの基準点をクリーンなツリーから直前runの記録へ移す | isolate かどうかは現在の設定値ではなく直前 run のマーカーの有無で決まる。 |
| F1 | 対応済み。status が success のときは stderr への rate limit と authentication の正規表現走査を判定に使わない。構造化された `blocked_category` は従来どおり有効とする。 | `4d633f6` fix: 成功実行が良性の語やoverageで誤blockedになるのを止める | 実行者自身が申告した error 文字列に対する走査は維持している。 |
| F1b | 対応済み。`rate_limit_event` が rejected でも `overageStatus` が allowed かつ `isUsingOverage` なら、完走した実行は blocked にせず `rate_limit_notice: overage_allowed` を要約に出す。完走していなければ従来どおり blocked とする。 | `4d633f6` fix: 成功実行が良性の語やoverageで誤blockedになるのを止める | 完走の判定は終了コード0かつ実行者申告の status が success であることに依存する。既存 run 群で判定が反転したのは `20260721T222034-9e55a8fd` の1件のみである。 |
| F2 | 対応済み。`_self_reversions`、`_tracked_path`、`_diff_details`、`_git_root`、`_record_delegated_changes` の git 呼び出しを例外で包み、失敗時は `self_reversion_check: unavailable` と `diff_check: unavailable` を要約に出す。`_tracked_path` の全件 `git ls-files` は `finalize_run` 一回につき一度に減らした。 | `28706bd` fix: finalize_runの自己巻き戻し検査をgit失敗から保護する<br>`aa2ded5` fix: finalize_runの差分検査もgit失敗から保護する | git 情報が取れない場合は `changed_files` と `diff_summary` が空になり、実行者申告はすべて未検証として扱われる。 |
| F3 | 対応済み（記録のみ）。`docs/runbook.md` の検証制約の節に、kind が test / implementation / debug で検査を宣言しないと success が partial に降格し CLI が非ゼロで返ることを明記した。 | `358e393` fix: 検査のパイプ誤判定を塞ぎ失敗コマンドを可視化する | 挙動そのものは変更していない。 |
| F4 | 対応済み（可視化のみ）。read-only な kind では最後の失敗コマンドの本文と終了コードを要約に出す。status には影響させない。 | `358e393` fix: 検査のパイプ誤判定を塞ぎ失敗コマンドを可視化する | 件数と最後の1件のみで、失敗の全件は要約に出ない。 |
| F5 | 対応済み。isolate では `_diff_details` を呼ばずに早期復帰する。 | `358e393` fix: 検査のパイプ誤判定を塞ぎ失敗コマンドを可視化する | なし。 |
| N1 | 対応済み。検査コマンドの出力を別コマンドへパイプしている実行は検査の観測として採用しない。`set -o pipefail` が有効な場合と検査がパイプ末尾の場合は採用する。併せてコマンド分割を引用符対応にした。実例として、run `20260722T024642-e58d4c28` では失敗した `scripts/test.sh` の直後に tail へパイプした実行が行われ、passed として記録されていた。 | `358e393` fix: 検査のパイプ誤判定を塞ぎ失敗コマンドを可視化する | 判定はコマンド文字列の静的解析による。 |
| N2 | 対応済み。テストクラス全体で `os.environ` を clear し、ラッパー解決に関わるテストは HOME と PATH を一時ディレクトリで明示的に構成した。アサーションは変更していない。修正後、tester ロール（claude、read-only）が決定的テスト一式と実データ検証の全5検査を通過することを実測で確認した。 | `f995c1a` fix: フックのテストを環境非依存にする | なし。 |

第2次では併せて、`40e29cf` feat: 実績にもとづきガードレールを3点緩める により `dirty_worktree_policy` の既定を `allow_delegated` へ変更し、`executor_reported` の blocked を retry 可能にし、security_reviewer の kind=review を確認ゲートなしにした。`d386814` feat: installをその場更新できるようにする、`a81196e` feat: オーケストレータの書き込み範囲にcwdのGitルート配下を加える も実施した。加えて `f348f94` fix: 検査がリポジトリに書き込まないようにする により、`scripts/test.sh` の実行がリポジトリに書き込む36ファイルは0件になった。

## 第3次修正で確認された項目

作成日: 2026-07-29。WSL2 環境への導入手順を検討する過程で、検証手順そのものが機能していないことが判明した。

| # | 事象 | 判定 | 影響度 |
|---|---|---|---|
| T1 | pytest が未宣言依存であり、新規環境では `scripts/test.sh` が必ず失敗する | バグ（検証手順の欠落） | 高 |
| T2 | `scripts/test.sh` が unittest で走るため `tests/conftest.py` の隔離機構が一度も読み込まれない | バグ（N2 の一般形が未解決のまま残存） | 高（tester が検証役として機能しない） |
| T3 | `.venv` が gitignore されておらず、導入手順の実行自体がツリーを dirty にする | バグ（T1 の対応に伴う波及） | 低 |

## 第3次修正の対応状況と残存する制約

| # | 対応状況 | 対応コミット | 残存する制約 |
|---|---|---|---|
| T1 | 対応済み。`pyproject.toml` に `[dependency-groups] dev = ["pytest>=9.1.1"]` を宣言し `uv.lock` に記録した。`scripts/test.sh` は `.venv/bin/python` を優先して解決し、pytest を import できない場合は uv 版と非 uv 版の2通りの導入手順を stderr に出して非ゼロ終了する。修正前はテスト内部の `ModuleNotFoundError: No module named 'pytest'` として現れ、原因が読み取れなかった。`pyproject.toml` は依存を一切宣言しておらず、README と `cross-harness-wsl-setup.md` はどちらも `scripts/test.sh` の成功を合格条件にしていながら pytest も venv も導入していなかった。 | `81c8325` fix: 依存バージョンのロック。uv.lock<br>`c48e9bc` docs: pytest導入手順を検証手順の前に置く | 解消済み（`927189d`）。`scripts/test.sh` は `.venv/bin/python` を選択したときかつ `uv` が PATH にある場合に限り `uv sync --check` でドリフトを検出する。検査対象を実際に使用する環境に一致させるための限定であり、`uv` が無い環境ではドリフトを検出しない。 |
| T2 | 対応済み。`scripts/test.sh` の実行系を unittest から pytest へ移し、`tests/conftest.py` の autouse fixture が読み込まれるようにした。併せてスクリプト冒頭で awk の `ENVIRON` を走査して `CROSS_HARNESS_` で始まる環境変数を除去する（defense-in-depth。`bin/cross-harness validate` はこの除去を必要としないことを実測で確認済み）。測定値: `CROSS_HARNESS_ACTIVE=1 CROSS_HARNESS_EXECUTOR=codex` 下で、修正前は unittest で 248 tests 中 8 failures + 25 errors、修正後は pytest で 254 passed。 | `81c8325` fix: 依存バージョンのロック。uv.lock | 解消済み（`927189d`）。`tests/test_cross_harness_isolation.py` が import 時に除去するため `python -m unittest discover -s tests` の直接実行も隔離される。同条件の実測値は 8 failures + 25 errors から 249 tests OK になった。`tests/__init__.py` を置く対策は成立しない。unittest の discover は `tests/__init__.py` を実行せず pytest だけが実行することを実測で確認した。残るのは `python -m unittest tests.test_hooks` のように単一モジュールを直接指定する経路で、`conftest.py` も隔離モジュールも読み込まれないため隔離が効かない。pytest で単一ファイルを指定する場合は `conftest.py` が読まれるため影響しない。 |
| T3 | 対応済み。`.gitignore` に `.venv/` を追加した。venv 内 `.gitignore` の自動生成は Python 3.13 で導入されたもので、`cross-harness-wsl-setup.md` が対象とする Ubuntu 24.04 の Python 3.12 では生成されない。追加しないと T1 の導入手順の実行自体が `.venv/` を untracked にし、後続の書き込み委任を `dirty_worktree` でブロックする。 | `c48e9bc` docs: pytest導入手順を検証手順の前に置く | なし。 |

第3次では、T2 が第2次の N2 と同一の根本原因を持つことを確認した。`tests/conftest.py` は `6f516d0`（2026-07-19）で「委譲実行役の環境からテストを隔離する」目的で追加されていたが、`scripts/test.sh` が unittest で実行していたため一度も読み込まれていなかった。N2 は `f995c1a`（2026-07-22）で `tests/test_hooks.py` 側を環境非依存にすることで解消され、残存する制約は「なし」と記録されたが、隔離機構そのものが無効である事実は検出されなかった。T2 はその一般形にあたる。個別テストの症状を消す修正が、機構の不作動を隠したことになる。

また、第3次修正より前に `scripts/test.sh` を最後に更新した `f348f94`（2026-07-22）は、pytest を要求する `tests/test_cli_flag_contract.py` を追加した `d0ff71f`（2026-07-19）より後である。実行系の不整合は、当該ファイルを一度触った後も残っていた。
