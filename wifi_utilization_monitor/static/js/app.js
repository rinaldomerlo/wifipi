// WiFi Spectrum Analyzer - Frontend Controller

// Application State
const state = {
    interfaces: [],
    selectedInterface: '',
    refreshRate: 5000, // ms
    isPaused: false,
    isScanning: false,
    countdown: 0,
    countdownIntervalId: null,
    lastStatusMessage: 'System Idle',
    lastStatusClass: 'idle',
    currentScanData: [],
    meta: null,
    activeBand: 'all',
    activeTab: 'spectrum-tab',
    highlightedBssid: null,
    lockedBssid: null,
    refreshIntervalId: null,
    canvasElements: {},
    canvasContexts: {}
};

// Frequency map constants
const BANDS = {
    '2.4GHz': { freqMin: 2400, freqMax: 2495, chanMin: 1, chanMax: 14 },
    '5GHz': { freqMin: 5150, freqMax: 5900, chanMin: 36, chanMax: 177 },
    '6GHz': { freqMin: 5925, freqMax: 7125, chanMin: 1, chanMax: 233 }
};

// 2.4 GHz channel to frequency mapping
const chanToFreq24 = {
    1: 2412, 2: 2417, 3: 2422, 4: 2427, 5: 2432, 6: 2437,
    7: 2442, 8: 2447, 9: 2452, 10: 2457, 11: 2462, 12: 2467,
    13: 2472, 14: 2484
};

// Helper: Get random color based on SSID/BSSID string
function getApColor(ssid, bssid, opacity = 1.0) {
    let hash = 0;
    const str = ssid || bssid || 'Unknown';
    for (let i = 0; i < str.length; i++) {
        hash = str.charCodeAt(i) + ((hash << 5) - hash);
    }
    const hue = Math.abs(hash) % 360;
    // Vary saturation and lightness slightly based on hash for richer palette
    const sat = 75 + (Math.abs(hash >> 8) % 15); // 75-90%
    const light = 50 + (Math.abs(hash >> 16) % 15); // 50-65%
    return `hsla(${hue}, ${sat}%, ${light}%, ${opacity})`;
}

// Helper: Format signal strength as class
function getSignalClass(sig) {
    if (sig >= -60) return 'strong';
    if (sig >= -75) return 'mid';
    return 'weak';
}

// Document Ready
document.addEventListener('DOMContentLoaded', () => {
    initElements();
    setupEventListeners();
    loadInterfaces().then(() => {
        // Trigger initial scan
        triggerScan();
        startAutoRefresh();
    });
});

// Initialize DOM elements and sizes
function initElements() {
    // Cache canvas references
    state.canvasElements = {
        'chart24': document.getElementById('chart24'),
        'chart5': document.getElementById('chart5'),
        'chart6': document.getElementById('chart6'),
        'chartUtil24': document.getElementById('chartUtil24'),
        'chartUtil5': document.getElementById('chartUtil5'),
        'chartUtil6': document.getElementById('chartUtil6')
    };

    // Get contexts and enable high-DPI scaling
    for (const [id, canvas] of Object.entries(state.canvasElements)) {
        if (canvas) {
            state.canvasContexts[id] = canvas.getContext('2d');
            resizeCanvas(canvas);
        }
    }

    // Set initial band visibility based on state.activeBand
    updateBandVisibility();

    // Set resize listener to re-draw canvas when screen size changes
    window.addEventListener('resize', () => {
        for (const [id, canvas] of Object.entries(state.canvasElements)) {
            if (canvas) {
                resizeCanvas(canvas);
            }
        }
        renderCharts();
    });
}

// Adjust canvas resolution for Retina displays
function resizeCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
}

