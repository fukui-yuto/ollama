from orchestrator import run

# 戻り値は (最終回答, 更新された会話履歴) のタプル
result, history = run("Pythonでフィボナッチ数列を生成する関数を書いてください", integrate=False)

# 会話履歴を引き継いで次の質問
result2, history = run("それをジェネレータ版に書き直してください", history=history, integrate=False)
