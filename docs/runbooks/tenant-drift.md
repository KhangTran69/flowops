# Runbook: tenant drift in the morning digest

Each weekday at 06:00, FlowOps checks every client tenant against its
baseline (Tenants page): required licences and free seats, security
defaults, and enabled users with no licence.

| Finding | Meaning | Action |
|---|---|---|
| `sku:X:free_units` drift | Fewer free licences than the baseline minimum. The next onboarding will fail. | Tell the account manager. Raise a PSA ticket for a licence purchase. |
| `sku:X` missing | The client no longer has that subscription. | Check with the account manager whether it was cancelled on purpose; update the baseline if so. |
| `security_defaults` drift | Someone switched off Microsoft security defaults. | **Security issue.** Escalate to the security team the same day. |
| `unlicensed_enabled_users` drift | Active accounts with no licence (often leavers not offboarded). | Check the names listed. Start an offboarding for real leavers. |
| status `error` | FlowOps couldn't reach that tenant. Other tenants were still checked. | See [credentials-and-access.md](credentials-and-access.md). |

To re-check one client after fixing: Tenants page > **Check drift**, or
`POST /tenants/drift-check?tenant=<key>`.
