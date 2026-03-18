"""
Multi-Agent Orchestrator
リーダー: deepseek-r1:7b
ワーカー: qwen2.5:7b / qwen2.5-coder:7b / llava-llama3 / nomic-embed-text

改善:
  1. ストリーミング出力  - ワーカー・統合ステップをリアルタイム表示
  2. 会話履歴の保持     - 前のターンを踏まえた文脈ある回答
  3. RAG               - rag_docs/ 内ドキュメントを nomic-embed-text で検索・注入
               対応形式: .txt / .md / .pdf / .docx / .csv / .json / .html
  4. 応答時間の表示     - 各ステップの処理時間を計測・表示
  5. 会話履歴の保存・復元 - sessions/ に JSON 保存、/save・/load で操作
"""

import sys
import io
import math
import csv
import time
import ollama
import json
import re
import base64
from datetime import datetime
from pathlib import Path

try:
    import fitz as pymupdf          # pymupdf
    _PDF_OK = True
except ImportError:
    _PDF_OK = False

try:
    from docx import Document as DocxDocument
    _DOCX_OK = True
except ImportError:
    _DOCX_OK = False

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

# Windows での文字化け対策
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


LEADER_MODEL = "deepseek-r1:7b"
EMBED_MODEL  = "nomic-embed-text"

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

# RAG ドキュメントフォルダ（存在しない場合はRAG無効）
RAG_DIR = Path("rag_docs")

# セッション保存フォルダ
SESSIONS_DIR = Path("sessions")

# インメモリベクトルストア: [{"text": str, "embedding": list[float], "source": str}]
_rag_store: list[dict] = []


