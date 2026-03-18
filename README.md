# Multi-Agent Orchestrator with Ollama

ローカルで動作するマルチエージェントAIシステム。
`deepseek-r1:7b` がルーターとしてタスクを振り分け、`qwen2.5:7b` が統合・日本語化を担当します。

---

## システム構成図

```
┌─────────────────────────────────────────────────────────┐
│                      ユーザー入力                       │
└───────────────────────────┬─────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│              deepseek-r1:7b  【ルーター】               │
│                                                         │
│  入力を分析し、タスク種別を判定                         │
│  出力: {"type": "code" / "text" / "image"}             │
└──────────┬──────────────────┬──────────────┬────────────┘
           │                  │              │
     type=code          type=text      type=image
           │                  │              │
           ▼                  ▼              ▼
┌──────────────┐   ┌──────────────┐  ┌──────────────────┐
│qwen2.5-coder │   │  qwen2.5:7b  │  │   llava-llama3   │
│    :7b       │   │              │  │                  │
│【コードAI】  │   │【テキストAI】│  │  【画像解析AI】  │
│              │   │              │  │                  │
│コード生成    │   │要約・翻訳    │  │画像の説明・分析  │
│デバッグ      │   │質問回答      │  │テキスト読み取り  │
│説明          │   │雑談          │  │グラフ解析        │
└──────┬───────┘   └──────┬───────┘  └────────┬─────────┘
       │                  │                   │
       └──────────────────┴───────────────────┘
                          │
                    ワーカー出力
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│               qwen2.5:7b  【統合・日本語化】            │
│                                                         │
│  ・外国語テキストをすべて日本語に翻訳                   │
│  ・自然な日本語に整理・要約                             │
│  ※ --raw 指定時はこのステップをスキップ                │
└───────────────────────────┬─────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│                      最終回答（日本語）                 │
└─────────────────────────────────────────────────────────┘

※ nomic-embed-text はRAG用途の埋め込み生成に使用（将来拡張用）
```

### 役割まとめ

| モデル | 役割 | 担当処理 |
|--------|------|----------|
| deepseek-r1:7b | ルーター | タスク分類（code / text / image） |
| qwen2.5:7b | テキストワーカー＋統合 | 質問回答・翻訳・要約、最終出力の日本語整理 |
| qwen2.5-coder:7b | コードワーカー | コード生成・デバッグ・説明 |
| llava-llama3 | 画像ワーカー | 画像説明・分析・テキスト読み取り |
| nomic-embed-text | 埋め込み | RAG用テキストベクトル化（将来拡張用） |

---

## 動作確認済み環境

| 項目 | 内容 |
|------|------|
| OS | Windows 11 |
| CPU | Intel Core i7-9700K (8コア) |
| RAM | 48 GB |
| GPU | NVIDIA GeForce RTX 2060 SUPER (VRAM 8GB) |
| Python | 3.12.8 |

---

## 1. 事前準備

### Ollama のインストール

