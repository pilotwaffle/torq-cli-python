# License detection investigation (release trust pack R0D)

Status: investigation record. **No LICENSE change is made in R0.** The
finding below is a legal-content discrepancy; any LICENSE edit requires
separate operator review.

## Observed facts (measured on `main`, 2026-08-03)

| Item | Value |
|---|---|
| File | `LICENSE` |
| Git blob SHA | `076ace7cbd4e079b7771e5789cec3f592ea1484b` |
| Blob size | 11,303 bytes |
| Canonical Apache-2.0 size | 11,358 bytes (apache.org `LICENSE-2.0.txt`, fetched 2026-08-03) |
| GitHub detection | key `other`, name `Other` |
| Declared intent | `pyproject.toml` license classifiers: `License :: OSI Approved :: Apache Software License`; `license = "Apache-2.0"` |
| Encoding/line-endings | clean UTF-8, LF; no BOM; no CRLF |

## Detection cause: substantive legal-text differences

The discrepancy is **legal content**, not formatting, encoding, BOM, or
detector delay. Five differences against the canonical text
(`https://www.apache.org/licenses/LICENSE-2.0.txt`):

1. **Section 5 (Contributions):** repo says "for the purpose of **tracking**
   and improving the Work"; canonical says "for the purpose of **discussing**
   and improving the Work".
2. **Section 6 (Trademarks):** repo omits the canonical qualifier "as required
   for **reasonable and customary use in** describing the origin of the Work
   and reproducing the content of the NOTICE file" — the repo text reads
   "except as required for describing the origin of the Work and reproducing
   the content of the NOTICE file."
3. **Section 9 (Accepting Warranty or Additional Liability):** repo says "You
   may **accept a fee in exchange for**, acceptance of support, warranty,
   indemnity…"; canonical says "You may **choose to offer, and charge a fee
   for,** acceptance of support, warranty, indemnity…".
4. **Appendix URL:** repo ends with `http://www.apache.org/licenses/`;
   canonical ends with `http://www.apache.org/licenses/LICENSE-2.0`.
5. **Leading blank line** before the title (cosmetic only).

The repository therefore ships a paraphrased/altered variant of Apache-2.0,
which GitHub's licensee classifier cannot map to the canonical license —
hence `Other`.

## Assessment

- The differences are not cosmetic: items 1–3 change license wording. The
  legal meaning is **not** proven preserved, so no automated fix is
  appropriate under R0 constraints.
- The declared intent across `pyproject.toml`, `SECURITY.md`, and the README
  is Apache-2.0.

## Recommendation (requires separate operator review)

Replace `LICENSE` with the byte-canonical Apache-2.0 text from
`https://www.apache.org/licenses/LICENSE-2.0.txt` (11,358 bytes, verified
against the blob hash of that exact text at replacement time), in a dedicated
legal-text PR reviewed by the operator. If Apache-2.0 was NOT the intended
license, the classifiers and metadata must be corrected instead. Until then,
release metadata must not assert a canonical Apache-2.0 LICENSE file.

## Follow-up gates (not blocking R0)

- After an authorized LICENSE fix, re-check `gh api
  repos/pilotwaffle/torq-cli-python/license` reports `apache-2.0`.
- A future release-consistency gate (post-R0) may assert LICENSE blob
  identity against the recorded canonical hash.
