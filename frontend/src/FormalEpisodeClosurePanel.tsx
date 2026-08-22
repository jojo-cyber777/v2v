import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, ChevronUp, Download, Film, LoaderCircle, RefreshCw } from 'lucide-react'

import {
  confirmXmaxLevel1Input,
  confirmXmaxLevel2Input,
  createFormalFinalOutput,
  formalFinalOutputDownloadUrl,
  getFormalClosureWorkspace,
  normalizeBackendAssetUrl,
  prepareXmaxLevel2Input,
  selectFormalClosureUnit,
  type FormalClosureUnit,
  type FormalClosureWorkspace,
  type XmaxLevel2PreparedInput,
} from './api'
import { level2RouteIdentityMatches, requestFormalRedraw, type FormalRedrawPhase } from './lib/providers/xmax/FormalRedrawLifecycle'

type Props = {
  episodeId: string
  visualTaskId: string
}

const attemptStatus = (unit: FormalClosureUnit) => {
  const attempt = unit.attempts[0]
  if (!attempt) return '等待生成'
  if (attempt.settlement_status === 'pending') return '结算中'
  if (attempt.settlement_status === 'settled' && attempt.playable) return '已结算，可选择'
  if (attempt.raw_output_persisted && attempt.normalization_status === 'failed') return '原始结果已保存，规范化失败'
  if (attempt.raw_output_persisted) return '原始结果已保存'
  if (attempt.status === 'failed' || attempt.status === 'persistence_failed') return '需要人工处理'
  return attempt.status === 'prepared' ? '准备中' : '生成中'
}

const selectedLabel = (unit: FormalClosureUnit) => {
  if (unit.selection?.selected_source_type === 'original') return '已选择原片'
  if (unit.selection?.selected_source_type === 'generated_attempt') return '已选择当前结果'
  return '待选择'
}

export const level2PreparedMatchesUnit = (unit: FormalClosureUnit, prepared: XmaxLevel2PreparedInput | null) => Boolean(
  prepared
  && unit.processing_mode === 'level2_crop'
  && level2RouteIdentityMatches(
    { crop: unit.crop, scale: unit.scale, preprocessedSourceSha256: unit.preprocessed_source_sha256 },
    { crop: prepared.crop, scale: prepared.scale, preprocessedSourceSha256: prepared.preprocessed_source_sha256 },
  ),
)

export const generatedUnitsEligibleForBulkSelection = (units: FormalClosureUnit[]) => units
  .map((unit) => ({ unit, attempt: unit.attempts.find((attempt) => attempt.playable) }))
  .filter(({ unit, attempt }) => Boolean(
    attempt
    && unit.selection?.selected_source_type !== 'original'
    && unit.status !== 'failed'
    && unit.execution_status !== 'failed',
  ))

