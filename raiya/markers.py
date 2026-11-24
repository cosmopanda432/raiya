"""markers.py -- kind の形の検査に使う語の表 (名の層)。spec 2026-10-24-marker-table-design.md
規則 (どこに印を求めるか・どう比べるか) は alaya._resolve_kind が持つ。ここは言語ごとの共立の表だけ。
import しない (葉のモジュール。AST pin)。"""

LANGUAGES = ("ja", "en")            # 和集合の順序。dict の反復順に頼らない
ROLES = ("decision_reject", "workaround_cause", "preference_suffix", "user_correction", "decision_assert", "hearsay", "workaround_avoid", "self_stated_cause", "conjecture")

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
    },
    "en": {
        # 暫定 (provisional): 受領証 `形の検査` が測るまで。spec §3.4
        "decision_reject": ("instead of", "rather than", "rejected", "decided against", "ruled out"),
        "workaround_cause": ("because", "caused by", "root cause", "due to", "→"),
        "preference_suffix": (),
        "user_correction": ("that's wrong", "not what i asked", "revert that", "undo that"),
        # 暫定 (provisional): en は空 (en preference_suffix と同じ扱い。x19 の実害は ja のみ・spec 2026-10-31 §7)
        "decision_assert": (),
        # 暫定 (provisional): en は空 (decision_assert / preference_suffix の en と同じ扱い。実害は ja のみ・#129 は ja。
        # "you said that" / "according to the record" は what you said that morning / the records に部分一致するので入れない。受領証が測るまで)
        "hearsay": (),
        # 暫定 (provisional): en は空 (他の役の en と同じ扱い。実害は ja のみ・#134 は ja。hearsay の en と同じ書き方)
        "workaround_avoid": (),
        # 暫定 (provisional): en は空 (他の役の en と同じ扱い。実害は ja のみ・#134)
        "self_stated_cause": (),
        # 暫定 (provisional): en は空 (他の役の en と同じ扱い。実害は ja のみ・#121)
        "conjecture": (),
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