// Set up UI listeners
function setupEventListeners() {
    // Scan triggers
    document.getElementById('scanBtn').addEventListener('click', () => {
        triggerScan();
    });

    // Pause/Resume button
    const pauseBtn = document.getElementById('pauseBtn');
    pauseBtn.addEventListener('click', () => {
        state.isPaused = !state.isPaused;
        if (state.isPaused) {
            pauseBtn.classList.add('active');
            pauseBtn.innerHTML = '<i class="fa-solid fa-play"></i>';
            stopAutoRefresh();
            updateStatusText('Auto-refresh paused', 'idle');
        } else {
            pauseBtn.classList.remove('active');
            pauseBtn.innerHTML = '<i class="fa-solid fa-pause"></i>';
            triggerScan();
            startAutoRefresh();
        }
    });

    // Interface selection
    const ifaceSelect = document.getElementById('ifaceSelect');
    ifaceSelect.addEventListener('change', (e) => {
        state.selectedInterface = e.target.value;
        localStorage.setItem('wifi_iface', state.selectedInterface);
        triggerScan();
    });

    // Refresh rate selection
    const refreshSelect = document.getElementById('refreshSelect');
    refreshSelect.addEventListener('change', (e) => {
        const val = e.target.value;
        if (val === 'manual') {
            state.refreshRate = null;
            stopAutoRefresh();
        } else {
            state.refreshRate = parseInt(val, 10);
            if (!state.isPaused) {
                startAutoRefresh();
            }
        }
    });

    // Tab switching
    const tabButtons = document.querySelectorAll('.tab-title');
    tabButtons.forEach(btn => {
        btn.addEventListener('click', (e) => {
            tabButtons.forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            
            const targetTab = btn.getAttribute('data-tab');
            btn.classList.add('active');
            document.getElementById(targetTab).classList.add('active');
            
            state.activeTab = targetTab;
            
            // Adjust canvas sizes and re-render
            setTimeout(() => {
                for (const [id, canvas] of Object.entries(state.canvasElements)) {
                    if (canvas && canvas.offsetParent !== null) {
                        resizeCanvas(canvas);
                    }
                }
                renderCharts();
            }, 50);
        });
    });

    // Band selection filtering
    const bandBtns = document.querySelectorAll('.band-btn');
    bandBtns.forEach(btn => {
        btn.addEventListener('click', (e) => {
            bandBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            
            state.activeBand = btn.getAttribute('data-band');
            updateBandVisibility();
            renderCharts();
            renderTable();
        });
    });

    // Search and filters for details table
    document.getElementById('tableSearch').addEventListener('input', () => {
        renderTable();
    });
    
    document.getElementById('tableFilterSecurity').addEventListener('change', () => {
        renderTable();
    });

    // Hover interactions on Canvas elements
    setupCanvasHover('chart24', '2.4GHz', 'tooltip24');
    setupCanvasHover('chart5', '5GHz', 'tooltip5');
    setupCanvasHover('chart6', '6GHz', 'tooltip6');
    setupCanvasHover('chartUtil24', '2.4GHz', 'tooltipUtil24');
    setupCanvasHover('chartUtil5', '5GHz', 'tooltipUtil5');
    setupCanvasHover('chartUtil6', '6GHz', 'tooltipUtil6');

    // Click outside table or canvas to clear highlights
    document.addEventListener('click', (e) => {
        if (!e.target.closest('#apTable') && 
            !e.target.closest('canvas') && 
            !e.target.closest('.band-btn')) {
            state.lockedBssid = null;
            state.highlightedBssid = null;
            renderCharts();
            renderTable();
        }
    });

    // Esc key clears highlights
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            state.lockedBssid = null;
            state.highlightedBssid = null;
            renderCharts();
            renderTable();
        }
    });
}

// Set up mouse interaction listeners for canvas maps
function setupCanvasHover(canvasId, bandName, tooltipId) {
    const canvas = state.canvasElements[canvasId];
    const tooltip = document.getElementById(tooltipId);
    if (!canvas || !tooltip) return;

    canvas.addEventListener('mousemove', (e) => {
        if (state.currentScanData.length === 0) return;

        const rect = canvas.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;

        // Perform mathematical hit testing on curves/bars
        let hit = null;
        
        if (canvasId.startsWith('chartUtil')) {
            hit = hitTestUtilization(mouseX, mouseY, rect.width, rect.height, bandName);
        } else {
            hit = hitTestSpectrum(mouseX, mouseY, rect.width, rect.height, bandName);
        }

        if (hit) {
            if (canvasId.startsWith('chartUtil')) {
                state.highlightedBssid = null;
                canvas.style.cursor = 'pointer';
                const percent = Math.round((hit.util / 255) * 100);
                tooltip.innerHTML = `
                    <div class="tt-title">Channel ${hit.channel} (${hit.band})</div>
                    <strong>Utilization:</strong> ${percent}%<br>
                    <strong>AP Count:</strong> ${hit.apCount} APs<br>
                    <strong>Type:</strong> ${hit.utilType}
                `;
            } else {
                state.highlightedBssid = hit.record.bssid;
                canvas.style.cursor = 'pointer';
                const rec = hit.record;
                tooltip.innerHTML = `
                    <div class="tt-title">${rec.ssid || '<i>Hidden SSID</i>'}</div>
                    <strong>BSSID:</strong> ${rec.bssid}<br>
                    <strong>Signal:</strong> ${rec.signal_dbm} dBm<br>
                    <strong>Channel:</strong> ${rec.channel} (${rec.channel_width_mhz} MHz)<br>
                    <strong>Security:</strong> ${rec.security}<br>
                    <strong>Freq:</strong> ${rec.freq_mhz} MHz
                `;
            }

            // Show tooltip to compute bounding dimensions
            tooltip.style.display = 'block';

            const tooltipRect = tooltip.getBoundingClientRect();
            const containerRect = rect; // canvas rect

            let left = mouseX + 15;
            let top = mouseY + 15;

            // Adjust height if tooltip runs off the bottom of the graph container
            if (top + tooltipRect.height > containerRect.height) {
                top = mouseY - tooltipRect.height - 15;
            }
            // Clamp top to stay within container bounds
            top = Math.max(5, Math.min(top, containerRect.height - tooltipRect.height - 5));

            // Adjust width if tooltip runs off the right edge of the graph container
            if (left + tooltipRect.width > containerRect.width) {
                left = mouseX - tooltipRect.width - 15;
            }
            // Clamp left to stay within container bounds
            left = Math.max(5, Math.min(left, containerRect.width - tooltipRect.width - 5));

            tooltip.style.left = `${left}px`;
            tooltip.style.top = `${top}px`;

            // Sync with other displays
            renderCharts();
            if (!canvasId.startsWith('chartUtil')) {
                highlightTableRow(state.highlightedBssid);
            }
        } else {
            state.highlightedBssid = null;
            canvas.style.cursor = 'crosshair';
            tooltip.style.display = 'none';
            
            renderCharts();
            // Fall back to locked highlight if present
            if (state.lockedBssid) {
                highlightTableRow(state.lockedBssid);
            } else {
                clearTableRowHighlights();
            }
        }
    });

    canvas.addEventListener('mouseleave', () => {
        tooltip.style.display = 'none';
        state.highlightedBssid = null;
        renderCharts();
        if (state.lockedBssid) {
            highlightTableRow(state.lockedBssid);
        } else {
            clearTableRowHighlights();
        }
    });

    canvas.addEventListener('click', (e) => {
        if (state.currentScanData.length === 0) return;
        const rect = canvas.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;
        
        let hit = null;
        if (!canvasId.startsWith('chartUtil')) {
            hit = hitTestSpectrum(mouseX, mouseY, rect.width, rect.height, bandName);
        }

        if (hit) {
            state.lockedBssid = state.lockedBssid === hit.record.bssid ? null : hit.record.bssid;
            renderCharts();
            renderTable();
            if (state.lockedBssid) {
                // Scroll the matching row into view
                const row = document.querySelector(`[data-bssid="${state.lockedBssid}"]`);
                if (row) {
                    row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }
            }
        } else {
            state.lockedBssid = null;
            renderCharts();
            renderTable();
        }
    });
}

