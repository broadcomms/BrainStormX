// app/admin/static/js/admin.js

class BrainStormXAdmin {
    constructor() {
        this.csrfToken = this.getCSRFToken();
        this.initializeComponents();
        this.setupEventListeners();
        this.startRealTimeUpdates();
    }

    initializeComponents() {
        this.initializeDataTables();
        this.initializeCharts();
        this.initializeModals();
    }

    initializeDataTables() {
        if (typeof window.DataTable !== "function") {
            return;
        }

        document.querySelectorAll(".data-table-enhanced").forEach((table) => {
            try {
                // eslint-disable-next-line no-new
                new window.DataTable(table, {
                    responsive: true,
                    pageLength: 25,
                    order: [[0, "desc"]],
                    columnDefs: [{ targets: "no-sort", orderable: false }],
                });
            } catch (error) {
                console.warn("Failed to initialise DataTable", error);
            }
        });
    }

    initializeCharts() {
        // Placeholder for future chart widgets. No-op to avoid runtime errors.
    }

    initializeModals() {
        // Placeholder for future modal/dialog wiring.
    }

    setupEventListeners() {
        // Hook for future DOM event bindings. Currently a no-op.
    }

    startRealTimeUpdates() {
        // Dashboard-specific realtime updates handled in admin-dashboard.js.
    }

    async performUserAction(action, userId, data = {}) {
        try {
            const response = await fetch(`/admin/api/users/${userId}/${action}`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": this.csrfToken,
                },
                body: JSON.stringify(data),
                credentials: "same-origin",
            });

            if (!response.ok) {
                const message = await response.text();
                throw new Error(message || "Action failed");
            }

            this.showNotification("Action completed successfully", "success");
            this.refreshUserTable();
        } catch (error) {
            this.showNotification(`Action failed: ${error.message}`, "error");
        }
    }

    refreshUserTable() {
        const table = document.querySelector(".users-table");
        if (table && typeof table.reload === "function") {
            table.reload();
        } else {
            window.location.reload();
        }
    }

    getCSRFToken() {
        const meta = document.querySelector("meta[name='csrf-token']");
        if (meta) {
            return meta.getAttribute("content") || "";
        }

        const input = document.querySelector("input[name='csrf_token']");
        if (input) {
            return input.value || "";
        }

        return "";
    }

    showNotification(message, type = "info") {
        const toneMap = {
            success: "success",
            error: "danger",
            warning: "warning",
            info: "info",
        };
        const tone = toneMap[type] || "info";
        const container = document.getElementById("admin-notification-area");

        if (container && window.bootstrap && typeof window.bootstrap.Toast === "function") {
            const toast = document.createElement("div");
            toast.className = `toast align-items-center text-bg-${tone} border-0 shadow`;
            toast.setAttribute("role", "alert");
            toast.setAttribute("aria-live", "assertive");
            toast.setAttribute("aria-atomic", "true");
            toast.dataset.bsDelay = "4000";

            const layout = document.createElement("div");
            layout.className = "d-flex";

            const body = document.createElement("div");
            body.className = "toast-body";
            body.textContent = message;

            const closeButton = document.createElement("button");
            closeButton.type = "button";
            closeButton.className = "btn-close btn-close-white me-2 m-auto";
            closeButton.setAttribute("data-bs-dismiss", "toast");
            closeButton.setAttribute("aria-label", "Close");

            layout.appendChild(body);
            layout.appendChild(closeButton);
            toast.appendChild(layout);

            container.appendChild(toast);
            try {
                const toastInstance = new window.bootstrap.Toast(toast);
                toast.addEventListener("hidden.bs.toast", () => {
                    toast.remove();
                });
                toastInstance.show();
            } catch (error) {
                console.warn("Unable to display toast notification", error);
                toast.remove();
            }
            return;
        }

    const fallback = document.createElement("div");
    fallback.className = `alert alert-${tone} position-fixed top-0 end-0 m-3 shadow fade show`;
        fallback.style.zIndex = "1080";
        fallback.textContent = message;
        document.body.appendChild(fallback);

        window.setTimeout(() => {
            fallback.classList.remove("show");
            window.setTimeout(() => fallback.remove(), 300);
        }, 4000);
    }
}

document.addEventListener("DOMContentLoaded", () => {
    window.adminApp = new BrainStormXAdmin();
});