let config;
let selectedProfile = "small";
let isAdmin = false;
let autoFqdn = "";
let sshKeys = [];
let lastPendingApprovalCount = null;

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const body = await response.json();
  if (!response.ok) throw body;
  return body;
}

function toast(message) {
  const el = $("#toast"); el.textContent = message; el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2500);
}

function resourceValues() {
  if (selectedProfile !== "custom") return config.profiles[selectedProfile];
  const form = $("#requestForm");
  return {vcpu: +form.vcpu.value, memory_gb: +form.memory_gb.value, disk_gb: +form.disk_gb.value};
}

function additionalDisksFromForm() {
  return [...document.querySelectorAll("#additionalDiskList .disk-row")].map(row => ({
    size_gb: +row.querySelector("[data-disk-size]").value,
    mountpoint: row.querySelector("[data-disk-mount]").value
  }));
}

function addAdditionalDisk() {
  const list = $("#additionalDiskList");
  if (list.children.length >= 20) return toast("A maximum of 20 data disks is supported");
  const index = list.children.length + 1;
  const row = document.createElement("div");
  row.className = "disk-row";
  row.innerHTML = `
    <label>Disk size (GB)<input data-disk-size type="number" min="1" max="8000" value="20" required></label>
    <label>Mountpoint<input data-disk-mount value="/data${index}" placeholder="/data${index}" required></label>
    <button class="disk-remove" type="button">Remove</button>
    <p class="disk-policy-notice hidden"></p>`;
  row.querySelector(".disk-remove").onclick = () => { row.remove(); updateSummary(); };
  row.oninput = updateSummary;
  list.appendChild(row);
  updateSummary();
}

function updateSummary() {
  const r = resourceValues();
  const disks = additionalDisksFromForm();
  const extra = disks.length
    ? ` · ${disks.map(disk => `${disk.size_gb} GB at ${disk.mountpoint}`).join(" · ")}`
    : "";
  $("#summary").textContent = `${r.vcpu} vCPU · ${r.memory_gb} GB RAM · ${r.disk_gb} GB LVM OS disk${extra}`;
  const computeReasons = ["vcpu", "memory_gb", "disk_gb"]
    .filter(key => r[key] > config.policy.automatic[key]);
  const oversizedAdditionalDisk = disks.some(
    disk => disk.size_gb > config.policy.automatic.disk_gb
  );
  document.querySelectorAll("#additionalDiskList .disk-row").forEach(row => {
    const sizeGb = +row.querySelector("[data-disk-size]").value;
    const warning = row.querySelector(".disk-policy-notice");
    const oversized = sizeGb > config.policy.automatic.disk_gb;
    warning.classList.toggle("hidden", !oversized);
    warning.textContent = oversized
      ? `This disk exceeds the ${config.policy.automatic.disk_gb} GB automatic provisioning limit and requires administrator approval.`
      : "";
  });
  const production = $("#environment").value === "production";
  const needsApproval = computeReasons.length || oversizedAdditionalDisk || production;
  const showComputeNotice = computeReasons.length || production;
  $("#approvalSummary").textContent = needsApproval ? "Administrator approval required" : "Eligible for automatic provisioning";
  $("#policyNotice").classList.toggle("hidden", !showComputeNotice);
  $("#policyNotice").textContent = production
    ? "Production requests require administrator approval."
    : "This custom size exceeds the automatic provisioning limit and requires administrator approval.";
}

function renderProfiles() {
  const profiles = {...config.profiles, custom: {label: "Custom", vcpu: "—", memory_gb: "—", disk_gb: "—"}};
  $("#profiles").innerHTML = Object.entries(profiles).map(([key, p]) => `
    <button type="button" class="profile ${key === selectedProfile ? "selected" : ""}" data-profile="${key}">
      <span class="check">●</span><h3>${p.label}</h3>
      <ul><li>${p.vcpu} vCPU</li><li>${p.memory_gb} GB memory</li><li>${p.disk_gb} GB OS disk</li></ul>
    </button>`).join("");
  document.querySelectorAll(".profile").forEach(button => button.onclick = () => {
    selectedProfile = button.dataset.profile;
    renderProfiles();
    $("#customResources").classList.toggle("hidden", selectedProfile !== "custom");
    updateSummary();
  });
}

function statusLabel(status) {
  return ({
    completed: "Completed",
    awaiting_approval: "Awaiting approval",
    resize_awaiting_approval: "Resize awaiting approval",
    rejected: "Rejected",
    failed: "Failed",
    provisioning: "Provisioning"
  })[status] || status;
}

