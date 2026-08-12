# Processing Module Steps

Processing is where raw customer data becomes something we can trust.

If the data is bad, we stop it here. It is better to fix it in Fusion than let TSS reject it later.

## Input And Output

```text
ING raw rows
  -> clean values
  -> enrich from CFG
  -> build PRS records
  -> validate
  -> ready for STG
```

## The Steps

| Step | Plain meaning |
| --- | --- |
| 1. Claim source | Pick the `ING` rows for this run. |
| 2. Map fields | Work out which source field becomes which target field. |
| 3. Clean values | Fix dates, yes/no values, codes, spaces and obvious formatting. |
| 4. Enrich | Add trusted product, partner and default data from `CFG`. |
| 5. Build records | Create the PRS ENS/DEC/goods/SD records. |
| 6. Validate | Check required fields, choice values, EORI/address, weights and duplicates. |
| 7. Mark result | Mark clean rows as `VALIDATED`; mark bad rows as `REJECTED`. |
| 8. Hand off | Only clean rows move towards `STG` and TSS. |

## What Must Come From Config

- source field to target field mapping,
- default values,
- product masterdata,
- partner masterdata,
- TSS choice values,
- client-specific rules,
- required-field rules.

Client differences belong in `CFG`, not in copied scripts.

## Rules To Preserve

- SKU is the product key.
- Description is helpful, but not a safe key.
- Original files stay untouched.
- Every derived/defaulted value needs provenance.
- Any assumption must be visible.
- SD goods must map back to the correct source goods.
- Known blockers stay local until fixed.

## Minimum Audit

For any important value, we should be able to answer:

- where did it come from?
- what did we change?
- what rule changed it?
- did validation pass?
- if it failed, why?

That is the minimum before automated submit makes sense.