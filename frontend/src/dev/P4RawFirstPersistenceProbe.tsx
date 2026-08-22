import { useRef, useState } from 'react'
import { uploadXmaxThirtySecondSegment, type XmaxLocalPersistenceProbe } from '../api'

const TASK_ID = 'xmax-30s-a2d1be01f71d4948'
const UNIT_ID = 'xmax-30s-a2d1be01f71d4948-segment-001'
const RECORDING_MS = 2300

type ProbeResults = {
  blobSize: number
  blobSha256: string
  mimeType: string
  success: XmaxLocalPersistenceProbe
  forcedFailure: XmaxLocalPersistenceProbe
}

function bytesToHex(bytes: ArrayBuffer) {
  return Array.from(new Uint8Array(bytes), (value) => value.toString(16).padStart(2, '0')).join('')
}

async function sha256(blob: Blob) {
  return bytesToHex(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()))
}

function selectMimeType() {
  for (const mimeType of ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm']) {
    if (MediaRecorder.isTypeSupported(mimeType)) return mimeType
  }
  throw new Error('This browser cannot record WebM.')
}

async function recordBrowserBlob(canvas: HTMLCanvasElement) {
  const context = canvas.getContext('2d')
  if (!context) throw new Error('Canvas 2D is unavailable.')
  const stream = canvas.captureStream(30)
  const mimeType = selectMimeType()
  const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 2_000_000 })
  const chunks: Blob[] = []
  let animationFrame = 0
  const startedAt = performance.now()

  const draw = (now: number) => {
    const elapsed = Math.max(0, now - startedAt)
    const progress = Math.min(1, elapsed / RECORDING_MS)
    context.fillStyle = '#111827'
    context.fillRect(0, 0, canvas.width, canvas.height)
    context.fillStyle = '#22c55e'
    context.fillRect(32 + progress * (canvas.width - 192), 92, 160, 160)
    context.fillStyle = '#f8fafc'
    context.font = 'bold 42px sans-serif'
    context.fillText('RAW FIRST', 32, 52)
    context.font = '24px monospace'
    context.fillText(`${Math.round(elapsed)} ms`, 32, canvas.height - 30)
    if (progress < 1) animationFrame = requestAnimationFrame(draw)
  }

  const completed = new Promise<Blob>((resolve, reject) => {
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data.size > 0) chunks.push(event.data)
    })
    recorder.addEventListener('error', () => reject(new Error('MediaRecorder failed.')), { once: true })
    recorder.addEventListener('stop', () => {
      cancelAnimationFrame(animationFrame)
      stream.getTracks().forEach((track) => track.stop())
      const blob = new Blob(chunks, { type: recorder.mimeType || mimeType })
      if (blob.size === 0) reject(new Error('MediaRecorder produced an empty Blob.'))
      else resolve(blob)
    }, { once: true })
  })

  animationFrame = requestAnimationFrame(draw)
  recorder.start(100)
  window.setTimeout(() => recorder.stop(), RECORDING_MS)
  return completed
}

function ProbeCard({ title, value }: { title: string; value: XmaxLocalPersistenceProbe }) {
  return <section style={{ border: '1px solid #334155', padding: 16, background: '#111827' }}>
    <h2 style={{ fontSize: 18, marginTop: 0 }}>{title}</h2>
    <dl style={{ display: 'grid', gridTemplateColumns: '190px 1fr', gap: 8, margin: 0 }}>
      <dt>Status</dt><dd>{value.status}</dd>
      <dt>Raw path</dt><dd style={{ overflowWrap: 'anywhere' }}>{value.raw_output.path}</dd>
      <dt>Raw SHA-256</dt><dd style={{ overflowWrap: 'anywhere' }}>{value.raw_output.sha256}</dd>
      <dt>Raw bytes</dt><dd>{value.raw_output.size}</dd>
      <dt>Raw unchanged</dt><dd>{String(value.raw_output.unchanged_after_normalization)}</dd>
      <dt>Normalization</dt><dd>{value.normalization.status}</dd>
      <dt>Normalized path</dt><dd style={{ overflowWrap: 'anywhere' }}>{value.normalization.path || '-'}</dd>
      <dt>Runtime loaded</dt><dd>{value.runtime.module_loaded_at}</dd>
      <dt>Runtime SHA</dt><dd style={{ overflowWrap: 'anywhere' }}>{value.runtime.module_sha256}</dd>
    </dl>
  </section>
}