function renderSshKeys() {
  const select = $("#sshKey");
  if (!sshKeys.length) {
    select.innerHTML = `<option value="">Add a key in Settings first</option>`;
    select.disabled = true;
  } else {
    select.disabled = false;
    select.innerHTML = sshKeys.length > 1
      ? `<option value="">Select SSH key…</option>${sshKeys.map(key =>
          `<option value="${esc(key.id)}">${esc(key.label)} · ${esc(key.login_user)} · ${esc(key.fingerprint)}</option>`
        ).join("")}`
      : `<option value="${esc(sshKeys[0].id)}">${esc(sshKeys[0].label)} · ${esc(sshKeys[0].login_user)} · ${esc(sshKeys[0].fingerprint)}</option>`;
  }
  $("#sshKeyList").innerHTML = sshKeys.length
    ? sshKeys.map(key => `<div class="key-card">
        <div><strong>${esc(key.label)}</strong><small>Login: ${esc(key.login_user)} · ${esc(key.fingerprint)}</small></div>
        <button class="key-delete" type="button" data-key-id="${esc(key.id)}" data-key-label="${esc(key.label)}">Delete</button>
      </div>`).join("")
    : `<div class="empty">No SSH public keys saved yet.</div>`;
  document.querySelectorAll(".key-delete").forEach(button => button.onclick = async () => {
    if (!confirm(`Delete SSH public key "${button.dataset.keyLabel}"?`)) return;
    button.disabled = true;
    try {
      await api(`/api/settings/ssh-keys/${encodeURIComponent(button.dataset.keyId)}`, {method: "DELETE"});
      await loadSshKeys();
      toast("SSH public key deleted");
    } catch (error) {
      button.disabled = false;
      toast(error.error || "Unable to delete key");
    }
  });
}

async function loadSshKeys() {
  sshKeys = await api("/api/settings/ssh-keys");
  renderSshKeys();
}

function resizeForm(r) {
  if (config.provisioning_backend !== "local_qemu" || r.status !== "completed") return "";
  const disks = r.additional_disks || (r.additional_disk ? [{...r.additional_disk, target: "vdb"}] : []);
  return `
    <details class="resize-panel" data-detail-key="resize-${esc(r.id)}">
      <summary>Resize existing VM</summary>
      <form class="resize-form" data-resize-id="${esc(r.id)}">
        <p>Enter the new total values. Increases above the self-service limits
          (${config.policy.automatic.vcpu} vCPU, ${config.policy.automatic.memory_gb} GB RAM,
          ${config.policy.automatic.disk_gb} GB per disk) are sent to an administrator for approval.</p>
        <p>To activate CPUs left offline by an earlier resize, submit the current values again. No reboot is needed.</p>
        <div class="grid three">
          <label>vCPU<input name="vcpu" type="number" min="${r.resources.vcpu}" max="${config.policy.hard.vcpu}" value="${r.resources.vcpu}" required></label>
          <label>Memory (GB)<input name="memory_gb" type="number" min="${r.resources.memory_gb}" max="${config.policy.hard.memory_gb}" value="${r.resources.memory_gb}" required></label>
          <label>OS disk (GB)<input name="disk_gb" type="number" min="${r.resources.disk_gb}" max="${config.policy.hard.disk_gb}" value="${r.resources.disk_gb}" required></label>
        </div>
        ${disks.length ? `<h4 class="resize-disk-heading">Portal-managed data disks</h4>
          <div class="disk-list">${disks.map((disk, index) => `
            <div class="disk-row resize-data-disk" data-disk-index="${index}">
              <label>New total size for ${esc(disk.mountpoint)} (GB)
                <input data-resize-disk-size type="number" min="${disk.size_gb}" max="8000" value="${disk.size_gb}" required>
              </label>
              <span>${esc(disk.target || `data disk ${index + 1}`)} · LVM · ${esc(disk.mountpoint)}</span>
            </div>`).join("")}</div>` : ""}
        <div class="resize-actions"><span class="resize-error"></span><button class="primary" type="submit">Resize online</button></div>
      </form>
    </details>`;
}