// Resolve channel grid boundaries and details for rendering
const chartLayout = {
    paddingLeft: 40,
    paddingRight: 30,
    paddingTop: 30,
    paddingBottom: 40
};

// Hit-testing implementation for Spectrum Curves (parabolic projection)
function hitTestSpectrum(mx, my, w, h, bandName) {
    const band = BANDS[bandName];
    const records = state.currentScanData.filter(r => r.band === bandName && r.channel !== null);
    
    const cw = w - chartLayout.paddingLeft - chartLayout.paddingRight;
    const ch = h - chartLayout.paddingTop - chartLayout.paddingBottom;
    const yBottom = chartLayout.paddingTop + ch;

    // Map frequency/signal coordinates to pixel boundaries
    const getX = (freq) => chartLayout.paddingLeft + (freq - band.freqMin) / (band.freqMax - band.freqMin) * cw;
    const getY = (dbm) => chartLayout.paddingTop + (dbm - (-20)) / ((-100) - (-20)) * ch;

    let closestHit = null;
    let minDistance = 999999;

    records.forEach(rec => {
        const centerFreq = rec.freq_mhz || 2412; // fallback
        const width = rec.channel_width_mhz || 20;
        const signal = rec.signal_dbm || -95;

        const xCenter = getX(centerFreq);
        const xStart = getX(centerFreq - width / 2);
        const xEnd = getX(centerFreq + width / 2);
        const yPeak = getY(signal);

        // Standard dome check using parabolic height curve: y = peak + (bottom-peak)*ratio^2
        if (mx >= xStart && mx <= xEnd) {
            const rx = (mx - xCenter) / ((xEnd - xStart) / 2); // -1 to +1
            const yCurve = yPeak + (yBottom - yPeak) * (rx * rx); // height at mx
            
            // Check if cursor is under the curve
            if (my >= yCurve && my <= yBottom) {
                // If cursor is under multiple overlapping curves, select the one whose curve is closest to the cursor Y position
                const dist = Math.abs(my - yCurve);
                if (dist < minDistance) {
                    minDistance = dist;
                    closestHit = { record: rec };
                }
            }
        }
    });

    return closestHit;
}

// Hit-testing implementation for Utilization Bar chart
function hitTestUtilization(mx, my, w, h, bandName) {
    // Utilization logic depends on how many bars are drawn
    const activeChannels = getActiveChannelsForBand(bandName);
    if (activeChannels.length === 0) return null;

    const cw = w - chartLayout.paddingLeft - chartLayout.paddingRight;
    const ch = h - chartLayout.paddingTop - chartLayout.paddingBottom;
    const barWidth = Math.min(45, (cw / activeChannels.length) * 0.7);
    const spacing = (cw / activeChannels.length);

    for (let i = 0; i < activeChannels.length; i++) {
        const chanObj = activeChannels[i];
        const xCenter = chartLayout.paddingLeft + (i * spacing) + (spacing / 2);
        const xStart = xCenter - barWidth / 2;
        const xEnd = xCenter + barWidth / 2;

        const yBottom = chartLayout.paddingTop + ch;
        const pct = chanObj.util / 255;
        const yTop = yBottom - (pct * ch);

        if (mx >= xStart && mx <= xEnd && my >= yTop && my <= yBottom) {
            return {
                channel: chanObj.channel,
                band: chanObj.band,
                util: chanObj.util,
                apCount: chanObj.apCount,
                utilType: chanObj.utilType
            };
        }
    }
    return null;
}

// Retrieve wireless interfaces from API
async function loadInterfaces() {
    try {
        const response = await fetch('/api/interfaces');
        const data = await response.json();
        state.interfaces = data.interfaces || [];
        
        const select = document.getElementById('ifaceSelect');
        select.innerHTML = '';
        
        state.interfaces.forEach(iface => {
            const option = document.createElement('option');
            option.value = iface;
            option.textContent = iface;
            select.appendChild(option);
        });

        // Restore saved preference if possible
        const saved = localStorage.getItem('wifi_iface');
        if (saved && state.interfaces.includes(saved)) {
            state.selectedInterface = saved;
            select.value = saved;
        } else if (state.interfaces.length > 0) {
            state.selectedInterface = state.interfaces[0];
            select.value = state.selectedInterface;
        }
    } catch (e) {
        console.error('Failed to load interfaces', e);
        updateStatusText('Failed to check interfaces', 'idle');
    }
}

