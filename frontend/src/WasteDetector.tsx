import React, { useEffect, useRef, useState } from "react";
import { apiErrorMessage } from "./planningSession";
import { WasteObservation } from "./types/api";
import { classifyVisionObservation, isUncertainVisionObservation, summarizeVisionDetections, VISION_CLASSIFICATION_CONFIDENCE } from "./visionClassification";
import "./WasteDetector.css";

export const DEFAULT_DETECTION_CONFIDENCE = 0.25;

export function litterName(value?: string | null): string {
  const normalized = (value || "").toLowerCase().replace(/[^a-z]/g, "");
  const names: Record<string, string> = {
    plasticbagwrapper: "Plastic bag / wrapper", papercardboard: "Paper / cardboard",
    bottlecaplid: "Bottle cap / lid", bottle: "Bottle", can: "Can", cigarette: "Cigarette",
    cup: "Cup", otherlitter: "Other litter",
  };
  return names[normalized] || value || "Unidentified item";
}

export function DetectionSettings({ confidence, busy, modelStatus, inferenceMs, onConfidence }: {
  confidence: number; busy: boolean; modelStatus: string; inferenceMs?: number;
  onConfidence: (value: number) => void;
}) {
  return <details className="vision-options">
    <summary>Detection settings</summary>
    <div>
      <label htmlFor="vision-confidence">Minimum model score: {Math.round(confidence * 100)}%</label>
      <input id="vision-confidence" type="range" min="0.05" max="0.95" step="0.05" value={confidence}
        disabled={busy} onChange={event => onConfidence(Number(event.target.value))} />
      <p>This filters possible detections; it is not the model's accuracy. At 95%, items with scores below 95% are hidden. A higher setting can miss visible litter.</p>
      <button type="button" className="yolo-action-btn" disabled={busy || confidence === DEFAULT_DETECTION_CONFIDENCE}
        onClick={() => onConfidence(DEFAULT_DETECTION_CONFIDENCE)}>Reset to default (25%)</button>
      <p>Changes apply to the next photo check or camera frame. Detection quality still depends on the image and the trained model.</p>
      <p>Detected items with a score below {Math.round(VISION_CLASSIFICATION_CONFIDENCE * 100)}%, or an unrecognized label, go to Others. Items below the minimum model score are not detected or counted.</p>
      <p className="vision-technical-status">{modelStatus}{inferenceMs === undefined ? "" : ` · Last model check: ${inferenceMs.toFixed(0)} ms`}</p>
    </div>
  </details>;
}

const categoryLabels = { plasticAndCans: "Plastic & Cans", paperAndPackaging: "Paper & Packaging", others: "Others" };

export function DetectionResults({ result, confidence, busy, onResetDefault }: {
  result: DetectionResult; confidence: number; busy: boolean;
  onResetDefault: () => void;
}) {
  const hasItems = result.detections.length > 0;
  const counts = summarizeVisionDetections(result.detections);
  return <div className="vision-results" aria-label="Detection results">
    <h3>{hasItems ? "Automatically classified" : "No items found in this image"}</h3>
    <dl className="vision-category-summary" aria-label="Items by category">
      {(Object.keys(categoryLabels) as Array<keyof typeof categoryLabels>).map(category =>
        <div key={category}><dt>{categoryLabels[category]}</dt><dd>{counts[category]}</dd></div>)}
    </dl>
    {!hasItems ? <>
      <p>This does not confirm the area is clean. Try a closer, clearer photo or a different angle.</p>
      {confidence > DEFAULT_DETECTION_CONFIDENCE && <>
        <p>This image was checked at a minimum score of {Math.round(confidence * 100)}%. This can hide visible litter.</p>
        <button type="button" className="yolo-action-btn" disabled={busy} onClick={onResetDefault}>Use default setting and check again</button>
      </>}
    </> : <>
      <p>Items are counted by category automatically. Unsure items go to Others.</p>
      <ul>{result.detections.map((observation, index) => {
        const category = classifyVisionObservation(observation);
        const uncertain = isUncertainVisionObservation(observation);
        const score = observation.confidence;
        return <li key={observation.detection_id}>
          <span><strong>{index + 1}. {uncertain ? "Unsure item" : litterName(observation.waste_class)}</strong>
            <small>{uncertain && observation.waste_class ? `Model label: ${litterName(observation.waste_class)} · ` : ""}
              Model score: {typeof score === "number" && Number.isFinite(score) && score >= 0 && score <= 1 ? `${Math.round(score * 100)}%` : "unavailable"}</small></span>
          <span className={`vision-category-badge ${category === "others" ? "vision-category-others" : ""}`}>{categoryLabels[category]}{uncertain ? " · unsure" : ""}</span>
        </li>;
      })}</ul>
    </>}
    <p className="vision-count-note">Counts reflect the latest photo or camera frame. Checking again replaces the counts; it does not add the same items twice. A detection does not confirm physical collection.</p>
  </div>;
}

