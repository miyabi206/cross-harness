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
