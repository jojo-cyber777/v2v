import { useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, Download, ImagePlus, LoaderCircle, Sparkles, Square } from 'lucide-react'
import {
  assetUrl,
  batchBindXmaxThirtySecondMappings,
  bindXmaxBrowserAttemptSession,
  cancelXmaxThirtySecondTask,
  createXmaxThirtySecondTask,
  failXmaxThirtySecondSegment,
  failXmaxBrowserAttempt,
  finalizeXmaxBrowserAttempt,
  getXmaxBrowserAttempt,
  getXmaxThirtySecondTask,
  getXmaxThirtySecondTasks,
  getLatestXmaxThirtySecondTask,
  getXmaxThirtySecondScenePlan,
  heartbeatXmaxThirtySecondTask,
  interruptXmaxThirtySecondTaskOnPageExit,
  mergeXmaxThirtySecondTask,
  normalizeBackendAssetUrl,
  preflightXmaxThirtySecondTask,
  prepareXmaxBrowserAttempt,
  prepareXmaxThirtySecondSegmentRetry,
  reviewXmaxThirtySecondSegment,
  resumeXmaxThirtySecondPreparation,
  setXmaxThirtySecondMergeSource,
  startXmaxThirtySecondSegment,
  type Episode,
  type Project,
  type XmaxThirtySecondSegment,
  type XmaxThirtySecondTask,
  type XmaxThirtySecondTaskSummary,
  type XmaxThirtySecondScenePlan,
  type XmaxThirtySecondShotMapping,
  type XmaxThirtySecondPreflight,
  type XmaxBrowserAttempt,
  uploadXmaxThirtySecondSegment,
} from '../../../api'
import XmaxSingleShotRunner, { type SingleShotRunnerStatus, type XmaxSingleShotRunnerDiagnostics } from './XmaxSingleShotRunner'
import { XmaxStartupTimeoutError } from './XmaxStartupLifecycle'
import { consumeFormalRedrawRequest, formalRedrawDetail, FORMAL_REDRAW_REQUEST_EVENT } from './FormalRedrawLifecycle'
import { sceneReviewUnits, XMAX_SCENE_MAPPINGS_SAVED_EVENT, XmaxSceneReview } from './XmaxSceneReview'
import { customerGenerationFailureMessage } from '../../../apiRequest'

const DEFAULT_PROMPT = 'Replace the on-screen person with the fictional adult shown in the reference image. Preserve the original motion, camera, timing, framing, clothing structure, background, props, lighting, and scene composition.'

type MultiShotStatus = SingleShotRunnerStatus | 'Ready' | 'Preparing clips' | 'Saving output' | 'Raw save failed' | 'Normalization failed' | 'Persistence failed' | 'Quality status failed' | 'Merge pending' | 'Merging' | 'Merge failed' | 'Interrupted'
type ShotMappingInput = XmaxThirtySecondShotMapping & {
  shot_type?: XmaxThirtySecondShotMapping['shotType']
  character_id?: string
  selected_character_id?: string
  preserve_source?: boolean
  reference_face_confirmed?: boolean
  preserve_original?: boolean
  reference_confirmed?: boolean
}
type ShotMappingView = XmaxThirtySecondShotMapping & {
  shot_type: XmaxThirtySecondShotMapping['shotType']
  character_id?: string
  preserve_original: boolean
  reference_confirmed: boolean
}
type LightDiagnostics = Pick<XmaxSingleShotRunnerDiagnostics, 'remoteFrameCount' | 'remoteMaxFrameGapMs' | 'finalBlobSize' | 'stopReason'> & {
  activeSessionCount: number
  activeRecorderCount: number
  activeRemoteTrackCount: number
  mountedInputVideoCount: number
  mountedRemoteVideoCount: number
  retainedBlobBytes: number
  activeObjectUrlCount: number
  outputStreamReceived: boolean
  outputFileReceived: boolean
  outputUploadAttempted: boolean
  recorderCreated: boolean
  recorderStarted: boolean
  recorderState: string
  recorderStopObserved: boolean
  recorderCompletionTimedOut: boolean
  dataavailableCount: number
  recorderChunkSizes: Array<{ relativeMs: number; size: number }>
  recorderTotalBytes: number
  disconnectReason: string
  restartCount: number
  persistenceAttemptCount: number
  cleanupStartedAt: number | null
  cleanupFinishedAt: number | null
  failure: XmaxSingleShotRunnerDiagnostics['failure']
}

function recorderDiagnosticsForPersistence(value: XmaxSingleShotRunnerDiagnostics, outputUploadAttempted: boolean): Record<string, unknown> {
  return {
    outputStreamReceived: value.outputStreamReceived,
    outputFileReceived: value.outputFileReceived,
    outputUploadAttempted,
    remoteFrameCount: value.remoteFrameCount,
    remoteMaxFrameGapMs: value.remoteMaxFrameGapMs,
    recorderCreated: value.recorderCreated,
    recorderStarted: value.recorderStarted,
    recorderState: value.recorderState,
    recorderStopObserved: value.recorderStopObserved,
    recorderCompletionTimedOut: value.recorderCompletionTimedOut,
    dataavailableCount: value.dataavailableCount,
    recorderChunkCount: value.recorderChunkCount,
    recorderChunkSizes: value.recorderChunkSizes,
    recorderTotalBytes: value.recorderTotalBytes,
    recorderMimeType: value.recorderMimeType,
    recorderError: value.recorderError,
    recorderStartedAt: value.recorderStartedAt,
    recorderStoppedAt: value.recorderStoppedAt,
    finalBlobSize: value.finalBlobSize,
    disconnectReason: value.disconnectReason,
    restartCount: value.restartCount,
    stopReason: value.stopReason,
    sessionId: value.sessionId || null,
    events: value.events,
    failure: value.failure,
  }
}

function parseSegmentDiagnostics(value?: string | null): Record<string, unknown> | null {
  if (!value) return null
  try {
    const parsed: unknown = JSON.parse(value)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null
  } catch {
    return null
  }
}

export type XmaxThirtySecondOrchestrationBridge = {
  orchestrationTaskId: string
  visualTaskId: string
  providerCalls: number
  xmaxSessions: number
  onProgress: (value: {
    stage: 'analyzing_shots' | 'transforming_shots' | 'merging_visual'
    transformCompleted: number
    transformTotal: number
    fallbackCount: number
    providerCalls: number
    xmaxSessions: number
  }) => Promise<void> | void
  onComplete: (task: XmaxThirtySecondTask) => Promise<void> | void
  onFailure: (error: Error) => Promise<void> | void
}

const OUTPUT_QUALITY_ERROR_CODES = new Set([
  'OUTPUT_EMPTY',
  'OUTPUT_FILE_TOO_SMALL',
  'OUTPUT_TOO_LARGE',
  'OUTPUT_NOT_READABLE',
  'OUTPUT_DURATION_INVALID',
  'OUTPUT_DURATION_TOO_SHORT',
  'OUTPUT_VIDEO_STREAM_INVALID',
  'OUTPUT_FRAME_DECODE_FAILED',
  'OUTPUT_BLACK_FRAMES',
  'OUTPUT_FORMAT_INVALID',
])

class SegmentOutputQualityError extends Error {
  readonly task: XmaxThirtySecondTask

  constructor(message: string, task: XmaxThirtySecondTask) {
    super(message)
    this.name = 'SegmentOutputQualityError'
    this.task = task
  }
}

function sourceUrl(episode: Episode) {
  if (!episode.video_filename) return ''
  const suffix = episode.video_filename.toLowerCase().endsWith('.mov') ? '.mov' : '.mp4'
  return normalizeBackendAssetUrl(assetUrl(`/uploads/${episode.id}${suffix}`))
}

function statusLabel(status: MultiShotStatus) {
  const labels: Record<string, string> = {
    'Preparing clips': 'Preparing shots',
    Preparing: 'Preparing Shot',
    'Saving output': 'Saving Shot output',
    'Persistence failed': 'Save failed',
    'Quality status failed': 'Quality status save failed',
    'Merge pending': 'Ready to merge',
    Merging: 'Merging final video',
    'Merge failed': 'Merge failed',
    Interrupted: 'Interrupted',
  }
  return labels[status] || status
}

function mapTaskStatus(nextTask: XmaxThirtySecondTask): MultiShotStatus {
  if (!nextTask.preparation_complete && nextTask.preparation_status !== 'failed') return 'Preparing clips'
  if (nextTask.action_state === 'awaiting_user_action') return 'Ready'
  if (nextTask.action_state === 'ready_to_merge') return 'Merge pending'
  if (nextTask.status === 'interrupted') return 'Interrupted'
  if (nextTask.status === 'failed') return 'Failed'
  if (nextTask.status === 'merge_failed') return 'Merge failed'
  if (nextTask.status === 'merge_pending') return 'Merge pending'
  if (nextTask.status === 'merging') return 'Merging'
  if (nextTask.status === 'completed' && nextTask.final_output_url && nextTask.result_valid) return 'Completed'
  if (nextTask.status === 'completed' && nextTask.segments.every((segment) => segment.status === 'completed')) return 'Merge pending'
  if (nextTask.status === 'cancelled') return 'Cancelled'
  return nextTask.status === 'pending' ? 'Ready' : 'Preparing'
}

function segmentUsesUserSource(segment: XmaxThirtySecondSegment) {
  return segment.preserve_source_by_user === true
}

function segmentHasCurrentOutput(segment: XmaxThirtySecondSegment) {
  return segment.status === 'completed'
    && segment.execution_status !== 'failed'
    && segment.quality_status === 'approved'
    && Boolean(segment.output_url)
}

function segmentUsesLevel2Evaluation(segment: XmaxThirtySecondSegment) {
  return segment.processing_mode === 'level2_crop'
    && segment.preserve_current_unit_version === true
    && Boolean(segment.preprocessed_source_url && segment.preprocessed_source_sha256)
}

function taskReviewPlan(task: XmaxThirtySecondTask, analyzedPlan?: XmaxThirtySecondScenePlan | null): XmaxThirtySecondScenePlan {
  const parentIds = new Set(task.segments.map((segment) => segment.parent_shot_id).filter(Boolean))
  return {
    episode_id: task.episode_id,
    project_id: task.project_id,
    source_duration: task.source_duration,
    threshold: analyzedPlan?.threshold || 0.35,
    candidate_cuts: analyzedPlan?.candidate_cuts || [],
    base_shot_count: parentIds.size || task.segments.length,
    transform_unit_count: task.segments.length,
    base_shots: analyzedPlan?.base_shots || [],
    shots: task.segments.map((segment) => ({
      sequence: segment.sequence,
      transform_unit_id: segment.transform_unit_id,
      parent_shot_id: segment.parent_shot_id || undefined,
      source_start: segment.source_start ?? segment.start_time,
      source_end: segment.source_end ?? segment.end_time,
      target_duration: segment.duration,
      scene_score: Number(segment.scene_score || 0),
      cut_method: segment.cut_method || 'persisted_task',
      risk_score: segment.risk_score || 0,
      risk_level: (segment.risk_level as 'low' | 'medium' | 'high' | undefined) || 'low',
      risk_factors: [],
      shot_type: (segment.shot_type as XmaxThirtySecondScenePlan['shots'][number]['shot_type']) || 'unknown',
      detected_people: [],
      mapping_status: segment.mapping_status || 'manual_required',
      preserve_original: Boolean(segment.preserve_original),
      first_frame_url: segment.first_frame_url || '',
      middle_frame_url: segment.middle_frame_url || '',
      last_frame_url: segment.last_frame_url || '',
    })),
  }
}

