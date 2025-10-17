/**
 * BrainStormX Headshot Studio — Face Guidance Overlay
 *
 * Responsibilities:
 * - Draw grid + circular target overlay atop the camera video
 * - Run MediaPipe Face Landmarker (blendshapes) in-browser for alignment, gaze, and smile checks
 * - Provide real-time textual guidance and composite score
 * - Auto-capture when conditions are met and stable for a short duration
 *
 * This module self-initializes on the headshot page if it finds expected DOM elements.
 */

/* global bootstrap */

const CDN = {
	// Pinned version for stability; use proper bundle + wasm + model URLs
	base: "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14",
		bundle: "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs",
		wasm: "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm",
		// Fallbacks
		bundleFallback: "https://unpkg.com/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs",
		wasmFallback: "https://unpkg.com/@mediapipe/tasks-vision@0.10.14/wasm",
	model: "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
};

const Defaults = {
	analysisSize: 512,
	frameStride: 2,
	stabilityMs: 1200,      // Stability time matching your UI (1000ms)
	centerTol: 0.12,        // Center tolerance from your UI (0.14)
	sizeRange: [0.4, 0.6],  // Face size range from your UI (0.4-0.6)
	rollTolDeg: 5,          // Tilt tolerance from your UI (5 degrees)
	smileThresh: 0.73,      // Smile threshold from your UI (0.73)
	blinkMax: 0.8,          // Keep lenient blink detection
	eyeGazeTolerance: 0.3,  // Eye contact tolerance from your UI (0.3)
	requireSmile: false,    // Require smile disabled as shown in your UI
	showDebug: true,        // Show debug info enabled as shown in your UI
	autoCapture: true,      // Auto-capture enabled as shown in your UI
};

const PREFS_KEY = "bsx_headshot_prefs_v1";

class FaceGuidance {
	constructor(opts = {}) {
		// Load saved preferences (if any)
		let saved = {};
		try {
			const raw = localStorage.getItem(PREFS_KEY);
			if (raw) saved = JSON.parse(raw);
		} catch {}
		this.opts = { ...Defaults, ...saved, ...opts };
		this.video = document.getElementById("cameraVideo");
		this.canvas = document.getElementById("guidanceCanvas");
		this.statusEl = document.getElementById("guidanceStatus") || document.getElementById("cameraStatus");
		this.captureBtn = document.getElementById("capturePhoto");
		this.startBtn = document.getElementById("startCamera");
		this.stopBtn = document.getElementById("stopCamera");
		this.retakeBtn = document.getElementById("retakePhoto");

		if (!this.video || !this.canvas) return; // no-op on non-studio pages

		this.ctx = this.canvas.getContext("2d");
		this.frame = 0;
		this.running = false;
		this.lastPassStart = null;
		this.lastAnalysis = null;
		this.modelReady = false;
		this.faceLandmarker = null;

		this._bindUi();
		this._observeVisibility();
		this._init();
	}

	_bindUi() {
		// Sync canvas size with the video element size on resize
		const resize = () => {
			const rect = this.video.getBoundingClientRect();
			this.canvas.width = Math.max(2, Math.floor(rect.width));
			this.canvas.height = Math.max(2, Math.floor(rect.height));
		};
		resize();
		new ResizeObserver(resize).observe(this.video);

		// Pause/resume with camera controls
		this.startBtn?.addEventListener("click", () => this.resume());
		this.stopBtn?.addEventListener("click", () => this.pause());
		this.retakeBtn?.addEventListener("click", () => this.resume());
	}

	_observeVisibility() {
		document.addEventListener("visibilitychange", () => {
			if (document.hidden) this.pause();
			else this.resume();
		});
	}

	async _init() {
			try {
							// Dynamically import Tasks Vision from CDN (ESM bundle) with fallback
							let vision;
							try {
								vision = await import(/* @vite-ignore */ CDN.bundle);
							} catch (e1) {
								vision = await import(/* @vite-ignore */ CDN.bundleFallback);
							}
							let fileset;
							try {
								fileset = await vision.FilesetResolver.forVisionTasks(CDN.wasm);
							} catch (e2) {
								fileset = await vision.FilesetResolver.forVisionTasks(CDN.wasmFallback);
							}
			this.faceLandmarker = await vision.FaceLandmarker.createFromOptions(fileset, {
				baseOptions: { modelAssetPath: CDN.model },
				outputFaceBlendshapes: true,
				runningMode: "VIDEO",
				numFaces: 1,
			});
			this.modelReady = true;
			this.resume();
		} catch (e) {
			console.warn("Face Landmarker unavailable, falling back to geometry-only overlay.", e);
			// Still run overlay loop without AI
			this.modelReady = false;
			this.resume();
		}
	}