export default function FormalEpisodeClosurePanel({ episodeId, visualTaskId }: Props) {
  const [workspace, setWorkspace] = useState<FormalClosureWorkspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [busyUnitId, setBusyUnitId] = useState('')
  const [bulkSelecting, setBulkSelecting] = useState(false)
  const [rendering, setRendering] = useState(false)
  const [error, setError] = useState('')
  const [redrawUnitId, setRedrawUnitId] = useState('')
  const [pointsLimit, setPointsLimit] = useState(20)
  const [redrawConfirmed, setRedrawConfirmed] = useState(false)
  const [redrawMode, setRedrawMode] = useState<'level1' | 'level2'>('level1')
  const [level2Draft, setLevel2Draft] = useState<{
    crop: { x: number; y: number; width: number; height: number }
    scale: { width: number; height: number }
  } | null>(null)
  const [level2Prepared, setLevel2Prepared] = useState<XmaxLevel2PreparedInput | null>(null)
  const [level2Busy, setLevel2Busy] = useState(false)
  const redrawRequestIdRef = useRef('')
  const [redrawPhase, setRedrawPhase] = useState<FormalRedrawPhase | ''>('')
  const [unitsExpanded, setUnitsExpanded] = useState(false)

  const reload = useCallback(async () => {
    try {
      const next = await getFormalClosureWorkspace(episodeId, visualTaskId)
      setWorkspace(next)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }, [episodeId, visualTaskId])

  useEffect(() => { void reload() }, [reload])
  useEffect(() => () => { redrawRequestIdRef.current = '' }, [])

  const latestFinal = workspace?.latest_final_output
    || workspace?.final_outputs.find((item) => item.status === 'ready' && Boolean(item.output_url))
  const latestFinalAttempt = workspace?.final_outputs[0]
  const selectedCount = useMemo(
    () => workspace?.units.filter((unit) => Boolean(unit.selection)).length || 0,
    [workspace],
  )

  const choose = async (unit: FormalClosureUnit, sourceType: 'generated_attempt' | 'original') => {
    const currentAttempt = unit.attempts.find((attempt) => attempt.playable)
    if (sourceType === 'generated_attempt' && !currentAttempt) return
    setBusyUnitId(unit.unit_id)
    setError('')
    try {
      const next = await selectFormalClosureUnit(
        episodeId,
        unit.unit_id,
        sourceType,
        currentAttempt?.id,
      )
      setWorkspace(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusyUnitId('')
    }
  }

  const chooseAllCurrentResults = async () => {
    if (!workspace) return
    const eligible = generatedUnitsEligibleForBulkSelection(workspace.units)
      .filter(({ unit, attempt }) => unit.selection?.selected_attempt_id !== attempt?.id)
    if (!eligible.length) return
    setBulkSelecting(true)
    setError('')
    try {
      let next = workspace
      for (const { unit, attempt } of eligible) {
        next = await selectFormalClosureUnit(episodeId, unit.unit_id, 'generated_attempt', attempt?.id)
      }
      setWorkspace(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      await reload()
    } finally {
      setBulkSelecting(false)
    }
  }

  const startRedraw = async (unitId: string) => {
    if (!Number.isInteger(pointsLimit) || pointsLimit <= 0 || !redrawConfirmed) return
    const requestId = crypto.randomUUID()
    redrawRequestIdRef.current = requestId
    setBusyUnitId(unitId)
    setError('')
    try {
      await requestFormalRedraw({
        target: window,
        requestId,
        taskId: visualTaskId,
        unitId,
        requestedPointsLimit: pointsLimit,
        onPhase: (phase, activeRequestId) => {
          if (redrawRequestIdRef.current === activeRequestId) setRedrawPhase(phase)
        },
      })
      if (redrawRequestIdRef.current !== requestId) return
      setRedrawUnitId('')
      setRedrawConfirmed(false)
      setLevel2Prepared(null)
      await reload()
    } catch (reason) {
      if (redrawRequestIdRef.current === requestId) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      if (redrawRequestIdRef.current === requestId) {
        redrawRequestIdRef.current = ''
        setBusyUnitId('')
      }
    }
  }

  const openRedraw = async (unit: FormalClosureUnit, mode: 'level1' | 'level2') => {
    setRedrawUnitId(unit.unit_id)
    setRedrawMode(mode)
    setRedrawConfirmed(false)
    setLevel2Prepared(null)
    setError('')
    if (mode === 'level1') {
      if (unit.processing_mode === 'level2_crop') {
        try {
          await confirmXmaxLevel1Input(visualTaskId, unit.unit_id)
          await reload()
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      }
      return
    }
    if (unit.crop && unit.scale) {
      setLevel2Draft({ crop: unit.crop, scale: unit.scale })
      return
    }
    try {
      const dimensions = await new Promise<{ width: number; height: number }>((resolve, reject) => {
        const video = document.createElement('video')
        video.preload = 'metadata'
        video.onloadedmetadata = () => resolve({ width: video.videoWidth, height: video.videoHeight })
        video.onerror = () => reject(new Error('无法读取当前镜头尺寸。'))
        video.src = normalizeBackendAssetUrl(unit.source_url)
      })
      const width = Math.max(2, Math.floor(dimensions.width / 4) * 2)
      const height = Math.max(2, Math.floor(dimensions.height / 4) * 2)
      setLevel2Draft({
        crop: {
          x: Math.floor((dimensions.width - width) / 4) * 2,
          y: Math.floor((dimensions.height - height) / 4) * 2,
          width,
          height,
        },
        scale: { width: dimensions.width - (dimensions.width % 2), height: dimensions.height - (dimensions.height % 2) },
      })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const updateCrop = (field: 'x' | 'y' | 'width' | 'height', value: number) => {
    setLevel2Prepared(null)
    setLevel2Draft((current) => current ? { ...current, crop: { ...current.crop, [field]: value } } : current)
  }

  const prepareLevel2 = async (unit: FormalClosureUnit) => {
    if (!level2Draft) return
    setLevel2Busy(true)
    setError('')
    try {
      const prepared = await prepareXmaxLevel2Input({
        task_id: visualTaskId,
        unit_id: unit.unit_id,
        crop: level2Draft.crop,
        scale: level2Draft.scale,
      })
      setLevel2Prepared(prepared)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLevel2Busy(false)
    }
  }

  const confirmLevel2 = async () => {
    if (!level2Prepared) return
    setLevel2Busy(true)
    setError('')
    try {
      await confirmXmaxLevel2Input(level2Prepared)
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLevel2Busy(false)
    }
  }

  const createFinal = async () => {
    if (!workspace?.selection_complete) return
    setRendering(true)
    setError('')
    try {
      await createFormalFinalOutput(episodeId, visualTaskId)
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setRendering(false)
    }
  }

  if (loading) return <section className="formal-closure-panel"><LoaderCircle className="spin" size={18} /> 正在加载镜头结果…</section>
  if (!workspace) return <section className="formal-closure-panel formal-closure-error">{error || '未找到可用镜头。'}</section>

  return <section className="formal-closure-panel" aria-label="正式镜头选择与最终成片">
    <header className="formal-closure-heading">
      <div>
        <span className="section-label">FINAL DELIVERY</span>
        <h3>镜头结果与最终成片</h3>
        <p>{workspace.project.name} · {workspace.episode.title}</p>
      </div>
      <div className="formal-closure-count"><strong>{selectedCount}/{workspace.units.length}</strong><span>镜头已选择</span></div>
    </header>

    <div className="formal-bulk-actions">
      <strong>镜头结果与最终成片 · 已选择 {selectedCount}/{workspace.units.length}</strong>
      <button className="secondary-button" onClick={() => setUnitsExpanded((current) => !current)}>
        {unitsExpanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
        {unitsExpanded ? '收起镜头结果' : '展开镜头结果'}
      </button>
    </div>

    {unitsExpanded && <>
    <div className="formal-bulk-actions">
      <button
        className="secondary-button"
        disabled={bulkSelecting || !generatedUnitsEligibleForBulkSelection(workspace.units).some(({ unit, attempt }) => unit.selection?.selected_attempt_id !== attempt?.id)}
        onClick={() => void chooseAllCurrentResults()}
      >
        {bulkSelecting ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />} 全部使用当前结果
      </button>
      <small>仅选择当前任务中已成功且可播放的生成结果；保留原片、失败或无结果镜头不会改变。</small>
    </div>

    <div className="formal-unit-list">
      {workspace.units.map((unit) => {
        const currentAttempt = unit.attempts.find((attempt) => attempt.playable)
        const previewUrl = currentAttempt?.output_url || unit.crop_preview_url || unit.source_url
        const redrawOpen = redrawUnitId === unit.unit_id
        const preparedRouteCurrent = level2PreparedMatchesUnit(unit, level2Prepared)
        return <article className="formal-unit" key={unit.unit_id}>
          <div className="formal-unit-media">
            <video controls playsInline preload="metadata" src={normalizeBackendAssetUrl(previewUrl)} />
          </div>
          <div className="formal-unit-body">
            <div className="formal-unit-title">
              <div><strong>镜头 {unit.sequence}</strong><span>{unit.start_time.toFixed(1)}–{unit.end_time.toFixed(1)} 秒</span></div>
              <span className={unit.selection ? 'formal-status selected' : 'formal-status'}>{selectedLabel(unit)}</span>
            </div>
            <div className="formal-unit-meta">
              <span>{unit.processing_mode === 'level2_crop' ? '放大裁剪模式' : '原构图模式'}</span>
              <span>{attemptStatus(unit)}</span>
              {currentAttempt?.consumed_points != null && <span>本次消耗 {currentAttempt.consumed_points} 积分</span>}
            </div>
            {unit.processing_mode === 'level2_crop' && unit.crop_preview_url && <a className="text-button" href={normalizeBackendAssetUrl(unit.crop_preview_url)} target="_blank" rel="noreferrer">查看裁剪预览</a>}
            <div className="formal-unit-actions">
              <button className="secondary-button" disabled={!currentAttempt || busyUnitId === unit.unit_id} onClick={() => void choose(unit, 'generated_attempt')}><Check size={15} /> 使用当前结果</button>
              <button className="secondary-button" disabled={busyUnitId === unit.unit_id} onClick={() => void openRedraw(unit, 'level1')}><RefreshCw size={15} /> 原画重绘</button>
              <button className="secondary-button" disabled={busyUnitId === unit.unit_id} onClick={() => void openRedraw(unit, 'level2')}><RefreshCw size={15} /> 放大裁剪重绘（Level 2）</button>
              <button className="secondary-button" disabled={busyUnitId === unit.unit_id} onClick={() => void choose(unit, 'original')}><Film size={15} /> 保留原片</button>
            </div>
            {redrawOpen && <div className="formal-redraw-confirmation">
              <strong>{redrawMode === 'level2' ? '放大裁剪重绘（Level 2）' : '原画重绘'}</strong>
              {redrawMode === 'level2' && level2Draft && <>
                <div className="formal-unit-meta">
                  {(['x', 'y', 'width', 'height'] as const).map((field) => <label key={field}>{field}<input type="number" min="0" step="2" value={level2Draft.crop[field]} disabled={level2Busy} onChange={(event) => updateCrop(field, Number(event.target.value))} /></label>)}
                  <span>输出 {level2Draft.scale.width}×{level2Draft.scale.height}</span>
                </div>
                <button className="secondary-button" disabled={level2Busy} onClick={() => void prepareLevel2(unit)}>{level2Busy ? <LoaderCircle className="spin" size={15} /> : <Film size={15} />} 生成裁剪预览（零积分）</button>
                {level2Prepared && <div className="formal-unit-media">
                  <video controls muted playsInline preload="metadata" src={normalizeBackendAssetUrl(level2Prepared.preprocessed_source_url)} />
                  <span>裁剪 {level2Prepared.crop.x},{level2Prepared.crop.y} · {level2Prepared.crop.width}×{level2Prepared.crop.height} → {level2Prepared.scale.width}×{level2Prepared.scale.height}</span>
                  <button className="secondary-button" disabled={level2Busy || preparedRouteCurrent} onClick={() => void confirmLevel2()}>{preparedRouteCurrent ? '已使用此 Level 2 路由' : '确认使用此裁剪'}</button>
                </div>}
                {!preparedRouteCurrent && <small>确认裁剪路由前不会创建临时 Key、Session 或产生积分变动。</small>}
              </>}
              <label>本镜头积分上限<input type="number" min="1" step="1" value={pointsLimit} onChange={(event) => setPointsLimit(Number(event.target.value))} /></label>
              <label className="formal-redraw-check"><input type="checkbox" checked={redrawConfirmed} onChange={(event) => setRedrawConfirmed(event.target.checked)} /> 我确认只重绘镜头 {unit.sequence}，最多创建一个 Session，不自动重试。</label>
              <button className="primary-button" disabled={!redrawConfirmed || !Number.isInteger(pointsLimit) || pointsLimit <= 0 || busyUnitId === unit.unit_id || (redrawMode === 'level2' && unit.processing_mode !== 'level2_crop')} onClick={() => void startRedraw(unit.unit_id)}>{busyUnitId === unit.unit_id ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />} {redrawMode === 'level2' ? '确认放大裁剪重绘' : '确认原画重绘'}</button>
              {busyUnitId === unit.unit_id && redrawPhase && <small>{redrawPhase}</small>}
            </div>}
          </div>
        </article>
      })}
    </div>
    </>}

    {error && <div className="formal-closure-error">{error}</div>}
    <div className="formal-final-actions">
      <button className="primary-button" disabled={!workspace.selection_complete || rendering} onClick={() => void createFinal()}>{rendering ? <LoaderCircle className="spin" size={16} /> : <Film size={16} />} {latestFinal?.status === 'ready' ? '重新合并最终成片' : '生成最终成片'}</button>
      {!workspace.selection_complete && <span>请先为每个镜头选择生成结果或原片。</span>}
    </div>

    {latestFinal?.status === 'ready' && latestFinal.output_url && <div className="formal-final-result">
      <div><strong>最终海外版</strong><span>{latestFinal.width}×{latestFinal.height} · H.264 / AAC · {latestFinal.duration?.toFixed(2)} 秒</span></div>
      <video controls playsInline preload="metadata" src={normalizeBackendAssetUrl(latestFinal.output_url)} />
      <a className="primary-button" href={formalFinalOutputDownloadUrl(latestFinal.id)} download={`${workspace.episode.title}.mp4`}><Download size={16} /> 下载 MP4</a>
    </div>}
    {latestFinalAttempt?.status === 'failed' && <div className="formal-closure-error">{latestFinalAttempt.error_code || 'FINAL_RENDER_FAILED'}：{latestFinalAttempt.error_message || '最终成片生成失败。'}</div>}
    {workspace.final_outputs.length > 1 && <details className="formal-final-history"><summary>历史成片（{workspace.final_outputs.length}）</summary>{workspace.final_outputs.map((item) => <div key={item.id}><span>{item.created_at} · {item.status}{item.id === latestFinal?.id ? ' · 当前' : ''}</span>{item.status === 'ready' && <a href={formalFinalOutputDownloadUrl(item.id)} download={`${workspace.episode.title}-${item.id}.mp4`}><Download size={14} /> 下载</a>}</div>)}</details>}
  </section>
}