export default function P4RawFirstPersistenceProbe() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [status, setStatus] = useState('Ready. No Xmax call, temporary key, Session, or credits are used.')
  const [error, setError] = useState('')
  const [running, setRunning] = useState(false)
  const [results, setResults] = useState<ProbeResults | null>(null)

  async function runProbe() {
    if (!canvasRef.current || running) return
    setRunning(true)
    setError('')
    setResults(null)
    try {
      setStatus('Recording a real browser MediaRecorder Blob...')
      const blob = await recordBrowserBlob(canvasRef.current)
      const blobSha256 = await sha256(blob)
      const suffix = crypto.randomUUID().replaceAll('-', '')
      const successProbeId = `browser-success-${suffix}`
      const failureProbeId = `browser-forced-failure-${suffix}`
      const diagnostics = { source: 'browser_media_recorder', audit_only: true, blob_sha256: blobSha256 }

      setStatus('Uploading through the formal P4 output function...')
      const success = await uploadXmaxThirtySecondSegment(
        TASK_ID,
        UNIT_ID,
        blob,
        'browser-media-recorder.webm',
        diagnostics,
        undefined,
        { probeId: successProbeId },
      ) as XmaxLocalPersistenceProbe

      setStatus('Forcing normalization failure after immutable raw persistence...')
      const forcedFailure = await uploadXmaxThirtySecondSegment(
        TASK_ID,
        UNIT_ID,
        blob,
        'browser-media-recorder.webm',
        diagnostics,
        undefined,
        { probeId: failureProbeId, forceNormalizationFailure: true },
      )
      if (forcedFailure.status !== 'normalization_failed') {
        throw new Error('The forced-failure probe unexpectedly returned success.')
      }
      if (success.raw_output.sha256 !== blobSha256 || forcedFailure.raw_output.sha256 !== blobSha256) {
        throw new Error('Browser Blob SHA-256 does not match persisted raw bytes.')
      }
      setResults({ blobSize: blob.size, blobSha256, mimeType: blob.type, success, forcedFailure })
      setStatus('PASS: success and forced-failure raw-first probes are persisted.')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      setStatus('Probe failed.')
    } finally {
      setRunning(false)
    }
  }

  return <main style={{ minHeight: '100vh', background: '#020617', color: '#e2e8f0', padding: 24, fontFamily: 'system-ui, sans-serif' }}>
    <div style={{ maxWidth: 1100, margin: '0 auto', display: 'grid', gap: 18 }}>
      <header>
        <h1 style={{ fontSize: 26, marginBottom: 8 }}>P4 Raw-first Browser Persistence Probe</h1>
        <p style={{ color: '#94a3b8', margin: 0 }}>Local audit only | Xmax calls 0 | Sessions 0 | Credits 0</p>
      </header>
      <canvas ref={canvasRef} width={960} height={544} style={{ width: '100%', maxWidth: 640, border: '1px solid #334155' }} />
      <div>
        <button type="button" onClick={runProbe} disabled={running} style={{ padding: '10px 16px', border: 0, background: running ? '#475569' : '#16a34a', color: 'white', cursor: running ? 'wait' : 'pointer' }}>
          {running ? 'Running local probe...' : 'Run browser MediaRecorder probe'}
        </button>
      </div>
      <p role="status" style={{ margin: 0 }}>{status}</p>
      {error && <pre role="alert" style={{ whiteSpace: 'pre-wrap', color: '#fca5a5' }}>{error}</pre>}
      {results && <>
        <section style={{ border: '1px solid #334155', padding: 16 }}>
          <strong>Browser Blob</strong>
          <div>Type: {results.mimeType}</div>
          <div>Bytes: {results.blobSize}</div>
          <div style={{ overflowWrap: 'anywhere' }}>SHA-256: {results.blobSha256}</div>
        </section>
        <ProbeCard title="Normalization success" value={results.success} />
        <ProbeCard title="Forced normalization failure" value={results.forcedFailure} />
      </>}
    </div>
  </main>
}
