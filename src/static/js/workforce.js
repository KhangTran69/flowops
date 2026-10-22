(() => {
  const feedback = document.getElementById("workforce-feedback");
  const show = (message, bad = false) => {
    if (!feedback) return;
    feedback.hidden = false;
    feedback.textContent = message;
    feedback.dataset.state = bad ? "error" : "success";
  };
  const send = async (url, method, data = {}) => {
    const response = await fetch(url, {
      method,
      headers: {"Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content || ""},
      body: method === "GET" ? undefined : JSON.stringify(data),
      credentials: "same-origin"
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`);
    return result;
  };
  document.querySelectorAll(".workforce-form").forEach(form => form.addEventListener("submit", async event => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(form).entries());
    if (payload.employee_id) payload.employee_id = Number(payload.employee_id);
    if (payload.serial_number === "") payload.serial_number = null;
    try {
      const result = await send(form.dataset.endpoint, "POST", payload);
      show(`Saved successfully${result.id ? ` (record #${result.id})` : ""}.`);
      window.setTimeout(() => window.location.reload(), 700);
    } catch (error) { show(error.message, true); }
  }));
  document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", async () => {
    const row = button.closest("tr");
    const id = row?.dataset.recordId;
    if (!id) return;
    try {
      let url, method = "POST", payload = {};
      switch (button.dataset.action) {
        case "approve": url = `/workforce/onboarding-requests/${id}/approve`; payload.comment = "Approved in manager queue."; break;
        case "reject": {
          const reason = window.prompt("Reason for rejection (required):");
          if (!reason?.trim()) return;
          url = `/workforce/onboarding-requests/${id}/reject`; payload = {reason: reason.trim()}; break;
        }
        case "submit": url = `/workforce/onboarding-requests/${id}/submit`; break;
        case "create-onboarding": url = "/workforce/onboarding-requests"; payload = {employee_id: Number(id)}; break;
        case "complete-task": {
          const notes = window.prompt("Completion notes (optional):") || "Completed by IT.";
          url = `/workforce/tasks/${id}`; method = "PATCH"; payload = {status: "DONE", completion_notes: notes}; break;
        }
        case "update-task": {
          const status = window.prompt("Status: TODO, IN_PROGRESS, BLOCKED, DONE", "IN_PROGRESS");
          if (!status) return;
          const assignee = window.prompt("Assignee account subject (leave empty to keep current):", "");
          url = `/workforce/tasks/${id}`; method = "PATCH"; payload = {status: status.toUpperCase()};
          if (assignee?.trim()) payload.assignee_subject = assignee.trim();
          break;
        }
        case "assign-asset": {
          const employee = window.prompt("Employee record ID:");
          if (!employee || !/^\d+$/.test(employee)) return;
          url = `/workforce/assets/${id}/assign`; payload = {employee_id: Number(employee)}; break;
        }
        case "return-asset": url = `/workforce/assets/${id}/return`; payload = {notes: "Returned to IT inventory."}; break;
        case "asset-maintenance": url = `/workforce/assets/${id}/status`; payload = {status: "MAINTENANCE"}; break;
        case "asset-available": url = `/workforce/assets/${id}/status`; payload = {status: "AVAILABLE"}; break;
        case "asset-history": {
          const history = await send(`/workforce/assets/${id}/history`, "GET");
          window.alert(history.length ? history.map(item => `Employee #${item.employee_id}: ${item.assigned_at} → ${item.returned_at || "currently assigned"}`).join("\n") : "No assignment history.");
          return;
        }
        case "retry-event": url = `/workforce/automation/events/${id}/retry`; break;
        case "simulate-event": url = `/workforce/automation/events/${id}/simulate`; break;
      }
      const result = await send(url, method, payload);
      show(result.simulated ? "Mock account provisioning completed. No external account was created." : "Update saved.");
      window.setTimeout(() => window.location.reload(), 700);
    } catch (error) { show(error.message, true); }
  }));
})();
