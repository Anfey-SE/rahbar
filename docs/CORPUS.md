# Corpus

Source: FAST-NUCES public website (nu.edu.pk). 6 documents, 246 pages (checked with pypdf on 4 Nov 2026).

We commit `corpus/sources.yaml`, `corpus/fetch.py` and `corpus/MANIFEST.md` (URL and SHA-256 for each file), not the PDFs. Run `python corpus/fetch.py` to download them, then `python corpus/fetch.py --verify` to check the bytes. Every document is a public page; nothing is behind a login.

| # | Required property | Document(s) that provide it | Evidence |
| --- | --- | --- | --- |
| 1 | At least 6 documents, 40 pages | All 6 documents in `corpus/sources.yaml` | 246 pages in total: Academic Rules Handbook 58, Annual Report 2023-24 85, Start-ups Policy 8, Prospectus rules 5, Student Handbook 64, Undergraduate Rules 2023 26. |
| 2 | Two versions of one policy | Undergraduate Academic Rules (Aug 2023) and Academic Rules Handbook (Jun 2026) | The 2023 file has `doc_version: 2023-08` and `superseded: true`. The 2026 file has `doc_version: 2026-06` and `superseded: false`. The retriever filters on these fields. |
| 3 | Scanned, image-only PDF | Faculty Entrepreneurship and Technical Start-ups Policy | 0 text characters in the first 5 of its 8 pages, so it needs OCR. The 2023 rules file is also scanned: 24 of its 26 pages have no text layer, so it needs OCR too. |
| 4 | A table with real information | Annual Report 2023-24, page 13 ("Faculty Summary") | Faculty counts in a table; a plain text extractor can scramble the columns. |
| 5 | Long definitions | Student Handbook (Jun 2026), Generative AI policy with numbered sub-sections | A fixed-size chunker can cut these in half. |
| 6 | A deliberately absent topic | Hostel rules | We left out the Prospectus "Transport and Hostel Facilities" PDF. The word "hostel" appears 0 times in both academic rules documents. It appears 2 times in the Student Handbook and 4 times in the Annual Report, as mentions only. A student could ask about hostel rules, so the system must say that the corpus does not cover it. |
| 7 | Exact-term content | Academic Rules Handbook (attendance is clause 1.21) and Prospectus 2026 Academic Rules (attendance is clause 1) | The same rule has different clause numbers in the two documents. This makes exact-term questions hard for embeddings. |