interface DetectionResult {
  model: string;
  image_width: number;
  image_height: number;
  inference_ms: number;
  detections: WasteObservation[];
  minimum_score?: number;
}

interface Props {
  apiBase: string;
  token: string;
  active: boolean;
  onDetections?: (observations: WasteObservation[]) => void;
}

export default function WasteDetector({ apiBase, token, active, onDetections }: Props) {
  const [result, setResult] = useState<DetectionResult | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [previewRatio, setPreviewRatio] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [camera, setCamera] = useState(false);
  const [confidence, setConfidence] = useState(DEFAULT_DETECTION_CONFIDENCE);
  const [modelStatus, setModelStatus] = useState("Checking local model...");
  const [modelReady, setModelReady] = useState<boolean | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const requestRef = useRef<AbortController | null>(null);
  const serialRef = useRef(0);
  const lastUploadRef = useRef<File | null>(null);
  const onDetectionsRef = useRef(onDetections);
  useEffect(() => { onDetectionsRef.current = onDetections; }, [onDetections]);

  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    fetch(`${apiBase}/api/vision/status`, {
      headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
    }).then(async response => {
      const data = await response.json();
      if (!response.ok) throw new Error(apiErrorMessage(data.detail));
      if (disposed) return;
      setModelReady(Boolean(data.ready));
      setModelStatus(data.ready ? `${data.model} · ${data.classes.length} litter classes · ${data.device}` : data.detail);
    }).catch(err => {
      if (disposed) return;
      setModelReady(false);
      if (controller.signal.aborted) setModelStatus("Model status unavailable. You can still try detection.");
      else setModelStatus(err.message || "Backend unavailable");
    }).finally(() => window.clearTimeout(timeout));
    return () => { disposed = true; window.clearTimeout(timeout); controller.abort(); };
  }, [apiBase, token]);

  useEffect(() => () => { requestRef.current?.abort(); serialRef.current++; }, []);
  useEffect(() => {
    if (active) return;
    serialRef.current++;
    requestRef.current?.abort();
    setCamera(false);
    setBusy(false);
  }, [active]);
  useEffect(() => {
    if (!busy) return;
    const started = Date.now();
    setElapsedSeconds(0);
    const timer = window.setInterval(() => setElapsedSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);
  useEffect(() => {
    if (!preview) return;
    return () => URL.revokeObjectURL(preview);
  }, [preview]);

  function cancelDetection() {
    serialRef.current++;
    requestRef.current?.abort();
    requestRef.current = null;
    setCamera(false);
    setBusy(false);
    setNotice("Check cancelled. You can check this photo again or choose another.");
  }

  async function detect(blob: Blob, showPendingPreview = false, minimumScore = confidence): Promise<boolean> {
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    const serial = ++serialRef.current;
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 60000);
    setBusy(true);
    setNotice(null);
    setError(null);
    if (showPendingPreview) {
      setResult(null);
      onDetectionsRef.current?.([]);
      setPreview(URL.createObjectURL(blob));
    }
    try {
      const form = new FormData();
      form.append("image", blob, "frame.jpg");
      const response = await fetch(`${apiBase}/api/vision/detect?confidence=${minimumScore}`, {
        method: "POST", headers: { Authorization: `Bearer ${token}` },
        body: form, signal: controller.signal,
      });
      const data = await response.json().catch(() => {
        throw new Error(`Detection server returned an invalid response (${response.status}). Check that the backend is running.`);
      });
      if (!response.ok) throw new Error(apiErrorMessage(data.detail || data));
      if (serial !== serialRef.current || controller.signal.aborted) return false;
      setResult({ ...data, minimum_score: minimumScore });
      onDetectionsRef.current?.(data.detections);
      setPreviewRatio(data.image_width / data.image_height);
      setPreview(URL.createObjectURL(blob));
      setModelStatus(`${data.model} · local YOLO inference`);
      setModelReady(true);
      return true;
    } catch (err) {
      if (serial === serialRef.current) {
        setError(timedOut ? "Detection exceeded 60 seconds. The first model load can take longer. Check the backend and try again." : (err as Error).message);
        setResult(null);
        onDetectionsRef.current?.([]);
      }
      return false;
    } finally {
      window.clearTimeout(timeout);
      if (serial === serialRef.current) { setBusy(false); requestRef.current = null; }
    }
  }

  useEffect(() => {
    if (!camera || !active) return;
    let cancelled = false;
    let stream: MediaStream | null = null;
    let timer: number | undefined;
    async function capture() {
      if (cancelled) return;
      const video = videoRef.current;
      if (!video || video.readyState < 2 || !video.videoWidth) {
        timer = window.setTimeout(capture, 200);
        return;
      }
      const canvas = document.createElement("canvas");
      const scale = Math.min(1, 1280 / video.videoWidth);
      canvas.width = Math.round(video.videoWidth * scale);
      canvas.height = Math.round(video.videoHeight * scale);
      canvas.getContext("2d")!.drawImage(video, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/jpeg", 0.85));
      if (cancelled) return;
      if (blob && !(await detect(blob))) {
        if (!cancelled) setCamera(false);
        return;
      }
      if (!cancelled) timer = window.setTimeout(capture, 1000);
    }
    async function start() {
      try {
        if (!navigator.mediaDevices?.getUserMedia) throw new Error("Webcam requires localhost or HTTPS and a supported browser.");
        stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        if (cancelled) { stream.getTracks().forEach(track => track.stop()); return; }
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play();
          if (!cancelled) void capture();
        }
      } catch (err) {
        if (!cancelled) { setError((err as Error).message); setCamera(false); }
      }
    }
    void start();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      serialRef.current++;
      requestRef.current?.abort();
      stream?.getTracks().forEach(track => track.stop());
      setBusy(false);
    };
  }, [camera, active, confidence, apiBase, token]);

  function resetAndCheck() {
    setConfidence(DEFAULT_DETECTION_CONFIDENCE);
    if (lastUploadRef.current && !camera) void detect(lastUploadRef.current, true, DEFAULT_DETECTION_CONFIDENCE);
  }

  function clearImage() {
    setResult(null);
    setPreview(null);
    setError(null);
    setNotice(null);
    lastUploadRef.current = null;
    onDetectionsRef.current?.([]);
  }

  return <section className="yolo-screen-card real-detector">
    <div className="yolo-screen-header">
      <div className="vision-heading"><div className="yolo-title-group"><strong>Photo / camera classification</strong>
        <span className="yolo-meta-pill">{modelReady === null ? "Checking vision…" : modelReady ? "Vision available" : "Vision needs attention"}</span></div>
        <p>Add a photo or start the camera. Items are counted automatically by category; unsure items go to Others.</p></div>
      <div className="yolo-controls">
        <label className="yolo-action-btn vision-upload-action">Add photo
          <input aria-label="Upload litter image" type="file" accept="image/jpeg,image/png,image/webp" disabled={busy || camera}
            onChange={event => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (!file) return;
              if (file.size > 10 * 1024 * 1024) { setError("Choose a photo smaller than 10 MB."); return; }
              lastUploadRef.current = file;
              void detect(file, true);
            }} />
        </label>
        <button type="button" className={`yolo-action-btn ${camera ? "active" : ""}`} disabled={busy && !camera}
          onClick={() => {
            if (!camera) {
              lastUploadRef.current = null;
              setResult(null); setPreview(null); onDetectionsRef.current?.([]);
            }
            setCamera(value => !value);
          }}>{camera ? "Stop camera" : "Start camera"}</button>
        {lastUploadRef.current && !camera && <button type="button" className="yolo-action-btn" disabled={busy}
          onClick={() => { if (lastUploadRef.current) void detect(lastUploadRef.current, true); }}>Check photo again</button>}
        {(preview || result) && !camera && <button type="button" className="yolo-action-btn" disabled={busy}
          onClick={clearImage}>Clear image &amp; counts</button>}
        {busy && <button type="button" className="yolo-action-btn" onClick={cancelDetection}>Cancel check</button>}
      </div>
    </div>
    <div className="vision-status-row"><span role="status" aria-live="polite">{busy ? `Checking for litter… ${elapsedSeconds}s${elapsedSeconds >= 5 ? " · First use can take longer." : ""}` : notice || (result && !camera && result.minimum_score !== confidence ? "Detection setting changed. Select Check photo again to update these results." : result ? `${result.detections.length} ${result.detections.length === 1 ? "item" : "items"} counted in the latest ${camera ? "camera frame" : "photo"}. ${result.detections.length ? "Categories updated automatically." : "Try a clearer photo if litter is visible."}` : "Choose a photo to classify litter.")}</span>
      {confidence > DEFAULT_DETECTION_CONFIDENCE && <span className="vision-strict-setting">Strict filter: {Math.round(confidence * 100)}%. Some items may be hidden.</span>}</div>
    <DetectionSettings confidence={confidence} busy={busy} modelStatus={modelStatus}
      inferenceMs={result?.inference_ms} onConfidence={setConfidence} />
    {error && <p className="vision-error" role="alert">{error}</p>}
    <div className={`vision-workspace ${result ? "has-results" : ""}`} aria-busy={busy}>
    <div className="vision-image-area">
      {camera && <video ref={videoRef} autoPlay playsInline muted className="vision-camera-preview" aria-label="Live camera preview" />}
      {preview ? <div className="vision-image-frame" style={{ "--vision-image-ratio": previewRatio } as React.CSSProperties}>
        <img src={preview} alt={result ? "Analyzed litter image" : "Uploaded image awaiting detection"}
          onLoad={event => { const img = event.currentTarget; if (img.naturalHeight) setPreviewRatio(img.naturalWidth / img.naturalHeight); }} />
        {result?.detections.map((observation, index) => {
          const box = observation.image_bbox;
          if (!box) return null;
          const uncertain = isUncertainVisionObservation(observation);
          const color = uncertain ? "#f59e0b" : "#10b981";
          return <div key={observation.detection_id} className="yolo-bbox" style={{ left: `${box.x * 100}%`, top: `${box.y * 100}%`, width: `${box.width * 100}%`, height: `${box.height * 100}%`, borderColor: color }}>
            <span className="yolo-bbox-tag" style={{ background: color }}>{index + 1}. {uncertain ? "Others · unsure" : litterName(observation.waste_class)}</span>
          </div>;
        })}
      </div> : <p className="vision-empty">Add a clear photo of the litter, or start the camera. Items will be counted by category automatically.</p>}
    </div>
    {result && <DetectionResults result={result} confidence={result.minimum_score ?? confidence} busy={busy} onResetDefault={resetAndCheck} />}
    </div>
  </section>;
}