[https://ollama.com](https://ollama.com) からインストーラーをダウンロードして実行。

インストール確認:

```bash
ollama --version
```

### Python パッケージのインストール

```bash
pip install ollama
```

---

## 2. モデルのインストール

以下のコマンドを順番に実行してください。合計約20GBのストレージが必要です。

```bash
# ルーターAI（タスク分類）
ollama pull deepseek-r1:7b

# 日本語テキスト汎用＋統合担当
ollama pull qwen2.5:7b

# コーディング特化
ollama pull qwen2.5-coder:7b

# 画像理解（Vision）
ollama pull llava-llama3

# テキスト埋め込み（RAG用）
ollama pull nomic-embed-text
```

インストール済みモデルの確認:

```bash
ollama list
```

期待される出力:

```
NAME                       SIZE
deepseek-r1:7b             4.7 GB
qwen2.5:7b                 4.7 GB
qwen2.5-coder:7b           4.7 GB
llava-llama3:latest        5.5 GB
nomic-embed-text:latest    274 MB
```

---

## 3. 使い方

### 対話モードで起動

```bash
python -X utf8 orchestrator.py
```

起動後、日本語でそのまま話しかけてください。ルーターAIが自動でタスクを判断し、最適なモデルに振り分けます。

```
=== Multi-Agent Orchestrator ===
終了: 'exit' または 'quit'
...

あなた: （ここに入力）
```

### 終了

```
あなた: exit
```

---

## 4. 入力パターン

### テキスト・質問（qwen2.5:7b が担当）

```
あなた: 量子コンピュータをわかりやすく説明してください
あなた: この文章を英語に翻訳してください：「本日は晴天なり」
あなた: 江戸時代の文化について教えて
```

### コーディング（qwen2.5-coder:7b が担当）

```
あなた: Pythonでクイックソートを実装してください
あなた: このコードのバグを直してください：print("Hello"
あなた: JavaScriptで非同期処理を書く方法を教えて
```

### 画像解析（llava-llama3 が担当）

画像ファイルのパスを `image:` の後に指定します。

```
あなた: この画像に何が写っていますか？ image: C:/Users/yuto/Desktop/photo.jpg
あなた: 画像のテキストを読み取ってください image: C:/screenshot.png
あなた: image: C:/chart.jpg グラフの内容を説明して
```

### オプション：ワーカーの生回答をそのまま表示

末尾に `--raw` を追加すると統合・日本語化ステップを省略します（高速化）。

```
あなた: Pythonでリストを逆順にする方法 --raw
```

---

## 5. 処理の流れ（詳細）

1つの入力に対して内部では以下の3ステップが実行されます。

```
ステップ1: ルーティング
  deepseek-r1:7b が入力を分析し、
  "text" / "code" / "image" のどれかに分類

ステップ2: ワーカー実行
  分類に対応する専門モデルが実際の処理を実行

ステップ3: 統合・日本語化（--raw なしの場合）
  qwen2.5:7b がワーカーの回答を
  自然な日本語に整理してユーザーに返す
  ※ ワーカー出力が長すぎる場合は1500文字でトランケート後に統合
```

---

## 6. Pythonコードから使う

`orchestrator.py` をモジュールとしてインポートして使えます。

```python
from orchestrator import run

# テキストタスク
result = run("AIとは何ですか？")

# コードタスク
result = run("Pythonで素数判定関数を書いてください")

# 画像タスク
result = run("画像の内容を説明して", image_path="C:/photo.jpg")

# 統合なし（ワーカーの生出力）
result = run("バブルソートを実装して", integrate=False)

print(result)
```

---

## 7. VRAM・RAM の目安

| モデル | VRAM使用量 | 動作モード |
|--------|-----------|-----------|
| deepseek-r1:7b | 約5GB | GPU |
| qwen2.5:7b | 約5GB | GPU |
| qwen2.5-coder:7b | 約5GB | GPU |
| llava-llama3 | 約6GB | GPU |
| nomic-embed-text | 約0.3GB | GPU |

※ 同時に複数モデルをロードすると VRAM を超えた分は自動的に RAM にオフロードされます。
※ RAM 48GB あるため大きなモデルの CPU 推論も可能です。

---

## 8. トラブルシューティング

### Ollama が起動していない

```
Error: dial tcp: connection refused
```

Ollama のデスクトップアプリを起動してから再実行してください。またはコマンドで起動:

```bash
ollama serve
```

### 文字化けが発生する

```bash
# -X utf8 オプションを必ず付けて実行
python -X utf8 orchestrator.py
```

### モデルが見つからない

```
Error: model not found
```

`ollama list` でインストール済みモデルを確認し、不足しているモデルを `ollama pull` で追加してください。

### GPU を使っていない（推論が遅い）

NVIDIA ドライバーと CUDA が正しくインストールされているか確認:

```bash
nvidia-smi
```

---

## 9. ファイル構成

```
.
├── orchestrator.py       # メインシステム（ルーター＋ワーカー＋統合）
├── test_orchestrator.py  # 動作テスト用スクリプト
└── README.md             # このファイル
```

---

## 10. モデルの追加・変更

`orchestrator.py` の先頭部分を編集することでモデルを変更できます。

```python
LEADER_MODEL = "deepseek-r1:7b"   # ルーターを変更したい場合ここを編集

WORKERS = {
    "text":  "qwen2.5:7b",         # テキスト担当・統合担当を変更したい場合
    "code":  "qwen2.5-coder:7b",   # コード担当を変更したい場合
    "image": "llava-llama3",        # 画像担当を変更したい場合
}
```

Ollama で利用可能なモデル一覧: [https://ollama.com/library](https://ollama.com/library)
