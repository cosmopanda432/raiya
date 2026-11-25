# 関連研究

[README に戻る](../README.md)

raiya の部品は、ほとんどが既存の研究やソフトウェア工学の型に対応する。このページは、その対応と、LLM の記憶システムとの違いをまとめる。

調べ方: 2026-11 に、書誌を出版社・arXiv・学会・著者のページで確かめた。主張は主に抄録で確かめ、Mem0、Zep、"Beyond Static Summarization" は本文の該当箇所も読んだ。本文を通して読み比べたものではない。Anderson (1972) は報告書の本文を取得できず、その位置づけは DoD の TCSEC (Orange Book) の引用によって確かめた。ACT-R の記述は ACT-R の文献と解説によるもので、Anderson and Lebiere (1998) は直接確かめていない。誤りに気づいたら Issue で知らせてほしい。

## raiya に固有の点

今回の検索で同じものが見つからなかったのは、次の組み合わせである。

- 記憶に書き込むかどうかを、ユーザーの発話と逐語で一致するか (NFKC と空白の正規化をした部分一致) という決定的な門 (`raiya/sansho.py`) だけで決める。
- その判定に、LLM の自己申告も、LLM や NLI による判定も使わない。

「何も消さない」「書き込む前に判定する」という性質は、それぞれ単独では先行例がある (下の「LLM の記憶システム」「書き込み時の判定」)。

## 原理ごとの対応

| raiya の原理 | 対応する考え方 | 文献 |
|---|---|---|
| LLM は提案だけをし、採否は決定的な門が決める | reference monitor (すべての操作を、迂回できない検査器に通す) と complete mediation (すべてのアクセスを検査する) | J. P. Anderson, *Computer Security Technology Planning Study*, ESD-TR-73-51, Vol. I/II, 1972-10; J. H. Saltzer and M. D. Schroeder, "The Protection of Information in Computer Systems", *Proc. IEEE* 63(9), 1975 (complete mediation) |
| 同上 (LLM の分野) | 信頼できる制御と信頼できないデータを分け、決定的な方針で通す | E. Debenedetti et al., "Defeating Prompt Injections by Design" (CaMeL), arXiv:2503.18813, 2025; S. Willison, "The Dual LLM pattern for building AI assistants that can resist prompt injection", 2023-04-25 |
| LLM の自己申告を読まない | 外部の手がかりなしでは、LLM は自分の答えを正しく直せない | J. Huang et al., "Large Language Models Cannot Self-Correct Reasoning Yet", ICLR 2024 |
| 自己整合性 (同じ問いへの答えどうしの一致) にも頼らない | 対比: 標本どうしの一致で幻覚を検出する方式 | P. Manakul, A. Liusie and M. Gales, "SelfCheckGPT: Zero-Resource Black-Box Hallucination Detection for Generative Large Language Models", EMNLP 2023 |
| ユーザーの逐語に接地した span だけを根拠にする | 生成文の主張を出典に照らして確かめる (帰属の検証) | L. Gao et al., "RARR: Researching and Revising What Language Models Say, Using Language Models", ACL 2023; S. Min et al., "FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation", EMNLP 2023 |
| ユーザーの本文とアシスタントの本文を区別し、貼り付けは証拠にしない | 情報の流れの制御 (信頼度の格子) | D. E. Denning, "A Lattice Model of Secure Information Flow", *CACM* 19(5), 1976 |
| 追記だけの台帳と、読むときに導く状態 | Event Sourcing | M. Fowler, "Event Sourcing", martinfowler.com, 2005 |
| 台帳の窓ごとにまとめて記録にする (`consolidate`) | 経験の記録から上位の推論を作る reflection / 海馬から新皮質への固定化 | J. S. Park et al., "Generative Agents: Interactive Simulacra of Human Behavior", UIST 2023; J. L. McClelland, B. L. McNaughton and R. C. O'Reilly, "Why There Are Complementary Learning Systems in the Hippocampus and Neocortex: Insights from the Successes and Failures of Connectionist Models of Learning and Memory", *Psychological Review* 102(3), 419–457, 1995 |
| 強化と発芽閾値。ただし減衰はしない | 対比: ACT-R の base-level activation は、使われた頻度と新しさで強まり、時間でべき乗則に従って減衰する | ACT-R (J. R. Anderson と C. Lebiere らの認知アーキテクチャ) |
| `raiya-inspect why` と evidences | データの来歴 (why-provenance) | P. Buneman, S. Khanna and W.-C. Tan, "Why and Where: A Characterization of Data Provenance", ICDT 2001; W3C PROV-DM (勧告, 2013-04-30) |
| hooks は失敗しても会話を止めず、LLM の経路は失敗したら何も書かない | fail-safe defaults | Saltzer and Schroeder, 1975 |