// Trigger scan endpoint
async function triggerScan() {
    const scanBtn = document.getElementById('scanBtn');
    
    state.isScanning = true;
    // Set UI loading state
    scanBtn.disabled = true;
    scanBtn.innerHTML = '<svg class="spinner" viewBox="0 0 50 50"><circle class="path" cx="25" cy="25" r="20" fill="none" stroke-width="5"></circle></svg> Scanning...';
    
    updateStatusText(`Scanning wireless spectrum on ${state.selectedInterface || 'interface'}...`, 'scanning');
    
    try {
        const url = `/api/scan?interface=${encodeURIComponent(state.selectedInterface)}&source=auto`;
        const response = await fetch(url);
        const data = await response.json();
        
        if (data.success) {
            state.currentScanData = data.records || [];
            state.meta = data.meta || null;
            
            // 6 GHz band tab is always available
            document.getElementById('btnBand6').style.display = 'inline-block';
            
            updateDashboardMetrics();
            renderCharts();
            renderTable();
            
            // Set dynamic status details
            const modeLabel = state.meta.data_source.toLowerCase().includes('mock') ? 'mock' : 'live';
            const suffix = state.meta.fallback ? ' (Fallback)' : '';
            state.lastStatusMessage = `${state.meta.data_source}${suffix}`;
            state.lastStatusClass = modeLabel;
            
            resetCountdown();
            updateStatusWithCountdown();
        } else {
            console.error('Scan API failed', data.error);
            state.lastStatusMessage = `Scan Error: ${data.error}`;
            state.lastStatusClass = 'idle';
            updateStatusText(state.lastStatusMessage, state.lastStatusClass);
        }
    } catch (e) {
        console.error('Scan request failed', e);
        state.lastStatusMessage = 'Scan request failed. Server offline?';
        state.lastStatusClass = 'idle';
        updateStatusText(state.lastStatusMessage, state.lastStatusClass);
    } finally {
        state.isScanning = false;
        // Reset loading button state
        scanBtn.disabled = false;
        scanBtn.innerHTML = '<i class="fa-solid fa-radar"></i> Scan Now';
    }
}

// Periodically run scans and countdown ticker
function startAutoRefresh() {
    stopAutoRefresh();
    if (state.refreshRate) {
        resetCountdown();
        state.refreshIntervalId = setInterval(() => {
            if (!state.isPaused && !state.isScanning) {
                triggerScan();
            }
        }, state.refreshRate);

        state.countdownIntervalId = setInterval(() => {
            if (!state.isPaused && !state.isScanning && state.refreshRate) {
                if (state.countdown > 1) {
                    state.countdown--;
                    updateStatusWithCountdown();
                } else {
                    state.countdown = Math.round(state.refreshRate / 1000);
                    updateStatusWithCountdown();
                }
            }
        }, 1000);
    }
}

function stopAutoRefresh() {
    if (state.refreshIntervalId) {
        clearInterval(state.refreshIntervalId);
        state.refreshIntervalId = null;
    }
    if (state.countdownIntervalId) {
        clearInterval(state.countdownIntervalId);
        state.countdownIntervalId = null;
    }
}

function resetCountdown() {
    if (state.refreshRate) {
        state.countdown = Math.round(state.refreshRate / 1000);
    }
}

function updateStatusWithCountdown(customMsg, customClass) {
    if (customMsg) {
        state.lastStatusMessage = customMsg;
        state.lastStatusClass = customClass || 'live';
    }
    
    if (state.isScanning) {
        updateStatusText(`Scanning wireless spectrum on ${state.selectedInterface || 'interface'}...`, 'scanning');
    } else if (state.isPaused) {
        updateStatusText('Auto-refresh paused', 'idle');
    } else if (!state.refreshRate) {
        updateStatusText(state.lastStatusMessage, state.lastStatusClass);
    } else {
        const countdownStr = ` — Next refresh in ${state.countdown}s`;
        updateStatusText(state.lastStatusMessage + countdownStr, state.lastStatusClass);
    }
}

// Update text in top status bar
function updateStatusText(text, stateClass) {
    const pulse = document.getElementById('statusPulse');
    const label = document.getElementById('statusText');
    
    if (pulse) pulse.className = 'pulse-dot ' + stateClass;
    if (label) label.textContent = text;
}

// Populate stats cards
function updateDashboardMetrics() {
    if (!state.meta) return;
    
    document.getElementById('statTotalAps').textContent = state.meta.total_aps;
    document.getElementById('statSourceDesc').textContent = `Total SSIDs seen live`;
    
    // Signal strength calculations (min, average, max)
    const signals = state.currentScanData.map(r => r.signal_dbm).filter(s => s !== null);
    if (signals.length > 0) {
        const minSig = Math.min(...signals);
        const maxSig = Math.max(...signals);
        const avgSig = Math.round(signals.reduce((a, b) => a + b, 0) / signals.length);
        
        document.getElementById('statMinSignal').textContent = `${minSig} dBm`;
        document.getElementById('statAvgSignal').textContent = `${avgSig} dBm`;
        document.getElementById('statMaxSignal').textContent = `${maxSig} dBm`;
    } else {
        document.getElementById('statMinSignal').textContent = '-';
        document.getElementById('statAvgSignal').textContent = '-';
        document.getElementById('statMaxSignal').textContent = '-';
    }
    
    document.getElementById('statLastUpdate').textContent = `Updated: ${state.meta.timestamp.split(' ')[1]}`;

    // Flash/pulse animation on statistics row for live indication
    const statsRow = document.querySelector('.stats-row');
    if (statsRow) {
        statsRow.classList.remove('pulse-update');
        void statsRow.offsetWidth; // force reflow
        statsRow.classList.add('pulse-update');
        setTimeout(() => statsRow.classList.remove('pulse-update'), 600);
    }
}

