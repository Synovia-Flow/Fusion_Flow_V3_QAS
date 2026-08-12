# Processing Module Steps

Processing is the point where raw data becomes something we would be happy to
submit.

It should be strict. If the data is not good enough, block it here instead of
letting TSS reject it later.

## Input and output

```text
ING raw rows
  -> normalise
  -> enrich from CFG
  -> construct PRS records
  -> validate
  -> ready for STG promotion
```

## Steps

| Step | What happens |
| --- | --- |
| 1. Claim source | Pick the raw `ING` rows for the run and open execution trace. |
| 2. Map | Read the configured source fields for the client/entity. |
| 3. Normalise | Clean dates, yes/no values, codes, whitespace and obvious formatting. |
| 4. Enrich | Add approved partner/product/default data from `CFG`. |
| 5. Construct | Build the PRS ENS/consignment/goods/SDI object. |
| 6. Validate | Required fields, choice values, dates, EORI/address, weights, values, duplicates. |
| 7. Mark result | `VALIDATED` if clean, `REJECTED` if not. |
| 8. Hand off | Only validated rows should move towards `STG` and TSS. |

## What should be config-driven

- source column to target field,
- default values,
- choice-value mapping,
- product and partner enrichment,
- conditional required fields,
- client-specific rules.

The engine should be reusable. The client differences should live in `CFG`, not
in copied code.

## Rules we need to preserve

- Product identity is SKU-first.
- Goods description is useful text, not a safe primary key.
- Weights should not be multiplied by `QtyPerUom`.
- Source files stay untouched.
- Every derived/defaulted value needs provenance.
- SDI goods must map back to the correct source goods.
- Known blockers stay local until fixed.

## Audit

Processing should leave enough trace to answer:

- source row,
- source file/email,
- field before,
- field after,
- rule/default used,
- validation result,
- error reason if rejected.

That is the minimum standard before we trust an automated submit.
