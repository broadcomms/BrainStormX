/* global Chart */

(function () {
	"use strict";

	const configElement = document.getElementById("admin-dashboard-config");
	if (!configElement) {
		return;
	}

	let config;
	try {
		config = JSON.parse(configElement.textContent || "{}");
	} catch (error) {
		console.error("Unable to parse admin dashboard config", error);
		return;
	}

	const dashboardRoot = document.querySelector("[data-metrics-endpoint]");
	if (!dashboardRoot || !config.metricsEndpoint) {
		return;
	}

	const metricElements = new Map();
	dashboardRoot.querySelectorAll("[data-metric]").forEach((element) => {
		const key = element.dataset.metric;
		if (key) {
			metricElements.set(key, element);
		}
	});

	const healthElements = {
		timestamp: document.getElementById("health-timestamp"),
		cpu: document.getElementById("health-cpu"),
		memory: document.getElementById("health-mem"),
		disk: document.getElementById("health-disk"),
		dbActive: document.getElementById("health-db-active"),
		dbPool: document.getElementById("health-db-pool"),
	};

	const logsBody = document.getElementById("recent-logs");
	const knownLogIds = new Set(Array.isArray(config.recentLogIds) ? config.recentLogIds : []);

	const intervalMs = Math.max(5000, (config.refreshIntervalSeconds || 30) * 1000);
	const intFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
	const decimalFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
	const percentFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
	const timeFormatter = new Intl.DateTimeFormat(undefined, {
		year: "numeric",
		month: "2-digit",
		day: "2-digit",
		hour: "2-digit",
		minute: "2-digit",
		second: "2-digit",
	});

	const chartCanvas = document.getElementById("admin-metrics-chart");
	const trendSeries = [];
	const trendMaxPoints = 20;
	let metricsChart = null;
	let refreshTimer = null;
	let isRefreshing = false;

	function asNumber(value) {
		if (typeof value === "number") {
			return value;
		}
		const numeric = Number(value);
		return Number.isFinite(numeric) ? numeric : 0;
	}

	function getValueByPath(source, path) {
		if (!source) {
			return undefined;
		}
		return path.split(".").reduce((acc, segment) => {
			if (acc && Object.prototype.hasOwnProperty.call(acc, segment)) {
				return acc[segment];
			}
			return undefined;
		}, source);
	}

	function formatNumber(value) {
		if (typeof value === "number") {
			return Number.isInteger(value) ? intFormatter.format(value) : decimalFormatter.format(value);
		}
		if (value === null || value === undefined) {
			return "—";
		}
		return String(value);
	}

	function setText(target, text) {
		if (target) {
			target.textContent = text;
		}
	}

	function percentText(value) {
		const numeric = typeof value === "number" ? value : Number(value);
		if (Number.isFinite(numeric)) {
			return `${percentFormatter.format(numeric)}%`;
		}
		return "0.0%";
	}

	function formatTimestamp(value) {
		if (!value) {
			return "—";
		}
		const date = value instanceof Date ? value : new Date(value);
		if (Number.isNaN(date.getTime())) {
			return "—";
		}
		return timeFormatter.format(date);
	}

	function updateMetricValues(metrics) {
		if (!metrics) {
			return;
		}
		metricElements.forEach((element, key) => {
			const raw = getValueByPath(metrics, key);
			if (raw !== undefined) {
				element.textContent = formatNumber(raw);
			}
		});
	}

	function updateHealth(health) {
		if (!health) {
			return;
		}
		setText(healthElements.timestamp, formatTimestamp(health.timestamp));

		const system = health.system || {};
		const memory = system.memory || {};
		const disk = system.disk || {};
		const database = health.database || {};

		setText(healthElements.cpu, percentText(system.cpu_percent));
		setText(healthElements.memory, percentText(memory.percent));
		setText(healthElements.disk, percentText(disk.percent));
		setText(healthElements.dbActive, formatNumber(database.active_connections));
		setText(healthElements.dbPool, formatNumber(database.connection_pool));
	}

	function buildEmptyLogsRow() {
		const row = document.createElement("tr");
		const cell = document.createElement("td");
		cell.colSpan = 4;
		cell.className = "text-center text-body-secondary py-3";
		cell.textContent = "No admin events recorded yet.";
		row.appendChild(cell);
		return row;
	}

	function updateRecentLogs(logs) {
		if (!logsBody) {
			return;
		}
		logsBody.innerHTML = "";

		if (!Array.isArray(logs) || logs.length === 0) {
			logsBody.appendChild(buildEmptyLogsRow());
			return;
		}

		logs.forEach((log) => {
			if (log && typeof log.id !== "undefined") {
				knownLogIds.add(log.id);
			}

			const row = document.createElement("tr");

			const whenCell = document.createElement("td");
			whenCell.setAttribute("data-label", "When");
			whenCell.textContent = formatTimestamp(log?.created_at);

			const adminCell = document.createElement("td");
			adminCell.setAttribute("data-label", "Admin");
			adminCell.textContent = log?.actor_name || (log?.actor_id ? `User #${log.actor_id}` : "System");

			const actionCell = document.createElement("td");
			actionCell.setAttribute("data-label", "Action");
			actionCell.textContent = log?.action || "—";

			const targetCell = document.createElement("td");
			targetCell.setAttribute("data-label", "Target");
			const type = log?.entity_type || "";
			const identifier = log?.entity_id || "";
			targetCell.textContent = (type || identifier) ? `${type} ${identifier}`.trim() : "—";

			row.appendChild(whenCell);
			row.appendChild(adminCell);
			row.appendChild(actionCell);
			row.appendChild(targetCell);
			logsBody.appendChild(row);
		});
	}

	function addTrendPoint(value, timestamp) {
		if (!Number.isFinite(value)) {
			return;
		}
		const point = { value, timestamp };
		trendSeries.push(point);
		if (trendSeries.length > trendMaxPoints) {
			trendSeries.shift();
		}

		if (metricsChart) {
			metricsChart.data.labels = trendSeries.map((entry) => formatTimestamp(entry.timestamp));
			metricsChart.data.datasets[0].data = trendSeries.map((entry) => entry.value);
			metricsChart.update("none");
		}
	}

	function ensureChart() {
		if (!chartCanvas || typeof Chart === "undefined") {
			return;
		}
		if (!metricsChart) {
			const parent = chartCanvas.parentElement;
			if (parent) {
				const { clientHeight, clientWidth } = parent;
				if (clientHeight > 0) {
					chartCanvas.height = clientHeight;
				}
				if (clientWidth > 0) {
					chartCanvas.width = clientWidth;
				}
			}
			const context = chartCanvas.getContext("2d");
			metricsChart = new Chart(context, {
				type: "line",
				data: {
					labels: trendSeries.map((entry) => formatTimestamp(entry.timestamp)),
					datasets: [
						{
							label: "Active users (24h)",
							data: trendSeries.map((entry) => entry.value),
							borderColor: "#2563eb",
							backgroundColor: "rgba(37, 99, 235, 0.15)",
							borderWidth: 2,
							tension: 0.3,
							fill: true,
						},
					],
				},
				options: {
					responsive: true,
					maintainAspectRatio: false,
					resizeDelay: 50,
					scales: {
						x: {
							ticks: {
								maxRotation: 0,
								autoSkip: true,
							},
						},
						y: {
							beginAtZero: true,
							ticks: {
								precision: 0,
							},
						},
					},
					plugins: {
						legend: {
							display: false,
						},
						tooltip: {
							callbacks: {
								label(context) {
									const value = context.parsed.y;
									return `Active users: ${intFormatter.format(value)}`;
								},
							},
						},
					},
				},
			});
		}
	}

	async function refreshMetrics() {
		if (isRefreshing) {
			return;
		}
		isRefreshing = true;
		dashboardRoot.dataset.loading = "true";

		try {
			const response = await fetch(config.metricsEndpoint, {
				credentials: "same-origin",
				headers: { Accept: "application/json" },
			});

			if (!response.ok) {
				throw new Error(`Request failed with status ${response.status}`);
			}

			const payload = await response.json();

			if (payload.metrics) {
				updateMetricValues(payload.metrics);
				const activityValue = asNumber(getValueByPath(payload.metrics, "users.active_today"));
				const pointTime = payload.health?.timestamp || Date.now();
				addTrendPoint(activityValue, new Date(pointTime));
				ensureChart();
			}
			if (payload.health) {
				updateHealth(payload.health);
			}
			if (payload.recent_logs) {
				updateRecentLogs(payload.recent_logs);
			}

			delete dashboardRoot.dataset.error;
		} catch (error) {
			console.error("Admin dashboard refresh failed", error);
			dashboardRoot.dataset.error = "true";
		} finally {
			delete dashboardRoot.dataset.loading;
			isRefreshing = false;
		}
	}

	function startAutoRefresh() {
		if (refreshTimer) {
			clearInterval(refreshTimer);
		}
		refreshTimer = setInterval(refreshMetrics, intervalMs);
	}

	function stopAutoRefresh() {
		if (refreshTimer) {
			clearInterval(refreshTimer);
			refreshTimer = null;
		}
	}

	document.addEventListener("visibilitychange", () => {
		if (document.hidden) {
			stopAutoRefresh();
		} else {
			startAutoRefresh();
			refreshMetrics();
		}
	});

	// Seed UI with initial payload embedded in the template.
	if (config.initialMetrics) {
		updateMetricValues(config.initialMetrics);
		const initialValue = asNumber(getValueByPath(config.initialMetrics, "users.active_today"));
		const initialTimestamp = config.initialHealth?.timestamp || Date.now();
		addTrendPoint(initialValue, new Date(initialTimestamp));
	}
	if (config.initialHealth) {
		updateHealth(config.initialHealth);
	}
	ensureChart();

	startAutoRefresh();
	refreshMetrics();
})();