function lifecycleControls(r) {
  if (config.provisioning_backend === "local_qemu" && r.status === "failed") {
    return `<div class="lifecycle-panel"><div class="lifecycle-actions">
      <button type="button" class="reject vm-delete failed-request-delete" aria-describedby="delete-help-${esc(r.id)}" data-id="${esc(r.id)}" data-hostname="${esc(r.hostname)}">
        <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6M14 10v6"/></svg>
        <span>Delete failed request</span>
      </button>
      </div><p class="failed-request-delete-help" id="delete-help-${esc(r.id)}">Remove this failed request from the portal. You will be asked to confirm; any leftover files require separate approval.</p></div>`;
  }
  if (config.provisioning_backend !== "local_qemu" || r.status !== "completed") return "";
  const state = r.vm_state || "unknown";
  const resizeSnapshots = (r.resize_snapshots || []).filter(
    snapshot => ["active", "expired"].includes(snapshot.status)
  );
  const manualSnapshots = (r.manual_snapshots || []).filter(snapshot => snapshot.status === "available");
  return `
    <div class="lifecycle-panel">
      <div class="vm-state"><span class="status-dot ${state === "running" ? "" : "stopped"}"></span>
        VM state: <strong>${esc(state)}</strong></div>
      <div class="lifecycle-actions">
        ${state === "running" ? `<button class="secondary vm-console" data-id="${esc(r.id)}"
          data-hostname="${esc(r.hostname)}">Open console</button>` : ""}
        ${["running", "shut off"].includes(state) && manualSnapshots.length < 3
          ? `<button class="secondary vm-operation" data-operation="create-snapshot"
              data-id="${esc(r.id)}">Create snapshot</button>` : ""}
        ${state === "shut off" ? `<button class="secondary vm-operation" data-operation="start" data-id="${esc(r.id)}">Start</button>` : ""}
        ${state === "running" ? `<button class="secondary vm-operation" data-operation="stop" data-id="${esc(r.id)}">Stop</button>
          <button class="secondary vm-operation" data-operation="reboot" data-id="${esc(r.id)}">Reboot</button>` : ""}
        ${r.last_failed_resize ? `<button class="secondary vm-operation" data-operation="retry-resize" data-id="${esc(r.id)}">Retry resize</button>` : ""}
        ${r.netbox?.status === "error" ? `<button type="button" class="secondary netbox-retry" data-id="${esc(r.id)}">Retry NetBox registration</button>` : ""}
        <button class="reject vm-delete" data-id="${esc(r.id)}" data-hostname="${esc(r.hostname)}">Delete VM</button>
      </div>
      <details class="snapshot-list" data-detail-key="manual-snapshots-${esc(r.id)}">
        <summary>Manual snapshots (${manualSnapshots.length}/3)</summary>
        ${manualSnapshots.length ? manualSnapshots.map(snapshot => `<div class="snapshot-row">
          <span><strong>${esc(snapshot.name)}</strong>
            <small>${esc(snapshot.created_at)} · ${snapshot.disks.length} disk(s) · ${esc(snapshot.consistency)}
              · by ${esc(snapshot.created_by)}</small>
            ${snapshot.description ? `<small>${esc(snapshot.description)}</small>` : ""}
          </span>
          <span class="snapshot-actions">
            ${state === "shut off" ? `<button class="reject vm-operation" data-operation="restore-snapshot"
              data-id="${esc(r.id)}" data-hostname="${esc(r.hostname)}"
              data-snapshot-id="${esc(snapshot.id)}">Restore</button>` : ""}
            <button class="reject vm-operation" data-operation="delete-snapshot"
              data-id="${esc(r.id)}" data-snapshot-id="${esc(snapshot.id)}">Delete</button>
          </span>
        </div>`).join("") : `<p class="snapshot-warning">No manual snapshots yet.</p>`}
        ${manualSnapshots.length && state !== "shut off"
          ? `<p class="snapshot-warning">Stop the VM before restoring a snapshot.</p>` : ""}
      </details>
      ${resizeSnapshots.length ? `<details class="snapshot-list" data-detail-key="resize-snapshots-${esc(r.id)}"><summary>Resize snapshots (${resizeSnapshots.length})</summary>
        ${resizeSnapshots.map(snapshot => `<div class="snapshot-row">
          <span><strong>${esc(snapshot.id)}</strong><small>${esc(snapshot.created_at)} · ${snapshot.disks.length} disk(s)
            · ${esc(snapshot.status)}${snapshot.expires_at ? ` · expires ${esc(snapshot.expires_at)}` : ""}</small></span>
          ${isAdmin && snapshot.status === "active" && state === "shut off" ? `<button class="reject vm-operation" data-operation="rollback-resize"
            data-id="${esc(r.id)}" data-hostname="${esc(r.hostname)}" data-snapshot-id="${esc(snapshot.id)}">Rollback</button>` : ""}
          ${isAdmin && snapshot.status === "expired" && state === "running" ? `<button class="reject vm-operation" data-operation="cleanup-resize-snapshot"
            data-id="${esc(r.id)}" data-snapshot-id="${esc(snapshot.id)}">Commit &amp; cleanup</button>` : ""}
        </div>`).join("")}
        ${isAdmin && resizeSnapshots.some(snapshot => snapshot.status === "active") && state !== "shut off"
          ? `<p class="snapshot-warning">Stop the VM before rollback.</p>` : ""}
        ${isAdmin && resizeSnapshots.some(snapshot => snapshot.status === "expired") && state !== "running"
          ? `<p class="snapshot-warning">Start the VM before expired snapshot cleanup.</p>` : ""}
      </details>` : ""}
    </div>`;
}

