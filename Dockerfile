# Pythonの軽量版を使用
FROM python:3.10-slim

# 音声再生に必要なFFmpegをインストール
RUN apt-get update && apt-get install -y ffmpeg && apt-get clean

# 作業ディレクトリの設定
WORKDIR /app

# 依存ライブラリのインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# プログラム全体のコピー
COPY . .

# ボットの起動
CMD ["python", "main.py"]