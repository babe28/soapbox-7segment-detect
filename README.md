# soapbox-7segment-detect

7セグ表示をカメラ映像から読み取り、ローカル画面表示・テキスト出力・HTTP送信・OBSブラウザソース配信を行うツールです。

## できること

- RTSP / カメラ映像を取り込み
- 上段・下段それぞれのROIを指定して7セグ数字を認識
- 認識結果を `123.456` の形式で表示
- `upper_time.txt` / `lower_time.txt` へ書き出し
- HTTP APIへ認識値を送信
- OBSブラウザソース向けにSSEで値を配信

## 必要環境

- Python 3.x
- Windows環境を想定

依存パッケージ:

```bash
pip install -r requirements.txt
```

## 起動方法

```bash
python app.py
```

## 使い方

1. `RTSP/カメラ` にカメラ番号またはURLを入力して接続します。
2. `上段のROI` / `下段のROI` を選んで、プレビュー上で範囲をドラッグします。
3. 必要に応じてしきい値や補正パラメータを調整します。
4. `認識スタート` を押すと認識が始まります。
5. 左下の `現在の状態` で、カメラ接続・認識中・OBS配信中かを確認できます。

## OBSブラウザソース連携

アプリ起動後、ローカルの配信サーバーが立ち上がります。表示URL はアプリ内に表示されます。

- 両方表示: `http://<PCのIP>:8765/`
- 上段のみ: `http://<PCのIP>:8765/?target=upper`
- 下段のみ: `http://<PCのIP>:8765/?target=lower`

OBSのブラウザソースに上記URLを設定すると、認識結果が自動反映されます。

見た目は `obs_overlay.html` を編集することで調整できます。

## 出力

- テキスト出力: `upper_time.txt`, `lower_time.txt`
- API送信: 画面の `URL` に指定したエンドポイントへPOST
- OBS配信: 内蔵Webサーバーから配信

## 主なファイル

- `app.py`: アプリ本体
- `obs_overlay.html`: OBSブラウザソース用の表示テンプレート
- `requirements.txt`: 依存パッケージ