export default function XmaxThirtySecondPanel({
  project,
  episode,
  orchestration,
  formalRedrawExecutorOnly = false,
}: {
  project: Project
  episode: Episode
  orchestration?: XmaxThirtySecondOrchestrationBridge
  formalRedrawExecutorOnly?: boolean
}) {
  const navigate = useNavigate()
  const localContainerRef = useRef<HTMLDivElement>(null)
  const remoteContainerRef = useRef<HTMLDivElement>(null)
  const runnerRef = useRef<XmaxSingleShotRunner | null>(null)
  const taskRef = useRef<XmaxThirtySecondTask | null>(null)
  const runInFlightRef = useRef(false)
  const mergeInFlightRef = useRef(false)
  const cancelledRef = useRef(false)
  const heartbeatTimerRef = useRef<number | null>(null)
  const preparationPollRef = useRef<{ key: string; promise: Promise<XmaxThirtySecondTask> } | null>(null)
  const preparationPollingStoppedRef = useRef(false)
  const localBlobRef = useRef<Blob | null>(null)
  const localObjectUrlRef = useRef('')
  const pendingUploadRef = useRef<{ taskId: string; segmentId: string; fileName: string } | null>(null)
  const persistenceAttemptCountRef = useRef(0)
  const orchestrationProviderCallsRef = useRef(0)
  const orchestrationSessionsRef = useRef(0)
  const orchestrationSessionIdsRef = useRef<Set<string>>(new Set())
  const formalRedrawExecutorRef = useRef<(unitId: string, pointsLimit: number) => Promise<{ status: 'finished' | 'failed'; error?: string }>>(async () => ({
    status: 'failed',
    error: 'The Xmax redraw executor is not ready.',
  }))
  const pageExitReportedRef = useRef(false)
  const activeBrowserAttemptRef = useRef<string | null>(null)
  const [referenceFile, setReferenceFile] = useState<File | null>(null)
  const [referencePreview, setReferencePreview] = useState('')
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT)
  const [status, setStatus] = useState<MultiShotStatus>('Ready')
  const [task, setTask] = useState<XmaxThirtySecondTask | null>(null)
  const [activeTaskId, setActiveTaskId] = useState<string | null>(() => new URLSearchParams(window.location.search).get('task_id') || window.localStorage.getItem(`xmax-thirty-second-active:${episode.id}`))
  const [completedResultTask, setCompletedResultTask] = useState<XmaxThirtySecondTask | null>(null)
  const [activeCandidates, setActiveCandidates] = useState<XmaxThirtySecondTaskSummary[]>([])
  const [historicalTasks, setHistoricalTasks] = useState<XmaxThirtySecondTaskSummary[]>([])
  const [taskSelectionRequired, setTaskSelectionRequired] = useState(false)
  const [historicalTask, setHistoricalTask] = useState<XmaxThirtySecondTask | null>(null)
  const [segments, setSegments] = useState<XmaxThirtySecondSegment[]>([])
  const [currentSequence, setCurrentSequence] = useState(0)
  const [error, setError] = useState('')
  const [localBlobUrl, setLocalBlobUrl] = useState('')
  const [localBlobSize, setLocalBlobSize] = useState(0)
  const [diagnostics, setDiagnostics] = useState<LightDiagnostics | null>(null)
  const [mergePhase, setMergePhase] = useState('')
  const [scenePlan, setScenePlan] = useState<XmaxThirtySecondScenePlan | null>(null)
  const [mappingError, setMappingError] = useState('')
  const [shotMappings, setShotMappingsRaw] = useState<Record<string, XmaxThirtySecondShotMapping>>({})
  const [preflight, setPreflight] = useState<XmaxThirtySecondPreflight | null>(null)
  const [selectedSequences, setSelectedSequences] = useState<number[]>([])
  const [batchCharacterId, setBatchCharacterId] = useState('')
  const [requestedPointsLimit, setRequestedPointsLimit] = useState(20)
  const [browserAttempt, setBrowserAttempt] = useState<XmaxBrowserAttempt | null>(null)
  const [level2RunConfirmed, setLevel2RunConfirmed] = useState(false)

  useEffect(() => {
    if (!orchestration) return
    orchestrationProviderCallsRef.current = Math.max(orchestrationProviderCallsRef.current, orchestration.providerCalls)
    orchestrationSessionsRef.current = Math.max(orchestrationSessionsRef.current, orchestration.xmaxSessions)
  }, [orchestration?.providerCalls, orchestration?.xmaxSessions])

  const normalizeShotMapping = (mapping: ShotMappingInput): XmaxThirtySecondShotMapping => ({
    shotType: mapping.shot_type ?? mapping.shotType ?? 'unknown',
    selectedCharacterId: mapping.character_id ?? mapping.selected_character_id ?? mapping.selectedCharacterId,
    preserveSource: mapping.preserve_original ?? mapping.preserve_source ?? mapping.preserveSource ?? false,
    referenceFaceConfirmed: mapping.reference_confirmed ?? mapping.reference_face_confirmed ?? mapping.referenceFaceConfirmed ?? false,
  })
  const setShotMappings = (next: Record<string, ShotMappingInput> | ((current: Record<string, ShotMappingInput>) => Record<string, ShotMappingInput>)) => {
    setPreflight(null)
    setShotMappingsRaw((current) => {
      const input = typeof next === 'function' ? next(current as Record<string, ShotMappingInput>) : next
      return Object.fromEntries(Object.entries(input).map(([sequence, mapping]) => [sequence, normalizeShotMapping(mapping)]))
    })
  }

  const episodeSource = useMemo(() => sourceUrl(episode), [episode.id, episode.video_filename])
  const persistedReferenceUrl = normalizeBackendAssetUrl(task?.reference_url)
  const referenceSource = referencePreview || persistedReferenceUrl
  const running = runInFlightRef.current || mergeInFlightRef.current || ['Preparing clips', 'Preparing', 'Connecting', 'Uploading reference', 'Starting transformation', 'Running', 'Recording', 'Stopping', 'Saving output', 'Merging'].includes(status)
  const hasIncompleteShot = Boolean(task?.segments.some((segment) => !segmentHasCurrentOutput(segment) && !segmentUsesUserSource(segment) && !segment.preserve_original))
  const confirmedCharacters = useMemo(() => project.characters.filter((character) => character.confirmed && character.priority_level === 'Locked' && Boolean(character.target_reference_path || character.main_reference_image || character.source_reference_image)), [project.characters])
  const mappingFor = (sequence: number): ShotMappingView | undefined => {
    const mapping = shotMappings[String(sequence)]
    if (!mapping) return undefined
    return {
      ...mapping,
      shot_type: mapping.shotType,
      character_id: mapping.selectedCharacterId,
      preserve_original: mapping.preserveSource === true,
      reference_confirmed: mapping.referenceFaceConfirmed === true,
    }
  }
  const draftMappingIsValid = (shot: XmaxThirtySecondScenePlan['shots'][number]) => {
    const mapping = mappingFor(shot.sequence)
    if (!mapping) return false
    if (mapping.shotType === 'single_character') {
      const characterId = mapping.selectedCharacterId
      const confirmed = mapping.referenceFaceConfirmed === true
      return Boolean(characterId && confirmed && confirmedCharacters.some((character) => character.id === characterId))
    }
    if (mapping.shotType === 'multi_character') return mapping.preserveSource === true
    return mapping.shotType === 'no_character' || mapping.shotType === 'transition'
  }
  const preflightFor = (sequence: number) => preflight?.shots.find((item) => item.sequence === sequence)
  const mappingIsValid = (shot: XmaxThirtySecondScenePlan['shots'][number]) => Boolean(draftMappingIsValid(shot) && preflightFor(shot.sequence)?.mappingValid === true)
  const reviewUnits = scenePlan ? sceneReviewUnits(scenePlan) : []
  const draftMappingsReady = Boolean(reviewUnits.length > 0 && reviewUnits.every(draftMappingIsValid))
  const mappingsReady = Boolean(reviewUnits.length > 0 && reviewUnits.every(mappingIsValid))
  const canStart = Boolean(episodeSource && prompt.trim() && mappingsReady && !running && !localBlobRef.current)
  const canPreflight = Boolean(episodeSource && draftMappingsReady && !running && !localBlobRef.current)
  const savedCount = segments.filter((segment) => segment.status === 'completed').length
  const allShotsSaved = segments.length > 0 && savedCount === segments.length
  const mergeReadyCount = segments.filter((segment) => {
    if (segmentUsesUserSource(segment) || segment.preserve_original || segment.mapping_status === 'preserve_original') return Boolean(segment.source_url)
    return segmentHasCurrentOutput(segment)
  }).length
  const allShotsReadyForMerge = segments.length > 0 && mergeReadyCount === segments.length
  const mergeUiVisible = Boolean(task && !task.final_output_url && (allShotsSaved || allShotsReadyForMerge))
  const canMerge = Boolean(task && allShotsReadyForMerge && !task.final_output_url && !running && !localBlobRef.current)
  const generatedApprovedCount = segments.filter((segment) => segmentHasCurrentOutput(segment) && !segmentUsesUserSource(segment)).length
  const sourceFallbackCount = segments.filter(segmentUsesUserSource).length
  const reviewRequiredCount = segments.filter((segment) => !segment.preserve_original && (segment.quality_status || 'review_required') === 'review_required').length
  const blockedCount = segments.filter((segment) => !segment.preserve_original && !segmentUsesUserSource(segment) && !segmentHasCurrentOutput(segment)).length
  const providerShotCount = task
    ? segments.filter((segment) => segment.shot_type === 'single_character' && !segmentHasCurrentOutput(segment) && !segmentUsesUserSource(segment)).length
    : reviewUnits.filter((shot) => mappingFor(shot.sequence)?.shotType === 'single_character').length
  const reviewingPersistedUnits = Boolean(task && reviewUnits.length === task.segments.length)

  const updateTask = (nextTask: XmaxThirtySecondTask) => {
    taskRef.current = nextTask
    setActiveTaskId(nextTask.task_id)
    if (!orchestration) window.localStorage.setItem(`xmax-thirty-second-active:${episode.id}`, nextTask.task_id)
    setTask(nextTask)
    setSegments(nextTask.segments)
  }

  const targetProxiesReady = (currentTask: XmaxThirtySecondTask, targetIds?: Set<string>) => {
    if (!targetIds?.size) return currentTask.preparation_complete
    const targets = currentTask.segments.filter((segment) => targetIds.has(segment.segment_id))
    return targets.length === targetIds.size
      && targets.every((segment) => segment.proxy_ready === true && segment.proxy_preparation_status === 'ready')
  }

  const waitForPreparation = (initialTask: XmaxThirtySecondTask, targetIds?: Set<string>) => {
    if (targetProxiesReady(initialTask, targetIds)) return Promise.resolve(initialTask)
    const pollKey = `${initialTask.task_id}:${targetIds?.size ? [...targetIds].sort().join(',') : '*'}`
    if (preparationPollRef.current?.key === pollKey) return preparationPollRef.current.promise
    const promise = (async () => {
      let currentTask = await resumeXmaxThirtySecondPreparation(episode.id, initialTask.task_id)
      updateTask(currentTask)
      setStatus(mapTaskStatus(currentTask))
      while (!targetProxiesReady(currentTask, targetIds)) {
        if (preparationPollingStoppedRef.current) throw new Error('Local proxy preparation polling stopped because the page was closed.')
        if (currentTask.status === 'failed' || currentTask.preparation_status === 'failed') {
          throw new Error(currentTask.error_message || 'Local proxy preparation failed.')
        }
        setMergePhase(`Preparing local clips ${currentTask.prepared_units}/${currentTask.preparation_total_units}`)
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
        currentTask = await getXmaxThirtySecondTask(currentTask.task_id, episode.id)
        updateTask(currentTask)
      }
      setMergePhase('')
      setStatus(mapTaskStatus(currentTask))
      return currentTask
    })().finally(() => {
      if (preparationPollRef.current?.key === pollKey) preparationPollRef.current = null
    })
    preparationPollRef.current = { key: pollKey, promise }
    return promise
  }

  const toggleShotSelection = (sequence: number) => {
    setSelectedSequences((current) => current.includes(sequence) ? current.filter((item) => item !== sequence) : [...current, sequence].sort((a, b) => a - b))
  }

  const selectAllShots = () => setSelectedSequences(reviewUnits.map((shot) => shot.sequence))
  const clearShotSelection = () => setSelectedSequences([])
  const selectSingleCharacterShots = () => setSelectedSequences(reviewUnits.filter((shot) => mappingFor(shot.sequence)?.shotType === 'single_character').map((shot) => shot.sequence))

  const applyBatchMapping = async (preserveSource: boolean) => {
    if (selectedSequences.length === 0) {
      setError('Select at least one Shot first.')
      return
    }
    if (!preserveSource && !batchCharacterId) {
      setError('Choose a confirmed character first.')
      return
    }
    try {
      const response = await batchBindXmaxThirtySecondMappings(episode.id, selectedSequences, batchCharacterId, preserveSource)
      const normalized = Object.fromEntries(response.mappings.map((mapping) => [String(mapping.sequence), {
        shotType: mapping.shot_type,
        selectedCharacterId: mapping.selected_character_id || undefined,
        referenceFaceConfirmed: mapping.reference_face_confirmed,
        preserveSource: mapping.preserve_source,
      }]))
      setShotMappings((current) => ({ ...current, ...normalized }))
      setError('')
      setMergePhase(`${selectedSequences.length} Shot mapping drafts updated. No task was created.`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const openTask = async (taskId: string) => {
    const nextTask = await getXmaxThirtySecondTask(taskId, episode.id)
    updateTask(nextTask)
    setTaskSelectionRequired(false)
    navigate(`/project/${project.id}/generate?episode_id=${encodeURIComponent(episode.id)}&task_id=${encodeURIComponent(taskId)}`)
  }

  const returnToCurrentResult = () => {
    window.localStorage.removeItem(`xmax-thirty-second-active:${episode.id}`)
    setActiveTaskId(null)
    taskRef.current = null
    setTask(null)
    setSegments([])
    setStatus('Ready')
    setError('')
    navigate(`/project/${project.id}/generate?episode_id=${encodeURIComponent(episode.id)}`)
  }

  const releaseLocalBlob = () => {
    if (localObjectUrlRef.current) URL.revokeObjectURL(localObjectUrlRef.current)
    localObjectUrlRef.current = ''
    localBlobRef.current = null
    pendingUploadRef.current = null
    setLocalBlobUrl('')
    setLocalBlobSize(0)
    setDiagnostics((current) => current ? { ...current, retainedBlobBytes: 0, activeObjectUrlCount: 0 } : current)
  }

  const retainLocalBlob = (blob: Blob, taskId: string, segmentId: string, fileName: string) => {
    releaseLocalBlob()
    const objectUrl = URL.createObjectURL(blob)
    localBlobRef.current = blob
    localObjectUrlRef.current = objectUrl
    pendingUploadRef.current = { taskId, segmentId, fileName }
    setLocalBlobUrl(objectUrl)
    setLocalBlobSize(blob.size)
    setDiagnostics((current) => current ? { ...current, retainedBlobBytes: blob.size, activeObjectUrlCount: 1 } : current)
  }

  useEffect(() => {
    let active = true
    const params = new URLSearchParams(window.location.search)
    const explicitTaskId = orchestration?.visualTaskId || params.get('task_id')
    const storedTaskId = orchestration ? null : window.localStorage.getItem(`xmax-thirty-second-active:${episode.id}`)
    void (async () => {
      const index = await getXmaxThirtySecondTasks(episode.id)
      if (!active) return
      setActiveCandidates(index.active_candidates)
      setHistoricalTasks(index.historical_tasks)

      const completedSummary = index.completed_results[0]
      if (completedSummary) {
        const completed = await getXmaxThirtySecondTask(completedSummary.task_id, episode.id)
        if (!active) return
        setCompletedResultTask(completed)
      } else {
        setCompletedResultTask(null)
      }
      if (!active) return

      const storedSummary = storedTaskId ? [...index.active_candidates, ...index.completed_results].find((item) => item.task_id === storedTaskId) : undefined
      const requestedTaskId = explicitTaskId || storedSummary?.task_id
      if (requestedTaskId) {
        const exact = await getXmaxThirtySecondTask(requestedTaskId, episode.id)
        if (!active) return
        updateTask(exact)
        setPrompt(exact.prompt || DEFAULT_PROMPT)
        setTaskSelectionRequired(false)
        setStatus(mapTaskStatus(exact))
        return
      }

      taskRef.current = null
      setTask(null)
      setSegments([])
      setActiveTaskId(null)
      const liveCandidates = index.active_candidates.filter((item) => ['pending', 'running', 'local_ready', 'uploading', 'merge_pending', 'merging'].includes(item.status))
      if (liveCandidates.length === 1) {
        const exact = await getXmaxThirtySecondTask(liveCandidates[0].task_id, episode.id)
        if (!active) return
        updateTask(exact)
        setPrompt(exact.prompt || DEFAULT_PROMPT)
        setTaskSelectionRequired(false)
        setStatus(mapTaskStatus(exact))
      } else if (liveCandidates.length > 1) {
        setTaskSelectionRequired(true)
        setStatus('Ready')
      } else {
        setTaskSelectionRequired(false)
        setStatus('Ready')
      }
    })().catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : String(reason))
    })
    return () => { active = false }
  }, [episode.id, orchestration?.visualTaskId])

  useEffect(() => {
    if (!task || task.preparation_complete || task.preparation_status === 'failed') return
    void waitForPreparation(task).catch((reason) => {
      if (preparationPollingStoppedRef.current) return
      setStatus('Failed')
      setError(reason instanceof Error ? reason.message : String(reason))
    })
  }, [task?.task_id])

  useEffect(() => {
    const refreshAfterMappingSave = (event: Event) => {
      const detail = (event as CustomEvent<{ episodeId?: string; activeTaskId?: string | null }>).detail
      if (detail?.episodeId !== episode.id) return
      const taskId = detail.activeTaskId || taskRef.current?.task_id
      if (!taskId) return
      void getXmaxThirtySecondTask(taskId, episode.id)
        .then((updated) => {
          updateTask(updated)
          setStatus(mapTaskStatus(updated))
          setError('')
        })
        .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
    }
    window.addEventListener(XMAX_SCENE_MAPPINGS_SAVED_EVENT, refreshAfterMappingSave)
    return () => window.removeEventListener(XMAX_SCENE_MAPPINGS_SAVED_EVENT, refreshAfterMappingSave)
  }, [episode.id])

  useEffect(() => {
    let active = true
    if (new URLSearchParams(window.location.search).get('legacy_latest') !== 'true') return
    void getLatestXmaxThirtySecondTask(episode.id).then((latest) => {
      if (!active || !latest) return
      if (latest.status === 'completed') {
        setHistoricalTask(latest)
        setStatus('Ready')
        return
      }
      updateTask(latest)
      setPrompt(latest.prompt || DEFAULT_PROMPT)
      setStatus(mapTaskStatus(latest))
      if (latest.status === 'interrupted') setError('上次浏览器运行已中断。已完成 Shot 会保留，点击继续生成只处理未完成 Shot。')
    }).catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : String(reason))
    })
    return () => { active = false }
  }, [episode.id])

  useEffect(() => {
    let active = true
    setScenePlan(null)
    setMappingError('')
    setPreflight(null)
    setShotMappings({})
    void getXmaxThirtySecondScenePlan(episode.id).then((plan) => {
      if (!active) return
      const currentTask = taskRef.current
      setScenePlan(currentTask ? taskReviewPlan(currentTask, plan) : plan)
      const initial: Record<string, XmaxThirtySecondShotMapping> = {}
      const reviewShots = currentTask?.segments || sceneReviewUnits(plan)
      reviewShots.forEach((shot) => { initial[String(shot.sequence)] = { shotType: 'unknown', preserveSource: false, referenceFaceConfirmed: false } })
      setShotMappings((current) => ({ ...initial, ...current }))
    }).catch((reason) => { if (active) setMappingError(reason instanceof Error ? reason.message : String(reason)) })
    return () => { active = false }
  }, [episode.id])

  useEffect(() => {
    if (!task) return
    const persisted: Record<string, XmaxThirtySecondShotMapping> = {}
    task.segments.forEach((segment) => {
      if (segment.shot_type && segment.shot_type !== 'unknown') persisted[String(segment.sequence)] = { shotType: segment.shot_type as XmaxThirtySecondShotMapping['shotType'], selectedCharacterId: segment.selected_character_id || undefined, preserveSource: Boolean(segment.preserve_original), referenceFaceConfirmed: segment.mapping_status === 'manual_confirmed' }
    })
    if (Object.keys(persisted).length) setShotMappingsRaw((current) => ({ ...current, ...persisted }))
    setScenePlan((current) => taskReviewPlan(task, current))
  }, [task?.task_id])

  useEffect(() => {
    if (!referenceFile) {
      setReferencePreview('')
      return
    }
    const url = URL.createObjectURL(referenceFile)
    setReferencePreview(url)
    return () => URL.revokeObjectURL(url)
  }, [referenceFile])

  const reportBrowserExit = () => {
    if (pageExitReportedRef.current || (!runInFlightRef.current && !mergeInFlightRef.current)) return
    const activeTask = taskRef.current
    if (!activeTask || activeTask.status === 'completed' || activeTask.status === 'cancelled') return
    pageExitReportedRef.current = true
    interruptXmaxThirtySecondTaskOnPageExit(activeTask.task_id)
    if (activeBrowserAttemptRef.current) {
      void finalizeXmaxBrowserAttempt(activeBrowserAttemptRef.current, true).catch(() => undefined)
    }
  }

  useEffect(() => {
    preparationPollingStoppedRef.current = false
    const handlePageExit = () => reportBrowserExit()
    window.addEventListener('pagehide', handlePageExit)
    window.addEventListener('beforeunload', handlePageExit)
    return () => {
      window.removeEventListener('pagehide', handlePageExit)
      window.removeEventListener('beforeunload', handlePageExit)
      reportBrowserExit()
      preparationPollingStoppedRef.current = true
      cancelledRef.current = true
      if (heartbeatTimerRef.current !== null) window.clearInterval(heartbeatTimerRef.current)
      heartbeatTimerRef.current = null
      const runner = runnerRef.current
      if (runner) void runner.stop('component_unmount')
      releaseLocalBlob()
    }
  }, [])

  const handleReferenceChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null
    event.target.value = ''
    if (!file) return
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      setError('Choose a PNG, JPEG, or WebP reference image.')
      return
    }
    setError('')
    setReferenceFile(file)
  }

  const applyDiagnostics = (value: XmaxSingleShotRunnerDiagnostics) => {
    if (orchestration && value.sessionId && !orchestrationSessionIdsRef.current.has(value.sessionId)) {
      orchestrationSessionIdsRef.current.add(value.sessionId)
      orchestrationSessionsRef.current = orchestrationSessionIdsRef.current.size
    }
    setDiagnostics({
      remoteFrameCount: value.remoteFrameCount,
      remoteMaxFrameGapMs: value.remoteMaxFrameGapMs,
      finalBlobSize: value.finalBlobSize,
      stopReason: value.stopReason,
      activeSessionCount: value.activeSessionCount,
      activeRecorderCount: value.activeRecorderCount,
      activeRemoteTrackCount: value.activeRemoteTrackCount,
      mountedInputVideoCount: value.mountedInputVideoCount,
      mountedRemoteVideoCount: value.mountedRemoteVideoCount,
      retainedBlobBytes: value.retainedBlobBytes,
      activeObjectUrlCount: value.activeObjectUrlCount,
      outputStreamReceived: value.outputStreamReceived,
      outputFileReceived: value.outputFileReceived,
      outputUploadAttempted: value.outputUploadAttempted,
      recorderCreated: value.recorderCreated,
      recorderStarted: value.recorderStarted,
      recorderState: value.recorderState,
      recorderStopObserved: value.recorderStopObserved,
      recorderCompletionTimedOut: value.recorderCompletionTimedOut,
      dataavailableCount: value.dataavailableCount,
      recorderChunkSizes: value.recorderChunkSizes,
      recorderTotalBytes: value.recorderTotalBytes,
      disconnectReason: value.disconnectReason,
      restartCount: value.restartCount,
      persistenceAttemptCount: persistenceAttemptCountRef.current,
      cleanupStartedAt: value.cleanupStartedAt,
      cleanupFinishedAt: value.cleanupFinishedAt,
      failure: value.failure,
    })
  }

  const beginHeartbeat = (taskId: string) => {
    if (heartbeatTimerRef.current !== null) window.clearInterval(heartbeatTimerRef.current)
    heartbeatTimerRef.current = window.setInterval(() => { void heartbeatXmaxThirtySecondTask(taskId).catch(() => undefined) }, 2000)
  }

  const endHeartbeat = () => {
    if (heartbeatTimerRef.current !== null) window.clearInterval(heartbeatTimerRef.current)
    heartbeatTimerRef.current = null
  }

  const reviewSegment = async (segment: XmaxThirtySecondSegment, qualityStatus: NonNullable<XmaxThirtySecondSegment['quality_status']>) => {
    if (!task) return
    const reviewed = await reviewXmaxThirtySecondSegment(task.task_id, segment.segment_id, qualityStatus)
    updateTask(reviewed)
  }

  const setMergeSource = async (segment: XmaxThirtySecondSegment, useSourceForMerge: boolean) => {
    if (!task) return
    const updated = await setXmaxThirtySecondMergeSource(task.task_id, segment.segment_id, useSourceForMerge)
    updateTask(updated)
    setStatus(mapTaskStatus(updated))
  }

  const prepareSegmentRetry = async (segment: XmaxThirtySecondSegment) => {
    if (!task) return
    const plan = await prepareXmaxThirtySecondSegmentRetry(task.task_id, segment.segment_id)
    setMergePhase(`Shot ${plan.sequence} retry prepared with ${plan.reference_filename || 'the latest character reference'}; no Provider call was started.`)
    const refreshed = await getXmaxThirtySecondTask(task.task_id, episode.id)
    updateTask(refreshed)
    setStatus(mapTaskStatus(refreshed))
  }

  const recoverSegmentAutomatically = async (failedTask: XmaxThirtySecondTask, segmentId: string) => {
    const failedSegment = failedTask.segments.find((item) => item.segment_id === segmentId)
    if (!failedSegment) throw new Error('The failed Transform Unit could not be reloaded.')
    if (failedSegment.use_source_for_merge) {
      setMergePhase(`Transform Unit ${failedSegment.sequence} used its source frame after one failed retry.`)
      updateTask(failedTask)
      return { action: 'fallback' as const, task: failedTask }
    }
    if (failedSegment.status !== 'failed' || Number(failedSegment.retry_version || 0) >= 1) {
      throw new Error(`Transform Unit ${failedSegment.sequence} failed without an available recovery action.`)
    }
    await prepareXmaxThirtySecondSegmentRetry(failedTask.task_id, segmentId, true)
    const retriedTask = await getXmaxThirtySecondTask(failedTask.task_id, episode.id)
    updateTask(retriedTask)
    setMergePhase(`Transform Unit ${failedSegment.sequence} failed validation and is retrying once.`)
    return { action: 'retry' as const, task: retriedTask }
  }

  const persistSegment = async (currentTask: XmaxThirtySecondTask, segment: XmaxThirtySecondSegment, blob: Blob, fileName: string, segmentDiagnostics: XmaxSingleShotRunnerDiagnostics, browserAttemptId: string, preserveCurrentUnitVersion: boolean, onOutputSaved: () => void) => {
    let remoteSaved = false
    try {
      persistenceAttemptCountRef.current += 1
      setDiagnostics((current) => current ? { ...current, outputUploadAttempted: true, persistenceAttemptCount: persistenceAttemptCountRef.current } : current)
      let saved = await uploadXmaxThirtySecondSegment(currentTask.task_id, segment.segment_id, blob, fileName, {
        ...recorderDiagnosticsForPersistence(segmentDiagnostics, true),
        remoteFrameCount: segmentDiagnostics.remoteFrameCount,
        remoteMaxFrameGapMs: segmentDiagnostics.remoteMaxFrameGapMs,
        stopReason: segmentDiagnostics.stopReason,
        finalBlobSize: segmentDiagnostics.finalBlobSize,
        outputStreamReceived: segmentDiagnostics.outputStreamReceived,
        outputFileReceived: segmentDiagnostics.outputFileReceived,
        inputLoop: segmentDiagnostics.inputLoop,
        inputPlaybackCount: segmentDiagnostics.inputPlaybackCount,
        inputPlaybackStartedAt: segmentDiagnostics.inputPlaybackStartedAt,
        inputEndedAt: segmentDiagnostics.inputEndedAt,
        remoteFirstMediaTime: segmentDiagnostics.remoteFirstMediaTime,
        remoteLastMediaTime: segmentDiagnostics.remoteLastMediaTime,
        recorderStartedAt: segmentDiagnostics.recorderStartedAt,
        recorderStoppedAt: segmentDiagnostics.recorderStoppedAt,
        proxyPreRoll: segmentDiagnostics.proxyPreRoll,
        proxyContentDuration: segmentDiagnostics.proxyContentDuration,
        proxyPostRoll: segmentDiagnostics.proxyPostRoll,
        proxyTotalDuration: segmentDiagnostics.proxyTotalDuration,
        referenceFaceCount: segmentDiagnostics.referenceFaceCount,
        retainedBlobBytes: blob.size,
        persistenceAttemptCount: persistenceAttemptCountRef.current,
      }, browserAttemptId)
      remoteSaved = true
      onOutputSaved()
      const savedSegment = saved.segments.find((item) => item.segment_id === segment.segment_id)
      if (orchestration && (preserveCurrentUnitVersion || (savedSegment?.status === 'completed' && savedSegment.quality_status === 'review_required'))) {
        try {
          saved = await reviewXmaxThirtySecondSegment(
            currentTask.task_id,
            segment.segment_id,
            'approved',
            'Automated output preflight passed in the one-click workflow.',
            browserAttemptId,
          )
        } catch (qualityError) {
          const reconciled = await getXmaxThirtySecondTask(currentTask.task_id, episode.id).catch(() => null)
          const reconciledSegment = reconciled?.segments.find((item) => item.segment_id === segment.segment_id)
          if (reconciled && reconciledSegment?.quality_status === 'approved') {
            saved = reconciled
          } else {
            throw qualityError
          }
        }
      }
      releaseLocalBlob()
      updateTask(saved)
      return saved
    } catch (reason) {
      const persistedAttempt = browserAttemptId
        ? await getXmaxBrowserAttempt(browserAttemptId).catch(() => null)
        : null
      if (!remoteSaved && persistedAttempt?.raw_output_persisted) {
        remoteSaved = true
        onOutputSaved()
        releaseLocalBlob()
        setBrowserAttempt(persistedAttempt)
        if (persistedAttempt.normalization_status === 'failed') {
          setStatus('Normalization failed')
          setError(`Shot ${segment.sequence} Remote was saved, but local timeline normalization failed. The raw WebM is preserved for zero-credit recovery: ${reason instanceof Error ? reason.message : String(reason)}`)
        } else {
          setStatus('Persistence failed')
          setError(`Shot ${segment.sequence} raw Remote was saved, but later local processing did not finish: ${reason instanceof Error ? reason.message : String(reason)}`)
        }
        throw reason
      }
      const refreshed = await getXmaxThirtySecondTask(currentTask.task_id, episode.id).catch(() => null)
      const refreshedSegment = refreshed?.segments.find((item) => item.segment_id === segment.segment_id)
      if (remoteSaved && refreshed && refreshedSegment?.quality_status === 'approved') {
        releaseLocalBlob()
        updateTask(refreshed)
        return refreshed
      }
      if (refreshed && refreshedSegment?.error_code && OUTPUT_QUALITY_ERROR_CODES.has(refreshedSegment.error_code)) {
        releaseLocalBlob()
        updateTask(refreshed)
        throw new SegmentOutputQualityError(reason instanceof Error ? reason.message : String(reason), refreshed)
      }
      if (remoteSaved) {
        releaseLocalBlob()
        setStatus('Quality status failed')
        setError(`Shot ${segment.sequence} output was saved, but its quality status could not be confirmed: ${reason instanceof Error ? reason.message : String(reason)}`)
        throw reason
      }
      retainLocalBlob(blob, currentTask.task_id, segment.segment_id, fileName)
      setStatus('Raw save failed')
      setError(`Shot ${segment.sequence} generated successfully, but the immutable raw Remote could not be saved: ${reason instanceof Error ? reason.message : String(reason)}`)
      throw reason
    }
  }

  const reportOrchestrationProgress = async (
    stage: 'analyzing_shots' | 'transforming_shots' | 'merging_visual',
    currentTask: XmaxThirtySecondTask,
  ) => {
    if (!orchestration) return
    const transformSegments = currentTask.segments.filter((segment) => segment.shot_type === 'single_character' && !segment.preserve_original)
    const transformCompleted = transformSegments.filter((segment) => segmentHasCurrentOutput(segment) || segmentUsesUserSource(segment)).length
    await orchestration.onProgress({
      stage,
      transformCompleted,
      transformTotal: transformSegments.length,
      fallbackCount: currentTask.segments.filter(segmentUsesUserSource).length,
      providerCalls: orchestrationProviderCallsRef.current,
      xmaxSessions: orchestrationSessionsRef.current,
    })
  }

  const runRemainingShots = async (
    initialTask: XmaxThirtySecondTask,
    startIndex: number,
    targetSegmentIds?: Set<string>,
    pointsLimitOverride?: number,
  ) => {
    if (!targetProxiesReady(initialTask, targetSegmentIds)) {
      throw new Error(`Local proxy preparation is incomplete (${initialTask.prepared_units}/${initialTask.preparation_total_units}).`)
    }
    let currentTask = initialTask
    beginHeartbeat(currentTask.task_id)
    try {
      for (let index = startIndex; index < currentTask.segments.length; index += 1) {
        if (cancelledRef.current) return currentTask
        let segment = currentTask.segments[index]
        if (targetSegmentIds && !targetSegmentIds.has(segment.segment_id)) continue
        const level2Evaluation = segmentUsesLevel2Evaluation(segment)
        if ((segmentHasCurrentOutput(segment) && !level2Evaluation) || segmentUsesUserSource(segment) || segment.preserve_original) continue
        let unitFinished = false
        while (!unitFinished && !cancelledRef.current) {
          persistenceAttemptCountRef.current = 0
          let latestRunnerDiagnostics: XmaxSingleShotRunnerDiagnostics | null = null
          let browserAttemptId: string | null = null
          let outputPersisted = false
          let preserveCurrentUnitVersion = false
          setCurrentSequence(segment.sequence)
          setStatus('Preparing')
          try {
            const effectivePointsLimit = pointsLimitOverride ?? requestedPointsLimit
            if (!Number.isInteger(effectivePointsLimit) || effectivePointsLimit <= 0) {
              throw new Error('Enter a positive whole-number credit freeze limit before starting Xmax.')
            }
            let grant
            try {
              grant = await prepareXmaxBrowserAttempt({
                task_id: currentTask.task_id,
                unit_id: segment.segment_id,
                requested_points_limit: effectivePointsLimit,
                idempotency_key: `browser-${crypto.randomUUID()}`,
              })
            } catch (prepareError) {
              setStatus('Failed')
              setError(customerGenerationFailureMessage(prepareError))
              return currentTask
            }
            browserAttemptId = grant.attempt.id
            preserveCurrentUnitVersion = grant.runtime.preserveCurrentUnitVersion
            activeBrowserAttemptRef.current = browserAttemptId
            setBrowserAttempt(grant.attempt)
            if (grant.runtime.model !== 'x2.0' || grant.runtime.playback.playbackRate !== 1) {
              throw new Error('The prepared Xmax runtime snapshot does not match the verified SDK contract.')
            }
            if (grant.runtime.processingMode === 'level2_crop' && !grant.runtime.sourceUrl) {
              throw new Error('The prepared Level 2 attempt has no preprocessed source URL.')
            }
            if (!preserveCurrentUnitVersion) {
              currentTask = await startXmaxThirtySecondSegment(currentTask.task_id, segment.segment_id)
              updateTask(currentTask)
              const runningSegment = currentTask.segments.find((item) => item.segment_id === segment.segment_id)
              if (!runningSegment) throw new Error('The active Transform Unit is missing after browser-start.')
              segment = runningSegment
            }
            if (runnerRef.current) throw new Error('A previous Shot Runner is still active.')
            const runner = new XmaxSingleShotRunner({
              temporaryApiKey: grant.temporaryApiKey,
              sourceUrl: normalizeBackendAssetUrl(grant.runtime.sourceUrl),
              referenceUrl: normalizeBackendAssetUrl(grant.runtime.refImageUrl),
              sourceFilename: `transform-unit-${String(segment.sequence).padStart(3, '0')}.mp4`,
              sourceDuration: grant.runtime.sourceDuration,
              contentDuration: grant.runtime.contentDuration,
              preRollDuration: grant.runtime.preRollDuration,
              postRollDuration: grant.runtime.postRollDuration,
              prompt: grant.runtime.prompt,
              localElement: localContainerRef.current as HTMLDivElement,
              remoteElement: remoteContainerRef.current as HTMLDivElement,
              onStatus: setStatus,
              onSessionCreated: async (sessionUid) => {
                const bound = await bindXmaxBrowserAttemptSession(grant.attempt.id, sessionUid)
                setBrowserAttempt(bound)
              },
              onDiagnostics: (value) => {
                latestRunnerDiagnostics = value
                applyDiagnostics(value)
              },
              onError: (reason) => setError(reason.message),
            })
            runnerRef.current = runner
            if (orchestration) {
              orchestrationProviderCallsRef.current += 1
            }
            const result = await runner.run()
            latestRunnerDiagnostics = result.diagnostics
            applyDiagnostics(result.diagnostics)
            if (cancelledRef.current) return currentTask
            if (!result.outputBlob) {
              throw new Error(
                result.diagnostics.failure?.message
                || `Transform Unit ${segment.sequence} completed without a WebM output.`,
              )
            }
            const leaked = [
              result.diagnostics.activeSessionCount > 0 ? 'Session' : '',
              result.diagnostics.activeRecorderCount > 0 ? 'Recorder' : '',
              result.diagnostics.activeRemoteTrackCount > 0 ? 'remote track' : '',
              result.diagnostics.mountedInputVideoCount > 0 ? 'input video' : '',
              result.diagnostics.mountedRemoteVideoCount > 0 ? 'remote video' : '',
            ].filter(Boolean)
            if (leaked.length > 0) throw new Error(`Transform Unit ${segment.sequence} cleanup left active resources: ${leaked.join(', ')}.`)
            retainLocalBlob(result.outputBlob, currentTask.task_id, segment.segment_id, result.fileName)
            setStatus('Saving output')
            currentTask = await persistSegment(
              currentTask,
              segment,
              result.outputBlob,
              result.fileName,
              result.diagnostics,
              grant.attempt.id,
              preserveCurrentUnitVersion,
              () => { outputPersisted = true },
            )
            const finalized = await finalizeXmaxBrowserAttempt(grant.attempt.id)
            setBrowserAttempt(finalized)
            activeBrowserAttemptRef.current = null
            await reportOrchestrationProgress('transforming_shots', currentTask)
            unitFinished = true
          } catch (reason) {
            if (browserAttemptId) {
              if (!outputPersisted) {
                await failXmaxBrowserAttempt(
                  browserAttemptId,
                  reason instanceof XmaxStartupTimeoutError
                    ? `XMAX_${reason.stage.toUpperCase()}_TIMEOUT`
                    : localBlobRef.current ? 'REMOTE_OUTPUT_PERSISTENCE_FAILED' : 'XMAX_BROWSER_ATTEMPT_FAILED',
                  reason instanceof Error ? reason.message : String(reason),
                  Boolean(localBlobRef.current),
                  latestRunnerDiagnostics
                    ? recorderDiagnosticsForPersistence(latestRunnerDiagnostics, false)
                    : undefined,
                ).then(setBrowserAttempt).catch(() => undefined)
              }
              await finalizeXmaxBrowserAttempt(browserAttemptId).then(setBrowserAttempt).catch(() => undefined)
              activeBrowserAttemptRef.current = null
            }
            if (preserveCurrentUnitVersion) {
              setStatus('Failed')
              setError(`Level 2 evaluation stopped without changing the current Unit: ${reason instanceof Error ? reason.message : String(reason)}`)
              return currentTask
            }
            if (localBlobRef.current && !(reason instanceof SegmentOutputQualityError)) throw reason
            let failedTask: XmaxThirtySecondTask
            if (reason instanceof SegmentOutputQualityError) {
              failedTask = reason.task
            } else {
              failedTask = await failXmaxThirtySecondSegment(
                currentTask.task_id,
                segment.segment_id,
                'SHOT_PROVIDER_OR_RECORDING_FAILED',
                reason instanceof Error ? reason.message : String(reason),
                latestRunnerDiagnostics ? recorderDiagnosticsForPersistence(latestRunnerDiagnostics, false) : undefined,
              )
            }
            if (orchestration) {
              currentTask = failedTask
              updateTask(currentTask)
              await reportOrchestrationProgress('transforming_shots', currentTask)
              setStatus('Failed')
              setError(
                latestRunnerDiagnostics?.failure?.message
                || (reason instanceof Error ? reason.message : String(reason)),
              )
              return currentTask
            } else {
              const recovery = await recoverSegmentAutomatically(failedTask, segment.segment_id)
              currentTask = recovery.task
              if (recovery.action === 'fallback') {
                await reportOrchestrationProgress('transforming_shots', currentTask)
                unitFinished = true
              } else {
                const refreshedSegment = currentTask.segments.find((item) => item.segment_id === segment.segment_id)
                if (!refreshedSegment) throw new Error('The retried Transform Unit is missing.')
                segment = refreshedSegment
                setError('')
              }
            }
          } finally {
            runnerRef.current = null
          }
        }
      }
      if (!cancelledRef.current) {
        setCurrentSequence(0)
        setStatus(currentTask.final_output_url ? 'Completed' : 'Merge pending')
        const fallbackUnits = currentTask.segments.filter(segmentUsesUserSource).length
        if (fallbackUnits > 0) {
          setMergePhase(`Generation continued successfully. ${fallbackUnits} Transform Unit${fallbackUnits === 1 ? '' : 's'} will use source frames for continuity.`)
        }
        setError('')
      }
      return currentTask
    } finally {
      endHeartbeat()
    }
  }

  const handlePreflight = async () => {
    if (!canPreflight || runInFlightRef.current) return
    runInFlightRef.current = true
    setError('')
    setStatus('Preparing clips')
    try {
      const checked = await preflightXmaxThirtySecondTask(episode.id, shotMappings)
      setPreflight(checked)
      setStatus('Ready')
      if (!checked.allValid) {
        const invalid = checked.shots.filter((item) => !item.mappingValid).map((item) => `Shot ${item.sequence}: ${item.errorMessage || item.errorCode || 'invalid mapping'}`)
        setError(`Reference preflight failed. ${invalid.join(' ')}`)
      }
    } catch (reason) {
      setStatus('Ready')
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      runInFlightRef.current = false
    }
  }

  const handleStart = async () => {
    if (!canStart || runInFlightRef.current) return
    runInFlightRef.current = true
    cancelledRef.current = false
    pageExitReportedRef.current = false
    setError('')
    setPreflight(null)
    setStatus('Preparing clips')
    try {
      let currentTask = taskRef.current
      if (!currentTask || currentTask.status === 'completed' || currentTask.status === 'cancelled' || !hasIncompleteShot) {
        persistenceAttemptCountRef.current = 0
        if (!scenePlan) throw new Error('The local Shot Review is not ready.')
        const checked = preflight?.allValid ? preflight : await preflightXmaxThirtySecondTask(episode.id, shotMappings)
        setPreflight(checked)
        if (!checked.allValid) {
          const invalid = checked.shots.filter((item) => !item.mappingValid).map((item) => `Shot ${item.sequence}: ${item.errorMessage || item.errorCode || 'invalid mapping'}`)
          throw new Error(`Character reference preflight failed. ${invalid.join(' ')}`)
        }
        currentTask = await createXmaxThirtySecondTask(episode.id, prompt.trim(), shotMappings)
        updateTask(currentTask)
      }
      if (!currentTask.preparation_complete) {
        setStatus('Preparing clips')
        currentTask = await waitForPreparation(currentTask)
      }
      const startIndex = currentTask.segments.findIndex((segment) => segment.status !== 'completed' && !segment.use_source_for_merge && !segment.preserve_original)
      if (startIndex < 0) {
        setStatus(currentTask.final_output_url ? 'Completed' : 'Merge pending')
        return
      }
      await runRemainingShots(currentTask, startIndex)
    } catch (reason) {
      if (!cancelledRef.current && !localBlobRef.current) {
        setStatus('Failed')
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      runInFlightRef.current = false
      runnerRef.current = null
      endHeartbeat()
    }
  }

  const handleRetrySave = async () => {
    const pending = pendingUploadRef.current
    const blob = localBlobRef.current
    const currentTask = taskRef.current
    if (!pending || !blob || !currentTask || runInFlightRef.current) return
    runInFlightRef.current = true
    setStatus('Saving output')
    try {
      persistenceAttemptCountRef.current += 1
      setDiagnostics((current) => current ? { ...current, persistenceAttemptCount: persistenceAttemptCountRef.current } : current)
      const segment = currentTask.segments.find((item) => item.segment_id === pending.segmentId)
      if (!segment) throw new Error('The pending Shot record is missing.')
      const saved = await uploadXmaxThirtySecondSegment(currentTask.task_id, segment.segment_id, blob, pending.fileName)
      releaseLocalBlob()
      updateTask(saved)
      setStatus(saved.final_output_url ? 'Completed' : saved.segments.every((item) => item.status === 'completed') ? 'Merge pending' : 'Ready')
      setError('')
    } catch (reason) {
      setStatus('Persistence failed')
      setError(`The local WebM is still available. Saving failed again: ${reason instanceof Error ? reason.message : String(reason)}`)
    } finally {
      runInFlightRef.current = false
    }
  }

  const handleStop = async () => {
    if (!runInFlightRef.current) return
    cancelledRef.current = true
    setStatus('Stopping')
    const runner = runnerRef.current
    if (runner) await runner.stop('cancelled').catch(() => undefined)
    const activeTask = taskRef.current
    if (activeTask) await cancelXmaxThirtySecondTask(activeTask.task_id).then(updateTask).catch(() => undefined)
    endHeartbeat()
    setStatus('Cancelled')
    runInFlightRef.current = false
    runnerRef.current = null
  }

  const handleMerge = async () => {
    const currentTask = taskRef.current
    if (!currentTask || (!allShotsSaved && !allShotsReadyForMerge) || currentTask.final_output_url || mergeInFlightRef.current || localBlobRef.current) return
    mergeInFlightRef.current = true
    setError('')
    setMergePhase('Validating all Shot files')
    setStatus('Merging')
    try {
      await Promise.resolve()
      setMergePhase('Aligning Shot durations')
      await Promise.resolve()
      setMergePhase('Merging video and restoring source audio')
      const merged = await mergeXmaxThirtySecondTask(currentTask.task_id)
      updateTask(merged)
      setMergePhase('')
      setStatus('Completed')
    } catch (reason) {
      setMergePhase('')
      setStatus('Merge failed')
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      mergeInFlightRef.current = false
    }
  }

  const prepareOrchestratedTargets = async (
    currentTask: XmaxThirtySecondTask,
    targetIds: Set<string>,
    explicitRedraw = false,
  ) => {
    let preparedTask = currentTask
    for (const segment of currentTask.segments) {
      if (!targetIds.has(segment.segment_id)) continue
      const quality = segment.quality_status || 'review_required'
      const retryable = segment.status === 'failed'
        || segment.execution_status === 'failed'
        || quality === 'invalid_output'
        || quality.startsWith('rejected_')
      const completedRedraw = explicitRedraw
        && segmentHasCurrentOutput(segment)
        && !segmentUsesLevel2Evaluation(segment)
      if (!retryable && !completedRedraw) continue
      await prepareXmaxThirtySecondSegmentRetry(preparedTask.task_id, segment.segment_id, false, completedRedraw)
      preparedTask = await getXmaxThirtySecondTask(preparedTask.task_id, episode.id)
      updateTask(preparedTask)
    }
    return preparedTask
  }

  const runOrchestratedVisual = async (
    requestedSegmentIds: string[],
    pointsLimitOverride?: number,
    explicitRedraw = false,
  ): Promise<{ status: 'finished' | 'failed'; error?: string }> => {
    if (!orchestration) return { status: 'failed', error: 'The Xmax orchestration handler is unavailable.' }
    if (runInFlightRef.current || mergeInFlightRef.current) return { status: 'failed', error: 'Another Xmax operation is already active.' }
    if (requestedSegmentIds.length === 0) return { status: 'failed', error: 'No Transform Unit was selected.' }
    let currentTask = taskRef.current
    if (!currentTask) throw new Error('The persisted visual task is not loaded.')
    const targetIds = new Set(requestedSegmentIds)
    runInFlightRef.current = true
    cancelledRef.current = false
    pageExitReportedRef.current = false
    setError('')
    try {
      if (!targetProxiesReady(currentTask, targetIds)) {
        setStatus('Preparing clips')
        currentTask = await waitForPreparation(currentTask, targetIds)
      }
      currentTask = await prepareOrchestratedTargets(currentTask, targetIds, explicitRedraw)
      await reportOrchestrationProgress('transforming_shots', currentTask)
      const startIndex = currentTask.segments.findIndex((segment) => targetIds.has(segment.segment_id))
      if (startIndex >= 0) currentTask = await runRemainingShots(currentTask, startIndex, targetIds, pointsLimitOverride)
      currentTask = await getXmaxThirtySecondTask(currentTask.task_id, episode.id)
      updateTask(currentTask)
      await reportOrchestrationProgress('transforming_shots', currentTask)
      const targetFailed = currentTask.segments.some((segment) => targetIds.has(segment.segment_id) && segment.execution_status === 'failed')
      if (targetFailed) {
        setStatus('Failed')
        return { status: 'failed', error: 'The selected Transform Unit failed.' }
      } else if (currentTask.segments.every((segment) => segment.preserve_original || segmentHasCurrentOutput(segment) || segmentUsesUserSource(segment))) {
        setStatus('Merge pending')
        setMergePhase('全部 Transform Unit 已有有效输出或已明确保留原画。')
      } else {
        setStatus('Ready')
        setMergePhase('本次选择的 Transform Unit 已处理。其余镜头仍等待用户操作。')
      }
      return { status: 'finished' }
    } catch (reason) {
      setStatus('Failed')
      const message = reason instanceof Error ? reason.message : String(reason)
      setError(message)
      return { status: 'failed', error: message }
    } finally {
      runInFlightRef.current = false
      runnerRef.current = null
      endHeartbeat()
    }
  }

  formalRedrawExecutorRef.current = (unitId, pointsLimit) => runOrchestratedVisual([unitId], pointsLimit, true)

  useEffect(() => {
    if (!orchestration) return undefined
    const handleFormalRedraw = (event: Event) => {
      const detail = formalRedrawDetail(event)
      if (
        detail?.taskId !== orchestration.visualTaskId
        || !detail.unitId
        || !Number.isInteger(detail.requestedPointsLimit)
        || Number(detail.requestedPointsLimit) <= 0
      ) return
      consumeFormalRedrawRequest(event, (request) => (
        formalRedrawExecutorRef.current(request.unitId, Number(request.requestedPointsLimit))
      ))
    }
    window.addEventListener(FORMAL_REDRAW_REQUEST_EVENT, handleFormalRedraw)
    return () => window.removeEventListener(FORMAL_REDRAW_REQUEST_EVENT, handleFormalRedraw)
  }, [orchestration?.visualTaskId])

  const preserveOrchestratedSource = async (segment: XmaxThirtySecondSegment) => {
    if (!orchestration || runInFlightRef.current || mergeInFlightRef.current || segment.preserve_original) return
    setError('')
    try {
      const updated = await setXmaxThirtySecondMergeSource(orchestration.visualTaskId, segment.segment_id, true)
      updateTask(updated)
      await reportOrchestrationProgress('transforming_shots', updated)
      setStatus(updated.action_state === 'ready_to_merge' ? 'Merge pending' : 'Ready')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const mergeOrchestratedVisual = async () => {
    if (!orchestration || mergeInFlightRef.current || runInFlightRef.current) return
    const currentTask = taskRef.current
    if (!currentTask || !currentTask.segments.every((segment) => segment.preserve_original || segmentHasCurrentOutput(segment) || segmentUsesUserSource(segment))) return
    mergeInFlightRef.current = true
    setError('')
    setStatus('Merging')
    try {
      await reportOrchestrationProgress('merging_visual', currentTask)
      const merged = await mergeXmaxThirtySecondTask(currentTask.task_id)
      updateTask(merged)
      if (!merged.final_output_url || merged.status !== 'completed') {
        throw new Error('The visual timeline did not produce a persisted final MP4.')
      }
      await orchestration.onComplete(merged)
      setStatus('Completed')
    } catch (reason) {
      const failure = reason instanceof Error ? reason : new Error(String(reason))
      setStatus('Merge failed')
      setError(failure.message)
      await orchestration.onFailure(failure)
    } finally {
      mergeInFlightRef.current = false
    }
  }

  const buttonLabel = mergeUiVisible ? 'Merge selected Shot versions' : status === 'Interrupted' || status === 'Failed' || hasIncompleteShot ? 'Continue incomplete Shots' : 'Start 30-second transformation'
  const creditLimitValid = Number.isInteger(requestedPointsLimit) && requestedPointsLimit > 0
  const attemptStatusPanel = <div className="xmax-p4-attempt-status">
    <label>本镜头积分上限<input type="number" min="1" step="1" value={requestedPointsLimit} disabled={running} onChange={(event) => setRequestedPointsLimit(Number(event.target.value))} /></label>
    {browserAttempt && <>
      <span>Unit {browserAttempt.unit_id}</span>
      <span>角色资产 {browserAttempt.character_asset_version}</span>
      <span>Session {browserAttempt.session_uid ? '已绑定' : '未创建'}</span>
      <span>Remote raw {browserAttempt.raw_output_persisted ? '已保存' : '未保存'}</span>
      <span>时间线规范化 {browserAttempt.normalization_status || '未开始'}</span>
      <span>输出 {browserAttempt.output_saved ? '已保存' : '未保存'}</span>
      <span>结算 {browserAttempt.settlement_status === 'pending' ? '待结算' : browserAttempt.settlement_status}</span>
      {browserAttempt.consumed_points != null && <strong>实际消费 {browserAttempt.consumed_points} 积分</strong>}
      {browserAttempt.failure_diagnostics && <span>{browserAttempt.failure_diagnostics.message}</span>}
    </>}
  </div>

  if (orchestration && formalRedrawExecutorOnly) {
    return <section className="xmax-thirty-second-panel xmax-formal-redraw-executor" aria-label="Xmax Unit redraw executor">
      <div className="panel-topline">
        <span className="panel-label">UNIT REDRAW</span>
        <span className={`xmax-single-shot-status ${status === 'Failed' ? 'status-failed' : ''}`}>{statusLabel(status)}</span>
      </div>
      <div className="xmax-single-shot-sdk-stage">
        <div><span>Input</span><div ref={localContainerRef} className="xmax-single-shot-sdk-container" /></div>
        <div><span>Output</span><div ref={remoteContainerRef} className="xmax-single-shot-sdk-container" /></div>
      </div>
      {browserAttempt && attemptStatusPanel}
      {error && <div className="xmax-thirty-second-error">{error}</div>}
    </section>
  }

  if (orchestration) {
    const transformSegments = segments.filter((segment) => segment.shot_type === 'single_character' && !segment.preserve_original)
    const transformCompleted = transformSegments.filter(segmentHasCurrentOutput).length
    const transformFailed = transformSegments.filter((segment) => (
      !segmentHasCurrentOutput(segment)
      && !segmentUsesUserSource(segment)
      && (segment.execution_status === 'failed' || segment.status === 'failed')
    )).length
    const sourceByUser = transformSegments.filter(segmentUsesUserSource).length
    const awaitingSegments = transformSegments.filter((segment) => !segmentHasCurrentOutput(segment) && !segmentUsesUserSource(segment))
    const awaitingUserAction = awaitingSegments.filter((segment) => segment.execution_status !== 'failed' && segment.status !== 'failed').length
    const allUnitsReady = segments.length > 0 && segments.every((segment) => segment.preserve_original || segmentHasCurrentOutput(segment) || segmentUsesUserSource(segment))
    return <section className="xmax-thirty-second-panel xmax-thirty-second-orchestrated">
      <div className="panel-topline"><span className="panel-label">VISUAL TRANSFORMATION</span><span className={`xmax-single-shot-status ${status === 'Failed' ? 'status-failed' : ''}`}>{statusLabel(status)}</span></div>
      <div className="xmax-thirty-second-batch-summary">
        <strong>转绘成功：{transformCompleted}/{transformSegments.length}</strong>
        <span>失败：{transformFailed}</span>
        <span>等待操作：{awaitingUserAction}</span>
        <span>使用原镜头：{sourceByUser}</span>
      </div>
      <div className="xmax-thirty-second-orchestration-actions">
        {attemptStatusPanel}
        <button className="primary-button" disabled={running || awaitingSegments.length === 0 || !creditLimitValid} onClick={() => void runOrchestratedVisual(awaitingSegments.map((segment) => segment.segment_id))}>
          {running ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />} 一键重绘全部
        </button>
        <button className="secondary-button" disabled={running || !allUnitsReady} onClick={() => void mergeOrchestratedVisual()}>
          <Check size={15} /> 合成最终海外版
        </button>
      </div>
      <div className="xmax-thirty-second-segments">
        {segments.map((segment) => {
          const currentOutput = segmentHasCurrentOutput(segment)
          const userSource = segmentUsesUserSource(segment)
          const mappedSource = segment.preserve_original || segment.mapping_status === 'preserve_original'
          const failed = segment.execution_status === 'failed' || segment.status === 'failed'
          const mappedCharacter = confirmedCharacters.find((character) => character.id === segment.selected_character_id)
          const level2Evaluation = segmentUsesLevel2Evaluation(segment)
          return <div className="xmax-thirty-second-segment" key={segment.segment_id}>
            <span>Unit {segment.sequence} · {segment.start_time.toFixed(1)}–{segment.end_time.toFixed(1)}s</span>
            {!mappedSource && segment.selected_character_id && <span>{mappedCharacter?.character_code || mappedCharacter?.name || segment.selected_character_id} · {segment.mapping_status || 'mapping_pending'}</span>}
            <strong>{currentOutput ? '转绘完成' : userSource ? '用户保留原画' : mappedSource ? '映射保留原画' : failed ? '转绘失败' : '等待用户操作'}</strong>
            {level2Evaluation && <div className="xmax-p4-attempt-status">
              <strong>放大裁剪模式</strong>
              <span>实际处理范围：x={segment.crop?.x}, y={segment.crop?.y}, {segment.crop?.width}×{segment.crop?.height} → {segment.scale?.width}×{segment.scale?.height}</span>
              <span>本次测试使用独立版本，当前已批准结果保持不变。</span>
              {segment.preprocessed_source_url && <video className="xmax-thirty-second-final-video" controls preload="metadata" playsInline src={normalizeBackendAssetUrl(segment.preprocessed_source_url)} />}
              <label><input type="checkbox" checked={level2RunConfirmed} disabled={running} onChange={(event) => setLevel2RunConfirmed(event.target.checked)} /> 我确认只运行当前 Unit 的放大裁剪测试</label>
              <button className="secondary-button" disabled={running || !creditLimitValid || !level2RunConfirmed} onClick={() => void runOrchestratedVisual([segment.segment_id])}>运行放大裁剪测试</button>
            </div>}
            {!currentOutput && !userSource && !mappedSource && <button className="text-button" disabled={running || !creditLimitValid} onClick={() => void runOrchestratedVisual([segment.segment_id])}>{failed ? '重新生成' : '重绘此镜头'}</button>}
            {!currentOutput && !userSource && !mappedSource && <button className="text-button" disabled={running} onClick={() => void preserveOrchestratedSource(segment)}>使用原镜头</button>}
            {!currentOutput && !userSource && !mappedSource && <small>每次点击重新转绘都会创建新的 Xmax Session，可能消耗积分。</small>}
            {currentOutput && segment.output_url && <a className="text-button" href={normalizeBackendAssetUrl(segment.output_url)} target="_blank" rel="noreferrer">查看转绘输出</a>}
            {segment.error_code && <details className="xmax-thirty-second-diagnostics"><summary>失败详情</summary><code>{JSON.stringify({ segmentId: segment.segment_id, errorCode: segment.error_code, errorMessage: segment.error_message, diagnostics: parseSegmentDiagnostics(segment.diagnostics_json) }, null, 2)}</code></details>}
          </div>
        })}
      </div>
      <div className="xmax-single-shot-sdk-stage"><div><span>Input</span><div ref={localContainerRef} className="xmax-single-shot-sdk-container" /></div><div><span>Output</span><div ref={remoteContainerRef} className="xmax-single-shot-sdk-container" /></div></div>
      {mergePhase && <div className="xmax-thirty-second-complete">{mergePhase}</div>}
      {error && <div className="xmax-thirty-second-error">{error}</div>}
      <details className="xmax-thirty-second-diagnostics"><summary>Technical details</summary><code>{JSON.stringify({ orchestrationTaskId: orchestration.orchestrationTaskId, visualTaskId: orchestration.visualTaskId, actionState: task?.action_state, status, currentSequence, transformCompleted, transformFailed, awaitingUserAction, sourceByUser, transformTotal: transformSegments.length, diagnostics }, null, 2)}</code></details>
    </section>
  }

  return <section className="xmax-thirty-second-panel">
    <div className="xmax-single-shot-notice"><strong>Multi-shot Xmax MVP</strong><span>Each Shot runs alone, is saved immediately, and the final merge is a separate local step. No new Provider call is made during merge.</span></div>
    <div className="xmax-thirty-second-layout">
      <section className="xmax-single-shot-controls">
        <div className="panel-topline"><span className="panel-label">SINGLE PERSON TRANSFORMATION</span><span className={`xmax-single-shot-status ${status === 'Failed' || status === 'Persistence failed' ? 'status-failed' : ''}`}>{statusLabel(status)}</span></div>
        <h2>Transform one multi-shot Episode</h2>
        <p className="xmax-single-shot-hint">Shots are detected locally from the source video. Review each range and bind one confirmed character before starting; preserved source Shots never call Xmax.</p>
        <div className="xmax-single-shot-source"><video controls preload="metadata" playsInline src={episodeSource} /><div className="xmax-single-shot-source-meta"><strong>{episode.title}</strong><span>{Number(episode.duration || 0).toFixed(2)}s source · {segments.length || 'fixed'} serial Shots</span></div></div>
        <XmaxSceneReview
          plan={scenePlan}
          mappings={shotMappings}
          confirmedCharacters={confirmedCharacters}
          disabled={running}
          selectedSequences={selectedSequences}
          onToggleSequence={toggleShotSelection}
          onMappingsChange={(next) => setShotMappings(next)}
          reviewingPersistedUnits={reviewingPersistedUnits}
          statusForShot={(shot) => {
            if (!draftMappingIsValid(shot)) return { valid: false, label: 'Needs mapping' }
            if (!preflight) return { valid: true, label: 'Ready for preflight' }
            if (mappingIsValid(shot)) return { valid: true, label: 'Allowed' }
            return { valid: false, label: preflightFor(shot.sequence)?.errorMessage || 'Reference preflight failed' }
          }}
        >
          <div className="xmax-thirty-second-batch-controls"><select value={batchCharacterId} disabled={running} onChange={(event) => setBatchCharacterId(event.target.value)}><option value="">Choose character for batch</option>{confirmedCharacters.map((character) => <option key={character.id} value={character.id}>{character.name}</option>)}</select><button className="text-button" disabled={running || !scenePlan} onClick={selectAllShots}>Select all</button><button className="text-button" disabled={running || selectedSequences.length === 0} onClick={clearShotSelection}>Clear</button><button className="text-button" disabled={running || !scenePlan} onClick={selectSingleCharacterShots}>Only single-character</button><button className="secondary-button" disabled={running || selectedSequences.length === 0 || !batchCharacterId} onClick={() => void applyBatchMapping(false)}>Apply character to selected</button><button className="secondary-button" disabled={running || selectedSequences.length === 0} onClick={() => void applyBatchMapping(true)}>Keep selected source</button></div>
          <div className="xmax-thirty-second-batch-summary"><span>generated approved: {generatedApprovedCount}</span><span>source fallback: {sourceFallbackCount}</span><span>review required: {reviewRequiredCount}</span><span>blocked: {blockedCount}</span><strong>Will call Xmax for Shots: {providerShotCount}</strong></div>
          {preflight && <details className="xmax-thirty-second-diagnostics"><summary>Advanced quality plan</summary><code>{JSON.stringify({ episodeDuration: scenePlan?.source_duration, baseShots: scenePlan?.base_shot_count, transformUnits: preflight.transformUnitCount, aiTransformUnits: preflight.aiTransformCount, estimatedSourceFallback: [preflight.estimatedSourceFallbackMin, preflight.estimatedSourceFallbackMax], highRiskUnits: preflight.highRiskCount }, null, 2)}</code></details>}
          {mappingError && <div className="xmax-thirty-second-error">Local scene analysis failed: {mappingError}</div>}
          {preflight && !preflight.allValid && <div className="xmax-thirty-second-error">Reference preflight: {preflight.shots.filter((item) => !item.mappingValid).map((item) => `Shot ${item.sequence} ${item.errorCode || 'invalid'}`).join(', ')}</div>}
          {scenePlan && <details className="xmax-thirty-second-diagnostics"><summary>Mapping diagnostics</summary><code>{JSON.stringify(reviewUnits.map((shot) => { const draft = shotMappings[String(shot.sequence)]; const checked = preflight?.shots.find((item) => item.sequence === shot.sequence); return { sequence: shot.sequence, selectedCharacterId: draft?.selectedCharacterId || null, referenceFaceConfirmed: draft?.referenceFaceConfirmed === true, preserveSource: draft?.preserveSource === true, mappingValid: mappingIsValid(shot), preflightValid: checked?.mappingValid ?? null } }), null, 2)}</code></details>}
        </XmaxSceneReview>
        <label className="xmax-single-shot-field">Prompt<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} disabled={running} /></label>
        {attemptStatusPanel}
        <div className="xmax-single-shot-actions"><button className="primary-button" disabled={mergeUiVisible ? !canMerge : ((!canPreflight && !canStart) || !creditLimitValid)} onClick={() => void (mergeUiVisible ? handleMerge() : mappingsReady ? handleStart() : handlePreflight())}>{running ? <LoaderCircle className="spin" size={17} /> : mergeUiVisible ? <Check size={17} /> : <Sparkles size={17} />} {running ? (status === 'Merging' ? 'Merging final video' : status === 'Preparing clips' ? `Preparing ${task?.prepared_units || 0}/${task?.preparation_total_units || segments.length || 0}` : `Shot ${currentSequence || 1}/${segments.length || 3}`) : mergeUiVisible ? buttonLabel : (mappingsReady ? buttonLabel : draftMappingsReady ? 'Check character references' : buttonLabel)}</button>{running && !mergeInFlightRef.current && <button className="secondary-button" onClick={() => void handleStop()}><Square size={14} /> Stop</button>}</div>
         {mergePhase && <div className="xmax-thirty-second-merge-phase">{mergePhase}</div>}
         {localBlobUrl && <div className="xmax-thirty-second-local-output"><strong>Shot generated; project save failed</strong><span>{localBlobSize} bytes retained locally</span><video controls playsInline src={localBlobUrl} /><div className="xmax-single-shot-actions"><a className="secondary-button" href={localBlobUrl} download={pendingUploadRef.current?.fileName || 'xmax-shot-local.webm'}><Download size={15} /> Download local WebM</a><button className="primary-button" onClick={() => void handleRetrySave()} disabled={running}><Download size={15} /> Retry save only</button></div></div>}
         {taskSelectionRequired && <div className="xmax-thirty-second-task-selector"><strong>Multiple active tasks need a choice</strong><span>Select the task to resume; no task was selected automatically.</span>{activeCandidates.map((item) => <button key={item.task_id} className="secondary-button" onClick={() => void openTask(item.task_id).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))}>{item.task_id} · {item.status} · {item.completed_shots}/{item.total_shots} Shots</button>)}</div>}
         {error && <div className="xmax-thirty-second-error">{error}</div>}
         {task && <div className="xmax-thirty-second-task-meta"><span>Task <strong>{task.task_id}</strong></span><span>Prepared Units <strong>{task.prepared_units}/{task.preparation_total_units}</strong></span><span>Saved Shots <strong>{savedCount}/{segments.length || 3}</strong></span><span>Blob retained <strong>{localBlobSize} bytes</strong></span></div>}
         {!task && historicalTask && <div className="xmax-thirty-second-task-meta"><span>Previous completed task <strong>{historicalTask.task_id}</strong></span><span>Saved Shots <strong>{historicalTask.segments.filter((segment) => segment.status === 'completed').length}/{historicalTask.segments.length}</strong></span><span>History only; a new mapping will create a new task.</span></div>}
         {completedResultTask && task?.task_id !== completedResultTask.task_id && <div className="xmax-thirty-second-current-result"><strong>Current formal result</strong><span>{completedResultTask.task_id} · {completedResultTask.final_duration?.toFixed(3) || 'n/a'}s · {completedResultTask.segments.length}/{completedResultTask.segments.length} Shots saved</span>{completedResultTask.final_output_url && <video controls playsInline preload="metadata" src={normalizeBackendAssetUrl(completedResultTask.final_output_url)} />}<div className="xmax-single-shot-actions"><a className="secondary-button" href={normalizeBackendAssetUrl(completedResultTask.final_output_url || '')} download={`xmax-30-second-${completedResultTask.task_id}.mp4`}><Download size={15} /> Download current result</a><button className="text-button" onClick={returnToCurrentResult}>Use current result</button></div></div>}
         {historicalTasks.length > 0 && <details className="xmax-thirty-second-task-history"><summary>Historical Xmax tasks ({historicalTasks.length})</summary>{historicalTasks.map((item) => <div key={item.task_id} className="xmax-thirty-second-task-history-row"><span><strong>{item.task_id}</strong> · {item.status} · {item.completed_shots}/{item.total_shots} Shots{item.error_code ? ` · ${item.error_code}` : ''}</span><button className="text-button" onClick={() => void openTask(item.task_id).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))}>Open exact task</button></div>)}</details>}
      </section>
      <section className="xmax-single-shot-output">
        <div className="panel-topline"><span className="panel-label">XMAX OUTPUT</span><span className="xmax-single-shot-status">{statusLabel(status)}</span></div>
        <div className="xmax-single-shot-sdk-stage"><div><span>Input</span><div ref={localContainerRef} className="xmax-single-shot-sdk-container" /></div><div><span>Output</span><div ref={remoteContainerRef} className="xmax-single-shot-sdk-container" /></div></div>
        <div className="xmax-thirty-second-segments">{segments.map((segment) => { const quality = segment.quality_status || (segment.preserve_original ? 'not_applicable' : 'review_required'); const retryable = quality === 'invalid_output' || quality.startsWith('rejected_') || segment.execution_status === 'failed' || segment.status === 'failed'; return <div className="xmax-thirty-second-segment" key={segment.segment_id}><span>Unit {segment.sequence} · {segment.start_time.toFixed(1)}–{segment.end_time.toFixed(1)}s</span><strong>{segment.execution_status || segment.status}</strong><span className="xmax-thirty-second-quality">Quality: {quality}</span>{segment.use_source_for_merge && <span className="xmax-thirty-second-quality">Continuity: source frame</span>}{segment.output_url && <a className="text-button" href={normalizeBackendAssetUrl(segment.output_url)} target="_blank" rel="noreferrer">Open segment</a>}{retryable && segment.reference_preview_url && <span className="xmax-thirty-second-retry-reference"><img src={normalizeBackendAssetUrl(segment.reference_preview_url)} alt={`Unit ${segment.sequence} retry reference`} /><span>{segment.reference_filename || 'Current character reference'}</span></span>}{segment.error_code && <details className="xmax-thirty-second-diagnostics"><summary>Failure details</summary><code>{JSON.stringify({ errorCode: segment.error_code, errorMessage: segment.error_message, expectedDuration: segment.duration, outputDuration: segment.output_duration, retryVersion: segment.retry_version, fallback: segment.use_source_for_merge, diagnostics: parseSegmentDiagnostics(segment.diagnostics_json) }, null, 2)}</code></details>}{segment.status === 'completed' && quality !== 'not_applicable' && <><button className="text-button" disabled={running} onClick={() => void reviewSegment(segment, 'approved')}>Approve</button><button className="text-button" disabled={running} onClick={() => void reviewSegment(segment, 'rejected_no_effect')}>No effect</button><button className="text-button" disabled={running} onClick={() => void reviewSegment(segment, 'rejected_identity_drift')}>Identity drift</button></>}{retryable && <button className="text-button" disabled={running} onClick={() => void prepareSegmentRetry(segment)}>Prepare Shot retry</button>}{retryable && !segment.use_source_for_merge && <button className="text-button" disabled={running} onClick={() => void setMergeSource(segment, true)}>Final use source frame</button>}{segment.use_source_for_merge && quality === 'approved' && <button className="text-button" disabled={running} onClick={() => void setMergeSource(segment, false)}>Restore transformed output</button>}</div> })}</div>
         {mergeUiVisible && !task?.final_output_url && <div className="xmax-thirty-second-complete"><Check size={15} /> {mergeReadyCount}/{segments.length} Shots ready for merge. {blockedCount > 0 ? `${blockedCount} Shot(s) still need approval or source fallback.` : 'Merge will use the selected Shot version and original Episode audio.'}</div>}
        {task?.final_output_url && <div className="xmax-thirty-second-complete"><Check size={15} /> Overseas version ready: {task.final_duration?.toFixed(3) || 'n/a'}s · {task.final_width || 'n/a'}×{task.final_height || 'n/a'} · audio {task.source_audio_restored ? 'restored' : 'not present'}{sourceFallbackCount > 0 && <span>Some scenes use their original frames to preserve continuity.</span>}<video className="xmax-thirty-second-final-video" controls playsInline src={normalizeBackendAssetUrl(task.final_output_url)} /><a className="secondary-button" href={normalizeBackendAssetUrl(task.final_output_url)} download={`xmax-30-second-${task.task_id}.mp4`}><Download size={15} /> Download final MP4</a></div>}
        {diagnostics && <details className="xmax-thirty-second-diagnostics"><summary>Resource diagnostics</summary><code>{JSON.stringify({ shotId: currentSequence || null, attemptId: task?.attempt_id || null, lifecycleState: status, retainedBlobCount: localBlobUrl ? 1 : 0, ...diagnostics }, null, 2)}</code></details>}
      </section>
    </div>
  </section>
}
