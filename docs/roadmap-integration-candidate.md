# Roadmap integration candidate — November 29, 2026

This isolated branch is for validation, not a replacement for individual PR
review or authorization to merge or deploy.

| PR | Pinned head |
| --- | --- |
| 88 (includes React 79, 80, 83, 85) | 420b5d5b725f283b9985a914f07ef4e5dbdede56 |
| 81 | 3ff08790dfcd89c7d0c0938e2609a7bf3ff69ed2 |
| 82 | 7ef36eade2cbd61fa6a3d185c8415322dc8d9dc2 |
| 87 | 1ac04ca08b80a68a005409e37ea415762582dbeb |
| 91 | dbb515815b0b6db02da4210715444a4a45f3034f |
| 84 | 139924691fbde1f2fe0016c9e02e81b4dc554cb6 |
| 89 | 0e34aab89c808762b3c6c05b07b9f8ecb67aed2c |
| 90 | 9e411c961000831f179270f4011be11e72cb33e4 |

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

The first combined run reproduced a missing local-only workforce browser
fixture flag. The follow-up restores the same flag used by #88 in browser CI
only; production workforce access is unchanged. It also includes the latest
account-configuration invalidation and notification attempt-budget changes.

The outbox chaos drill retains recovery and terminal-state assertions but now
counts both the abandoned and recovery claims. Only one successful simulated
send is observed; no exactly-once external effect is inferred.

Follow-up: protected operator workforce denial, focus navigation and active
retry lease guard, plus opt-in connector observed revision identities. Full
provider CDC and live workforce identity remain gated.
