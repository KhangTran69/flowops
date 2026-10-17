# Runbook: a ChatOps approval link is rejected or replayed

1. A `409` on approve/reject usually means the token was already used or the
   step is no longer pending. Check the step status before retrying.
2. A `401` means the signature is invalid or the token expired (24 h TTL).
   Generate a fresh link from the request detail page.
3. Never reuse a token across approve and reject; each token is action-bound.
4. If the step is blocked, complete the prerequisites first.
5. Record the actor (`chatops:slack`, `chatops:teams`, `chatops:webhook`) in the
   audit timeline.
