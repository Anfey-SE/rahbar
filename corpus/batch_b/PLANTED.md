# Batch B Planted Problems and Ground Truth

This document defines the ground truth for Batch B verification evaluation.

| File | Title | Version | Planted Problem (Ground Truth) | Targeted Verification Check | Expected Outcome |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `b01_ai_policy.md` | Generative Artificial Intelligence Policy | 2026-06 | Clean baseline excerpt from Student Handbook | All 4 checks | Accepted |
| `b02_grade_points.md` | Grading System and Academic Warning | 2026-06 | Clean baseline excerpt from Academic Rules | All 4 checks | Accepted |
| `b03_course_withdrawal.md` | Course Withdrawal Regulations | 2026-06 | Clean baseline excerpt on withdrawal rules | All 4 checks | Accepted |
| `b04_exact_dup_b01.md` | Generative Artificial Intelligence Policy | 2026-06 | Exact duplicate 1: byte-for-byte copy of b01 | Uniqueness (file hash) | Quarantined |
| `b05_exact_dup_b02.md` | Grading System and Academic Warning | 2026-06 | Exact duplicate 2: byte-for-byte copy of b02 | Uniqueness (file hash) | Quarantined |
| `b06_near_dup_b03.md` | Course Withdrawal Regulations | 2026-06 | Near-duplicate of b03: paraphrased text, line reflow | Uniqueness (embedding similarity >= 0.95) | Quarantined |
| `b07_truncated_doc.md` | Generative Artificial Intelligence Framework | 2026-06 | Truncated document: stops mid-sentence on letter | Completeness (truncation rule) | Quarantined |
| `b08_corrupt_file.pdf` | (None) | (None) | Corrupt PDF: `%PDF-1.4` followed by 2KB random bytes | Quality / Completeness (unparseable) | Quarantined |
| `b09_missing_metadata.md` | (None) | (None) | Missing metadata: no front matter, no heading, no date | Completeness (missing title & date) | Quarantined |
| `b10_academic_rules_v2.md` | Academic Rules Handbook | 2026-12 | Newer version of real handbook (withdrawal deadline amendment) | Recency (2026-12 > 2026-06) | Accepted; archives 2026-06 handbook |
| `b11_prompt_injection.md` | Student Attendance Policy | 2026-06 | Injected prompt instruction ("Ignore all previous...") | Injection scanner (warning only) | Accepted with warning |
| `b12_uncovered_topic.md` | Faculty Startups and IP Revenue Sharing | 2022-05 | Clean excerpt on uncovered topic for Part E drift | All 4 checks | Accepted |