// Control display configurations for bands
function updateBandVisibility() {
    const b = state.activeBand;
    document.getElementById('wrapper24').style.display = (b === 'all' || b === '2.4ghz') ? 'flex' : 'none';
    document.getElementById('wrapper5').style.display = (b === 'all' || b === '5ghz') ? 'flex' : 'none';
    document.getElementById('wrapper6').style.display = (b === 'all' || b === '6ghz') ? 'flex' : 'none';
    
    const util24 = document.getElementById('wrapperUtil24');
    const util5 = document.getElementById('wrapperUtil5');
    const util6 = document.getElementById('wrapperUtil6');
    if (util24) util24.style.display = (b === 'all' || b === '2.4ghz') ? 'flex' : 'none';
    if (util5) util5.style.display = (b === 'all' || b === '5ghz') ? 'flex' : 'none';
    if (util6) util6.style.display = (b === 'all' || b === '6ghz') ? 'flex' : 'none';
    
    // Trigger canvas resized in case container display changed
    setTimeout(() => {
        for (const [id, canvas] of Object.entries(state.canvasElements)) {
            if (canvas && canvas.offsetParent !== null) {
                resizeCanvas(canvas);
            }
        }
    }, 10);
}

// Master rendering router for all canvases
function renderCharts() {
    if (state.activeTab === 'spectrum-tab') {
        if (state.activeBand === 'all' || state.activeBand === '2.4ghz') {
            drawSpectrum('chart24', '2.4GHz');
        }
        if (state.activeBand === 'all' || state.activeBand === '5ghz') {
            drawSpectrum('chart5', '5GHz');
        }
        if (state.activeBand === 'all' || state.activeBand === '6ghz') {
            drawSpectrum('chart6', '6GHz');
        }
    } else if (state.activeTab === 'utilization-tab') {
        if (state.activeBand === 'all' || state.activeBand === '2.4ghz') {
            drawUtilizationChart('chartUtil24', '2.4GHz');
        }
        if (state.activeBand === 'all' || state.activeBand === '5ghz') {
            drawUtilizationChart('chartUtil5', '5GHz');
        }
        if (state.activeBand === 'all' || state.activeBand === '6ghz') {
            drawUtilizationChart('chartUtil6', '6GHz');
        }
    }
}