	resume() {
		if (this.running) return;
		this.running = true;
		this._loop();
	}

	pause() {
		this.running = false;
		this._clear();
	}

	_loop = () => {
		if (!this.running) return;
		this.frame++;
		const { width: w, height: h } = this.canvas;
		this._clear();
		this._drawGrid(w, h);
		this._drawTarget(w, h);

		let passed = false;
		let score = 0;
		let analysis = null;

		if (this.modelReady && this.video.readyState >= 2) {
			try {
				if (this.frame % this.opts.frameStride === 0) {
					const ts = performance.now();
					const result = this.faceLandmarker.detectForVideo(this.video, ts);
					analysis = this._analyze(result, w, h);
					this.lastAnalysis = analysis; // Store for debug logging
					passed = analysis.passed;
					score = analysis.score;
					this._drawDebug(analysis, w, h);
					this._drawConfidenceIndicator(analysis, w, h);
					this._drawDynamicFeedback(analysis, w, h);
					this._status(analysis.message || (passed ? "Aligned" : "Adjust your position"));
				} else {
					// Use last analysis for drawing overlays on non-analysis frames
					analysis = this.lastAnalysis;
					if (analysis) {
						passed = analysis.passed;
						score = analysis.score;
						this._drawDebug(analysis, w, h);
						this._drawConfidenceIndicator(analysis, w, h);
						this._drawDynamicFeedback(analysis, w, h);
					}
				}
			} catch (e) {
				// Model hiccup; ignore this frame
			}
		} else {
			this._status("Align within the circle and grid");
		}

		// Stability gate for auto-capture (if enabled)
		const now = performance.now();
		

        // No verbose console logging in production
		
		// Remove manual test trigger - debugging complete
		
		if (passed) {
			if (!this.lastPassStart) this.lastPassStart = now;
			const dt = now - this.lastPassStart;
			if (this.opts.autoCapture) {
				this._drawProgress(dt / this.opts.stabilityMs, w, h);
				if (dt >= this.opts.stabilityMs) {
					this._autoCapture();
					this.lastPassStart = null;
				}
			}
		} else {
			this.lastPassStart = null;
		}

		requestAnimationFrame(this._loop);
	};

