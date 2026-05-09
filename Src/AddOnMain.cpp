#pragma warning(disable : 4819)
#define _WINSOCK_DEPRECATED_NO_WARNINGS
#include "APIEnvir.h"
#include "ACAPinc.h"
#include "ResourceIds.hpp"
#include "DGModule.hpp"
#include "DG.h"
#include "Location.hpp"
#include "FileSystem.hpp"
#include "ExampleDialog.hpp"
#include "StoryService.hpp"
#include "ElementService.hpp"
#include "HashTable.hpp"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <thread>
#include <vector>
#include <string>
#include <mutex>
#include <queue>
#include <atomic>
#include <cmath>
#include <fstream>
#include <chrono>
#include <iomanip>
#include <map>
#include <deque>
#include <algorithm>
#include <cctype>

#pragma comment(lib, "ws2_32.lib")

// --- [DEBUG]: 外部ログ出力関数の実装 ---
void WriteExternalLog(const std::string& message) {
    static std::mutex logMutex;
    std::lock_guard<std::mutex> lock(logMutex);
    try {
        std::ofstream logFile("C:\\AC28_AddOnProject\\archicad-addon-pyQt\\server_log.txt", std::ios_base::app);
        if (logFile.is_open()) {
            auto now = std::chrono::system_clock::now();
            auto in_time_t = std::chrono::system_clock::to_time_t(now);
            struct tm buf;
            localtime_s(&buf, &in_time_t);
            logFile << "[" << std::put_time(&buf, "%Y-%m-%d %H:%M:%S") << "] " << message << std::endl;
            logFile.close();
        }
    } catch (...) {}
}

// --- データ構造定義 ---
struct ChangeData {
    std::string guid;
    std::string group; 
    std::string propId;
    std::string propName;
    std::string propGroup;
    std::string specialType;
    std::string value;
    UInt32 modiStamp = 0;
};

struct CommandBatch {
    std::string type;
    std::vector<ChangeData> changes;
    std::string rawJson;
};

static const GSResID AddOnInfoID = ID_ADDON_INFO;
static const short AddOnMenuID = ID_ADDON_MENU;
static HANDLE serverProcessHandle = NULL;

// --- [RE-DESIGN]: 完全アトミック制御フラグ ---
static std::mutex commandMutex;
static std::deque<CommandBatch> commandQueue;
static std::atomic<bool> isExecuting(false);
static std::atomic<bool> dispatchScheduled(false);

static std::thread listenerThread;
static SOCKET listenSocket = INVALID_SOCKET;
static std::atomic<bool> stopListener(false);

static std::mutex sendMutex;
static std::deque<std::string> sendQueue;
static std::thread senderThread;
static std::atomic<bool> stopSender(false);
static std::atomic<bool> g_selectingFromPyQt(false);

static constexpr GSType EventLoopDispatcherCmdID = 'EVLP';
static constexpr Int32 EventLoopDispatcherCmdVersion = 1;
static const API_ModulID OwnModuleID = { 11, 11 };

struct ElementTypeInfo { API_ElemTypeID id; const char* key; };
static const ElementTypeInfo SupportedTypes[] = {
    { API_WallID, "Wall" }, { API_ColumnID, "Column" }, { API_BeamID, "Beam" },
    { API_SlabID, "Slab" }, { API_WindowID, "Window" }, { API_DoorID, "Door" },
    { API_ZoneID, "Zone" }, { API_RoofID, "Roof" }, { API_ObjectID, "Object" },
    { API_MorphID, "Morph" }, { API_MeshID, "Mesh" }
};

// --- 関数前方宣言 ---
void ProcessOneCommand();
void ExecuteBatch(const CommandBatch& batch);
void DoSearch(const std::string& json);
void ExportDefs(const std::string& json);
void ExportValues(const std::string& json);
void MarkChangeFlags(const std::string& json, bool isClear);
void SetChangeStatus(const std::string& json);
void SetupBIMOverride(const std::string& json, bool silent = false);
void ToggleBIMOverride(const std::string& json, bool enable);
GSErrCode EventLoopCommandHandler(GSHandle params, GSPtr resultData, bool silentMode);
void StartPythonServer();
void EnqueueProjectConfiguration();
static void LogLoadedAddOnBinary();

static void ScheduleProcessOneCommand() {
    const GSErrCode err = ACAPI_AddOnAddOnCommunication_CallFromEventLoop(
        &OwnModuleID,
        EventLoopDispatcherCmdID,
        EventLoopDispatcherCmdVersion,
        nullptr,
        true,
        nullptr
    );
    if (err != NoError) {
        WriteExternalLog("Failed to schedule command processing: " + std::to_string(err));
        dispatchScheduled.store(false);
    }
}

static GS::UniString Escape(const GS::UniString& input) {
    GS::UniString out = input; out.ReplaceAll("\"", "\\\""); out.ReplaceAll("\n", " "); out.ReplaceAll("\r", " "); return out;
}

static bool IsValidNumber(const std::string& s) {
    if (s.empty()) return false;
    char* end = nullptr;
    std::strtod(s.c_str(), &end);
    return end != s.c_str() && *end == '\0';
}

