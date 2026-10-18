"""他の PC に渡す配布フォルダを作る。

  python packaging/make_package.py [出力先フォルダ]   （省略時はデスクトップ）

作成されるもの:
  BIM連携アドオン_Archicad28/
    maedaAddOnPyqt.apx        … Archicad 28 アドオン（Build/Release から）
    BIMServer/
      server.exe              … PyQt 画面（Python・PyQt6・openpyxl を同梱。Python のインストール不要）
      _internal/              … server.exe の実行に必要なファイル
      dialog_order.json, 変更フラグプロパティ.xml, BIM変更管理Graphic Override.xml
    使い方.txt

アドオンは自分のフォルダ（と親フォルダ）から BIMServer/server.exe を探して起動し、
ログ・DB・XML も BIMServer フォルダを使う。開発中の DB やログは含めない。
"""
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "BIM連携アドオン_Archicad28"
APX = ROOT / "Build" / "Release" / "maedaAddOnPyqt.apx"
DATA_FILES = ["dialog_order.json", "変更フラグプロパティ.xml", "BIM変更管理Graphic Override.xml"]
WORK = ROOT / "Build" / "package_work"

README = """\
BIM連携アドオン（Archicad 28 用）  {date}
==================================================

Archicad 28（日本語版）のモデル・オブジェクトの情報を PyQt の画面に取り出し、
表で編集して Archicad に反映するアドオンです。
Python のインストールは不要です（BIMServer\\server.exe に同梱しています）。


■ 必要なもの
  ・Windows 10 / 11（64 ビット）
  ・Archicad 28 日本語版


■ インストール
  1. このフォルダ（{name}）を、変更しない場所にフォルダごとコピーします。
       例: C:\\BIMAddon\\{name}
     ※ maedaAddOnPyqt.apx と BIMServer フォルダは同じフォルダに置いたままにしてください。
     ※ デスクトップや USB メモリ上のままでも動きますが、移動するとアドオンの再登録が必要です。

  2. Archicad 28 を起動し、メニュー［オプション］→［アドオン管理］を開きます。

  3. ［参照...］（または［追加］）で、コピーしたフォルダの maedaAddOnPyqt.apx を選び、
     有効（チェック）にして［OK］を押します。

  4. 初回は Windows の警告（SmartScreen）が出る場合があります。
       ・［詳細情報］→［実行］で起動できます。
     ※ Archicad と画面は PC 内部（127.0.0.1 のポート 5000 / 5001）だけで通信します。外部とは通信しません。


■ 使い方
  1. Archicad でプロジェクトを開きます。
  2. アドオンのメニューを選ぶと「構成取得」ダイアログが表示されます。
  3. ［構成を取得］を押すと、PyQt の画面（条件設定）が表示されます。
     （初回は起動に数秒～十数秒かかることがあります）
  4. 階・要素タイプを選んで［要素を抽出］→ プロパティを選んで［データ取得］。
  5. 表で編集し［反映］で Archicad に書き込みます（取り消しは Archicad の［元に戻す］）。

  ・敷地情報：［敷地マネージャー］ボタン（延焼ラインも一緒に取得）
  ・一覧表：［一覧表から取得］ボタン
  ・Excel：データ表示ウィンドウの［Excel出力］［Excel取込］


■ ファイルの置き場所
  BIMServer フォルダに次のファイルが作られます（消してもかまいません。次回作り直されます）。
    server_log.txt     … 動作ログ（不具合の調査に使います）
    bim_manager.db     … 取得したデータ・変更履歴
    pyqt_window.ini    … 画面の大きさ・位置


■ うまく動かないとき
  ・PyQt の画面が出ない
      BIMServer\\server_log.txt を確認してください。
      BIMServer\\server.exe をダブルクリックして、エラーが出ないか確認してください
      （単体で起動した場合は画面は表示されず、Archicad からの［構成を取得］を待ちます）。
  ・アドオンがメニューに出ない
      Archicad 28 以外（27・29 など）では動きません。アドオン管理で読み込みエラーが出ていないか確認してください。
  ・ポート 5000 / 5001 を他のソフトが使っていると通信できません。
"""


def desktop():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "[Environment]::GetFolderPath('Desktop')"],
                             capture_output=True, text=True, check=True).stdout.strip()
        if out:
            return Path(out)
    except Exception:
        pass
    return Path.home() / "Desktop"


def build_server_exe():
    dist = WORK / "dist"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", "server",
           "--distpath", str(dist), "--workpath", str(WORK / "build"), "--specpath", str(WORK),
           "--exclude-module", "tkinter", "--exclude-module", "pytest",
           str(ROOT / "server.py")]
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)
    exe_dir = dist / "server"
    if not (exe_dir / "server.exe").exists():
        raise SystemExit("server.exe が作成されませんでした")
    return exe_dir


def main():
    out_root = Path(sys.argv[1]) if len(sys.argv) > 1 else desktop()
    if not APX.exists():
        raise SystemExit(f"アドオンがありません（先に cmake --build Build --config Release）: {APX}")
    exe_dir = build_server_exe()

    pkg = out_root / PACKAGE_NAME
    if pkg.exists():
        shutil.rmtree(pkg)
    (pkg / "BIMServer").mkdir(parents=True)
    shutil.copy2(APX, pkg / APX.name)
    shutil.copytree(exe_dir, pkg / "BIMServer", dirs_exist_ok=True)
    for name in DATA_FILES:
        shutil.copy2(ROOT / name, pkg / "BIMServer" / name)
    (pkg / "使い方.txt").write_text(
        README.format(date=date.today().isoformat(), name=PACKAGE_NAME), encoding="utf-8-sig")

    size = sum(f.stat().st_size for f in pkg.rglob("*") if f.is_file())
    print(f"作成しました: {pkg}  （{size / 1024 / 1024:.1f} MB）")


if __name__ == "__main__":
    main()
