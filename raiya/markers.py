"""markers.py -- kind の形の検査に使う語の表 (名の層)。spec 2026-10-24-marker-table-design.md
規則 (どこに印を求めるか・どう比べるか) は alaya._resolve_kind が持つ。ここは言語ごとの共立の表だけ。
import しない (葉のモジュール。AST pin)。"""

LANGUAGES = ("ja", "en")            # 和集合の順序。dict の反復順に頼らない
ROLES = ("decision_reject", "workaround_cause", "preference_suffix", "user_correction", "decision_assert", "hearsay", "workaround_avoid", "self_stated_cause", "conjecture",
         "decision_assert_in_sentence", "conjecture_in_sentence", "approval", "approval_negation")

MARKERS = {
    "ja": {
        "decision_reject": ("却下", "不採用", "ではなく", "でなく", "採らない"),
        "workaround_cause": ("原因", "ため", "→"),
        "preference_suffix": ("してほしい", "てほしい", "したい", "を好む"),
        # ユーザーの逐語の訂正 (回避策 の入口・spec 2026-10-29-workaround-entry-and-pair-design §1)。
        # 短い語は入れない: miss fixture (user_correction_markers_20261029.json) に当たらない長い形だけ。
        "user_correction": ("元に戻してください", "やり直してください", "やめてください"),
        # 性向 の申告を 決定 へ上げる断定の印 (#117 KP2b)。**文末一致** (疑の文を除いた残りの各文の末尾の 。.!！ を外して endswith)。
        # miss fixture (decision_assert_markers_20261031.json) に当たらない形だけ。
        "decision_assert": ("にした", "にしました", "に決めた", "に決定した", "で行く", "を採用した", "を採用する"),
        # 伝聞の言 (#129 RH2・spec 2026-11-04-resume-hearsay-design §5.2/§7): assistant が user の言を伝える・記録の言として語る形。
        # 行う者が自分の作業を述べる形 (記録しました・記帳済み・伝える前に) には当たらない長い形だけ。
        # miss fixture (hearsay_markers_20261104.json)。短い語 (伝え・記録 単独) は入れない。
        "hearsay": (
            "と伝えました", "と伝えていました", "とおっしゃっていました", "と言っていました", "という話でした",
            "ことが記録されています", "記録に残っています",
            # #139 (spec 2026-11-04-resume-hearsay-design §7.1 (2026-11-05 追補) H1・利用者 2026-11-05 裁定で狭め): 決まりを伝える言。
            # る / ない + ことになってい(ます|る) に限る。な・う・い + こと (「大変なことになっています」「どういうことになっているか」) の慣用句は入れない。
            # 「ことになる」単独、「ことになっていた」「ことになってる」「ことになっていて」などの活用形も意図して入れない (miss fixture に固定)。
            "ることになっています", "ることになっている", "ないことになっています", "ないことになっている",
            # #143 (spec 2026-11-04-resume-hearsay-design §7.2): 現在形の伝聞「と伝えています」(#142 C-2)。「伝えておきます」「伝えてから」は入れない (miss fixture)。副詞 +「と」(ちゃんと伝えています) や、行う者が伝えたことの報告にも当たる (known_hits、受け入れる限界)。
            "と伝えています",
            # #146 (spec 2026-11-04-resume-hearsay-design §7.3): 「と話していました」「と話しています」(40 往復 #20 の実測)。「話しておきます」「話してから」「話します」は入れない (miss fixture)。
            "と話していました", "と話しています",
            # #153 (spec 2026-11-04-resume-hearsay-design §7.4 (NV)・利用者 2026-11-16 裁定): 引用の と + 言う動詞 + てい(ます|ました)。列は例で、完全ではない (原典は言う動詞を数え上げない)。
            # 書い・記し は入れない (文書を引く言に当たる)。単純過去 (と述べました) も入れない。既存の 6 形 (と伝えています 等) は重複させない。行う者が資料・人の言を引く文にも当たる (known_hits、受け入れる限界)。
            "と言っています", "と述べています", "と述べていました", "と語っています", "と語っていました", "とおっしゃっています",
            "と仰っています", "と仰っていました", "と説明しています", "と説明していました", "と報告しています", "と報告していました",
            "と宣言しています", "と宣言していました", "と明言しています", "と明言していました", "と表明しています", "と表明していました",
            "と発言しています", "と発言していました", "と指示しています", "と指示していました", "と依頼しています", "と依頼していました",
            "と答えています", "と答えていました", "と回答しています", "と回答していました", "と聞いています", "と聞いていました",
            "と伺っています", "と伺っていました",
        ),
        # 回避の言い切り (#134 W1・spec 2026-11-04-self-stated-workaround-design §5.2/§7): user 自身が原因と回避を同じ span で述べる入口の印。
        # miss fixture (workaround_avoid_markers_20261104.json) に当たらない形だけ。外した語: 回避できる (「回避できるようにしたい」
        # 「回避できる方法を探しています」に当たる)、解消しました / 解消します (「問題は解消しましたか」「解消しますか」に当たる)。
        # 直り / 通りました / 動きました は候補にも入れない (結果の報告で回避の言い切りでない)。疑の文 (「回避できますか？」) は表でなく
        # alaya の規則 (疑の文を除いた残り) で落とす。？の無い疑問 (「回避できますか」) は表には当たるが、案 B の規則 (自述の入口は文末一致) では上がらない (fixture の known_hits・test_question_less_table_hits_do_not_raise)。
        # 自述の入口の原因の印 (#134 W1 案 B・spec 2026-11-04-self-stated-workaround-design §7「7 狭め」): workaround_cause の「ため」「→」は
        # 日常語 (念のため・ためしに・そのため) に当たるので使わず、「原因」だけ。申告 回避策 の content の検査と訂正の入口は workaround_cause のまま。
        "self_stated_cause": ("原因",),
        # 推量の言 (#121 A'・spec 2026-11-07-tentative-preference-design §7): 描画だけが読む。**文末一致** (文中の「とかも」「かもめ」は印でない。文末の「とかも」は当たる = 既知の限界、spec §7)。
        "conjecture": ("かも", "かもしれない", "かもしれません", "かもね", "かもな"),
        "workaround_avoid": ("回避できます", "回避できました", "避けられます", "避けられました"),
        # 文中一致の対の役 (R3b・spec 2026-11-26-en-marker-table-design §4.2): ja は文末一致の役 (decision_assert・conjecture) だけで足りるので空。
        "decision_assert_in_sentence": (),
        "conjecture_in_sentence": (),
        # 承認の語 (承認の対・spec 2026-11-27-ratified-decision-design §3.3/§4・T1): user の短い承認の往復に当たる語。`go` は en 表 (「GO」「Go」「ＧＯ」は NFKC + casefold の照合で当たる。ASCII の語の境は `_has_marker` の R2b が持つ)。1 人の利用者の癖に合わせた初版。
        "approval": ("推奨で", "推奨どおり"),
        # 承認の語の後ろにあれば承認にしない否定の語 (T5・spec 2026-11-27-ratified-decision-design §7.2 N3)。表だけ直せば語を足し引きできる。
        # 入れない: 「ず」「待」の 1 字 (「まずは」「期待どおり」を外す)。語表に無い否定 (「見送り」「厳しい」) は当たる (既知の限界)。
        "approval_negation": ("ない", "ません", "まだ", "待って", "待て", "保留", "やめ", "止め", "だめ", "ダメ"),
    },
    "en": {
        # 2026-11-26 測定 (spec 2026-11-26-en-marker-table-design §1.3・§5.6・裁定 9): rejected を外した (LongMemEval の user の 12 本から見た 4 本が受け身 = 決定でなかった。
        # KP (a)・越境 E・発芽閾値にも効く書く側。能動の却下は decided against が担う)。残る 4 語は今のまま。
        "decision_reject": ("instead of", "rather than", "decided against", "ruled out"),
        # 2026-11-26 測定: 今の 5 語のまま。content だけを見る役で、段 1 には LLM の content が無いため測れていない (設計書 §1.6)。
        "workaround_cause": ("because", "caused by", "root cause", "due to", "→"),
        # spec 2026-11-26-en-marker-table-design §5.1 (P1・利用者 2026-11-26 裁定 4): ja の「を好む」「てほしい」に当たる 8 形だけ。
        # 入れない: i want to / i'd like to / i would like to (ja の「したい」の写しだが、欲 と 勝解 は語の形で分けられず、(ii) が残り全体に当たるので事実の文まで 性向 に落とす = §1.4 の P2)、
        # i want / i'd like (#121 の pin が落ちる)、please (U で 1407 件)。fixture: tests/fixtures/en_markers_20261126.json。
        "preference_suffix": ("i prefer", "i'd prefer", "i would prefer", "i'd rather", "i would rather", "i want you to", "i'd like you to", "i would like you to"),
        # 2026-11-26 測定: 今の 4 語のまま (U の当たりが 0〜3 件で、足す根拠の観測が無い。R1 で `That’s wrong` も当たる)。
        "user_correction": ("that's wrong", "not what i asked", "revert that", "undo that"),
        # 文末一致 (ja の decision_assert と同じ座)。en は () のまま: 英語の断定の印は文末に来ないので、文中一致の対の役 decision_assert_in_sentence に置く (spec 2026-11-26-en-marker-table-design §4.2 R3b)。
        "decision_assert": (),
        # 伝聞の言 (RH2・spec 2026-11-26-en-marker-table-design §5.3・裁定 6): 長い形だけ 11 語。誤った当たりの害は、その 1 回の 再開点 の更新が記録のみになることだけ。
        # 入れない: you said that (what you said that morning に当たる = miss fixture)、you mentioned 単独 (「the retry module you mentioned」に当たる)、
        # according to the record(s) / as recorded (miss fixture)。known_hits: 受け答えの文 + 自分の作業 (「You mentioned that earlier, and I fixed it.」) にも当たる (受け入れる限界)。
        "hearsay": (
            "you mentioned that", "you told me that", "as you mentioned", "as you said", "you said you", "you've mentioned",
            "you had mentioned", "you were planning to", "the user said", "the user mentioned", "you asked me to",
        ),
        # 回避の言い切り (W1・§5.4・裁定 7): 文末一致 (ja と同じ座)。入れない: fixed it / fixes it / solved it / it works / that works (結果の報告で回避の言い切りでない。ja が外した「直りました」「動きました」と同じ)。
        # 正しく当たる側は作文の fixture だけで測った (LongMemEval に当たる側の文が無い・設計書 §1.6)。
        "workaround_avoid": (
            "works around it", "worked around it", "avoids it", "avoided it", "avoids the issue", "avoided the issue",
            "avoids the problem", "avoided the problem", "gets around it", "got around it",
        ),
        # 自述の入口の原因の印 (W1 案 B・§5.4): 5 語。入れない: because / due to (ja の「ため」と同じ日常語。W1 案 B で ja も外した)。known_hits: 「The cause is unknown.」にも当たる。
        "self_stated_cause": ("the cause is", "the cause was", "the root cause", "was caused by", "is caused by"),
        # 暫定 (provisional): en は空 (他の役の en と同じ扱い。実害は ja のみ・#121)
        "conjecture": (),
        # 文中一致の対の役 (R3b): 問いでない文の中のどこかに (語の境つきで) 在れば、文末一致の役と同じ働きをする。
        # decision_assert_in_sentence (spec 2026-11-26-en-marker-table-design §5.2・裁定 5): 16 語。入れない: i went with (同行の意味)、we use / i'm using / we're using (習慣の記述)、
        # i'll use (予定)、i chose (任意。ja に「選んだ」が無い)、i ended up。打ち消し (decided not to) と問い (Should we go with…?) には当たらず 性向 のまま (fail-closed)。
        # conjecture_in_sentence (描画だけ・#121 A'・§5.5・裁定 8): maybe / perhaps / probably の 3 語。入れない: might / could be (assistant の turn に多い。A で 17491・3384 件)、
        # i think (「I think I'll go with X」は決めの文)、not sure / i'm not sure (疑の扱いは `?` と札「一部問い」の側)。
        "decision_assert_in_sentence": (
            "we decided to", "i decided to", "we've decided to", "i've decided to", "we decided on", "i decided on",
            "we'll go with", "i'll go with", "we're going with", "i'm going with", "let's go with", "we went with",
            "we chose", "we settled on", "i settled on", "let's use",
        ),
        "conjecture_in_sentence": ("maybe", "perhaps", "probably"),
        # 承認の語 (T1): ja の表は ASCII の字を持てない (casefold の不変条件) ので `go` は en に置く。照合は union (言語を問わない)。
        "approval": ("go",),
        # 英語の否定 (`no go`・`don't go`) は _is_approval_text の ASCII の英字の残りの検査で外れるので en は ()。
        "approval_negation": (),
    },
}


def union(role):
    """role の印を全言語ぶん、LANGUAGES の順に、初出を残して重複を除いた tuple で返す。
    未知の role は KeyError。"""
    out = []
    for lang in LANGUAGES:
        for m in MARKERS[lang][role]:
            if m not in out:
                out.append(m)
    return tuple(out)