async function loadRequests() {
  const openDetails = new Set(
    [...document.querySelectorAll("#requestList details[data-detail-key][open]")]
      .map(item => item.dataset.detailKey)
  );
  const requests = await api("/api/requests");
  const pendingApprovalCount = requests.filter(
    r => ["awaiting_approval", "resize_awaiting_approval"].includes(r.status)
  ).length;
  $("#requestCount").textContent = isAdmin ? pendingApprovalCount : requests.length;
  if (isAdmin) {
    const alert = $("#approvalAlert");
    alert.classList.toggle("hidden", pendingApprovalCount === 0);
    alert.textContent = pendingApprovalCount === 1
      ? "1 virtual machine request is waiting for administrator approval."
      : `${pendingApprovalCount} virtual machine requests are waiting for administrator approval.`;
    if (
      lastPendingApprovalCount !== null &&
      pendingApprovalCount > lastPendingApprovalCount
    ) toast("A new virtual machine request is waiting for approval");
    lastPendingApprovalCount = pendingApprovalCount;
  }
  $("#requestList").innerHTML = requests.length ? requests.map(r => `
    <article class="request-card">
      <div class="request-top"><div class="request-heading"><div class="request-title"><h3>${esc(r.domain ? `${r.hostname}.${r.domain}` : r.hostname)}</h3><span class="request-id">#${esc(r.id)}</span></div>
        <p class="request-subtitle">Virtual machine details and access information</p></div>
        <div><span class="badge ${r.status}">${statusLabel(r.status)}</span></div></div>
      <div class="request-info-grid">
        <section class="request-info-group">
          <h4>Placement</h4>
          <dl>
            <div><dt>Environment</dt><dd>${esc(r.environment)}</dd></div>
            <div><dt>Network</dt><dd>${esc(r.network_label || r.network)}</dd></div>
            <div><dt>IP address</dt><dd class="ip-address ${r.ip_address ? "" : "pending"}">${r.ip_address ? esc(r.ip_address) : (["completed", "provisioning"].includes(r.status) ? "Waiting for DHCP" : "Not assigned")}</dd></div>
            ${isAdmin ? `<div><dt>Requested by</dt><dd>${esc(r.requested_by || "legacy")}</dd></div>` : ""}
          </dl>
        </section>
        <section class="request-info-group">
          <h4>Compute</h4>
          <dl>
            <div><dt>vCPU</dt><dd>${r.resources.vcpu}</dd></div>
            <div><dt>Memory</dt><dd>${r.resources.memory_gb} GB</dd></div>
          </dl>
        </section>
        ${r.ssh_key ? `<section class="request-info-group">
          <h4>SSH access</h4>
          <dl>
            <div><dt>Login</dt><dd>${esc(r.ssh_login_user || config.ssh_login_user)}</dd></div>
            <div><dt>Key</dt><dd>${esc(r.ssh_key.label)}</dd></div>
            <div class="wide"><dt>Fingerprint</dt><dd class="fingerprint">${esc(r.ssh_key.fingerprint)}</dd></div>
          </dl>
        </section>` : ""}
        ${(() => {
          const disks = r.additional_disks || (r.additional_disk ? [r.additional_disk] : []);
          return `<section class="request-info-group">
            <h4>Data storage</h4>
            <dl>
              <div><dt>OS disk</dt><dd>${r.resources.disk_gb} GB · LVM</dd></div>
              ${disks.map((disk, index) => `<div><dt>Data disk ${index + 1}</dt><dd>${disk.size_gb} GB · LVM · ${esc(disk.mountpoint)}</dd></div>`).join("")}
            </dl>
          </section>`;
        })()}
      </div>
      ${r.approval_reasons?.length ? `<p class="notice">${r.approval_reasons.map(esc).join(" · ")}</p>` : ""}
      ${r.pending_resize ? `<div class="pending-resize">
        <strong>Requested resize</strong>
        <span>${r.pending_resize.resources.vcpu} vCPU · ${r.pending_resize.resources.memory_gb} GB RAM · ${r.pending_resize.resources.disk_gb} GB OS disk</span>
        ${r.pending_resize.additional_disks.length ? `<span>${r.pending_resize.additional_disks.map(disk => `${esc(disk.mountpoint)}: ${disk.size_gb} GB`).join(" · ")}</span>` : ""}
        <small>${r.pending_resize.approval_reasons.map(esc).join(" · ")}</small>
      </div>` : ""}
      ${r.approved_by ? `<div class="approval-decision approved">Approved by ${esc(r.approved_by)}</div>` : ""}
      ${r.rejected_by ? `<div class="approval-decision rejected">Rejected by ${esc(r.rejected_by)}</div>` : ""}
      ${r.resize_approved_by ? `<div class="approval-decision approved">Resize approved by ${esc(r.resize_approved_by)}</div>` : ""}
      ${r.resize_rejected_by ? `<div class="approval-decision rejected">Resize rejected by ${esc(r.resize_rejected_by)}</div>` : ""}
      ${isAdmin && ["awaiting_approval", "resize_awaiting_approval"].includes(r.status) ? `<div class="request-actions"><button class="approve" data-action="approve" data-id="${r.id}">Approve</button><button class="reject" data-action="reject" data-id="${r.id}">Reject</button></div>` : ""}
      ${lifecycleControls(r)}
      ${resizeForm(r)}
      <details data-detail-key="events-${esc(r.id)}"><summary>Workflow events (${r.events.length})</summary><ul class="timeline">${r.events.map(e => `<li>${esc(e.name)}${e.detail ? `<small>${esc(e.detail)}</small>` : ""}</li>`).join("")}</ul></details>
    </article>`).join("") : `<div class="empty">No provisioning requests yet.</div>`;
  document.querySelectorAll("#requestList details[data-detail-key]").forEach(item => {
    item.open = openDetails.has(item.dataset.detailKey);
  });
  document.querySelectorAll("[data-action]").forEach(button => button.onclick = async () => {
    const action = button.dataset.action;
    const actionButtons = button.closest(".request-actions").querySelectorAll("button");
    actionButtons.forEach(item => { item.disabled = true; });
    button.textContent = action === "approve" ? "Approving…" : "Rejecting…";
    try {
      let reason = "";
      if (action === "reject") {
        reason = window.prompt("Rejection reason (included in the email):", "") ?? "";
        if (!reason.trim()) {
          actionButtons.forEach(item => { item.disabled = false; });
          button.textContent = "Reject";
          return;
        }
      }
      await api(`/api/requests/${button.dataset.id}/${action}`, {
        method: "POST",
        body: JSON.stringify({reason}),
      });
      toast(action === "approve" ? "Request approved" : "Request rejected");
      await loadRequests();
    } catch (error) {
      toast((error.errors || [error.error || `Unable to ${action} request`]).join(" "));
      actionButtons.forEach(item => { item.disabled = false; });
      button.textContent = action === "approve" ? "Approve" : "Reject";
    }
  });
  document.querySelectorAll(".vm-operation").forEach(button => button.onclick = async () => {
    const operation = button.dataset.operation;
    let payload = {};
    if (operation === "stop" && !confirm("Shut down this VM gracefully?")) return;
    if (operation === "reboot" && !confirm("Reboot this VM?")) return;
    if (operation === "create-snapshot") {
      const name = prompt("Snapshot name:", "");
      if (!name?.trim()) return;
      const description = prompt("Description (optional):", "") ?? "";
      payload = {name: name.trim(), description: description.trim()};
    }
    if (operation === "restore-snapshot") {
      const confirmation = prompt(
        `Restore discards the VM's current active state and creates a new branch from this snapshot. Type ${button.dataset.hostname} to confirm:`,
        ""
      );
      if (confirmation !== button.dataset.hostname) return;
      payload = {
        snapshot_id: button.dataset.snapshotId,
        confirm_hostname: confirmation,
      };
    }
    if (operation === "delete-snapshot") {
      let plan;
      try {
        plan = await api(
          `/api/requests/${button.dataset.id}/snapshots/${button.dataset.snapshotId}/delete-plan`
        );
      } catch (error) {
        toast(error.error || "Unable to inspect snapshot deletion targets");
        return;
      }
      const targets = plan.delete_targets.length
        ? plan.delete_targets.map((target, index) => `${index + 1}. ${target}`).join("\n")
        : "(No files; snapshot metadata only)";
      const retained = plan.retained_disk_files.length
        ? `\n\nQCOW2 files retained for disk-chain safety:\n${plan.retained_disk_files.join("\n")}`
        : "";
      if (!confirm(
        `Delete snapshot "${plan.snapshot_name}"?\n\n` +
        `Exact deletion targets (${plan.delete_target_count}):\n${targets}${retained}\n\n${plan.warning}`
      )) return;
      const confirmation = prompt(
        `Type snapshot ID ${plan.snapshot_id} to confirm deletion:`,
        ""
      );
      if (confirmation !== plan.snapshot_id) return;
      payload = {
        snapshot_id: plan.snapshot_id,
        confirm_snapshot_id: confirmation,
        confirmed_targets: plan.delete_targets,
      };
    }
    if (operation === "cleanup-resize-snapshot") {
      let plan;
      try {
        plan = await api(
          `/api/requests/${button.dataset.id}/resize-snapshots/${button.dataset.snapshotId}/cleanup-plan`
        );
      } catch (error) {
        toast(error.error || "Unable to inspect resize snapshot cleanup targets");
        return;
      }
      const targets = plan.delete_targets.length
        ? plan.delete_targets.map((target, index) => `${index + 1}. ${target}`).join("\n")
        : "(No files)";
      if (!confirm(
        `Commit and clean up expired resize snapshot ${plan.snapshot_id}?\n\n` +
        `Disk commits: ${plan.pending_commit_disks.join(", ") || "(already committed)"}\n\n` +
        `Exact deletion targets (${plan.delete_target_count}):\n${targets}\n\n${plan.warning}`
      )) return;
      const confirmation = prompt(
        `Type snapshot ID ${plan.snapshot_id} to confirm cleanup:`,
        ""
      );
      if (confirmation !== plan.snapshot_id) return;
      payload = {
        snapshot_id: plan.snapshot_id,
        confirm_snapshot_id: confirmation,
        confirmed_targets: plan.delete_targets,
      };
    }
    if (operation === "rollback-resize") {
      const confirmation = prompt(
        `Rollback discards changes made after the snapshot. Type ${button.dataset.hostname} to confirm:`,
        ""
      );
      if (confirmation !== button.dataset.hostname) return;
      payload = {
        snapshot_id: button.dataset.snapshotId,
        confirm_hostname: confirmation,
      };
    }
    button.disabled = true;
    const original = button.textContent;
    button.textContent = `${original}…`;
    try {
      await api(`/api/requests/${button.dataset.id}/${operation}`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast(`${original} completed`);
      await loadRequests();
    } catch (error) {
      toast(error.detail || error.error || `${original} failed`);
      button.disabled = false;
      button.textContent = original;
    }
  });
  document.querySelectorAll(".vm-console").forEach(button => button.onclick = () => {
    openConsole(button.dataset.id, button.dataset.hostname);
  });
  document.querySelectorAll(".netbox-retry").forEach(button => button.onclick = async () => {
    button.disabled = true;
    try {
      await api(`/api/requests/${button.dataset.id}/register-netbox`, {method: "POST", body: "{}"});
      toast("VM registered in NetBox");
      await loadRequests();
    } catch (error) {
      toast(error.error || "NetBox registration failed");
    } finally {
      button.disabled = false;
    }
  });
  document.querySelectorAll(".vm-delete").forEach(button => button.onclick = async () => {
    const originalContent = [...button.childNodes];
    let plan;
    try {
      plan = await api(`/api/requests/${button.dataset.id}/delete-plan`);
    } catch (error) {
      toast(error.error || "Unable to inspect VM deletion targets");
      return;
    }
    if (!confirm(
      `Delete ${plan.failed_request ? "failed request" : "virtual machine"} ${plan.hostname}?\n\n` +
      `${plan.warning} This action cannot be undone.`
    )) return;
    let confirmFiles = false;
    if (plan.failed_request && plan.file_count > 0) {
      confirmFiles = confirm(`Also permanently delete ${plan.file_count} leftover VM file(s)?\n\n` +
        plan.file_names.join("\n") + "\n\nCancel keeps both the files and the request.");
      if (!confirmFiles) return;
    }
    const confirmation = prompt(`Type ${plan.hostname} to confirm deletion:`, "");
    if (confirmation !== plan.hostname) return;
    button.disabled = true;
    button.textContent = "Deleting…";
    try {
      await api(`/api/requests/${button.dataset.id}`, {
        method: "DELETE",
        body: JSON.stringify({
          confirm_hostname: confirmation,
          confirmation_token: plan.confirmation_token,
          confirm_files: confirmFiles,
        }),
      });
      toast(`${plan.failed_request ? "Failed request" : "VM"} ${plan.hostname} deleted`);
      await loadRequests();
    } catch (error) {
      button.disabled = false;
      button.replaceChildren(...originalContent);
      toast(error.detail || error.error || "VM deletion failed");
    }
  });
  document.querySelectorAll(".resize-form").forEach(form => form.onsubmit = async event => {
    event.preventDefault();
    const button = form.querySelector("button");
    const errorBox = form.querySelector(".resize-error");
    button.disabled = true;
    button.textContent = "Resizing…";
    errorBox.textContent = "";
    try {
      const data = Object.fromEntries(new FormData(form));
      data.additional_disks = [...form.querySelectorAll(".resize-data-disk")].map(row => ({
        size_gb: +row.querySelector("[data-resize-disk-size]").value
      }));
      const result = await api(`/api/requests/${form.dataset.resizeId}/resize`, {method: "POST", body: JSON.stringify(data)});
      toast(
        result.status === "resize_awaiting_approval"
          ? "Resize submitted for administrator approval"
          : "VM resized online without reboot"
      );
      await loadRequests();
    } catch (error) {
      errorBox.textContent = (error.errors || [error.error || "Online resize failed"]).join(" ");
      button.disabled = false;
      button.textContent = "Resize online";
    }
  });
}

function closeConsole() {
  $("#consoleModal").classList.add("hidden");
  $("#consoleFrame").src = "about:blank";
}

async function openConsole(requestId, hostname) {
  closeConsole();
  $("#consoleTitle").textContent = `${hostname} console`;
  $("#consoleModal").classList.remove("hidden");
  try {
    const ticket = await api(`/api/requests/${encodeURIComponent(requestId)}/console-ticket`);
    $("#consoleFrame").src =
      `/spice-html5/spice_auto.html?v=20260803-1&path=${encodeURIComponent(ticket.path)}&title=${encodeURIComponent(hostname)}`;
  } catch (error) {
    closeConsole();
    toast(error.error || "Console connection failed");
  }
}

async function init() {
  config = await api("/api/config");
  isAdmin = config.role === "admin";
  $("#signedInUser").textContent = config.username;
  document.querySelector(".aside-note small").textContent = config.provisioning_backend === "local_qemu" ? "Local QEMU backend" : "VMware vcsim connected";
  if (isAdmin) {
    $("#requestNavLabel").textContent = "Admin requests";
    $("#requestsHeading").textContent = "All provisioning requests";
    $("#requestsDescription").textContent = "Review all users' requests, approvals and workflow events.";
    document.querySelector('[data-view="catalog"]').classList.add("hidden");
    $("#userSettingsHeader").classList.add("hidden");
    $("#sshKeySettings").classList.add("hidden");
    $("#adminSettingsHeader").classList.remove("hidden");
    $("#adminPasswordSettings").classList.remove("hidden");
  }
  $("#environment").innerHTML = Object.entries(config.environments).map(([value, x]) => `<option value="${value}">${x.label}</option>`).join("");
  $("#image").innerHTML = config.images.map(x => `<option>${x}</option>`).join("");
  $("#network").innerHTML = config.networks.map(x => `<option value="${esc(x.portgroup)}">${esc(x.cidr)} (${esc(x.description)})</option>`).join("");
  $("#hostmasterNote").textContent = `${config.hostmaster.description} PoC simulation recipient: ${config.hostmaster.email}.`;
  $("#sshLoginUser").value = config.ssh_login_user;
  renderProfiles(); updateSummary(); loadRequests(); loadSshKeys();

  document.querySelectorAll(".nav-item").forEach(item => item.onclick = () => {
    document.querySelectorAll(".nav-item,.view").forEach(x => x.classList.remove("active"));
    item.classList.add("active"); $(`#${item.dataset.view}`).classList.add("active");
    if (item.dataset.view === "requests") loadRequests();
    if (item.dataset.view === "settings" && !isAdmin) loadSshKeys();
  });
  if (isAdmin) document.querySelector('[data-view="requests"]').click();
  $("#environment").onchange = updateSummary;
  $("[name=hostname]").oninput = event => {
    const hostname = event.target.value.trim().toLowerCase() || "vm-name";
    const fqdn = $("#fqdn");
    const nextAutoFqdn = event.target.value.trim() ? `${hostname}.*` : "";
    if (!fqdn.value || fqdn.value === autoFqdn) fqdn.value = nextAutoFqdn;
    autoFqdn = nextAutoFqdn;
    fqdn.placeholder = `${hostname}.*`;
  };
  $("#fqdn").onfocus = event => {
    if (event.target.value.endsWith(".*")) {
      const star = event.target.value.length - 1;
      requestAnimationFrame(() => event.target.setSelectionRange(star, star + 1));
    }
  };
  $("#customResources").oninput = updateSummary;
  $("#addAdditionalDisk").onclick = addAdditionalDisk;
  $("#sshKeyForm").onsubmit = async event => {
    event.preventDefault();
    const errors = $("#sshKeyErrors");
    errors.classList.add("hidden");
    try {
      const data = Object.fromEntries(new FormData(event.target));
      await api("/api/settings/ssh-keys", {method: "POST", body: JSON.stringify(data)});
      event.target.reset();
      $("#sshLoginUser").value = config.ssh_login_user;
      await loadSshKeys();
      toast("SSH public key saved");
    } catch (error) {
      errors.innerHTML = (error.errors || [error.error || "Unable to save key"]).map(esc).join("<br>");
      errors.classList.remove("hidden");
    }
  };
  $("#adminPasswordForm").onsubmit = async event => {
    event.preventDefault();
    const button = event.target.querySelector("button");
    const errors = $("#adminPasswordErrors");
    errors.classList.add("hidden");
    button.disabled = true;
    button.textContent = "Changing…";
    try {
      const data = Object.fromEntries(new FormData(event.target));
      await api("/api/admin/settings/password", {
        method: "POST",
        body: JSON.stringify(data)
      });
      event.target.reset();
      toast("Administrator password changed");
    } catch (error) {
      errors.innerHTML = (error.errors || [error.error || "Unable to change password"])
        .map(esc).join("<br>");
      errors.classList.remove("hidden");
    } finally {
      button.disabled = false;
      button.textContent = "Change password";
    }
  };
  $("#refresh").onclick = loadRequests;
  $("#consoleClose").onclick = closeConsole;
  $("#consoleModal").onclick = event => {
    if (event.target.id === "consoleModal") closeConsole();
  };
  $("#logout").onclick = async () => {
    await api("/api/auth/logout", {method: "POST", body: "{}"});
    location.href = "/login.html";
  };
  $("#requestForm").onsubmit = async event => {
    event.preventDefault();
    const submit = $("#submitRequest");
    if (submit.disabled) return;
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    submit.innerHTML = "Submitting… <span>●</span>";
    $("#approvalSummary").textContent = "Request is being validated and submitted. Please wait.";
    $("#errors").classList.add("hidden");
    const data = Object.fromEntries(new FormData(event.target));
    data.profile = selectedProfile;
    data.additional_disks = additionalDisksFromForm();
    try {
      const result = await api("/api/requests", {method: "POST", body: JSON.stringify(data)});
      toast(result.status === "awaiting_approval" ? "Request submitted for approval" : "Virtual machine request completed");
      document.querySelector('[data-view="requests"]').click();
    } catch (error) {
      $("#errors").innerHTML = (error.errors || [error.error || "Request failed"]).map(esc).join("<br>");
      $("#errors").classList.remove("hidden");
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
      submit.innerHTML = "Submit request <span>→</span>";
      updateSummary();
    }
  };
}

init().catch(error => { console.error(error); toast("Portal API is unavailable"); });
setInterval(() => {
  const requestsView = $("#requests");
  const editingResize = document.querySelector("#requestList .resize-panel[open]");
  if (!document.hidden && requestsView?.classList.contains("active") && !editingResize) {
    loadRequests().catch(() => {});
  }
}, 10000);