// Drawing logic: Spectral curve channels map
function drawSpectrum(canvasId, bandName) {
    const canvas = state.canvasElements[canvasId];
    const ctx = state.canvasContexts[canvasId];
    if (!canvas || !ctx || canvas.offsetParent === null) return;

    // Clear with transparent layer
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const w = canvas.width / (window.devicePixelRatio || 1);
    const h = canvas.height / (window.devicePixelRatio || 1);

    const cw = w - chartLayout.paddingLeft - chartLayout.paddingRight;
    const ch = h - chartLayout.paddingTop - chartLayout.paddingBottom;

    const band = BANDS[bandName];

    // Grid details
    const minDbm = -100;
    const maxDbm = -20;
    const dbmRange = maxDbm - minDbm;

    const getX = (freq) => chartLayout.paddingLeft + (freq - band.freqMin) / (band.freqMax - band.freqMin) * cw;
    const getY = (dbm) => chartLayout.paddingTop + (dbm - maxDbm) / (minDbm - maxDbm) * ch;

    // Draw background grid lines (y axis - dBm levels)
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
    ctx.lineWidth = 1;
    ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
    ctx.font = '10px Outfit';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';

    for (let dbm = maxDbm; dbm >= minDbm; dbm -= 10) {
        const y = getY(dbm);
        ctx.beginPath();
        ctx.moveTo(chartLayout.paddingLeft, y);
        ctx.lineTo(chartLayout.paddingLeft + cw, y);
        ctx.stroke();
        
        // Draw labels
        ctx.fillText(`${dbm} dBm`, chartLayout.paddingLeft - 8, y);
    }

    // Draw channel lines & text markers on x axis
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';

    const channelsToDraw = [];
    if (bandName === '2.4GHz') {
        for (let chan = 1; chan <= 13; chan++) {
            channelsToDraw.push({ chan: chan, freq: chanToFreq24[chan] });
        }
    } else if (bandName === '5GHz') {
        // Draw subset of channels to avoid clutter
        const standardChans = [36, 40, 44, 48, 52, 56, 60, 64, 100, 108, 116, 124, 132, 140, 149, 157, 165];
        standardChans.forEach(c => {
            channelsToDraw.push({ chan: c, freq: 5000 + c * 5 });
        });
    } else if (bandName === '6GHz') {
        // Draw representative 6G channels
        const representative6g = [1, 17, 33, 49, 65, 81, 97, 113, 129, 145, 161, 177, 193, 209, 225];
        representative6g.forEach(c => {
            channelsToDraw.push({ chan: c, freq: 5950 + c * 5 });
        });
    }

    channelsToDraw.forEach(item => {
        const x = getX(item.freq);
        if (x >= chartLayout.paddingLeft && x <= chartLayout.paddingLeft + cw) {
            // Channel center line (subtle)
            ctx.strokeStyle = 'rgba(255, 255, 255, 0.02)';
            ctx.beginPath();
            ctx.moveTo(x, chartLayout.paddingTop);
            ctx.lineTo(x, chartLayout.paddingTop + ch);
            ctx.stroke();
            
            // X label text
            ctx.fillStyle = 'rgba(255, 255, 255, 0.3)';
            ctx.fillText(item.chan, x, chartLayout.paddingTop + ch + 8);
            
            // Frequency label below
            ctx.fillStyle = 'rgba(255, 255, 255, 0.15)';
            ctx.font = '8px Outfit';
            ctx.fillText(`${item.freq}`, x, chartLayout.paddingTop + ch + 20);
            ctx.font = '10px Outfit';
        }
    });

    // Draw bottom/side frame borders
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
    ctx.beginPath();
    ctx.moveTo(chartLayout.paddingLeft, chartLayout.paddingTop);
    ctx.lineTo(chartLayout.paddingLeft, chartLayout.paddingTop + ch);
    ctx.lineTo(chartLayout.paddingLeft + cw, chartLayout.paddingTop + ch);
    ctx.stroke();

    // Sort records to draw weaker APs first, stronger APs on top
    const aps = state.currentScanData
        .filter(r => r.band === bandName && r.channel !== null)
        .sort((a, b) => a.signal_dbm - b.signal_dbm);

    if (aps.length === 0) {
        ctx.fillStyle = 'rgba(255, 255, 255, 0.2)';
        ctx.font = 'italic 13px Outfit';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(`No active ${bandName} networks in scan results`, chartLayout.paddingLeft + cw/2, chartLayout.paddingTop + ch/2);
        return;
    }

    const activeHighlight = state.highlightedBssid || state.lockedBssid;

    // Draw curves
    aps.forEach(rec => {
        const centerFreq = rec.freq_mhz || 2412;
        const width = rec.channel_width_mhz || 20;
        const signal = rec.signal_dbm || -95;

        const xCenter = getX(centerFreq);
        const xStart = getX(centerFreq - width / 2);
        const xEnd = getX(centerFreq + width / 2);
        const yPeak = getY(signal);
        const yBottom = chartLayout.paddingTop + ch;
        const curveWidth = xEnd - xStart;

        // Colors
        const isThisHighlighted = (rec.bssid === state.highlightedBssid || rec.bssid === state.lockedBssid);
        
        let opacityFill = 0.15;
        let opacityStroke = 0.7;
        let strokeWidth = 1.5;

        if (activeHighlight) {
            if (isThisHighlighted) {
                opacityFill = 0.35;
                opacityStroke = 1.0;
                strokeWidth = 3;
            } else {
                opacityFill = 0.03;
                opacityStroke = 0.15;
                strokeWidth = 1;
            }
        }

        const strokeColor = getApColor(rec.ssid, rec.bssid, opacityStroke);
        const fillColor = getApColor(rec.ssid, rec.bssid, opacityFill);

        // Path drawing (bell curve dome)
        ctx.beginPath();
        ctx.moveTo(xStart, yBottom);
        ctx.bezierCurveTo(
            xStart + curveWidth * 0.4, yBottom,
            xCenter - curveWidth * 0.25, yPeak,
            xCenter, yPeak
        );
        ctx.bezierCurveTo(
            xCenter + curveWidth * 0.25, yPeak,
            xEnd - curveWidth * 0.4, yBottom,
            xEnd, yBottom
        );

        // Fill with vertical gradient fading to transparent at bottom
        const grad = ctx.createLinearGradient(0, yPeak, 0, yBottom);
        grad.addColorStop(0, fillColor);
        grad.addColorStop(1, 'rgba(0, 0, 0, 0)');
        
        ctx.fillStyle = grad;
        ctx.fill();
        
        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = strokeWidth;
        ctx.stroke();

        // Draw network name label near peak
        if (isThisHighlighted || (!activeHighlight && signal > -70 && curveWidth > 40)) {
            ctx.fillStyle = isThisHighlighted ? '#ffffff' : 'rgba(255, 255, 255, 0.7)';
            ctx.font = isThisHighlighted ? 'bold 11px Outfit' : '10px Outfit';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'bottom';
            
            // Display full SSID name (up to 32 characters)
            let displayName = rec.ssid || 'Hidden';
            
            ctx.fillText(displayName, xCenter, yPeak - 4);
        }
    });
}

