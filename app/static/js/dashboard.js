document.addEventListener('DOMContentLoaded', () => {
    // 1. Fetch Metrics Data
    fetchMetrics();

    // 2. Setup Live Demo Button
    const runBtn = document.getElementById('btn-run-demo');
    if (runBtn) {
        runBtn.addEventListener('click', runLiveDemo);
    }
});

async function fetchMetrics() {
    try {
        // Fetch from the API (with a default header if needed, but here we assume no auth or standard auth)
        // Adjust endpoint as necessary. We will define this in our dashboard.py router.
        const response = await fetch('/api/v1/metrics/history');
        if (!response.ok) throw new Error('Failed to fetch metrics');
        
        const data = await response.json();
        
        // Update top metrics with animation
        const prsEl = document.getElementById('metric-prs');
        const tokensEl = document.getElementById('metric-tokens');
        const costEl = document.getElementById('metric-cost');
        const latencyEl = document.getElementById('metric-latency');

        animateValue(prsEl, 0, data.totals.prs, 1500, (val) => Math.floor(val));
        animateValue(tokensEl, 0, data.totals.tokens, 1500, (val) => Math.floor(val).toLocaleString());
        animateValue(costEl, 0, data.totals.cost, 1500, (val) => '$' + val.toFixed(2));
        animateValue(latencyEl, 0, data.totals.latency, 1500, (val) => Math.floor(val) + 'ms');

        // Render Charts
        renderActivityChart(data.history);
        renderFindingsChart(data.findings);
    } catch (error) {
        console.error("Error loading metrics:", error);
    }
}

// Utility function to animate numbers counting up
function animateValue(obj, start, end, duration, formatter) {
    let startTimestamp = null;
    const step = (timestamp) => {
        if (!startTimestamp) startTimestamp = timestamp;
        const progress = Math.min((timestamp - startTimestamp) / duration, 1);
        
        // easeOutQuart easing function for a smooth finish
        const easeProgress = 1 - Math.pow(1 - progress, 4);
        const current = easeProgress * (end - start) + start;
        
        obj.innerHTML = formatter(current);
        
        if (progress < 1) {
            window.requestAnimationFrame(step);
        } else {
            obj.innerHTML = formatter(end); // ensure exact final value
        }
    };
    window.requestAnimationFrame(step);
}

function renderActivityChart(historyData) {
    const ctx = document.getElementById('activityChart').getContext('2d');
    
    // Prepare data
    const labels = historyData.map(d => d.date);
    const prsData = historyData.map(d => d.prs);
    
    new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'PRs Reviewed',
                data: prsData,
                borderColor: '#00f0ff',
                backgroundColor: 'rgba(0, 240, 255, 0.1)',
                borderWidth: 2,
                tension: 0.4,
                fill: true
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: '#9ca3af' }
                },
                x: {
                    grid: { display: false },
                    ticks: { color: '#9ca3af', maxTicksLimit: 7 }
                }
            }
        }
    });
}

function renderFindingsChart(findingsData) {
    const ctx = document.getElementById('findingsChart').getContext('2d');
    
    new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['Security', 'Style', 'Logic', 'Test'],
            datasets: [{
                data: [
                    findingsData.security,
                    findingsData.style,
                    findingsData.logic,
                    findingsData.test
                ],
                backgroundColor: [
                    '#ef4444', // Red
                    '#8a2be2', // Purple
                    '#fbbf24', // Yellow
                    '#10b981'  // Green
                ],
                borderWidth: 0,
                hoverOffset: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'right',
                    labels: {
                        color: '#ffffff',
                        padding: 20,
                        font: { family: 'Inter', size: 12 }
                    }
                }
            },
            cutout: '75%'
        }
    });
}

// Live Demo SSE handling
let eventSource = null;

function runLiveDemo() {
    const code = document.getElementById('demo-code').value;
    const btn = document.getElementById('btn-run-demo');
    const statusBadge = document.getElementById('demo-status');
    const consoleDiv = document.getElementById('sse-stream');
    
    if (!code.trim()) return;

    // Reset UI
    btn.disabled = true;
    statusBadge.className = 'status-badge running';
    statusBadge.textContent = 'Running';
    consoleDiv.innerHTML = '';
    appendLog('system', 'Initializing LangGraph multi-agent workflow...');

    if (eventSource) {
        eventSource.close();
    }

    // Since SSE is GET only via browser EventSource, we can pass code via query param 
    // OR we do a POST to start the job, and then subscribe to SSE with a job ID.
    // For simplicity in this demo, let's assume we do a POST and then fetch a stream.
    
    startDemoStream(code, consoleDiv, statusBadge, btn);
}

async function startDemoStream(code, consoleDiv, statusBadge, btn) {
    try {
        // Step 1: POST the code to get a task ID
        const response = await fetch('/api/v1/demo/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: code })
        });
        
        if (!response.ok) throw new Error("Failed to start demo");
        
        const data = await response.json();
        const taskId = data.task_id;
        
        appendLog('system', `Task ${taskId} enqueued. Connecting to stream...`);
        
        // Step 2: Subscribe to SSE stream
        eventSource = new EventSource(`/api/v1/demo/stream?task_id=${taskId}`);
        
        eventSource.onmessage = function(event) {
            const msg = JSON.parse(event.data);
            
            if (msg.type === 'step') {
                appendLog('agent', `[${msg.agent}] ${msg.message}`);
            } else if (msg.type === 'finding') {
                appendLog('finding', `⚠️ Found: ${msg.message}`);
            } else if (msg.type === 'complete') {
                appendLog('result', '✅ Review completed successfully!');
                
                statusBadge.className = 'status-badge complete';
                statusBadge.textContent = 'Complete';
                btn.disabled = false;
                eventSource.close();
            } else if (msg.type === 'error') {
                appendLog('system', `❌ Error: ${msg.message}`);
                statusBadge.className = 'status-badge error';
                statusBadge.textContent = 'Error';
                btn.disabled = false;
                eventSource.close();
            }
        };
        
        eventSource.onerror = function() {
            appendLog('system', 'Connection lost or stream ended.');
            statusBadge.className = 'status-badge idle';
            statusBadge.textContent = 'Idle';
            btn.disabled = false;
            eventSource.close();
        };
        
    } catch (e) {
        appendLog('system', `Error: ${e.message}`);
        statusBadge.className = 'status-badge error';
        statusBadge.textContent = 'Error';
        btn.disabled = false;
    }
}

function appendLog(type, message) {
    const consoleDiv = document.getElementById('sse-stream');
    const line = document.createElement('div');
    line.className = `console-line ${type}`;
    line.textContent = message;
    consoleDiv.appendChild(line);
    // Auto-scroll to bottom
    consoleDiv.scrollTop = consoleDiv.scrollHeight;
}
