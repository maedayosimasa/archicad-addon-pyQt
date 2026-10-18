#ifndef EXAMPLE_DIALOG_HPP
#define EXAMPLE_DIALOG_HPP

#include "APIEnvir.h"
#include "ACAPinc.h"
#include "DGModule.hpp"

class ExampleDialog : public DG::Palette,
                      public DG::PanelObserver,
                      public DG::ButtonItemObserver
{
public:
    ExampleDialog ();
    ~ExampleDialog ();

    static ExampleDialog& GetInstance ();
    static GSErrCode PaletteAPIControlCallBack (Int32 referenceID, API_PaletteMessageID messageID, GS::IntPtr param);
    static Int32 PaletteRefId ();
    static const GS::Guid& PaletteGuid ();

private:
    virtual void PanelOpened (const DG::PanelOpenEvent& ev) override;
    virtual void PanelCloseRequested (const DG::PanelCloseRequestEvent& ev, bool* accepted) override;
    virtual void ButtonClicked (const DG::ButtonClickEvent& ev) override;

    DG::Button getConfigButton;
    DG::Button cancelButton;
};

#endif
