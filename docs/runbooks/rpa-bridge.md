# RPA bridge operations

Use the [RPA bridge guide](../rpa-bridge.md) to configure the adapter. For an
incident, inspect the FlowOps job and audit records first, then the external
bot queue using the same `idempotency_key`. If the job is retry-scheduled, do
not manually launch a second bot run unless the receiving system confirms the
first request was not accepted. Requeue only after reconciling the external
state.
