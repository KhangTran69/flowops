# Roadmap integration candidate — November 29, 2026

This isolated branch is for validation, not a replacement for individual PR
review or authorization to merge or deploy.

| PR | Pinned head |
| --- | --- |
| 88 (includes React 79, 80, 83, 85) | 790d6469ffc5ad08c6790f8b82b91c0e1d2cb0ed |
| 81 | 3ff08790dfcd89c7d0c0938e2609a7bf3ff69ed2 |
| 82 | 7ef36eade2cbd61fa6a3d185c8415322dc8d9dc2 |
| 87 | 8c0f9e6d9f1f013f61071cbb1a5f7394a60868ab |
| 91 | 2d4f8346ec30fc58682e31336e8f16b2c5d1f8d2 |
| 84 | 139924691fbde1f2fe0016c9e02e81b4dc554cb6 |
| 89 | 0e34aab89c808762b3c6c05b07b9f8ecb67aed2c |
| 90 | 2a6914608d97e43497f622f44a0824a9a3169f7a |

Shared recovery #86 is included in #89's selected script. CI retains the
workforce and connector PostgreSQL modules, all existing PostgreSQL tests,
both queue benchmarks, API benchmark, evidence artifacts, browser and
monitoring gates. The combined job test module retains both console contract
and lease-fencing tests. Review individual changes before integrating; any
newer head requires reconciliation and new combined validation.

Review #86, #81, #82, #84, #87, #89, #90 and #91 independently; preserve
React #79/#80 → #83 → #85 → #88. Real Entra browser identity, provider
sandbox and watermark validation, representative staging and manual WCAG
remain release gates. No deployment is authorized by this branch.