// Retrieve channels and BSS load values for a specific band
function getActiveChannelsForBand(targetBand) {
    const counts = {};
    const utils = {};

    state.currentScanData.forEach(rec => {
        const chan = rec.channel;
        const band = rec.band;
        const util = rec.channel_utilisation;
        
        if (chan !== null && band === targetBand) {
            counts[chan] = (counts[chan] || 0) + 1;
            if (util !== null) {
                utils[chan] = Math.max(utils[chan] || 0, util);
            }
        }
    });

    const list = [];
    if (targetBand === '2.4GHz') {
        for (let c = 1; c <= 13; c++) {
            const apCount = counts[c] || 0;
            let util = utils[c];
            let utilType = 'BSS Load';
            
            if (util === undefined) {
                util = Math.min(255, apCount * 25); 
                utilType = 'Estimated (AP density)';
            }
            if (apCount > 0 || util > 20) {
                list.push({ channel: c, band: '2.4GHz', util, apCount, utilType });
            }
        }
    } else if (targetBand === '5GHz') {
        Object.keys(counts).forEach(c => {
            const chan = parseInt(c, 10);
            const apCount = counts[c] || 0;
            let util = utils[chan];
            let utilType = 'BSS Load';
            
            if (util === undefined) {
                util = Math.min(255, apCount * 18);
                utilType = 'Estimated (AP density)';
            }
            list.push({ channel: chan, band: '5GHz', util, apCount, utilType });
        });
    } else if (targetBand === '6GHz') {
        Object.keys(counts).forEach(c => {
            const chan = parseInt(c, 10);
            const apCount = counts[c] || 0;
            let util = utils[chan];
            let utilType = 'BSS Load';
            
            if (util === undefined) {
                util = Math.min(255, apCount * 15);
                utilType = 'Estimated (AP density)';
            }
            list.push({ channel: chan, band: '6GHz', util, apCount, utilType });
        });
    }

    return list.sort((a, b) => a.channel - b.channel);
}

// Drawing logic: Channel utilization bar chart
function drawUtilizationChart(canvasId, bandName) {
    const canvas = state.canvasElements[canvasId];
    const ctx = state.canvasContexts[canvasId];
    if (!canvas || !ctx || canvas.offsetParent === null) return;

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const w = canvas.width / (window.devicePixelRatio || 1);
    const h = canvas.height / (window.devicePixelRatio || 1);

    const cw = w - chartLayout.paddingLeft - chartLayout.paddingRight;
    const ch = h - chartLayout.paddingTop - chartLayout.paddingBottom;

    const activeChannels = getActiveChannelsForBand(bandName);

    // Draw horizontal grid lines (Y axis - %)
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
    ctx.lineWidth = 1;
    ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
    ctx.font = '10px Outfit';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';

    for (let pct = 100; pct >= 0; pct -= 20) {
        const y = chartLayout.paddingTop + (1 - pct / 100) * ch;
        ctx.beginPath();
        ctx.moveTo(chartLayout.paddingLeft, y);
        ctx.lineTo(chartLayout.paddingLeft + cw, y);
        ctx.stroke();
        ctx.fillText(`${pct}%`, chartLayout.paddingLeft - 8, y);
    }

    if (activeChannels.length === 0) {
        ctx.fillStyle = 'rgba(255, 255, 255, 0.2)';
        ctx.font = 'italic 13px Outfit';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(`No ${bandName} utilization data.`, chartLayout.paddingLeft + cw/2, chartLayout.paddingTop + ch/2);
        return;
    }

    const spacing = cw / activeChannels.length;
    const barWidth = Math.min(40, spacing * 0.7);

    // Draw bars
    for (let i = 0; i < activeChannels.length; i++) {
        const chanObj = activeChannels[i];
        const xCenter = chartLayout.paddingLeft + (i * spacing) + (spacing / 2);
        const xStart = xCenter - barWidth / 2;

        const yBottom = chartLayout.paddingTop + ch;
        const utilizationPct = chanObj.util / 255;
        const barHeight = utilizationPct * ch;
        const yTop = yBottom - barHeight;

        // Choose color based on percentage level
        let colorGradStart = varColor('green');
        let colorGradEnd = 'rgba(0, 230, 118, 0.3)';
        
        if (utilizationPct > 0.6) {
            colorGradStart = varColor('pink');
            colorGradEnd = 'rgba(255, 59, 116, 0.3)';
        } else if (utilizationPct > 0.3) {
            colorGradStart = varColor('orange');
            colorGradEnd = 'rgba(255, 145, 0, 0.3)';
        }

        // Draw bar with gradient
        const barGrad = ctx.createLinearGradient(0, yTop, 0, yBottom);
        barGrad.addColorStop(0, colorGradStart);
        barGrad.addColorStop(1, colorGradEnd);

        ctx.fillStyle = barGrad;
        
        // Rounded bar corners (top only)
        drawRoundedRect(ctx, xStart, yTop, barWidth, Math.max(2, barHeight), 4, true, false);

        // Grid label (Channel and band)
        ctx.fillStyle = 'rgba(255, 255, 255, 0.3)';
        ctx.font = '9px Outfit';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(`CH ${chanObj.channel}`, xCenter, yBottom + 6);
        
        ctx.fillStyle = chanObj.band === '2.4GHz' ? 'rgba(0, 210, 255, 0.2)' : chanObj.band === '5GHz' ? 'rgba(171, 71, 188, 0.2)' : 'rgba(255, 152, 0, 0.2)';
        ctx.font = '7px Outfit';
        ctx.fillText(chanObj.band, xCenter, yBottom + 18);
    }

    // Outer border
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
    ctx.beginPath();
    ctx.moveTo(chartLayout.paddingLeft, chartLayout.paddingTop);
    ctx.lineTo(chartLayout.paddingLeft, chartLayout.paddingTop + ch);
    ctx.lineTo(chartLayout.paddingLeft + cw, chartLayout.paddingTop + ch);
    ctx.stroke();
}

// Utility: CSS Variable resolution fallback helper
function varColor(name) {
    if (name === 'pink') return '#ff3b74';
    if (name === 'blue') return '#00d2ff';
    if (name === 'purple') return '#ab47bc';
    if (name === 'green') return '#00e676';
    if (name === 'orange') return '#ff9100';
    return '#ffffff';
}

