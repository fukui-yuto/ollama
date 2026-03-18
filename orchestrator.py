"""
Multi-Agent Orchestrator
リーダー: deepseek-r1:7b
ワーカー: qwen2.5:7b / qwen2.5-coder:7b / llava-llama3 / nomic-embed-text
"""

import sys
import io
import ollama
import json
import re
import base64
from pathlib import Path

# Windows での文字化け対策
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


LEADER_MODEL = "deepseek-r1:7b"

WORKERS = {
    "text":  "qwen2.5:7b",
    "code":  "qwen2.5-coder:7b",
    "image": "llava-llama3",
}

ROUTER_PROMPT = """タスクを分類してください。

ルール:
- コード作成・デバッグ・関数作成・プログラム説明 → code
- 画像の説明・分析（image:が含まれる） → image
- それ以外（要約・翻訳・質問回答・雑談） → text

ユーザー入力: {input}
画像ファイル: {image}

必ずこのJSON形式のみで答えること。前置き・説明・改行は不要:
{{"type": "text"}}"""


def extract_json(text: str) -> dict:
    """モデル出力からJSONを抽出する"""
    # <think>タグを除去（DeepSeek R1の思考過程）
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # JSON部分を抽出
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"type": "text", "reason": "判断できなかったためtextにフォールバック"}


def route_task(user_input: str, image_path: str = None) -> dict:
    """リーダーAIがタスクの種類を判断する"""
    print(f"\n[リーダー] タスクを分析中...")

    prompt = ROUTER_PROMPT.format(
        input=user_input,
        image=image_path if image_path else "なし"
    )

    response = ollama.chat(
        model=LEADER_MODEL,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response["message"]["content"]
    result = extract_json(raw)
    print(f"[リーダー] 判断: {result['type']} - {result.get('reason', '')}")
    return result


def run_worker(task_type: str, user_input: str, image_path: str = None) -> str:
    """専門モデルにタスクを実行させる"""
    model = WORKERS[task_type]
    print(f"[ワーカー] {model} が処理中...\n")

    if task_type == "image" and image_path:
        # 画像をbase64エンコード
        image_data = Path(image_path).read_bytes()
        image_b64 = base64.b64encode(image_data).decode()
        response = ollama.chat(
            model=model,
            messages=[{
                "role": "user",
                "content": user_input,
                "images": [image_b64]
            }]
        )
    else:
        response = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": user_input}]
        )

    return response["message"]["content"]


def integrate_result(user_input: str, worker_output: str, task_type: str) -> str:
    """リーダーが結果を統合・整理する"""
    print(f"\n[リーダー] 結果を統合中...")

    # ワーカー出力が長すぎる場合はトランケート（繰り返しループを防ぐ）
    MAX_WORKER_CHARS = 1500
    if len(worker_output) > MAX_WORKER_CHARS:
        worker_output = worker_output[:MAX_WORKER_CHARS] + "\n...(省略)"

    prompt = f"""ユーザーの質問に対するAIの回答を、純粋な日本語に翻訳・整理してください。

重要なルール:
1. 出力に英語・中国語・その他の外国語を一切含めないこと
2. すべての外来語・専門用語を日本語に置き換えること（例: quantum=量子、qubit=量子ビット、algorithm=アルゴリズム）
3. 300字以内で簡潔にまとめること
4. 日本語のみで回答すること

質問: {user_input}

翻訳・整理する回答:
{worker_output}

上記を日本語のみで簡潔にまとめた結果:"""

    # 統合・日本語化はqwen2.5:7bの方が得意なためそちらを使用
    response = ollama.chat(
        model=WORKERS["text"],
        messages=[{"role": "user", "content": prompt}],
        options={"num_predict": 1024, "repeat_penalty": 1.3, "temperature": 0.3}
    )

    raw = response["message"]["content"]
    # <think>タグを除去（念のため）
    result = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    return result


def run(user_input: str, image_path: str = None, integrate: bool = True) -> str:
    """
    メインのオーケストレーション処理

    Args:
        user_input: ユーザーからの入力テキスト
        image_path: 画像ファイルのパス（省略可）
        integrate:  Trueならリーダーが結果を統合、FalseならWorker出力をそのまま返す
    """
    print("=" * 50)
    print(f"[入力] {user_input}")
    if image_path:
        print(f"[画像] {image_path}")
    print("=" * 50)

    # 1. リーダーがルーティング判断
    routing = route_task(user_input, image_path)
    task_type = routing["type"]

    # 画像が指定されているのにtextと判断された場合は上書き
    if image_path and task_type != "image":
        task_type = "image"

    # 2. ワーカーが実行
    worker_result = run_worker(task_type, user_input, image_path)

    # 3. リーダーが統合（オプション）
    if integrate:
        final = integrate_result(user_input, worker_result, task_type)
    else:
        final = worker_result

    print("\n" + "=" * 50)
    print("[最終回答]")
    print("=" * 50)
    print(final)
    return final


def chat_loop():
    """対話型CLIループ"""
    print("\n=== Multi-Agent Orchestrator ===")
    print("終了: 'exit' または 'quit'")
    print("画像付き: 'image: /path/to/image.jpg' を入力に含める")
    print("統合なし: '--raw' を末尾に追加するとワーカー出力をそのまま表示")
    print("=" * 32 + "\n")

    while True:
        try:
            user_input = input("あなた: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n終了します。")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("終了します。")
            break

        # オプション解析
        integrate = True
        if user_input.endswith("--raw"):
            integrate = False
            user_input = user_input[:-5].strip()

        image_path = None
        image_match = re.search(r"image:\s*(\S+)", user_input)
        if image_match:
            image_path = image_match.group(1)
            user_input = user_input[:image_match.start()].strip()
            if not Path(image_path).exists():
                print(f"[エラー] 画像ファイルが見つかりません: {image_path}\n")
                continue

        run(user_input, image_path=image_path, integrate=integrate)
        print()


if __name__ == "__main__":
    chat_loop()