## LLM の記憶システム

| システム | 記憶を書き込み・まとめるもの | 古い記憶の扱い |
|---|---|---|
| MemGPT (C. Packer et al., arXiv:2310.08560, 2023) | LLM 自身 (関数呼び出しによる)。OS の階層的な記憶に倣い、文脈の窓と外部の保存先の間で情報を動かす | LLM が管理する |
| Mem0 (P. Chhikara et al., arXiv:2504.19413, 2025) | LLM が会話から抽出し、ADD / UPDATE / DELETE / NOOP のどれかを選ぶ | LLM が追加・更新・削除する (新しい情報と矛盾する記憶は消す) |
| A-Mem (W. Xu et al., arXiv:2502.12110, NeurIPS 2025) | LLM。新しい記憶が来ると、古い記憶の文脈や属性も書き換える (memory evolution) | 書き換える |
| Zep / Graphiti (P. Rasmussen et al., arXiv:2501.13956, 2025) | LLM が抽出し、時間つきの知識グラフにする | 矛盾した事実に無効になった時刻を付ける。二重の時間軸 (bi-temporal) を持ち、生の入力を来歴として残す |
| raiya | 決定的な門 (逐語の部分一致) | 消さない・弱めない。置き換えられた記憶には `superseded_by`、人が取り下げた記憶には `withdrawn` の印を付け、世代として残す (取り下げは `restore` で戻せる) |

古い記憶に無効の印を付けて残すという点で、Zep / Graphiti は raiya にいちばん近い。違いは印の中身にある。Zep は事実が有効だった時刻を付け、raiya はどの記憶に置き換えられたか、誰がなぜ取り下げたかを付ける。なお Zep の論文は無効の印を付けることを述べているが、消さないとは明言していない。

## 書き込み時の判定と、記憶への毒入れ

記憶に書き込む前に候補を確かめる研究は、2025〜2026 年に増えている。どれも判定は LLM、NLI、自己整合性、検証器によるもので、意味の近さを見る。raiya の判定は逐語の一致だけを見る。

- "Beyond Static Summarization: Proactive Memory Extraction for LLM Agents", arXiv:2601.04463: 記憶の候補を元の往復と NLI で照合し、含意されないものは LLM が直すか捨てる (本文 v2 §3.4)。
- "ConsistencyGate: Preventing Memory Contamination in LLM Agents via Self-Consistency Admission Control", arXiv:2607.22962: LLM に何度も尋ね、裏付けの点数の平均が閾値を超えた事実だけを書き込む。
- "MemGuard: Persisting Verifier Signals for LLM-Agent Memory Governance", arXiv:2608.21867: 検証器の点数を候補に付けて書き込みを決め、その信号を検索・矛盾の解消・要約でも使い続ける。

記憶への毒入れ (memory poisoning) の研究は、書き込める永続記憶そのものが攻撃面になることを示している (MemoryGraft、"From Untrusted Input to Trusted Memory")。総説は、防御として来歴の追跡を挙げている。raiya がユーザーの本文とアシスタントの本文を分け、貼り付けを証拠にしないのは、この問題への一つの答えにあたる。

- "MemoryGraft: Persistent Compromise of LLM Agents via Poisoned Experience Retrieval", arXiv:2512.16962
- "A Survey on Long-Term Memory Security in LLM Agents: Attacks, Defenses, and Governance Across the Memory Lifecycle", arXiv:2604.16548
- "From Untrusted Input to Trusted Memory: A Systematic Study of Memory Poisoning Attacks in LLM Agents", arXiv:2606.04329

## 評価

LLM の記憶システムは、LoCoMo (Maharana et al., ACL 2024) と LongMemEval (Wu et al., ICLR 2025) で比べるのが通例である。LongMemEval には「情報の更新」と「答えない (abstention)」の項目がある。raiya の receipts は自前の fixture と実 LLM による smoke だけで、これらのベンチマークの結果はまだない ([テスト・receipts](receipts.md))。

## raiya の限界との関係

逐語の一致だけで判定するので、ユーザーが言い換えた内容や、ユーザーの発言自体の誤りは止められない。詳しくは README の「止めきれないもの」を見てほしい。
