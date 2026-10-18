#include "ElementService.hpp"
#include "BM.hpp"

// Utility to get the name of a category value (e.g., "Load-Bearing", "External")
static GS::UniString GetCategoryValueName (const API_Guid& elemGuid, API_ElemCategoryID catID)
{
    API_ElemCategory cat = {};
    cat.categoryID = catID;
    API_ElemCategoryValue val = {};
    
    if (ACAPI_Category_GetCategoryValue (elemGuid, cat, &val) == NoError) {
        GS::Array<API_ElemCategoryValue> allValues;
        // Correct signature: pass &cat
        if (ACAPI_Category_GetElementCategoryValues (&cat, &allValues) == NoError) {
            for (const auto& v : allValues) {
                if (v.guid == val.guid) return v.name;
            }
        }
    }
    return "---";
}

static void FillElementDetails (const API_Guid& guid, ElementInfo& info)
{
    info.elementID = "---";
    info.categoryID = "---";
    info.structuralFunction = "---";
    info.position = "---";
    info.width = 0.0;
    info.height = 0.0;

    // 1. Geometry Info (Width/Height)
    API_Element element = {};
    element.header.guid = guid;
    if (ACAPI_Element_Get (&element) == NoError) {
        API_ElemTypeID typeID = element.header.type.typeID;
        if (typeID == API_WallID) {
            info.width = element.wall.thickness;
            info.height = element.wall.height;
        } else if (typeID == API_ColumnID) {
            info.height = element.column.height;
            API_ElementMemo memo = {};
            if (ACAPI_Element_GetMemo (guid, &memo) == NoError) {
                if (memo.columnSegments != nullptr) {
                    API_ColumnSegmentType* segs = memo.columnSegments;
                    info.width = segs[0].assemblySegmentData.nominalWidth;
                }
                ACAPI_DisposeElemMemoHdls (&memo);
            }
        } else if (typeID == API_BeamID) {
            API_ElementMemo memo = {};
            if (ACAPI_Element_GetMemo (guid, &memo) == NoError) {
                if (memo.beamSegments != nullptr) {
                    API_BeamSegmentType* segs = memo.beamSegments;
                    info.width = segs[0].assemblySegmentData.nominalWidth;
                    info.height = segs[0].assemblySegmentData.nominalHeight;
                }
                ACAPI_DisposeElemMemoHdls (&memo);
            }
        } else if (typeID == API_SlabID) {
            info.width = element.slab.thickness;
        }
    }

    // 2. Classification (Category ID)
    GS::Array<GS::Pair<API_Guid, API_Guid>> classificationPairs;
    if (ACAPI_Element_GetClassificationItems (guid, classificationPairs) == NoError) {
        for (const auto& pair : classificationPairs) {
            if (pair.second == APINULLGuid) continue;
            API_ClassificationItem item = {};
            item.guid = pair.second;
            if (ACAPI_Classification_GetClassificationItem (item) == NoError) {
                info.categoryID = item.id;
                break;
            }
        }
    }

    // 3. Category Values (Structural Function, Position)
    info.structuralFunction = GetCategoryValueName (guid, API_ElemCategory_StructuralFunction);
    info.position = GetCategoryValueName (guid, API_ElemCategory_Position);

    // 4. Properties (Element ID)
    GS::Array<API_PropertyDefinition> definitions;
    if (ACAPI_Element_GetPropertyDefinitions (guid, API_PropertyDefinitionFilter_All, definitions) == NoError) {
        for (const auto& def : definitions) {
            if (def.name == "ID" || def.name == "Element ID" || def.name.Contains(GS::UniString("ID"))) {
                GS::Array<API_Property> properties;
                // status!=HasValue の value は不定 → GetPropertyValueString でクラッシュするため除外
                if (ACAPI_Element_GetPropertyValues (guid, {def}, properties) == NoError && !properties.IsEmpty() &&
                    properties[0].status == API_Property_HasValue) {
                    ACAPI_Property_GetPropertyValueString (properties[0], &info.elementID);
                }
                break;
            }
        }
    }
}

GS::Array<ElementInfo> ElementService::GetElementsByTypeAndStories (
    API_ElemTypeID typeID,
    const GS::Array<short>& stories
)
{
    GS::Array<ElementInfo> result;
    GS::Array<API_Guid> allElementGuids;
    API_ElemType type (typeID);
    
    if (ACAPI_Element_GetElemList (type, &allElementGuids) != NoError || allElementGuids.IsEmpty()) 
        return result;

    for (const auto& guid : allElementGuids) {
        API_Elem_Head header = {};
        header.guid = guid;
        header.type = type;

        if (ACAPI_Element_GetHeader (&header) == NoError) {
            bool isMatch = stories.IsEmpty();
            for (short s : stories) {
                if (header.floorInd == s) { isMatch = true; break; }
            }

            if (isMatch) {
                ElementInfo info;
                info.guid = guid;
                info.floorInd = header.floorInd;
                FillElementDetails (guid, info);
                result.Push (info);
                if (result.GetSize() >= 100) break;
            }
        }
    }
    return result;
}

bool ElementService::CheckElementPresence (API_ElemTypeID typeID)
{
    API_ElemType type (typeID);
    GS::Array<API_Guid> elemList;
    return (ACAPI_Element_GetElemList (type, &elemList) == NoError && !elemList.IsEmpty());
}