archicad28バージョンAddOnテンプレートをもとに、モデル、オブジェクトの構成を取得し、双方向通信でエキスポートで
python常駐でpyQtで表示した物から、大中小項目で検索、取得し 指定したものを検索してプロパティ要素や、セグメント要素などを取得し、
編集し、インポートでモデル、オブジェクトを変更できるものを作りたい。
      
Archicad 28 API Development Kit
VS Code (Windows)
CMake を使用
テンプレートでbuildして環境は設定済み
Archicad 28テンプレートベースで作成
C++ Add-On 双方向 TCP Socket 双方向 Python
PythonでARCHICAD28と双方向通信（ローカル、json)でpyQtにエキスポート又はインポート
トランザクションACAPI_CallUndoableCommand() 実装   エラー時ロールバック
差分更新（modiStamp連携）実装  不一致ならスキップ or 警告   手動判断
更新対象限定  変更ありのみ表示。
差分処理層を「バリデーション付き差分抽出」にする
PropertyDefinition に含まれるメタ情報（型・列挙値・単位・編集可否）を取得し、それを使って「バリデーション付き差分抽出」を実装する
BuildChangedProperties() で：
PropertyDefinitionベースの型検証 , enum検証 , 編集可否確認,差分抽出 ,availableチェック,editableチェック

変更箇所を赤塗り ,PyQt一覧クリックで対象表示,,確認ボタンで承認,一覧削除,赤解除
未確認状態を ARCHICAD 側のプロパティとして保持し、PyQt起動時に再取得する

基本、日本語表示で答えてください。

最適アーキテクチャ
① Add-on → 対象GUID取得
② Python → 詳細取得
③ PyQt → 編集
④ Excel → 差分確認
⑤ ユーザー承認
⑥ 更新前再チェック（modiStamp）
⑦ 差分のみ更新（C++ API）

archicad構成
Element
 ├─ Header（GUID / type / layer）
 ├─ Geometry（Memo）
 ├─ Parameters（type別構造体）
 ├─ Properties（別API）
 ├─ Classification（別API）
 └─ Analytical Model（別API）


