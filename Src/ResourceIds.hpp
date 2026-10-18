#ifndef RESOURCEIDS_HPP
#define RESOURCEIDS_HPP

#pragma warning(disable : 4819)

#define ID_ADDON_INFO               32000
#define ID_ADDON_MENU               32500

// Add-On Identifier (MDID) — RFIX/AddOnFix.grc と AddOnMain.cpp (OwnModuleID) で共用
// ※ 片方だけ変えると CallFromEventLoop が自分に届かずコマンドが一切実行されない
#define ADDON_MDID_DEVELOPER_ID     930179925
#define ADDON_MDID_LOCAL_ID         99967209

// Main Dialog (32501) — 構成取得 / キャンセル の2ボタン
#define ID_ADDON_DLG                32501
#define ID_SEARCH_DIALOG            32501
#define GetConfigButtonID           1
#define CancelButtonID              2

// Legacy IDs for ResultDialog.cpp
#define ID_RESULT_DIALOG            32502
#define ResultListID                1
#define CloseButtonID               2

#endif