static std::string ToLowerAscii(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [] (unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

static bool ContainsInsensitive(const std::string& text, const std::string& needle) {
    return ToLowerAscii(text).find(ToLowerAscii(needle)) != std::string::npos;
}

static bool IsElementIdDefinition(const GS::UniString& propName, const GS::UniString& groupName) {
    const std::string nameUtf8 = std::string((const char*)propName.ToCStr(CC_UTF8));
    const std::string groupUtf8 = std::string((const char*)groupName.ToCStr(CC_UTF8));

    const bool nameMatches =
        propName == "ID" ||
        propName == "Element ID" ||
        ContainsInsensitive(nameUtf8, "element id") ||
        ContainsInsensitive(nameUtf8, "id");
    const bool groupMatches =
        groupName == "ID and Categories" ||
        groupName == "IDとカテゴリ" ||
        ContainsInsensitive(groupUtf8, "categories");

    return nameMatches && groupMatches;
}

static std::string GetV(const std::string& json, const std::string& key) {
    std::string searchKey = "\"" + key + "\"";
    size_t keyPos = json.find(searchKey);
    if (keyPos == std::string::npos) return "";
    size_t colonPos = json.find(":", keyPos + searchKey.length());
    if (colonPos == std::string::npos) return "";
    size_t valStart = json.find_first_not_of(" \t\r\n", colonPos + 1);
    if (valStart == std::string::npos) return "";
    if (json[valStart] == '\"') {
        size_t startQuote = valStart + 1;
        size_t endQuote = json.find("\"", startQuote);
        if (endQuote == std::string::npos) return "";
        return json.substr(startQuote, endQuote - startQuote);
    } else {
        size_t valEnd = json.find_first_of(",{} \t\r\n", valStart);
        if (valEnd == std::string::npos) return json.substr(valStart);
        return json.substr(valStart, valEnd - valStart);
    }
}

static GS::Array<API_Guid> ExtractGuids(const std::string& json, const std::string& key) {
    GS::Array<API_Guid> guids;
    size_t pos = json.find("\"" + key + "\"");
    if (pos == std::string::npos) return guids;
    pos = json.find("[", pos); if (pos == std::string::npos) return guids;
    size_t endArr = json.find("]", pos); if (endArr == std::string::npos) return guids;
    size_t current = pos;
    while (current < endArr) {
        size_t s = json.find("\"", current); if (s == std::string::npos || s > endArr) break;
        size_t e = json.find("\"", s + 1); if (e == std::string::npos || e > endArr) break;
        std::string gStr = json.substr(s + 1, e - s - 1);
        if (gStr.length() == 36) {
            try { guids.Push(GSGuid2APIGuid(GS::Guid(gStr.c_str()))); } catch(...) {}
        }
        current = e + 1;
    }
    return guids;
}

static std::vector<std::string> ExtractStringArray(const std::string& json, const std::string& key) {
    std::vector<std::string> values;
    size_t pos = json.find("\"" + key + "\"");
    if (pos == std::string::npos) return values;
    pos = json.find("[", pos);
    if (pos == std::string::npos) return values;
    size_t endArr = json.find("]", pos);
    if (endArr == std::string::npos) return values;
    size_t current = pos;
    while (current < endArr) {
        size_t s = json.find("\"", current);
        if (s == std::string::npos || s > endArr) break;
        size_t e = json.find("\"", s + 1);
        if (e == std::string::npos || e > endArr) break;
        values.push_back(json.substr(s + 1, e - s - 1));
        current = e + 1;
    }
    return values;
}

// --- [通信レイヤー]: 非同期送信 ---
void EnqueueResult(const std::string& json) {
    std::lock_guard<std::mutex> lock(sendMutex);
    std::string msg = json;
    if (msg.empty()) return;
    if (msg.back() != '\n') msg += "\n";
    sendQueue.push_back(msg);
}

void SenderLoop() {
    while (!stopSender) {
        std::string msg;
        {
            std::lock_guard<std::mutex> lock(sendMutex);
            if (!sendQueue.empty()) { msg = sendQueue.front(); sendQueue.pop_front(); }
        }
        if (!msg.empty()) {
            WriteExternalLog("Attempting to send message to Python (" + std::to_string(msg.length()) + " bytes)");
            bool sentOk = false;
            int retry = 0;
            const int MAX_RETRY = 3;
            while (retry < MAX_RETRY && !stopSender) {
                SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
                if (s != INVALID_SOCKET) {
                    sockaddr_in addr{}; addr.sin_family = AF_INET; addr.sin_port = htons(5000); addr.sin_addr.s_addr = inet_addr("127.0.0.1");
                    if (connect(s, (sockaddr*)&addr, sizeof(addr)) == 0) {
                        int sent = send(s, msg.c_str(), (int)msg.length(), 0);
                        if (sent != SOCKET_ERROR) {
                            WriteExternalLog("Successfully sent to Python (" + std::to_string(sent) + " bytes)");
                            shutdown(s, SD_SEND);
                            // Python の ACK を読み切ってから正常クローズ（RST 防止）
                            char drain[256];
                            while (recv(s, drain, sizeof(drain), 0) > 0) {}
                            sentOk = true;
                        }
                    }
                    closesocket(s);
                }
                if (sentOk) break;
                retry++;
                if (retry < MAX_RETRY) {
                    WriteExternalLog("Send retry " + std::to_string(retry) + " for " + std::to_string(msg.length()) + " bytes");
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                }
            }
            if (!sentOk) WriteExternalLog("FAILED to send message after retries. Dropping message.");
            continue; 
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

void EnqueueProjectConfiguration() {
    GS::UniString out = "{\"type\":\"project_config\",\"stories\":[";
    GS::Array<StoryData> stories = StoryService::GetAllStories();
    for (UInt32 i = 0; i < stories.GetSize(); i++) {
        if (i > 0) out.Append(",");
        out.Append("{\"index\":"); out.Append(GS::UniString::Printf("%d", stories[i].index));
        out.Append(",\"name\":\""); out.Append(Escape(stories[i].name)); out.Append("\"}");
    }
    out.Append("],\"elementTypes\":{");
    bool first = true;
    for (const auto& t : SupportedTypes) {
        GS::Array<ElementInfo> e = ElementService::GetElementsByTypeAndStories(t.id, {});
        if (e.IsEmpty()) continue;
        if (!first) out.Append(",");
        out.Append("\""); out.Append(t.key); out.Append("\":"); out.Append(GS::UniString::Printf("%u", (UInt32)e.GetSize()));
        first = false;
    }
    out.Append("}}");
    EnqueueResult(std::string((const char*)out.ToCStr(CC_UTF8)));
}

void EnqueueCommand(const CommandBatch& cmd) {
    if (cmd.type == "apply_changes" && cmd.changes.size() > 1000) {
        for (size_t i = 0; i < cmd.changes.size(); i += 1000) {
            CommandBatch subBatch; subBatch.type = "apply_changes";
            size_t end = (std::min)(i + 1000, cmd.changes.size());
            subBatch.changes.assign(cmd.changes.begin() + i, cmd.changes.begin() + end);
            { std::lock_guard<std::mutex> lock(commandMutex); commandQueue.push_back(subBatch); }
        }
    } else {
        std::lock_guard<std::mutex> lock(commandMutex); commandQueue.push_back(cmd);
    }
    bool expected = false;
    if (dispatchScheduled.compare_exchange_strong(expected, true)) {
        ScheduleProcessOneCommand();
    }
}

void ProcessOneCommand() {
    dispatchScheduled.store(false);
    bool expected = false;
    if (!isExecuting.compare_exchange_strong(expected, true)) return;
    CommandBatch batch;
    {
        std::lock_guard<std::mutex> lock(commandMutex);
        if (commandQueue.empty()) { isExecuting.store(false); return; }
        batch = std::move(commandQueue.front()); commandQueue.pop_front();
    }
    WriteExternalLog("EXEC START: " + batch.type);
    try { ExecuteBatch(batch); } catch (...) { WriteExternalLog("CRITICAL: Exception in ExecuteBatch"); }
    WriteExternalLog("EXEC END: " + batch.type);
    isExecuting.store(false);
    if (!commandQueue.empty()) {
        bool exp = false; if (dispatchScheduled.compare_exchange_strong(exp, true)) ScheduleProcessOneCommand();
    }
}

enum class ConflictPolicy { AbortAll, SkipConflicts, ForceOverwrite };
struct ElementSyncResult { std::string guid; std::string status; std::string reason; UInt32 currentStamp = 0; };

static ElementSyncResult ApplyElementChanges_Internal(const std::string& guidStr, const std::vector<ChangeData>& changes) {
    ElementSyncResult res; res.guid = guidStr; res.status = "success";
    API_Guid eg; try { eg = GSGuid2APIGuid(GS::Guid(guidStr.c_str())); } catch(...) { res.status = "error"; res.reason = "Invalid GUID"; return res; }
    API_Element element = {}; BNZeroMemory(&element, sizeof(API_Element)); element.header.guid = eg;
    if (ACAPI_Element_Get(&element) != NoError) { res.status = "error"; res.reason = "Element not found"; return res; }
    res.currentStamp = (UInt32)element.header.modiStamp;
    ConflictPolicy policy = ConflictPolicy::SkipConflicts; 
    bool hasCriticalChange = false, hasIdChange = false;
    for (const auto& c : changes) {
        if (c.group == "element" || c.group == "parameter") hasCriticalChange = true;
        if (c.group == "element_id" || c.specialType == "element_id" || c.propId == "builtin:element_id") hasIdChange = true;
    }
    if (hasIdChange) policy = ConflictPolicy::ForceOverwrite;
    else if (hasCriticalChange) policy = ConflictPolicy::AbortAll;
    if (!changes.empty() && changes[0].modiStamp != 0) {
        if ((UInt32)element.header.modiStamp != changes[0].modiStamp) {
            if (policy == ConflictPolicy::AbortAll) { res.status = "conflict"; res.reason = "modiStamp mismatch (Critical)"; return res; }
            else if (policy == ConflictPolicy::SkipConflicts) { res.status = "skipped"; res.reason = "modiStamp mismatch (Property)"; return res; }
        }
    }
    API_Element updateElem = element; API_Element mask = {}; ACAPI_ELEMENT_MASK_CLEAR(mask); bool elementChanged = false;
    GS::Array<API_PropertyDefinition> propDefsToUpdate; GS::HashTable<API_Guid, ChangeData> propValueMap;
    GS::Array<API_Guid> classItemsToAdd; bool hasElementIdChange = false; GS::UniString newElementId;
    API_RenovationStatusType newRenovationStatus = API_DefaultStatus;
    struct CategoryChange { API_ElemCategoryID id; GS::UniString valueName; }; GS::Array<CategoryChange> categoryChanges;
    for (const auto& data : changes) {
        if (data.specialType == "element_id" || data.propId == "builtin:element_id") { hasElementIdChange = true; newElementId = GS::UniString(data.value.c_str(), CC_UTF8); continue; }
        if (data.propId == "builtin:RenovationStatus") {
            if (data.value == "既存") newRenovationStatus = API_ExistingStatus; else if (data.value == "新設") newRenovationStatus = API_NewStatus; else if (data.value == "解体") newRenovationStatus = API_DemolishedStatus;
            if (newRenovationStatus != API_DefaultStatus) { updateElem.header.renovationStatus = newRenovationStatus; ACAPI_ELEMENT_MASK_SET(mask, API_Elem_Head, renovationStatus); elementChanged = true; }
            continue;
        }
        if (data.propId == "builtin:StructuralFunction") { categoryChanges.Push({ API_ElemCategory_StructuralFunction, GS::UniString(data.value.c_str(), CC_UTF8) }); continue; }
        if (data.propId == "builtin:Position") { categoryChanges.Push({ API_ElemCategory_Position, GS::UniString(data.value.c_str(), CC_UTF8) }); continue; }
        if (data.group == "element" || data.group == "parameter") {
            if (data.propId == "builtin:Layer") { updateElem.header.layer = ACAPI_CreateAttributeIndex((Int32)std::stoi(data.value)); ACAPI_ELEMENT_MASK_SET(mask, API_Elem_Head, layer); elementChanged = true; }
            else {
                switch (updateElem.header.type.typeID) {
                    case API_ColumnID: if (data.propId == "builtin:Height" && IsValidNumber(data.value)) { updateElem.column.height = std::stod(data.value); ACAPI_ELEMENT_MASK_SET(mask, API_ColumnType, height); elementChanged = true; } break;
                    case API_WallID: if (data.propId == "builtin:Thickness" && IsValidNumber(data.value)) { updateElem.wall.thickness = std::stod(data.value); ACAPI_ELEMENT_MASK_SET(mask, API_WallType, thickness); elementChanged = true; } break;
                    default: break;
                }
            }
            continue;
        }
        if (data.group == "classification") { try { API_Guid classGuid = GSGuid2APIGuid(GS::Guid(data.propId.c_str())); if (classGuid != APINULLGuid) classItemsToAdd.Push(classGuid); } catch(...) {} continue; }
        try {
            GS::Guid guidObj = GS::Guid(data.propId.c_str()); if (guidObj.IsNull()) continue;
            API_Guid apiGuid = GSGuid2APIGuid(guidObj); API_PropertyDefinition pDef = {}; pDef.guid = apiGuid;
            if (ACAPI_Property_GetPropertyDefinition(pDef) == NoError) { propDefsToUpdate.Push(pDef); propValueMap.Add(apiGuid, data); }
        } catch(...) {}
    }
    bool anyApplied = false, opError = false;
    if (elementChanged && ACAPI_Element_Change(&updateElem, &mask, nullptr, 0, true) != NoError) opError = true; else if (elementChanged) anyApplied = true;
    if (hasElementIdChange && ACAPI_Element_ChangeElementInfoString(&eg, &newElementId) != NoError) opError = true; else if (hasElementIdChange) anyApplied = true;
    if (!classItemsToAdd.IsEmpty()) {
        GS::Array<GS::Pair<API_Guid, API_Guid>> existing; ACAPI_Element_GetClassificationItems(eg, existing);
        for (const auto& newItemGuid : classItemsToAdd) {
            API_ClassificationSystem system = {};
            if (ACAPI_Classification_GetClassificationItemSystem(newItemGuid, system) == NoError) {
                for (const auto& pair : existing) if (pair.first == system.guid) ACAPI_Element_RemoveClassificationItem(eg, pair.second);
                if (ACAPI_Element_AddClassificationItem(eg, newItemGuid) != NoError) opError = true; else anyApplied = true;
            }
        }
    }
    for (const auto& catChange : categoryChanges) {
        API_ElemCategory cat = {}; cat.categoryID = catChange.id; GS::Array<API_ElemCategoryValue> allValues;
        if (ACAPI_Category_GetElementCategoryValues(&cat, &allValues) == NoError) {
            for (const auto& v : allValues) if (v.name == catChange.valueName) { if (ACAPI_Category_SetCategoryValue(eg, cat, v) == NoError) anyApplied = true; else opError = true; break; }
        }
    }
    if (!propDefsToUpdate.IsEmpty()) {
        GS::Array<API_Property> properties;
        if (ACAPI_Element_GetPropertyValues(eg, propDefsToUpdate, properties) == NoError) {
            GS::Array<API_Property> propsToSet;
            for (auto& prop : properties) {
                ChangeData* dPtr = propValueMap.GetPtr(prop.definition.guid); if (dPtr == nullptr) continue;
                API_PropertyDefinition def = {}; def.guid = prop.definition.guid; ACAPI_Property_GetPropertyDefinition(def);
                if (prop.status == API_Property_NotAvailable || def.defaultValue.hasExpression || !def.canValueBeEditable) continue;
                prop.isDefault = false; prop.status = API_Property_HasValue; bool valueSet = false;
                if (def.valueType == API_PropertyStringValueType) { prop.value.singleVariant.variant.type = API_PropertyStringValueType; prop.value.singleVariant.variant.uniStringValue = GS::UniString(dPtr->value.c_str(), CC_UTF8); valueSet = true; }
                else if (def.valueType == API_PropertyIntegerValueType) {
                    if (IsValidNumber(dPtr->value)) { prop.value.singleVariant.variant.type = API_PropertyIntegerValueType; prop.value.singleVariant.variant.intValue = (Int32)std::stol(dPtr->value); valueSet = true; }
                    else if (def.collectionType == API_PropertySingleChoiceEnumerationCollectionType) {
                        GS::UniString target(dPtr->value.c_str(), CC_UTF8);
                        for (const auto& ev : def.possibleEnumValues) if (ev.displayVariant.uniStringValue == target) { prop.value.singleVariant.variant = ev.keyVariant; valueSet = true; break; }
                    }
                } else if (def.valueType == API_PropertyRealValueType && IsValidNumber(dPtr->value)) { prop.value.singleVariant.variant.type = API_PropertyRealValueType; prop.value.singleVariant.variant.doubleValue = std::stod(dPtr->value); valueSet = true; }
                else if (def.valueType == API_PropertyBooleanValueType) { prop.value.singleVariant.variant.type = API_PropertyBooleanValueType; prop.value.singleVariant.variant.boolValue = (dPtr->value == "True" || dPtr->value == "true" || dPtr->value == "1"); valueSet = true; }
                if (valueSet) propsToSet.Push(prop);
            }
            if (!propsToSet.IsEmpty() && ACAPI_Element_SetProperties(eg, propsToSet) != NoError) opError = true; else if (!propsToSet.IsEmpty()) anyApplied = true;
        }
    }
    if (opError) { res.status = "error"; res.reason = "API error"; }
    else if (!anyApplied) { res.status = "skipped"; res.reason = "No changes"; }
    ACAPI_Element_GetHeader(&element.header); res.currentStamp = (UInt32)element.header.modiStamp; return res;
}

// ArchiCAD のメインウィンドウをフォアグラウンドに持ってくる
struct AcWndData { DWORD pid; HWND hwnd; };
static BOOL CALLBACK FindAcMainWnd(HWND hwnd, LPARAM lp) {
    auto* d = reinterpret_cast<AcWndData*>(lp);
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (pid == d->pid && IsWindowVisible(hwnd) && GetWindow(hwnd, GW_OWNER) == nullptr) {
        d->hwnd = hwnd;
        return FALSE;
    }
    return TRUE;
}
static void BringAcToFront() {
    AcWndData wd = { GetCurrentProcessId(), nullptr };
    EnumWindows(FindAcMainWnd, reinterpret_cast<LPARAM>(&wd));
    if (wd.hwnd) SetForegroundWindow(wd.hwnd);
}

static GSErrCode OnSelectionChanged(const API_Neig* selElemNeig) {
    if (g_selectingFromPyQt) return NoError;
    if (selElemNeig == nullptr) return NoError;
    if (selElemNeig->guid == APINULLGuid) return NoError;
    GS::UniString guidUni = APIGuid2GSGuid(selElemNeig->guid).ToUniString();
    std::string guidStr((const char*)guidUni.ToCStr(CC_UTF8));
    if (guidStr.empty()) return NoError;
    WriteExternalLog("OnSelectionChanged: guid=" + guidStr);
    EnqueueResult("{\"type\":\"selection_changed\",\"guid\":\"" + guidStr + "\"}");
    return NoError;
}

static void SetChangeStatusForElement(const API_Guid& eg, int csVal) {
    const GS::UniString kCSName("ChangeStatus", CC_UTF8);
    GS::Array<API_PropertyDefinition> allDefs;
    if (ACAPI_Element_GetPropertyDefinitions(eg, API_PropertyDefinitionFilter_All, allDefs) != NoError) return;
    API_Guid csGuid = APINULLGuid;
    API_PropertyDefinition csDef = {};
    for (UInt32 i = 0; i < allDefs.GetSize(); i++) {
        if (allDefs[i].name == kCSName) { csDef = allDefs[i]; csGuid = allDefs[i].guid; break; }
    }
    if (csGuid == APINULLGuid || !csDef.canValueBeEditable || csDef.defaultValue.hasExpression) return;
    GS::Array<API_PropertyDefinition> single; single.Push(csDef);
    GS::Array<API_Property> ps;
    if (ACAPI_Element_GetPropertyValues(eg, single, ps) != NoError || ps.IsEmpty()) return;
    API_Property p = ps[0];
    if (p.status == API_Property_NotAvailable) return;
    p.value.singleVariant.variant.type = API_PropertyIntegerValueType;
    p.value.singleVariant.variant.intValue = csVal;
    p.isDefault = false; p.status = API_Property_HasValue;
    GS::Array<API_Property> toSet; toSet.Push(p);
    ACAPI_Element_SetProperties(eg, toSet);
}

void ExecuteBatch(const CommandBatch& batch) {
    if (batch.type == "apply_changes") {
        if (batch.changes.empty()) return;
        std::map<std::string, std::vector<ChangeData>> elementMap;
        for (const auto& c : batch.changes) elementMap[c.guid].push_back(c);
        struct Context { std::map<std::string, std::vector<ChangeData>> elementMap; GS::Array<ElementSyncResult> res; std::string status; } ctx;
        ctx.elementMap = elementMap; ctx.status = "success";

        // ChangeStatus 定義をバッチ前に1回だけ取得（25回→1回に削減）
        API_Guid csDefGuid = APINULLGuid;
        API_PropertyDefinition cachedCsDef = {};
        if (!ctx.elementMap.empty()) {
            try {
                API_Guid firstEg = GSGuid2APIGuid(GS::Guid(ctx.elementMap.begin()->first.c_str()));
                GS::Array<API_PropertyDefinition> allDefs;
                const GS::UniString kCSName("ChangeStatus", CC_UTF8);
                if (ACAPI_Element_GetPropertyDefinitions(firstEg, API_PropertyDefinitionFilter_All, allDefs) == NoError) {
                    for (UInt32 i = 0; i < allDefs.GetSize(); i++) {
                        if (allDefs[i].name == kCSName && allDefs[i].canValueBeEditable && !allDefs[i].defaultValue.hasExpression) {
                            csDefGuid = allDefs[i].guid; cachedCsDef = allDefs[i]; break;
                        }
                    }
                }
            } catch(...) {}
        }

        ACAPI_CallUndoableCommand("Sync BIM Properties", [&]() -> GSErrCode {
            int sc = 0, cc = 0, ec = 0;
            GS::Array<API_PropertyDefinition> csSingle;
            if (csDefGuid != APINULLGuid) csSingle.Push(cachedCsDef);
            for (auto const& [guid, changes] : ctx.elementMap) {
                ElementSyncResult r = ApplyElementChanges_Internal(guid, changes); ctx.res.Push(r);
                if (r.status == "success") {
                    sc++;
                    if (csDefGuid != APINULLGuid) {
                        try {
                            API_Guid eg = GSGuid2APIGuid(GS::Guid(guid.c_str()));
                            GS::Array<API_Property> ps;
                            if (ACAPI_Element_GetPropertyValues(eg, csSingle, ps) == NoError && !ps.IsEmpty()) {
                                API_Property p = ps[0];
                                if (p.status != API_Property_NotAvailable) {
                                    p.value.singleVariant.variant.type = API_PropertyIntegerValueType;
                                    p.value.singleVariant.variant.intValue = 1;
                                    p.isDefault = false; p.status = API_Property_HasValue;
                                    GS::Array<API_Property> toSet; toSet.Push(p);
                                    ACAPI_Element_SetProperties(eg, toSet);
                                }
                            }
                        } catch(...) {}
                    }
                } else if (r.status == "conflict" || r.status == "skipped") cc++; else ec++;
            }
            if (ec > 0) ctx.status = "failed"; else if (cc > 0) ctx.status = "partial"; return NoError;
        });
        GS::UniString out = "{\"type\":\"sync_result\",\"results\":[";
        for (UInt32 i = 0; i < ctx.res.GetSize(); i++) {
            if (i > 0) out.Append(","); const auto& r = ctx.res[i];
            out.Append("{\"guid\":\""); out.Append(GS::UniString(r.guid.c_str())); out.Append("\",");
            out.Append("\"status\":\""); out.Append(GS::UniString(r.status.c_str())); out.Append("\",");
            out.Append("\"reason\":\""); out.Append(Escape(GS::UniString(r.reason.c_str()))); out.Append("\",");
            out.Append("\"currentStamp\":"); out.Append(GS::UniString::Printf("%u", r.currentStamp)); out.Append("}");
        }
        out.Append("],\"status\":\""); out.Append(GS::UniString(ctx.status.c_str())); out.Append("\"}");
        std::string finalJson = std::string((const char*)out.ToCStr(CC_UTF8));
        WriteExternalLog("Enqueueing sync_complete: " + std::to_string(finalJson.length()) + " bytes. Status=" + ctx.status);
        EnqueueResult(finalJson);
        } 
 else if (batch.type == "other") {
        const std::string& cmd = batch.rawJson;
        if (cmd.find("\"ready\"") != std::string::npos) EnqueueProjectConfiguration();
        else if (cmd.find("\"get_elements\"") != std::string::npos) DoSearch(cmd);
        else if (cmd.find("\"get_definitions\"") != std::string::npos) ExportDefs(cmd);
        else if (cmd.find("\"get_values\"") != std::string::npos) ExportValues(cmd);
        else if (cmd.find("\"select_elements\"") != std::string::npos) {
            GS::Array<API_Guid> guids = ExtractGuids(cmd, "guids");
            WriteExternalLog("select_elements: " + std::to_string(guids.GetSize()) + " guid(s)");
            if (!guids.IsEmpty()) {
                GS::Array<API_Neig> sel;
                for (const auto& g : guids) {
                    API_Neig n;
                    BNZeroMemory(&n, sizeof(n));
                    GSErrCode e = ACAPI_Selection_SetSelectedElementNeig(&g, &n);
                    if (e == NoError) {
                        sel.Push(n);
                    } else {
                        WriteExternalLog("select_elements: SetSelectedElementNeig failed err=" + std::to_string(e));
                    }
                }
                if (!sel.IsEmpty()) {
                    g_selectingFromPyQt = true;
                    ACAPI_Selection_DeselectAll();                    // 既存の選択を全解除
                    GSErrCode e = ACAPI_Selection_Select(sel, true);  // 新しい要素を選択
                    g_selectingFromPyQt = false;
                    WriteExternalLog("select_elements: Select returned " + std::to_string(e));
                    BringAcToFront();
                }
            }
        }
        else if (cmd.find("\"mark_change_flags\"") != std::string::npos) MarkChangeFlags(cmd, false);
        else if (cmd.find("\"clear_change_flags\"") != std::string::npos) MarkChangeFlags(cmd, true);
        else if (cmd.find("\"set_change_status\"") != std::string::npos) SetChangeStatus(cmd);
        else if (cmd.find("\"setup_bim_override\"") != std::string::npos) SetupBIMOverride(cmd);
        else if (cmd.find("\"apply_bim_override\"") != std::string::npos) ToggleBIMOverride(cmd, true);
        else if (cmd.find("\"remove_bim_override\"") != std::string::npos) ToggleBIMOverride(cmd, false);
    }
}

void DoSearch(const std::string& json) {
    GS::Array<short> stories; size_t sPos = json.find("\"stories\":");
    if (sPos != std::string::npos) {
        size_t s = json.find("[", sPos), e = json.find("]", (s != std::string::npos) ? s : 0);
        if (s != std::string::npos && e != std::string::npos) {
            std::string sl = json.substr(s + 1, e - s - 1); size_t p = 0, n;
            while ((n = sl.find(",", p)) != std::string::npos) { try { stories.Push((short)std::stoi(sl.substr(p, n - p))); } catch(...) {} p = n + 1; }
            if (p < sl.length()) try { stories.Push((short)std::stoi(sl.substr(p))); } catch(...) {}
        }
    }
    GS::Array<API_Guid> results;
    for (const auto& t : SupportedTypes) {
        if (json.find("\"" + std::string(t.key) + "\":true") != std::string::npos) {
            GS::Array<API_Guid> all; if (ACAPI_Element_GetElemList(API_ElemType(t.id), &all) != NoError) continue;
            for (const auto& g : all) {
                API_Elem_Head h = {}; h.guid = g; if (ACAPI_Element_GetHeader(&h) != NoError) continue;
                bool m = stories.IsEmpty(); for (short s : stories) if (h.floorInd == s) { m = true; break; }
                if (m) { results.Push(g); if (results.GetSize() >= 1000) break; }
            }
        }
        if (results.GetSize() >= 1000) break;
    }
    GS::UniString out = "{\"type\":\"elements\",\"elements\":[";
    for (UInt32 i = 0; i < results.GetSize(); i++) {
        if (i > 0) out.Append(","); out.Append("{\"guid\":\""); out.Append(APIGuid2GSGuid(results[i]).ToUniString()); out.Append("\"}");
    }
    out.Append("]}"); EnqueueResult(std::string((const char*)out.ToCStr(CC_UTF8)));
}

void ExportDefs(const std::string& json) {
    GS::Array<API_Guid> guids = ExtractGuids(json, "guids"); if (guids.IsEmpty()) return;
    GS::Array<API_PropertyDefinition> defs; ACAPI_Element_GetPropertyDefinitions(guids[0], API_PropertyDefinitionFilter_All, defs);
    GS::UniString out = "{\"type\":\"property_definitions\",\"definitions\":[";
    out.Append("{\"guid\":\"builtin:element_id\",\"name\":\"ID\",\"group\":\"IDとカテゴリ\",\"editable\":true,\"valueType\":1,\"collectionType\":0}");
    out.Append(",{\"guid\":\"builtin:RenovationStatus\",\"name\":\"リノベーションステータス\",\"group\":\"IDとカテゴリ\",\"editable\":true,\"valueType\":2,\"collectionType\":1,\"enums\":[\"既存\",\"新設\",\"解体\"]}");
    out.Append(",{\"guid\":\"builtin:StructuralFunction\",\"name\":\"構造機能\",\"group\":\"IDとカテゴリ\",\"editable\":true,\"valueType\":2,\"collectionType\":1,\"enums\":[\"耐力要素\",\"非耐力要素\",\"未定義\"]}");
    out.Append(",{\"guid\":\"builtin:Position\",\"name\":\"位置\",\"group\":\"IDとカテゴリ\",\"editable\":true,\"valueType\":2,\"collectionType\":1,\"enums\":[\"外部\",\"内部\",\"未定義\"]}");
    for (UInt32 i = 0; i < defs.GetSize(); i++) {
        API_PropertyGroup grp = {}; grp.guid = defs[i].groupGuid; ACAPI_Property_GetPropertyGroup(grp);
        if (IsElementIdDefinition(defs[i].name, grp.name)) continue;
        out.Append(","); out.Append("{\"guid\":\""); out.Append(APIGuid2GSGuid(defs[i].guid).ToUniString()); out.Append("\",");
        out.Append("\"name\":\""); out.Append(Escape(defs[i].name)); out.Append("\",");
        out.Append("\"group\":\""); out.Append(Escape(grp.name)); out.Append("\",");
        out.Append("\"editable\":"); out.Append(defs[i].canValueBeEditable ? "true" : "false"); out.Append(",");
        out.Append("\"valueType\":"); out.Append(GS::UniString::Printf("%d", (int)defs[i].valueType)); out.Append(",");
        out.Append("\"collectionType\":"); out.Append(GS::UniString::Printf("%d", (int)defs[i].collectionType));
        if (defs[i].collectionType == API_PropertySingleChoiceEnumerationCollectionType || defs[i].collectionType == API_PropertyMultipleChoiceEnumerationCollectionType) {
            out.Append(",\"enums\":[");
            for (UInt32 j = 0; j < defs[i].possibleEnumValues.GetSize(); j++) {
                if (j > 0) out.Append(","); out.Append("\""); out.Append(Escape(defs[i].possibleEnumValues[j].displayVariant.uniStringValue)); out.Append("\"");
            }
            out.Append("]");
        }
        out.Append("}");
    }
    out.Append("]}"); EnqueueResult(std::string((const char*)out.ToCStr(CC_UTF8)));
}

void ExportValues(const std::string& json) {
    GS::Array<API_Guid> guids = ExtractGuids(json, "guids"); std::vector<std::string> pgs = ExtractStringArray(json, "propGuids");
    if (guids.IsEmpty() || pgs.empty()) return;
    GS::Array<API_PropertyDefinition> pDefs; bool incID = false, incRen = false, incSF = false, incPos = false;
    for (const auto& s : pgs) {
        if (s == "builtin:element_id") incID = true; else if (s == "builtin:RenovationStatus") incRen = true; else if (s == "builtin:StructuralFunction") incSF = true; else if (s == "builtin:Position") incPos = true;
        else { try { API_PropertyDefinition d = {}; d.guid = GSGuid2APIGuid(GS::Guid(s.c_str())); if (ACAPI_Property_GetPropertyDefinition(d) == NoError) pDefs.Push(d); } catch (...) {} }
    }
    GS::UniString out = "{\"type\":\"property_values\",\"values\":[";
    for (UInt32 i = 0; i < guids.GetSize(); i++) {
        if (i > 0) out.Append(","); API_Elem_Head h = {}; h.guid = guids[i]; ACAPI_Element_GetHeader(&h);
        out.Append("{\"guid\":\""); out.Append(APIGuid2GSGuid(guids[i]).ToUniString()); out.Append("\",");
        out.Append("\"modiStamp\":"); out.Append(GS::UniString::Printf("%u", h.modiStamp)); out.Append(",");
        out.Append("\"props\":{"); bool first = true;
        auto add = [&](const std::string& id, const GS::UniString& val) { if (!first) out.Append(","); out.Append("\"" + GS::UniString(id.c_str()) + "\":\""); out.Append(Escape(val)); out.Append("\""); first = false; };
        if (incID) { GS::UniString v; if (ACAPI_Element_GetElementInfoString(&guids[i], &v) == NoError) add("builtin:element_id", v); }
        if (incRen) { GS::UniString v = (h.renovationStatus == API_ExistingStatus ? "既存" : (h.renovationStatus == API_NewStatus ? "新設" : "解体")); add("builtin:RenovationStatus", v); }
        if (incSF) { API_ElemCategory c = {API_ElemCategory_StructuralFunction}; API_ElemCategoryValue v = {}; if (ACAPI_Category_GetCategoryValue(guids[i], c, &v) == NoError) add("builtin:StructuralFunction", v.name); }
        if (incPos) { API_ElemCategory c = {API_ElemCategory_Position}; API_ElemCategoryValue v = {}; if (ACAPI_Category_GetCategoryValue(guids[i], c, &v) == NoError) add("builtin:Position", v.name); }
        GS::Array<API_Property> ps; if (!pDefs.IsEmpty() && ACAPI_Element_GetPropertyValues(guids[i], pDefs, ps) == NoError) {
            for (UInt32 j = 0; j < ps.GetSize(); j++) {
                GS::UniString v;
                if (ACAPI_Property_GetPropertyValueString(ps[j], &v) != NoError) v = "---";
                add(std::string((const char*)APIGuid2GSGuid(ps[j].definition.guid).ToUniString().ToCStr(CC_UTF8)), v);
            }
        }
        out.Append("}}");
    }
    out.Append("]}"); EnqueueResult(std::string((const char*)out.ToCStr(CC_UTF8)));
}

void MarkChangeFlags(const std::string& json, bool isClear) {
    GS::Array<API_Guid> guids = ExtractGuids(json, "guids");
    std::string propNameStr = GetV(json, "propName");
    std::string flagValueStr = isClear ? "" : GetV(json, "flag");

    if (guids.IsEmpty() || propNameStr.empty()) {
        EnqueueResult("{\"type\":\"flag_result\",\"status\":\"error\",\"reason\":\"missing params\",\"propCount\":0,\"elemCount\":0}");
        return;
    }

    GS::UniString propName(propNameStr.c_str(), CC_UTF8);
    GS::UniString flagValue(flagValueStr.c_str(), CC_UTF8);
    const GS::UniString kGroupName("変更管理", CC_UTF8);

    int propCount = 0, elemCount = 0;
    bool created = false;
    GS::Array<API_Neig> selNeigs;

    // ─── Step 1: プロパティ定義を検索、なければ自動作成 ───
    API_Guid defGuid = APINULLGuid;
    {
        GS::Array<API_PropertyDefinition> defs;
        ACAPI_Element_GetPropertyDefinitions(guids[0], API_PropertyDefinitionFilter_All, defs);

        // 名前で検索
        for (UInt32 i = 0; i < defs.GetSize(); i++) {
            if (defs[i].name == propName) { defGuid = defs[i].guid; break; }
        }

        if (defGuid == APINULLGuid) {
            if (isClear) {
                // 解除対象プロパティが存在しない → 何もせず正常終了
                EnqueueResult("{\"type\":\"flag_result\",\"status\":\"ok\",\"created\":false,\"propCount\":0,\"elemCount\":0}");
                return;
            }

            // グループを名前で検索（既存定義の groupGuid から逆引き）
            API_Guid groupGuid = APINULLGuid;
            for (UInt32 i = 0; i < defs.GetSize() && groupGuid == APINULLGuid; i++) {
                API_PropertyGroup grp = {}; grp.guid = defs[i].groupGuid;
                if (ACAPI_Property_GetPropertyGroup(grp) == NoError && grp.name == kGroupName)
                    groupGuid = grp.guid;
            }

            // グループが見つからなければ作成
            ACAPI_CallUndoableCommand("BIM 変更管理グループ/プロパティ作成", [&]() -> GSErrCode {
                if (groupGuid == APINULLGuid) {
                    API_PropertyGroup newGroup = {};
                    newGroup.name = kGroupName;
                    GSErrCode err = ACAPI_Property_CreatePropertyGroup(newGroup);
                    if (err != NoError) return err;
                    groupGuid = newGroup.guid;
                }

                // プロパティ定義を作成（文字列・単一値・全要素タイプ対象）
                API_PropertyDefinition newDef = {};
                newDef.groupGuid = groupGuid;
                newDef.name = propName;
                newDef.valueType = API_PropertyStringValueType;
                newDef.collectionType = API_PropertySingleCollectionType;
                newDef.defaultValue.hasExpression = false;
                newDef.defaultValue.basicValue.singleVariant.variant.type = API_PropertyStringValueType;

                GSErrCode err2 = ACAPI_Property_CreatePropertyDefinition(newDef);
                if (err2 != NoError) return err2;
                defGuid = newDef.guid;
                created = true;
                return NoError;
            });

            if (defGuid == APINULLGuid) {
                EnqueueResult("{\"type\":\"flag_result\",\"status\":\"error\",\"reason\":\"property create failed\",\"propCount\":0,\"elemCount\":0}");
                return;
            }
        }
    }

    // ─── Step 2: 各要素にフラグ値を書き込む ───
    API_PropertyDefinition def = {}; def.guid = defGuid;
    if (ACAPI_Property_GetPropertyDefinition(def) != NoError || !def.canValueBeEditable || def.defaultValue.hasExpression) {
        EnqueueResult("{\"type\":\"flag_result\",\"status\":\"error\",\"reason\":\"def not usable\",\"propCount\":0,\"elemCount\":0}");
        return;
    }
    GS::Array<API_PropertyDefinition> singleDef; singleDef.Push(def);

    ACAPI_CallUndoableCommand("BIM 変更フラグ 設定", [&]() -> GSErrCode {
        for (UInt32 i = 0; i < guids.GetSize(); i++) {
            const API_Guid& eg = guids[i];
            GS::Array<API_Property> props;
            if (ACAPI_Element_GetPropertyValues(eg, singleDef, props) != NoError || props.IsEmpty()) continue;
            API_Property prop = props[0];
            if (prop.status == API_Property_NotAvailable) continue;

            if (isClear) {
                prop.isDefault = true;
                prop.status = API_Property_NotEvaluated;
            } else {
                if (def.valueType != API_PropertyStringValueType) continue;
                prop.value.singleVariant.variant.type = API_PropertyStringValueType;
                prop.value.singleVariant.variant.uniStringValue = flagValue;
                prop.isDefault = false;
                prop.status = API_Property_HasValue;
            }

            GS::Array<API_Property> propsToSet; propsToSet.Push(prop);
            if (ACAPI_Element_SetProperties(eg, propsToSet) == NoError) propCount++;

            API_Neig neig;
            if (ACAPI_Selection_SetSelectedElementNeig(&eg, &neig) == NoError) selNeigs.Push(neig);
            elemCount++;
        }
        return NoError;
    });

    // フラグ設定時のみ要素を選択状態にする
    if (!isClear && !selNeigs.IsEmpty()) {
        ACAPI_Selection_Select(selNeigs, true);
    }

    // ─── ChangeStatus プロパティも更新（SetupBIMOverride 実行済みの場合） ───
    {
        const GS::UniString kCSName("ChangeStatus", CC_UTF8);
        API_Guid csGuid = APINULLGuid;
        GS::Array<API_PropertyDefinition> allDefs;
        if (ACAPI_Element_GetPropertyDefinitions(guids[0], API_PropertyDefinitionFilter_All, allDefs) == NoError) {
            for (UInt32 i = 0; i < allDefs.GetSize(); i++) {
                if (allDefs[i].name == kCSName) { csGuid = allDefs[i].guid; break; }
            }
        }
        if (csGuid != APINULLGuid) {
            API_PropertyDefinition csDef = {}; csDef.guid = csGuid;
            if (ACAPI_Property_GetPropertyDefinition(csDef) == NoError && csDef.canValueBeEditable && !csDef.defaultValue.hasExpression) {
                GS::Array<API_PropertyDefinition> csSingle; csSingle.Push(csDef);
                const int csVal = isClear ? 0 : 1;
                ACAPI_CallUndoableCommand("BIM ChangeStatus 更新", [&]() -> GSErrCode {
                    for (UInt32 i = 0; i < guids.GetSize(); i++) {
                        GS::Array<API_Property> ps;
                        if (ACAPI_Element_GetPropertyValues(guids[i], csSingle, ps) != NoError || ps.IsEmpty()) continue;
                        API_Property p = ps[0]; if (p.status == API_Property_NotAvailable) continue;
                        if (isClear) { p.isDefault = true; p.status = API_Property_NotEvaluated; }
                        else { p.value.singleVariant.variant.type = API_PropertyIntegerValueType; p.value.singleVariant.variant.intValue = csVal; p.isDefault = false; p.status = API_Property_HasValue; }
                        GS::Array<API_Property> toSet; toSet.Push(p);
                        ACAPI_Element_SetProperties(guids[i], toSet);
                    }
                    return NoError;
                });
            }
        }
    }

    GS::UniString out = GS::UniString::Printf(
        "{\"type\":\"flag_result\",\"status\":\"ok\",\"created\":%s,\"propCount\":%d,\"elemCount\":%d}",
        created ? "true" : "false", propCount, elemCount
    );
    EnqueueResult(std::string((const char*)out.ToCStr(CC_UTF8)));
}

// ─── SetChangeStatus: 指定GUIDの ChangeStatus プロパティを 0/1/2 に設定 ───
void SetChangeStatus(const std::string& json) {
    GS::Array<API_Guid> guids = ExtractGuids(json, "guids");
    std::string statusStr = GetV(json, "status");
    if (guids.IsEmpty() || statusStr.empty()) {
        EnqueueResult("{\"type\":\"change_status_result\",\"status\":\"error\",\"reason\":\"missing params\",\"set\":0,\"value\":0}");
        return;
    }
    int csVal = 0;
    try { csVal = std::stoi(statusStr); } catch(...) {}
    if (csVal < 0 || csVal > 2) csVal = 0;

    int count = 0;
    ACAPI_CallUndoableCommand("BIM ChangeStatus 変更", [&]() -> GSErrCode {
        for (UInt32 i = 0; i < guids.GetSize(); i++) {
            SetChangeStatusForElement(guids[i], csVal);
            count++;
        }
        return NoError;
    });

    GS::UniString out = GS::UniString::Printf(
        "{\"type\":\"change_status_result\",\"status\":\"ok\",\"set\":%d,\"value\":%d}",
        count, csVal
    );
    EnqueueResult(std::string((const char*)out.ToCStr(CC_UTF8)));
}

// ─── XML / File ヘルパー (SetupBIMOverride 用) ───
static std::string XmlTagContent(const std::string& xml, const std::string& tag) {
    const std::string open = "<" + tag + ">";
    const std::string openAttr = "<" + tag + " ";
    const std::string close = "</" + tag + ">";
    size_t s = xml.find(open);
    size_t startOff;
    if (s != std::string::npos) {
        startOff = s + open.length();
    } else {
        s = xml.find(openAttr);
        if (s == std::string::npos) return "";
        const size_t gt = xml.find('>', s);
        if (gt == std::string::npos) return "";
        startOff = gt + 1;
    }
    const size_t e = xml.find(close, startOff);
    if (e == std::string::npos) return "";
    return xml.substr(startOff, e - startOff);
}

static std::string XmlElementOuter(const std::string& xml, const std::string& tag, size_t from = 0) {
    const std::string open  = "<"  + tag;
    const std::string close = "</" + tag + ">";
    const size_t s = xml.find(open, from);
    if (s == std::string::npos) return "";
    const size_t e = xml.find(close, s);
    if (e == std::string::npos) return "";
    return xml.substr(s, e + close.length() - s);
}

static int XmlInt(const std::string& xml, const std::string& tag, int def = 0) {
    const std::string v = XmlTagContent(xml, tag);
    if (v.empty()) return def;
    try { return std::stoi(v); } catch(...) { return def; }
}

static std::string WstrToLogStr(const std::wstring& ws) {
    std::string s; s.reserve(ws.size());
    for (wchar_t c : ws) s += (c < 128) ? static_cast<char>(c) : '?';
    return s;
}

static std::wstring FindProjectDirW() {
    IO::Location loc;
    if (ACAPI_GetOwnLocation(&loc) != NoError) return L"";
    for (int i = 0; i < 6; i++) {
        loc.DeleteLastLocalName();
        IO::Location t = loc;
        t.AppendToLocal(IO::Name("server.py"));
        bool ex = false;
        if (IO::fileSystem.Contains(t, &ex) == NoError && ex) {
            GS::UniString p;
            loc.ToPath(&p);
            std::wstring ws((const wchar_t*)p.ToUStr().Get());
            if (!ws.empty() && ws.back() != L'\\' && ws.back() != L'/') ws += L'\\';
            return ws;
        }
    }
    return L"";
}

static bool ReadFileToUniString(const std::wstring& wpath, GS::UniString& out) {
    HANDLE h = CreateFileW(wpath.c_str(), GENERIC_READ, FILE_SHARE_READ, NULL,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return false;
    DWORD sz = GetFileSize(h, NULL);
    if (sz == 0 || sz == INVALID_FILE_SIZE) { CloseHandle(h); return false; }
    std::vector<char> buf(sz + 1, 0);
    DWORD rd = 0;
    const bool ok = ReadFile(h, buf.data(), sz, &rd, NULL) != FALSE;
    CloseHandle(h);
    if (!ok || rd == 0) return false;
    size_t start = 0;
    if (rd >= 3 && (unsigned char)buf[0]==0xEF &&
                   (unsigned char)buf[1]==0xBB &&
                   (unsigned char)buf[2]==0xBF) start = 3;
    buf[rd] = '\0';
    out = GS::UniString(buf.data() + start, CC_UTF8);
    return true;
}

// ─── SetupBIMOverride: ChangeStatus プロパティ + Graphic Override ルール/コンビネーション作成 ───
void SetupBIMOverride(const std::string& /*json*/, bool silent) {
    const GS::UniString kGroupName        ("\xe5\xa4\x89\xe6\x9b\xb4\xe7\xae\xa1\xe7\x90\x86", CC_UTF8); // 変更管理
    const GS::UniString kCSGroupName      ("\xe5\xa4\x89\xe6\x9b\xb4\xe3\x83\x95\xe3\x83\xa9\xe3\x82\xb0", CC_UTF8); // 変更フラグ（プロパティXMLと同じグループ名）
    const GS::UniString kCSName           ("ChangeStatus", CC_UTF8);
    const GS::UniString kRuleGroupName    ("BIM\xe5\xa4\x89\xe6\x9b\xb4\xe7\xae\xa1\xe7\x90\x86", CC_UTF8); // BIM変更管理
    const GS::UniString kRuleChangedName  ("BIM\xe5\xa4\x89\xe6\x9b\xb4\xe6\xb8\x88", CC_UTF8);  // BIM変更済
    const GS::UniString kRuleConfirmedName("BIM\xe7\xa2\xba\xe8\xaa\x8d\xe6\xb8\x88", CC_UTF8);  // BIM確認済
    const GS::UniString kComboName        ("BIM\xe5\xa4\x89\xe6\x9b\xb4\xe7\xae\xa1\xe7\x90\x86", CC_UTF8); // BIM変更管理

    WriteExternalLog("SetupBIMOverride: start (silent=" + std::string(silent ? "true" : "false") + ")");

    bool propCreated = false, comboCreated = false;
    API_Guid csDefGuid = APINULLGuid;

    // Step 0: XML ファイルディレクトリを取得（binary 位置から server.py のある親フォルダを検索）
    const std::wstring projDir = FindProjectDirW();
    WriteExternalLog("SetupBIMOverride: xmlDir=" + WstrToLogStr(projDir));

    // ── Phase 1: ChangeStatus プロパティを確認 → XML インポート → API フォールバック ──
    WriteExternalLog("SetupBIMOverride Phase1: ChangeStatus property check");
    GS::Array<API_Guid> anyGuids;
    for (const auto& t : SupportedTypes)
        if (ACAPI_Element_GetElemList(API_ElemType(t.id), &anyGuids) == NoError && !anyGuids.IsEmpty()) break;
    WriteExternalLog("  anyGuids count=" + std::to_string(anyGuids.GetSize()));

    auto findCSGuid = [&]() {
        csDefGuid = APINULLGuid;
        if (anyGuids.IsEmpty()) return;
        GS::Array<API_PropertyDefinition> defs;
        ACAPI_Element_GetPropertyDefinitions(anyGuids[0], API_PropertyDefinitionFilter_All, defs);
        for (UInt32 i = 0; i < defs.GetSize(); i++)
            if (defs[i].name == kCSName) { csDefGuid = defs[i].guid; break; }
    };
    findCSGuid();
    WriteExternalLog("  ChangeStatus existing=" + std::string(csDefGuid != APINULLGuid ? "YES" : "NO"));

    if (csDefGuid == APINULLGuid) {
        // 変更フラグプロパティ = 変更フラグプロパティ
        const std::wstring propXmlPath = projDir + L"変更フラグプロパティ.xml";
        GS::UniString propXml;
        WriteExternalLog("  Phase1: reading " + WstrToLogStr(propXmlPath));
        if (!projDir.empty() && ReadFileToUniString(propXmlPath, propXml)) {
            WriteExternalLog("  Phase1: XML read OK len=" + std::to_string(propXml.GetLength()));
            const GSErrCode ie = ACAPI_Property_Import(propXml, API_SkipConflictingProperties);
            WriteExternalLog("  Phase1: ACAPI_Property_Import=" + std::to_string(ie));
            if (ie == NoError) { propCreated = true; findCSGuid(); }
        } else {
            WriteExternalLog("  Phase1: XML not found, fallback to API create");
        }
    } else {
        WriteExternalLog("  Phase1: ChangeStatus already exists, skip import");
    }

    // フォールバック: API で直接 ChangeStatus プロパティを作成
    if (csDefGuid == APINULLGuid && !anyGuids.IsEmpty()) {
        API_Guid groupGuid = APINULLGuid;
        GS::Array<API_PropertyDefinition> defs;
        ACAPI_Element_GetPropertyDefinitions(anyGuids[0], API_PropertyDefinitionFilter_All, defs);
        for (UInt32 i = 0; i < defs.GetSize() && groupGuid == APINULLGuid; i++) {
            API_PropertyGroup grp = {}; grp.guid = defs[i].groupGuid;
            if (ACAPI_Property_GetPropertyGroup(grp) == NoError && grp.name == kCSGroupName)
                groupGuid = grp.guid;
        }
        ACAPI_CallUndoableCommand("BIM ChangeStatus \xe3\x83\x97\xe3\x83\xad\xe3\x83\x91\xe3\x83\x86\xe3\x82\xa3\xe4\xbd\x9c\xe6\x88\x90", [&]() -> GSErrCode {
            if (groupGuid == APINULLGuid) {
                API_PropertyGroup ng = {}; ng.name = kCSGroupName;
                GSErrCode e = ACAPI_Property_CreatePropertyGroup(ng); if (e != NoError) return e;
                groupGuid = ng.guid;
            }
            API_PropertyDefinition nd = {};
            nd.groupGuid = groupGuid; nd.name = kCSName;
            nd.valueType = API_PropertyIntegerValueType;
            nd.collectionType = API_PropertySingleChoiceEnumerationCollectionType;
            nd.defaultValue.hasExpression = false;
            nd.defaultValue.basicValue.singleVariant.variant.type = API_PropertyIntegerValueType;
            nd.defaultValue.basicValue.singleVariant.variant.intValue = 0;
            auto addEnum = [&](int key, const char* disp) {
                API_SingleEnumerationVariant ev = {};
                ev.keyVariant.type = API_PropertyIntegerValueType; ev.keyVariant.intValue = key;
                ev.displayVariant.type = API_PropertyStringValueType;
                ev.displayVariant.uniStringValue = GS::UniString(disp, CC_UTF8);
                nd.possibleEnumValues.Push(ev);
            };
            addEnum(0, "\xe6\x9c\xaa\xe5\xa4\x89\xe6\x9b\xb4"); // 未変更
            addEnum(1, "\xe5\xa4\x89\xe6\x9b\xb4\xe6\xb8\x88"); // 変更済
            addEnum(2, "\xe7\xa2\xba\xe8\xaa\x8d\xe6\xb8\x88"); // 確認済
            GSErrCode e2 = ACAPI_Property_CreatePropertyDefinition(nd); if (e2 != NoError) return e2;
            csDefGuid = nd.guid; propCreated = true; return NoError;
        });
    }

    GS::UniString csGuidStr = (csDefGuid != APINULLGuid) ?
        APIGuid2GSGuid(csDefGuid).ToUniString() : GS::UniString("N/A");

    // Step 2: ルールグループを検索または作成
    // ── Phase 2: BIM変更管理Graphic Override.xml → ルール・コンビネーション作成 ──
    WriteExternalLog("SetupBIMOverride Phase2: BIM graphic override check");
    const std::wstring goXmlPath = projDir + L"BIM変更管理Graphic Override.xml";
    WriteExternalLog("  Phase2: GO XML path=" + WstrToLogStr(goXmlPath));

    API_Guid rgGuid = APINULLGuid;
    { API_OverrideRuleGroup rg = {APINULLGuid, kRuleGroupName};
      if (ACAPI_GraphicalOverride_GetOverrideRuleGroup(rg) == NoError) { rgGuid = rg.guid; WriteExternalLog("  Phase2: rule group already exists"); }
      else { API_OverrideRuleGroup ng = {APINULLGuid, kRuleGroupName};
             if (ACAPI_GraphicalOverride_CreateOverrideRuleGroup(ng) == NoError) { rgGuid = ng.guid; WriteExternalLog("  Phase2: rule group created"); } } }
    if (rgGuid == APINULLGuid) {
        WriteExternalLog("  Phase2: ERROR rule group creation failed");
        if (!silent) EnqueueResult("{\"type\":\"bim_override_result\",\"status\":\"error\",\"action\":\"setup\","
                      "\"reason\":\"rule group failed\",\"propCreated\":false,"
                      "\"combinationCreated\":false,\"changeStatusGuid\":\"N/A\"}");
        return;
    }

    // ── Step 3: BIM変更管理Graphic Override.xml からルール仕様（スタイル＋条件）を読み込む ──
    struct RuleSpec {
        std::string nameUtf8;
        std::string criterionXml;
        short pen; short fillPen;
        double r, g, b;
    };
    std::vector<RuleSpec> specs;
    bool xmlParsed = false;
    if (!projDir.empty()) {
        GS::UniString goXmlUni;
        if (ReadFileToUniString(goXmlPath, goXmlUni)) {
            const std::string goXml = std::string((const char*)goXmlUni.ToCStr(CC_UTF8));
            size_t pos = 0;
            const std::string closeRule = "</OverrideRule>";
            while (true) {
                const size_t rs = goXml.find("<OverrideRule>", pos);
                if (rs == std::string::npos) break;
                const size_t re = goXml.find(closeRule, rs);
                if (re == std::string::npos) break;
                const std::string rx = goXml.substr(rs, re + closeRule.length() - rs);
                RuleSpec sp;
                sp.nameUtf8     = XmlTagContent(rx, "Name");
                sp.pen          = static_cast<short>(XmlInt(rx, "LineMarkerTextPen", 2));
                sp.fillPen      = static_cast<short>(XmlInt(rx, "FillFGPen", sp.pen));
                sp.r            = XmlInt(rx, "Red",   128) / 255.0;
                sp.g            = XmlInt(rx, "Green", 128) / 255.0;
                sp.b            = XmlInt(rx, "Blue",  128) / 255.0;
                sp.criterionXml = XmlElementOuter(rx, "CriterionExpression");
                if (!sp.nameUtf8.empty()) specs.push_back(sp);
                pos = re + closeRule.length();
            }
            xmlParsed = !specs.empty();
            WriteExternalLog("  Phase2: GO XML parsed " + std::to_string(specs.size()) + " rules");
        } else {
            WriteExternalLog("  Phase2: Cannot read GO XML, using hardcoded fallback");
        }
    }
    if (!xmlParsed) {
        const std::string cName = std::string((const char*)kRuleChangedName.ToCStr(CC_UTF8));
        const std::string fName = std::string((const char*)kRuleConfirmedName.ToCStr(CC_UTF8));
        specs.push_back({cName, "", 20, 20, 230/255.0, 25/255.0,  25/255.0});
        specs.push_back({fName, "", 80, 80, 25/255.0,  204/255.0, 25/255.0});
    }

    // ── Step 4: ルールを新規作成 or 既存ルールに条件を適用（更新） ──
    API_Guid rChangedGuid = APINULLGuid, rConfGuid = APINULLGuid;
    for (auto& sp : specs) {
        const GS::UniString ruleName(sp.nameUtf8.c_str(), CC_UTF8);
        API_Guid* tgt = nullptr;
        if      (ruleName == kRuleChangedName)   tgt = &rChangedGuid;
        else if (ruleName == kRuleConfirmedName) tgt = &rConfGuid;
        else continue;

        // 既存ルールの確認
        API_OverrideRule existing = {APINULLGuid, ruleName};
        const bool ruleExists = (ACAPI_GraphicalOverride_GetOverrideRuleByName(existing, rgGuid) == NoError);

        if (ruleExists) {
            *tgt = existing.guid;
            if (!sp.criterionXml.empty()) {
                // 既存ルールに XML から取得した条件を設定して更新
                existing.criterionXML = GS::UniString(sp.criterionXml.c_str(), CC_UTF8);
                const GSErrCode ce = ACAPI_GraphicalOverride_ChangeOverrideRule(existing);
                WriteExternalLog("  " + sp.nameUtf8 + ": criterion " + (ce == NoError ? "updated OK" : "update FAILED err=" + std::to_string(ce)));
            } else {
                WriteExternalLog("  " + sp.nameUtf8 + ": exists, no criterion in XML");
            }
        } else {
            // 新規ルール作成
            API_OverrideRule r = {APINULLGuid, ruleName};
            r.criterionXML = GS::UniString(sp.criterionXml.c_str(), CC_UTF8);
            r.style = API_OverrideRuleStyle();
            r.style.lineMarkerTextPen                      = sp.pen;
            r.style.surfaceOverride                        = API_RGBColor{sp.r, sp.g, sp.b};
            r.style.surfaceType.overrideCutSurface          = true;
            r.style.surfaceType.overrideUncutSurface        = true;
            r.style.fillForegroundPenOverride              = sp.fillPen;
            r.style.fillTypeForegroundPen.overrideCutFill      = true;
            r.style.fillTypeForegroundPen.overrideCoverFill    = true;
            r.style.fillTypeForegroundPen.overrideDraftingFill = true;
            if (ACAPI_GraphicalOverride_CreateOverrideRule(r, rgGuid) == NoError) {
                *tgt = r.guid;
                WriteExternalLog("  " + sp.nameUtf8 + ": created" + (sp.criterionXml.empty() ? " (no criterion)" : " with criterion"));
            } else {
                WriteExternalLog("  " + sp.nameUtf8 + ": create FAILED");
            }
        }
    }

    // Step 5: コンビネーションを検索または作成
    { API_OverrideCombination combo = {APINULLGuid, kComboName};
      if (ACAPI_GraphicalOverride_GetOverrideCombination(combo, nullptr) != NoError) {
          GS::Array<API_Guid> rl;
          if (rChangedGuid != APINULLGuid) rl.Push(rChangedGuid);
          if (rConfGuid    != APINULLGuid) rl.Push(rConfGuid);
          API_OverrideCombination nc = {APINULLGuid, kComboName};
          if (ACAPI_GraphicalOverride_CreateOverrideCombination(nc, rl) == NoError) comboCreated = true;
      } }

    if (!silent) {
        GS::UniString out = "{\"type\":\"bim_override_result\",\"status\":\"ok\",\"action\":\"setup\","
                            "\"propCreated\":"; out.Append(propCreated ? "true" : "false");
        out.Append(",\"combinationCreated\":"); out.Append(comboCreated ? "true" : "false");
        out.Append(",\"combinationName\":\""); out.Append(Escape(kComboName));
        out.Append("\",\"changeStatusGuid\":\""); out.Append(csGuidStr); out.Append("\"}");
        EnqueueResult(std::string((const char*)out.ToCStr(CC_UTF8)));
    }
}

// ─── ToggleBIMOverride: Navigator 内の全ビューにコンビネーションを適用/解除 ───
static void ApplyOverrideToNavItems(API_NavigatorItem& parent, const GS::UniString& comboName, int& count) {
    GS::Array<API_NavigatorItem> children;
    if (ACAPI_Navigator_GetNavigatorChildrenItems(&parent, &children) != NoError) return;
    for (auto& child : children) {
        child.mapId = parent.mapId;
        API_NavigatorView view = {};
        if (ACAPI_Navigator_GetNavigatorView(&child, &view) == NoError) {
            GS::ucsncpy(view.overrideCombination, comboName.ToUStr(), API_UniLongNameLen);
            if (ACAPI_Navigator_ChangeNavigatorView(&child, &view) == NoError) count++;
        }
        ApplyOverrideToNavItems(child, comboName, count);
    }
}

void ToggleBIMOverride(const std::string& /*json*/, bool enable) {
    const GS::UniString kComboName("BIM変更管理", CC_UTF8);
    GS::UniString targetName = enable ? kComboName : GS::UniString("");
    int viewCount = 0;

    API_NavigatorSet navSet = {};
    BNZeroMemory(&navSet, sizeof(navSet));
    navSet.mapId = API_PublicViewMap;
    Int32 setId = 0;
    if (ACAPI_Navigator_GetNavigatorSet(&navSet, &setId) != NoError) {
        EnqueueResult("{\"type\":\"bim_override_result\",\"status\":\"error\",\"action\":\"toggle\",\"reason\":\"navigator failed\",\"viewCount\":0}");
        return;
    }
    API_NavigatorItem root = {}; root.guid = navSet.rootGuid; root.mapId = API_PublicViewMap;
    ApplyOverrideToNavItems(root, targetName, viewCount);

    GS::UniString out = "{\"type\":\"bim_override_result\",\"status\":\"ok\",\"action\":\"";
    out.Append(enable ? "applied" : "removed");
    out.Append("\",\"viewCount\":"); out.Append(GS::UniString::Printf("%d", viewCount)); out.Append("}");
    EnqueueResult(std::string((const char*)out.ToCStr(CC_UTF8)));
}

void StartCommandListener() {
    stopListener = false;
    listenerThread = std::thread([]() {
        listenSocket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP); if (listenSocket == INVALID_SOCKET) return;
        sockaddr_in svc{}; svc.sin_family = AF_INET; svc.sin_addr.s_addr = INADDR_ANY; svc.sin_port = htons(5001);
        if (bind(listenSocket, (SOCKADDR*)&svc, sizeof(svc)) == SOCKET_ERROR) { closesocket(listenSocket); listenSocket = INVALID_SOCKET; return; }
        listen(listenSocket, 5); WriteExternalLog("Command listener started on port 5001");
        while (!stopListener) {
            fd_set r; FD_ZERO(&r); if (listenSocket != INVALID_SOCKET) {
                FD_SET(listenSocket, &r); timeval t{1, 0};
                if (select(0, &r, NULL, NULL, &t) > 0) {
                    SOCKET as = accept(listenSocket, NULL, NULL); if (as != INVALID_SOCKET) {
                        std::string buffer; char b[4096]; int br;
                        while ((br = recv(as, b, sizeof(b), 0)) > 0) { buffer.append(b, br); }
                        if (!buffer.empty()) {
                            size_t pos = 0;
                            while (true) {
                                size_t s = buffer.find("{", pos); if (s == std::string::npos) break;
                                int brace = 1; size_t e = s + 1;
                                for (; e < buffer.size(); e++) { if (buffer[e] == '{') brace++; else if (buffer[e] == '}') brace--; if (brace == 0) break; }
                                if (brace != 0 || e >= buffer.size()) break;
                                std::string obj = buffer.substr(s, e - s + 1); CommandBatch batch;
                                if (obj.find("\"apply_changes\"") != std::string::npos) {
                                    batch.type = "apply_changes"; size_t cPos = obj.find("\"changes\":[");
                                    if (cPos != std::string::npos) {
                                        cPos += 11; while (true) {
                                            size_t os = obj.find("{", cPos); if (os == std::string::npos) break;
                                            size_t oe = obj.find("}", os); if (oe == std::string::npos) break;
                                            std::string cObj = obj.substr(os, oe - os + 1); ChangeData d;
                                            d.guid = GetV(cObj, "guid"); d.group = GetV(cObj, "group"); d.propId = GetV(cObj, "propId");
                                            d.propName = GetV(cObj, "propName"); d.propGroup = GetV(cObj, "propGroup");
                                            d.specialType = GetV(cObj, "specialType"); d.value = GetV(cObj, "value");
                                            std::string stp = GetV(cObj, "modiStamp"); if (!stp.empty()) d.modiStamp = (UInt32)std::stoul(stp);
                                            if (!d.guid.empty()) batch.changes.push_back(d); cPos = oe + 1;
                                        }
                                    }
                                } else { batch.type = "other"; batch.rawJson = obj; }
                                EnqueueCommand(batch); pos = e + 1;
                            }
                        }
                        closesocket(as);
                    }
                }
            }
        }
    });
}

void StartPythonServer() {
    if (serverProcessHandle) { DWORD e; if (GetExitCodeProcess(serverProcessHandle, &e) && e == STILL_ACTIVE) return; CloseHandle(serverProcessHandle); serverProcessHandle = NULL; }
    IO::Location loc; if (ACAPI_GetOwnLocation(&loc) == NoError) {
        for (int i = 0; i < 5; i++) {
            loc.DeleteLastLocalName(); IO::Location t = loc; t.AppendToLocal(IO::Name("server.py"));
            bool ex; if (IO::fileSystem.Contains(t, &ex) == NoError && ex) {
                GS::UniString p; t.ToPath(&p); std::wstring c = L"python \"" + std::wstring((const wchar_t*)p.ToUStr().Get()) + L"\"";
                STARTUPINFO si = {sizeof(si)}; PROCESS_INFORMATION pi;
                if (CreateProcess(NULL, (wchar_t*)c.data(), NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi)) { serverProcessHandle = pi.hProcess; CloseHandle(pi.hThread); WriteExternalLog("Started Python server process"); break; }
            }
        }
    }
}

static void LogLoadedAddOnBinary() {
    IO::Location l; if (ACAPI_GetOwnLocation(&l) == NoError) { GS::UniString p; l.ToPath(&p); WriteExternalLog("Loaded binary: " + std::string((const char*)p.ToCStr(CC_UTF8))); }
    WriteExternalLog(std::string("Build stamp: ") + __DATE__ + " " + __TIME__);
}

GSErrCode __stdcall MenuCommandHandler (const API_MenuParams*) { ExampleDialog::GetInstance ().Show (); ExampleDialog::GetInstance ().BringToFront (); return NoError; }
GSErrCode EventLoopCommandHandler(GSHandle, GSPtr, bool) { ProcessOneCommand(); return NoError; }

// プロジェクト開始時に ChangeStatus プロパティと BIM変更管理コンビネーションを自動確認・作成
static GSErrCode OnProjectEvent(API_NotifyEventID notifCode, Int32 /*param*/) {
    if (notifCode == APINotify_Open || notifCode == APINotify_New)
        SetupBIMOverride("", true);
    return NoError;
}

extern "C" {
    API_AddonType CheckEnvironment(API_EnvirParams* envir) { RSGetIndString(&envir->addOnInfo.name, AddOnInfoID, 1, ACAPI_GetOwnResModule()); RSGetIndString(&envir->addOnInfo.description, AddOnInfoID, 2, ACAPI_GetOwnResModule()); return APIAddon_Normal; }
    GSErrCode RegisterInterface(void) { GSErrCode err = ACAPI_MenuItem_RegisterMenu(AddOnMenuID, 0, MenuCode_UserDef, MenuFlag_Default); if (err == NoError) err = ACAPI_AddOnAddOnCommunication_RegisterSupportedService(EventLoopDispatcherCmdID, EventLoopDispatcherCmdVersion); return err; }
    GSErrCode Initialize(void) {
        WSAData wsa; WSAStartup(MAKEWORD(2,2), &wsa); ACAPI_KeepInMemory(true); stopSender = false; senderThread = std::thread(SenderLoop); LogLoadedAddOnBinary(); StartCommandListener(); StartPythonServer();
        GSErrCode err = ACAPI_MenuItem_InstallMenuHandler(AddOnMenuID, MenuCommandHandler);
        if (err == NoError) err = ACAPI_AddOnIntegration_InstallModulCommandHandler(EventLoopDispatcherCmdID, EventLoopDispatcherCmdVersion, EventLoopCommandHandler);
        ACAPI_Notification_CatchSelectionChange(OnSelectionChanged);
        ACAPI_ProjectOperation_CatchProjectEvent(APINotify_Open | APINotify_New, OnProjectEvent);
        // AddOnマネージャーからリロード時など、プロジェクトが既に開いていれば即時セットアップ
        { GS::Array<API_Guid> chk;
          for (const auto& t : SupportedTypes)
              if (ACAPI_Element_GetElemList(API_ElemType(t.id), &chk) == NoError && !chk.IsEmpty())
                  { WriteExternalLog("Initialize: project already open, running BIM auto-setup"); SetupBIMOverride("", true); break; } }
        if (err == NoError) ACAPI_RegisterModelessWindow(ExampleDialog::PaletteRefId(), ExampleDialog::PaletteAPIControlCallBack, API_PalEnabled_FloorPlan + API_PalEnabled_Section + API_PalEnabled_Elevation + API_PalEnabled_InteriorElevation + API_PalEnabled_3D + API_PalEnabled_Detail + API_PalEnabled_Worksheet + API_PalEnabled_Layout + API_PalEnabled_DocumentFrom3D, GSGuid2APIGuid(ExampleDialog::PaletteGuid()));
        return err;
    }
    GSErrCode FreeData(void) { stopListener = true; stopSender = true; ACAPI_UnregisterModelessWindow (ExampleDialog::PaletteRefId ()); if (listenSocket != INVALID_SOCKET) { shutdown(listenSocket, SD_BOTH); closesocket(listenSocket); listenSocket = INVALID_SOCKET; } if (listenerThread.joinable()) listenerThread.join(); if (senderThread.joinable()) senderThread.join(); if (serverProcessHandle) { DWORD e = 0; if (GetExitCodeProcess(serverProcessHandle, &e) && e == STILL_ACTIVE) TerminateProcess(serverProcessHandle, 0); CloseHandle(serverProcessHandle); serverProcessHandle = NULL; } WSACleanup(); return NoError; }
}