# ─────────────────────────────────────────────
# 1. RAG
# ─────────────────────────────────────────────

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot    = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _extract_text(path: Path) -> str:
    """ファイル種別に応じてテキストを抽出する"""
    suffix = path.suffix.lower()

    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")

    if suffix == ".pdf":
        if not _PDF_OK:
            print(f"  [警告] pymupdf 未インストールのため {path.name} をスキップ")
            return ""
        doc = pymupdf.open(str(path))
        return "\n".join(page.get_text() for page in doc)

    if suffix == ".docx":
        if not _DOCX_OK:
            print(f"  [警告] python-docx 未インストールのため {path.name} をスキップ")
            return ""
        doc = DocxDocument(str(path))
        return "\n".join(p.text for p in doc.paragraphs)

    if suffix == ".csv":
        rows = []
        with path.open(encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                rows.append(", ".join(row))
        return "\n".join(rows)

    if suffix == ".json":
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            obj = json.loads(raw)
            return json.dumps(obj, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return raw

    if suffix == ".html":
        if not _BS4_OK:
            print(f"  [警告] beautifulsoup4 未インストールのため {path.name} をスキップ")
            return ""
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
        return soup.get_text(separator="\n")

    return ""


# 対応拡張子一覧
_SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf", ".docx", ".csv", ".json", ".html"}


def load_rag_docs(rag_dir: Path = RAG_DIR) -> int:
    """
    RAG_DIR 内の対応ファイルを再帰的に読み込み、
    500字ごとにチャンク分割して埋め込みを生成・ストアに保存する。

    対応形式: .txt / .md / .pdf / .docx / .csv / .json / .html
    戻り値: 保存したチャンク数
    """
    global _rag_store
    _rag_store = []

    if not rag_dir.exists():
        return 0

    files = [p for p in rag_dir.rglob("*") if p.suffix.lower() in _SUPPORTED_SUFFIXES]
    for path in files:
        print(f"  [RAG] 読み込み中: {path.name}")
        text = _extract_text(path).strip()
        if not text:
            continue
        chunks = [text[i:i + 500] for i in range(0, len(text), 500)]
        for chunk in chunks:
            resp = ollama.embeddings(model=EMBED_MODEL, prompt=chunk)
            _rag_store.append({
                "text":      chunk,
                "embedding": resp["embedding"],
                "source":    path.name,
            })

    return len(_rag_store)


def _retrieve(query: str, top_k: int = 3) -> str:
    """クエリに最も近い上位 top_k チャンクを結合して返す"""
    if not _rag_store:
        return ""
    resp  = ollama.embeddings(model=EMBED_MODEL, prompt=query)
    q_emb = resp["embedding"]
    ranked = sorted(
        _rag_store,
        key=lambda x: _cosine_similarity(q_emb, x["embedding"]),
        reverse=True,
    )
    return "\n\n".join(
        f"[{r['source']}]\n{r['text']}" for r in ranked[:top_k]
    )


# ─────────────────────────────────────────────
# 2. セッション保存・復元
# ─────────────────────────────────────────────

def save_history(name: str, history: list[dict]) -> Path:
    """会話履歴を sessions/<name>.json に保存する"""
    SESSIONS_DIR.mkdir(exist_ok=True)
    path = SESSIONS_DIR / f"{name}.json"
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "history":  history,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_history(name: str) -> list[dict]:
    """sessions/<name>.json から会話履歴を復元する"""
    path = SESSIONS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"セッション '{name}' が見つかりません: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["history"]


def list_sessions() -> list[str]:
    """保存済みセッションの名前一覧を返す"""
    if not SESSIONS_DIR.exists():
        return []
    return [p.stem for p in sorted(SESSIONS_DIR.glob("*.json"))]


# ─────────────────────────────────────────────
# 3. Routing
# ─────────────────────────────────────────────

def extract_json(text: str) -> dict:
    """モデル出力からJSONを抽出する"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"type": "text", "reason": "判断できなかったためtextにフォールバック"}


def route_task(user_input: str, image_path: str = None) -> dict:
    """deepseek-r1:7b がタスクの種類を判断する"""
    print(f"\n[リーダー] {LEADER_MODEL} がタスクを分析中...")
    prompt = ROUTER_PROMPT.format(
        input=user_input,
        image=image_path if image_path else "なし",
    )
    response = ollama.chat(
        model=LEADER_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    raw    = response["message"]["content"]
    result = extract_json(raw)
    print(f"[リーダー] 判断: {result['type']} - {result.get('reason', '')}")
    return result


# ─────────────────────────────────────────────
# 4. Worker — ストリーミング + 会話履歴 + RAGコンテキスト
# ─────────────────────────────────────────────

def run_worker(
    task_type:   str,
    user_input:  str,
    history:     list[dict],
    image_path:  str = None,
    rag_context: str = "",
) -> str:
    """専門モデルにタスクを実行させる（ストリーミング出力）"""
    model = WORKERS[task_type]
    print(f"[ワーカー] {model} が処理中...\n")

    # 会話履歴をベースに、RAGコンテキストをシステムメッセージとして先頭に注入
    messages: list[dict] = []
    if rag_context:
        messages.append({
            "role":    "system",
            "content": f"以下の参考情報を踏まえて回答してください:\n\n{rag_context}",
        })
    messages.extend(history)

    if task_type == "image" and image_path:
        image_b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
        messages.append({
            "role":    "user",
            "content": user_input,
            "images":  [image_b64],
        })
    else:
        messages.append({"role": "user", "content": user_input})

    # ストリーミングで出力しながら全文を収集
    full_response = ""
    for chunk in ollama.chat(model=model, messages=messages, stream=True):
        token = chunk["message"]["content"]
        print(token, end="", flush=True)
        full_response += token
    print()  # 改行

    return full_response


# ─────────────────────────────────────────────
# 5. Integration — ストリーミング
# ─────────────────────────────────────────────

def integrate_result(user_input: str, worker_output: str) -> str:
    """qwen2.5:7b が結果を統合・日本語化する（ストリーミング出力）"""
    print(f"\n[統合] {WORKERS['text']} が整理中...\n")

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

    full_response = ""
    for chunk in ollama.chat(
        model=WORKERS["text"],
        messages=[{"role": "user", "content": prompt}],
        options={"num_predict": 1024, "repeat_penalty": 1.3, "temperature": 0.3},
        stream=True,
    ):
        token = chunk["message"]["content"]
        print(token, end="", flush=True)
        full_response += token
    print()

    return re.sub(r"<think>.*?</think>", "", full_response, flags=re.DOTALL).strip()


# ─────────────────────────────────────────────
# 6. Main orchestration — 応答時間計測
# ─────────────────────────────────────────────

def run(
    user_input: str,
    history:    list[dict] = None,
    image_path: str = None,
    integrate:  bool = True,
    use_rag:    bool = True,
) -> tuple[str, list[dict]]:
    """
    メインのオーケストレーション処理

    Args:
        user_input: ユーザーからの入力テキスト
        history:    会話履歴（省略時は空リスト）
        image_path: 画像ファイルのパス（省略可）
        integrate:  Trueならリーダーが結果を統合、FalseならWorker出力をそのまま返す
        use_rag:    TrueならRAGドキュメントを検索してコンテキストに注入する

    Returns:
        (最終回答文字列, 更新された会話履歴)
    """
    if history is None:
        history = []

    print("=" * 50)
    print(f"[入力] {user_input}")
    if image_path:
        print(f"[画像] {image_path}")
    print("=" * 50)

    timings: dict[str, float] = {}

    # 1. RAG 検索
    rag_context = ""
    if use_rag and _rag_store:
        t0 = time.perf_counter()
        rag_context = _retrieve(user_input)
        timings["RAG"] = time.perf_counter() - t0
        if rag_context:
            chunk_count = rag_context.count("\n\n") + 1
            print(f"[RAG] 関連チャンク {chunk_count} 件を注入\n")

    # 2. ルーティング
    t0 = time.perf_counter()
    routing   = route_task(user_input, image_path)
    timings["ルーター"] = time.perf_counter() - t0
    task_type = routing["type"]
    if image_path and task_type != "image":
        task_type = "image"

    # 3. ワーカー実行（ストリーミング）
    print("\n[ワーカー出力]")
    print("-" * 50)
    t0 = time.perf_counter()
    worker_result = run_worker(task_type, user_input, history, image_path, rag_context)
    timings["ワーカー"] = time.perf_counter() - t0

    # 4. 統合（ストリーミング）
    if integrate:
        print("\n[統合結果]")
        print("-" * 50)
        t0 = time.perf_counter()
        final = integrate_result(user_input, worker_result)
        timings["統合"] = time.perf_counter() - t0
    else:
        final = worker_result

    # 5. 応答時間サマリー
    total = sum(timings.values())
    parts = "  ".join(f"{k}:{v:.1f}s" for k, v in timings.items())
    print(f"\n[時間] {parts}  合計:{total:.1f}s")

    # 6. 会話履歴を更新（immutable に追記）
    updated_history = history + [
        {"role": "user",      "content": user_input},
        {"role": "assistant", "content": final},
    ]

    print("=" * 50)
    return final, updated_history


# ─────────────────────────────────────────────
# 7. 対話型 CLI
# ─────────────────────────────────────────────

def chat_loop():
    print("\n=== Multi-Agent Orchestrator ===")
    print("終了              : 'exit' または 'quit'")
    print("画像付き          : 'image: /path/to/image.jpg' を入力に含める")
    print("統合なし          : '--raw' を末尾に追加")
    print("RAG無効           : '--norag' を末尾に追加")
    print("履歴クリア        : '/clear'")
    print("ドキュメント再読込: '/reload'")
    print("履歴を保存        : '/save <名前>'")
    print("履歴を復元        : '/load <名前>'")
    print("保存済み一覧      : '/sessions'")
    print("=" * 32 + "\n")

    # RAG ドキュメントの初期ロード
    if RAG_DIR.exists():
        print(f"[RAG] {RAG_DIR}/ からドキュメントを読み込み中...")
        count = load_rag_docs()
        print(f"[RAG] {count} チャンク読み込み完了\n")
    else:
        print(f"[RAG] {RAG_DIR}/ が存在しないためRAGは無効です\n")

    history: list[dict] = []

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
        if user_input == "/clear":
            history = []
            print("[履歴をクリアしました]\n")
            continue
        if user_input == "/reload":
            print("[RAG] ドキュメントを再読み込み中...")
            count = load_rag_docs()
            print(f"[RAG] {count} チャンク読み込み完了\n")
            continue
        if user_input == "/sessions":
            names = list_sessions()
            if names:
                print("[保存済みセッション]")
                for n in names:
                    print(f"  {n}")
            else:
                print("[保存済みセッションはありません]")
            print()
            continue
        if user_input.startswith("/save"):
            parts = user_input.split(maxsplit=1)
            name  = parts[1].strip() if len(parts) > 1 else ""
            if not name:
                # 名前省略時はタイムスタンプを使用
                name = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = save_history(name, history)
            print(f"[保存完了] {path}\n")
            continue
        if user_input.startswith("/load"):
            parts = user_input.split(maxsplit=1)
            name  = parts[1].strip() if len(parts) > 1 else ""
            if not name:
                print("[エラー] /load <名前> の形式で指定してください\n")
                continue
            try:
                history = load_history(name)
                print(f"[復元完了] '{name}' ({len(history) // 2} ターン)\n")
            except FileNotFoundError as e:
                print(f"[エラー] {e}\n")
            continue

        # オプション解析
        integrate = True
        use_rag   = True

        if "--raw" in user_input:
            integrate  = False
            user_input = user_input.replace("--raw", "").strip()
        if "--norag" in user_input:
            use_rag    = False
            user_input = user_input.replace("--norag", "").strip()

        image_path  = None
        image_match = re.search(r"image:\s*(\S+)", user_input)
        if image_match:
            image_path = image_match.group(1)
            user_input = user_input[:image_match.start()].strip()
            if not Path(image_path).exists():
                print(f"[エラー] 画像ファイルが見つかりません: {image_path}\n")
                continue

        _, history = run(
            user_input,
            history=history,
            image_path=image_path,
            integrate=integrate,
            use_rag=use_rag,
        )
        print()


if __name__ == "__main__":
    chat_loop()
