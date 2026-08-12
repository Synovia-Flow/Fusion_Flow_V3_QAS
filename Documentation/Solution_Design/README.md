# Solution Design

This folder is for the higher-level V3 material: decks, process notes and design
summaries.

Keep it short. The useful thing here is not a long explanation; it is a clear
view of how the flow is meant to work and what still needs to move from the
current BKD automation into proper V3 modules.

## Main files

| File | Purpose |
| --- | --- |
| `Fusion_Flow_E2E_Solution_Design.pptx` | End-to-end platform view. |
| `Fusion_Flow_PRS_Processing_Module.pptx` | Processing / PRS module view. |
| `PRS_Processing_Module_Deck.html` | Browser version of the PRS deck. |
| `Fusion_Flow_Status_Summary.md` | Short status summary. |
| `Processing_Module_Steps.md` | Processing steps and ownership. |

## What the docs should make clear

- `ING` is evidence.
- `CFG` is approved config/masterdata.
- `PRS` is the clean record before submit.
- `STG/API/TSS` are the submit and response layers.
- `EXC/LOG/CHG` are for audit, execution and traceability.

If a document cannot explain where a value came from and where it goes next, it
needs tightening.
