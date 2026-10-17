# n8n workflows

These are importable n8n workflow exports for the FlowOps API.

## Import

1. Run n8n locally (free Community edition):
   ```bash
   docker run -it --rm -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n
   ```
2. Open http://localhost:5678.
3. Workflows → Import from File → choose a JSON file.
4. Set environment variables in n8n:
   - `FLOWOPS_API_URL` e.g. `http://host.docker.internal:8000`
   - `FLOWOPS_WEBHOOK_API_KEY` if webhook intake is protected
   - `SLACK_WEBHOOK_URL` for notification cards
5. Activate the workflow and use the webhook test URL.

| File | Trigger | Action |
|---|---|---|
| `01-ticket-intake.json` | POST `/webhook/flowops-ticket` | Forward to `/webhooks/intake`, notify channel |
| `02-onboarding-request.json` | POST `/webhook/flowops-onboarding` | Create request, start DAG run |
| `03-chatops-approval.json` | POST `/webhook/flowops-approval` | Post Slack approve/reject card |

The ChatOps workflow expects `approve_url` and `reject_url` in the event body.
The backend exposes these on pending gated steps; a delivery dispatcher can
forward them to this webhook.

## Screenshots

`flows/screenshots/` is reserved for real n8n canvas captures. No screenshots
were fabricated; capture them after importing the workflows locally.