	_clear() { this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height); }

	_drawGrid(w, h) {
		const ctx = this.ctx;
		ctx.save();
		ctx.strokeStyle = "rgba(255,255,255,0.25)";
		ctx.lineWidth = 1;
		// rule of thirds
		const v1 = (w / 3) | 0, v2 = (2 * w / 3) | 0;
		const h1 = (h / 3) | 0, h2 = (2 * h / 3) | 0;
		ctx.beginPath();
		ctx.moveTo(v1, 0); ctx.lineTo(v1, h);
		ctx.moveTo(v2, 0); ctx.lineTo(v2, h);
		ctx.moveTo(0, h1); ctx.lineTo(w, h1);
		ctx.moveTo(0, h2); ctx.lineTo(w, h2);
		ctx.stroke();
		ctx.restore();
	}

	_drawTarget(w, h) {
		const ctx = this.ctx;
		ctx.save();
		
		// Professional headshot framing guide
		const cx = w / 2, cy = h / 2;
		
		// Main target circle - optimal face position
		const faceRadius = Math.min(w, h) * 0.22;  // Smaller, more precise target
		ctx.beginPath();
		ctx.arc(cx, cy - h * 0.05, faceRadius, 0, Math.PI * 2);  // Slightly above center
		ctx.strokeStyle = "rgba(0, 255, 120, 0.4)";
		ctx.lineWidth = 2;
		ctx.stroke();
		
		// Shoulder guide - wider oval
		ctx.beginPath();
		ctx.ellipse(cx, cy + h * 0.25, w * 0.35, h * 0.15, 0, 0, Math.PI * 2);
		ctx.strokeStyle = "rgba(0, 255, 120, 0.2)";
		ctx.lineWidth = 1;
		ctx.stroke();
		
		// Corner alignment marks
		const cornerSize = 20;
		const margin = 30;
		ctx.strokeStyle = "rgba(255, 255, 255, 0.6)";
		ctx.lineWidth = 2;
		
		// Top-left
		ctx.beginPath();
		ctx.moveTo(margin, margin + cornerSize);
		ctx.lineTo(margin, margin);
		ctx.lineTo(margin + cornerSize, margin);
		ctx.stroke();
		
		// Top-right  
		ctx.beginPath();
		ctx.moveTo(w - margin - cornerSize, margin);
		ctx.lineTo(w - margin, margin);
		ctx.lineTo(w - margin, margin + cornerSize);
		ctx.stroke();
		
		ctx.restore();
	}

	_drawProgress(p, w, h) {
		const ctx = this.ctx; const width = Math.max(0, Math.min(1, p));
		const barW = Math.floor(w * 0.5), barH = 8;
		const x = (w - barW) / 2, y = h - barH - 10;
		ctx.save();
		ctx.fillStyle = "rgba(255,255,255,0.2)";
		ctx.fillRect(x, y, barW, barH);
		ctx.fillStyle = "rgba(25,135,84,0.9)"; // bootstrap success
		ctx.fillRect(x, y, barW * width, barH);
		ctx.restore();
	}

	_status(text) {
		if (this.statusEl) this.statusEl.textContent = text;
	}

	_autoCapture() {
		// Trigger existing capture flow; if in captured state, ignore
		// quiet: avoid console spam in production
		
		// Try multiple ways to trigger capture
		if (this.captureBtn) {
			// First try normal click if visible
			if (this.captureBtn.offsetParent !== null) {
				this.captureBtn.click();
			} else {
				// If button is hidden, try to show it first or click anyway
				const originalDisplay = this.captureBtn.style.display;
				this.captureBtn.style.display = '';
				this.captureBtn.click();
				// Restore original display if needed
				if (originalDisplay) {
					setTimeout(() => {
						this.captureBtn.style.display = originalDisplay;
					}, 100);
				}
			}
		} else {
			// no button available
		}
	}

	updateOptions(partial = {}) {
		this.opts = { ...this.opts, ...partial };
		try { localStorage.setItem(PREFS_KEY, JSON.stringify(this.opts)); } catch {}
		this.lastPassStart = null;
	}

	_analyze(result, w, h) {
		// Defaults for when no detection
		if (!result || !result.faceLandmarks || result.faceLandmarks.length === 0) {
			return { passed: false, score: 0, message: "Face not detected" };
		}

		const lm = result.faceLandmarks[0];
		// Compute face bbox from landmarks (normalized [0..1])
		let minX = 1, minY = 1, maxX = 0, maxY = 0;
		for (const p of lm) {
			minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
			minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
		}
		const cxN = (minX + maxX) / 2, cyN = (minY + maxY) / 2;
		const hN = (maxY - minY); // normalized height

		// Improved positioning analysis
		const centerOk = Math.hypot(cxN - 0.5, cyN - 0.45) < this.opts.centerTol;  // Target slightly above center
		const sizeOk = hN >= this.opts.sizeRange[0] && hN <= this.opts.sizeRange[1];
		
		// Better size guidance with bidirectional feedback
		const optimalSize = (this.opts.sizeRange[0] + this.opts.sizeRange[1]) / 2;
		const sizeTooSmall = hN < this.opts.sizeRange[0];
		const sizeTooLarge = hN > this.opts.sizeRange[1];

		// Roll estimate: use two eye outer corners indices (33 left, 263 right in FaceMesh schema)
		const leftEye = lm[33], rightEye = lm[263];
		let rollDeg = 0;
		if (leftEye && rightEye) {
			rollDeg = Math.atan2((rightEye.y - leftEye.y), (rightEye.x - leftEye.x)) * 180 / Math.PI;
		}
		const rollOk = Math.abs(rollDeg) <= this.opts.rollTolDeg;

		// Enhanced blendshapes analysis for smile/eyes
		let smileOk = true, eyesOk = true; // Default to true if detection unavailable
		let message = "Align within the circle";
		let score = 0;
		let debugInfo = {}; // For troubleshooting

		if (result.faceBlendshapes && result.faceBlendshapes.length) {
			const map = new Map();
			for (const c of result.faceBlendshapes[0].categories) map.set(c.categoryName, c.score);
			
			// Enhanced smile detection with better blendshape combinations
			const smileLeft = map.get("mouthSmileLeft") || 0;
			const smileRight = map.get("mouthSmileRight") || 0;
			const mouthDimpler = map.get("mouthDimpleLeft") || 0 + map.get("mouthDimpleRight") || 0;
			const cheekPuff = (map.get("cheekPuff") || 0) * 0.3; // Subtle contribution
			const smile = Math.max(smileLeft, smileRight) + (mouthDimpler * 0.2) + cheekPuff;
			
			// Enhanced eye contact detection - look for direct gaze
			const blink = Math.max(map.get("eyeBlinkLeft") || 0, map.get("eyeBlinkRight") || 0);
			const eyeGaze = Math.max(
				Math.abs(map.get("eyeLookInLeft") || 0),
				Math.abs(map.get("eyeLookOutLeft") || 0),
				Math.abs(map.get("eyeLookUpLeft") || 0),
				Math.abs(map.get("eyeLookDownLeft") || 0),
				Math.abs(map.get("eyeLookInRight") || 0),
				Math.abs(map.get("eyeLookOutRight") || 0),
				Math.abs(map.get("eyeLookUpRight") || 0),
				Math.abs(map.get("eyeLookDownRight") || 0),
			);
			
			// Store debug info for troubleshooting
			debugInfo = { 
				smileLeft: smileLeft.toFixed(3), 
				smileRight: smileRight.toFixed(3), 
				smileTotal: smile.toFixed(3), 
				threshold: this.opts.smileThresh.toFixed(3),
				eyeGaze: eyeGaze.toFixed(3),
				blink: blink.toFixed(3)
			};
			
			// Apply thresholds with bypass options
			smileOk = !this.opts.requireSmile || smile >= this.opts.smileThresh;
			eyesOk = blink <= this.opts.blinkMax && eyeGaze <= this.opts.eyeGazeTolerance;

			// Provide specific feedback for issues
			if (this.opts.requireSmile && !smileOk) {
				message = `Add a gentle smile (${(smile * 100).toFixed(0)}%/${(this.opts.smileThresh * 100).toFixed(0)}%)`;
			}
			if (!eyesOk) {
				if (blink > this.opts.blinkMax) {
					message = "Open your eyes";
				} else if (eyeGaze > this.opts.eyeGazeTolerance) {
					message = "Look directly at the camera";
				}
			}
		}

		// Improved messaging with bidirectional feedback
		if (!centerOk) {
			const deltaX = cxN - 0.5, deltaY = cyN - 0.45;
			if (Math.abs(deltaX) > Math.abs(deltaY)) {
				message = deltaX > 0 ? "Move left slightly" : "Move right slightly";
			} else {
				message = deltaY > 0 ? "Move up slightly" : "Position face in target";
			}
		}
		else if (sizeTooSmall) message = "Move closer to camera";
		else if (sizeTooLarge) message = "Move back from camera"; 
		else if (!rollOk) message = `Straighten head (${Math.abs(rollDeg.toFixed(1))}° tilt)`;
		else if (!eyesOk) message = "Open eyes and look at camera";
		else if (!smileOk) message = "Add a gentle smile";

		// Calculate final score and pass status
		const flags = [centerOk, sizeOk, rollOk, smileOk, eyesOk];
		
		// Only check required flags for pass status
		const requiredFlags = [
			centerOk, 
			sizeOk, 
			rollOk, 
			this.opts.requireSmile ? smileOk : true,  // Skip smile if not required
			eyesOk
		];
		const passed = requiredFlags.every(Boolean);
		
		// No console logging for flag analysis to keep UI responsive
		
		// Calculate weighted score for display purposes
		const flagWeights = [1.0, 1.0, 0.8, this.opts.requireSmile ? 0.7 : 0.0, 1.0];
		const totalWeight = flagWeights.reduce((sum, weight, i) => sum + (flags[i] ? weight : 0), 0);
		const maxWeight = flagWeights.reduce((sum, weight) => sum + weight, 0);
		score = maxWeight > 0 ? totalWeight / maxWeight : 0; // Weighted score 0..1
		
		return { 
			passed, 
			score, 
			message, 
			bbox: { minX, minY, maxX, maxY }, 
			rollDeg,
			debugInfo,
			flags: {
				centerOk,
				sizeOk, 
				rollOk,
				smileOk,
				eyesOk
			}
		};
	}

	_drawDebug(analysis, w, h) {
		if (!analysis || !analysis.bbox) return;
		const ctx = this.ctx;
		const { minX, minY, maxX, maxY } = analysis.bbox;
		ctx.save();
		
		// Enhanced face bounding box with confidence coloring
		const confidence = analysis.score || 0;
		const alpha = 0.6 + (confidence * 0.4); // Higher confidence = more opaque
		
		if (confidence > 0.8) {
			ctx.strokeStyle = `rgba(0, 255, 120, ${alpha})`;  // Green for good alignment
		} else if (confidence > 0.5) {
			ctx.strokeStyle = `rgba(255, 193, 7, ${alpha})`;   // Amber for partial alignment
		} else {
			ctx.strokeStyle = `rgba(255, 99, 71, ${alpha})`;   // Red for poor alignment
		}
		
		ctx.lineWidth = 3;
		ctx.strokeRect(minX * w, minY * h, (maxX - minX) * w, (maxY - minY) * h);
		
		// Add corner indicators for better visibility
		const cornerSize = 15;
		const x1 = minX * w, y1 = minY * h;
		const x2 = maxX * w, y2 = maxY * h;
		
		// Top-left corner
		ctx.beginPath();
		ctx.moveTo(x1, y1 + cornerSize);
		ctx.lineTo(x1, y1);
		ctx.lineTo(x1 + cornerSize, y1);
		ctx.stroke();
		
		// Top-right corner
		ctx.beginPath();
		ctx.moveTo(x2 - cornerSize, y1);
		ctx.lineTo(x2, y1);
		ctx.lineTo(x2, y1 + cornerSize);
		ctx.stroke();
		
		// Bottom-left corner
		ctx.beginPath();
		ctx.moveTo(x1, y2 - cornerSize);
		ctx.lineTo(x1, y2);
		ctx.lineTo(x1 + cornerSize, y2);
		ctx.stroke();
		
		// Bottom-right corner
		ctx.beginPath();
		ctx.moveTo(x2 - cornerSize, y2);
		ctx.lineTo(x2, y2);
		ctx.lineTo(x2, y2 - cornerSize);
		ctx.stroke();
		
		ctx.restore();
	}

	_drawConfidenceIndicator(analysis, w, h) {
		if (!analysis) return;
		const ctx = this.ctx;
		const confidence = analysis.score || 0;
		
		ctx.save();
		
		// Main confidence bar in top-right corner
		const barWidth = 120, barHeight = 8;
		const x = w - barWidth - 20, y = 20;
		
		// Background - adjust size based on debug mode
		ctx.fillStyle = "rgba(0, 0, 0, 0.4)";
		const bgHeight = this.opts.showDebug ? barHeight + 60 : barHeight + 10;
		ctx.fillRect(x - 5, y - 5, barWidth + 10, bgHeight);
		
		// Progress bar
		ctx.fillStyle = "rgba(255, 255, 255, 0.2)";
		ctx.fillRect(x, y, barWidth, barHeight);
		
		// Confidence fill with color coding
		let fillColor;
		if (confidence > 0.8) fillColor = "rgba(0, 255, 120, 0.9)";      // Green
		else if (confidence > 0.5) fillColor = "rgba(255, 193, 7, 0.9)"; // Amber  
		else fillColor = "rgba(255, 99, 71, 0.9)";                       // Red
		
		ctx.fillStyle = fillColor;
		ctx.fillRect(x, y, barWidth * confidence, barHeight);
		
		// Label
		ctx.fillStyle = "rgba(255, 255, 255, 0.9)";
		ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
		ctx.textAlign = "right";
		ctx.fillText(`${Math.round(confidence * 100)}%`, x + barWidth, y - 8);
		
		// Debug info for smile detection (if available)
		if (this.opts.showDebug) {
			ctx.font = "9px -apple-system, BlinkMacSystemFont, sans-serif";
			ctx.textAlign = "left";
			ctx.fillStyle = "rgba(255, 255, 255, 0.9)";
			const debugY = y + barHeight + 12;
			
			// Always show debug status
			ctx.fillText(`DEBUG MODE: ON`, x, debugY);
			
			if (analysis && analysis.flags) {
				// Show flag status
				const flags = analysis.flags;
				const flagStr = `${flags.centerOk ? '✓' : '✗'}Pos ${flags.sizeOk ? '✓' : '✗'}Size ${flags.rollOk ? '✓' : '✗'}Roll`;
				ctx.fillText(flagStr, x, debugY + 12);
				
				const eyeSmileStr = `${flags.eyesOk ? '✓' : '✗'}Eyes ${flags.smileOk ? '✓' : '✗'}Smile`;
				ctx.fillText(eyeSmileStr, x, debugY + 24);
				
				if (analysis.debugInfo) {
					// Show numeric values
					ctx.fillText(`👄 ${analysis.debugInfo.smileTotal}/${analysis.debugInfo.threshold} ReqSmile:${this.opts.requireSmile}`, x, debugY + 36);
					ctx.fillText(`👁 Gaze:${analysis.debugInfo.eyeGaze} Blink:${analysis.debugInfo.blink}`, x, debugY + 48);
				}
				
				// Show passed status
				ctx.fillStyle = analysis.passed ? "rgba(0, 255, 120, 0.9)" : "rgba(255, 99, 71, 0.9)";
				ctx.fillText(`PASSED: ${analysis.passed}`, x, debugY + 60);
			} else {
				ctx.fillText(`No face detected`, x, debugY + 12);
			}
		}
		
		ctx.restore();
	}

	_drawDynamicFeedback(analysis, w, h) {
		if (!analysis || !analysis.bbox) return;
		const ctx = this.ctx;
		
		ctx.save();
		
		// Draw directional arrows for positioning feedback
		const centerX = w / 2, centerY = h / 2;
		const faceX = (analysis.bbox.minX + analysis.bbox.maxX) / 2 * w;
		const faceY = (analysis.bbox.minY + analysis.bbox.maxY) / 2 * h;
		
		const deltaX = faceX - centerX;
		const deltaY = faceY - (centerY - h * 0.05); // Target is slightly above center
		
		const threshold = 30; // Minimum distance to show arrow
		
		if (Math.abs(deltaX) > threshold || Math.abs(deltaY) > threshold) {
			const arrowLength = 40;
			const arrowSize = 8;
			
			// Calculate arrow direction
			const distance = Math.hypot(deltaX, deltaY);
			const dirX = -deltaX / distance; // Negative because we want to point where to move
			const dirY = -deltaY / distance;
			
			// Arrow position (offset from face center)
			const arrowX = faceX + dirX * 60;
			const arrowY = faceY + dirY * 60;
			
			ctx.strokeStyle = "rgba(255, 193, 7, 0.8)";
			ctx.fillStyle = "rgba(255, 193, 7, 0.8)";
			ctx.lineWidth = 3;
			
			// Draw arrow shaft
			ctx.beginPath();
			ctx.moveTo(arrowX - dirX * arrowLength, arrowY - dirY * arrowLength);
			ctx.lineTo(arrowX, arrowY);
			ctx.stroke();
			
			// Draw arrow head
			ctx.beginPath();
			ctx.moveTo(arrowX, arrowY);
			ctx.lineTo(
				arrowX - dirX * arrowSize + dirY * arrowSize * 0.5,
				arrowY - dirY * arrowSize - dirX * arrowSize * 0.5
			);
			ctx.lineTo(
				arrowX - dirX * arrowSize - dirY * arrowSize * 0.5,
				arrowY - dirY * arrowSize + dirX * arrowSize * 0.5
			);
			ctx.closePath();
			ctx.fill();
		}
		
		// Draw roll indicator for head tilt
		if (analysis.rollDeg && Math.abs(analysis.rollDeg) > 5) {
			const rollX = centerX + 80, rollY = centerY - 80;
			const rollRadius = 20;
			
			ctx.strokeStyle = "rgba(255, 99, 71, 0.8)";
			ctx.lineWidth = 3;
			
			// Draw level indicator circle
			ctx.beginPath();
			ctx.arc(rollX, rollY, rollRadius, 0, Math.PI * 2);
			ctx.stroke();
			
			// Draw current tilt line
			const angle = analysis.rollDeg * Math.PI / 180;
			const x1 = rollX + Math.cos(angle) * rollRadius * 0.8;
			const y1 = rollY + Math.sin(angle) * rollRadius * 0.8;
			const x2 = rollX - Math.cos(angle) * rollRadius * 0.8;
			const y2 = rollY - Math.sin(angle) * rollRadius * 0.8;
			
			ctx.beginPath();
			ctx.moveTo(x1, y1);
			ctx.lineTo(x2, y2);
			ctx.stroke();
			
			// Draw target level line (horizontal)
			ctx.strokeStyle = "rgba(0, 255, 120, 0.6)";
			ctx.beginPath();
			ctx.moveTo(rollX - rollRadius * 0.8, rollY);
			ctx.lineTo(rollX + rollRadius * 0.8, rollY);
			ctx.stroke();
		}
		
		ctx.restore();
	}
}

// Auto initialize on DOM ready (module may be loaded defer)
(function initWhenReady() {
	const boot = () => {
		const fg = new FaceGuidance();
		window.BSX_Guidance = fg;
		document.addEventListener("bsx:headshot:updateOptions", (e) => {
			if (!e || !e.detail) return;
			fg.updateOptions(e.detail);
		});
	};
	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
	else boot();
})();