// Utility: Draw rounded rectangles on canvas
function drawRoundedRect(ctx, x, y, width, height, radius, fill, stroke) {
    if (height < radius) radius = height / 2;
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.lineTo(x + width - radius, y);
    ctx.quadraticCurveTo(x + width, y, x + width, y + radius);
    ctx.lineTo(x + width, y + height);
    ctx.lineTo(x, y + height);
    ctx.lineTo(x, y + radius);
    ctx.quadraticCurveTo(x, y, x + radius, y);
    ctx.closePath();
    if (fill) ctx.fill();
    if (stroke) ctx.stroke();
}

// Filter and populate rows in the lower dashboard table
function renderTable() {
    const tbody = document.getElementById('apTableBody');
    if (!tbody) return;

    if (state.currentScanData.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="table-empty">No scan data available. Trigger a scan above.</td>
            </tr>
        `;
        return;
    }

    // Filters
    const searchQuery = document.getElementById('tableSearch').value.toLowerCase().trim();
    const filterSecurity = document.getElementById('tableFilterSecurity').value;
    const band = state.activeBand;

    const filtered = state.currentScanData.filter(rec => {
        // Band Filter
        if (band !== 'all') {
            if (band === '2.4ghz' && rec.band !== '2.4GHz') return false;
            if (band === '5ghz' && rec.band !== '5GHz') return false;
            if (band === '6ghz' && rec.band !== '6GHz') return false;
        }

        // Security Filter
        if (filterSecurity !== 'all') {
            const sec = rec.security.toLowerCase();
            if (filterSecurity === 'open' && sec !== 'open') return false;
            if (filterSecurity === 'wpa3' && !sec.includes('wpa3')) return false;
            if (filterSecurity === 'wpa2' && !sec.includes('wpa2')) return false;
            if (filterSecurity === 'wpa' && !sec.includes('wpa') && sec.includes('wpa2')) return false;
        }

        // Search text
        if (searchQuery) {
            const ssid = (rec.ssid || '').toLowerCase();
            const bssid = rec.bssid.toLowerCase();
            const vendor = (rec.vendor || '').toLowerCase();
            const channel = String(rec.channel);
            
            if (!ssid.includes(searchQuery) && 
                !bssid.includes(searchQuery) && 
                !vendor.includes(searchQuery) && 
                !channel.includes(searchQuery)) {
                return false;
            }
        }

        return true;
    });

    if (filtered.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="table-empty">No matching access points found.</td>
            </tr>
        `;
        return;
    }

    // Sort APs: Strongest signal first
    filtered.sort((a, b) => b.signal_dbm - a.signal_dbm);

    tbody.innerHTML = '';
    filtered.forEach(rec => {
        const tr = document.createElement('tr');
        tr.setAttribute('data-bssid', rec.bssid);
        
        // Highlight states
        if (rec.bssid === state.highlightedBssid || rec.bssid === state.lockedBssid) {
            tr.className = 'highlighted';
        }

        // Signal badge
        const sigClass = getSignalClass(rec.signal_dbm);
        const sigBadge = `<span class="badge badge-signal ${sigClass}">${rec.signal_dbm} dBm</span>`;

        // Security class
        const secClass = rec.security === 'OPEN' ? 'open' : '';
        const secBadge = `<span class="badge badge-sec ${secClass}">${rec.security}</span>`;

        // Colored dot for SSID
        const apColor = getApColor(rec.ssid, rec.bssid);
        const dotColorIndicator = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background-color:${apColor};margin-right:8px;"></span>`;

        tr.innerHTML = `
            <td><strong>${dotColorIndicator}${rec.ssid || '<i>Hidden SSID</i>'}</strong></td>
            <td style="font-family:'Space Grotesk', monospace; font-size:0.8rem;">${rec.bssid.toUpperCase()}</td>
            <td>${sigBadge}</td>
            <td><strong>${rec.channel !== null ? rec.channel : '-'}</strong></td>
            <td>${rec.channel_width_mhz} MHz</td>
            <td><span style="font-size:0.8rem; color:var(--text-secondary);">${rec.band || '-'}</span></td>
            <td>${secBadge}</td>
        `;

        // Mouse hover sync
        tr.addEventListener('mouseenter', () => {
            state.highlightedBssid = rec.bssid;
            tr.classList.add('highlighted');
            renderCharts();
        });

        tr.addEventListener('mouseleave', () => {
            if (state.lockedBssid !== rec.bssid) {
                state.highlightedBssid = null;
                tr.classList.remove('highlighted');
            }
            renderCharts();
        });

        // Mouse click locks highlight
        tr.addEventListener('click', (e) => {
            state.lockedBssid = state.lockedBssid === rec.bssid ? null : rec.bssid;
            renderCharts();
            renderTable();
        });

        tbody.appendChild(tr);
    });
}

// Link highlighting from canvas hover into table row
function highlightTableRow(bssid) {
    clearTableRowHighlights();
    const row = document.querySelector(`[data-bssid="${bssid}"]`);
    if (row) {
        row.classList.add('highlighted');
    }
}

function clearTableRowHighlights() {
    const rows = document.querySelectorAll('#apTableBody tr');
    rows.forEach(r => {
        const rBssid = r.getAttribute('data-bssid');
        if (rBssid !== state.lockedBssid) {
            r.classList.remove('highlighted');
        }
    });
}
