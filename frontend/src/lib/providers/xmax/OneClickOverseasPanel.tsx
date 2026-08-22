import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, Download, Film, LoaderCircle, Sparkles, UserRound } from 'lucide-react'
import {
  api,
  completeOverseasEpisodeVisual,
  createOverseasEpisodeTask,
  createXmaxThirtySecondTask,
  failOverseasEpisodeTask,
  getLatestOverseasEpisodeTask,
  getOverseasEpisodeTask,
  getXmaxThirtySecondTask,
  getXmaxThirtySecondScenePlan,
  getXmaxThirtySecondTasks,
  mergeXmaxThirtySecondTask,
  normalizeBackendAssetUrl,
  preflightXmaxThirtySecondTask,
  updateOverseasEpisodeVisualProgress,
  type Character,
  type Episode,
  type Language,
  type OverseasEpisodeStage,
  type OverseasEpisodeTask,
  type Project,
  type Voice,
  type XmaxThirtySecondScenePlan,
  type XmaxThirtySecondShotMapping,
  type XmaxThirtySecondTask,
} from '../../../api'
import XmaxThirtySecondPanel from './XmaxThirtySecondPanel'
import FormalEpisodeClosurePanel from '../../../FormalEpisodeClosurePanel'
import { targetLanguageOptions } from '../../../i18n'
import {
  sceneReviewMappingsFromPlan,
  sceneReviewUnits,
  sceneReviewMappingsReady,
} from './XmaxSceneReview'
import { validEpisodeTargetCharacters } from '../../episodeCharacters'
import { isEpisodeUploadComplete } from '../../../apiRequest'

const STAGE_LABELS: Record<OverseasEpisodeStage, string> = {
  preparing: '准备素材',
  analyzing_shots: '分析镜头',
  transforming_shots: '转绘镜头',
  merging_visual: '合并画面',
  separating_audio: '分离对白',
  adapting_dialogue: '适配翻译与配音',
  rendering_subtitles: '渲染英文字幕',
  muxing_final: '输出最终视频',
  completed: '海外版已完成',
  failed: '生成失败',
}

const DEFAULT_OVERSEAS_PROMPT = 'Replace the selected character complete identity and visible appearance with the fictional adult from the reference image. Preserve the original motion, pose, performance, timing, camera movement, framing, lighting, background, props, and all unselected people. Do not add people or perform a face-only replacement.'

function stageDisplayLabel(stage: OverseasEpisodeStage) {
  if (stage === 'separating_audio') return '准备背景音'
  if (stage === 'adapting_dialogue') return '生成正式混音'
  if (stage === 'muxing_final') return '合成最终视频'
  if (stage === 'completed') return '已就绪'
  return STAGE_LABELS[stage]
}

function characterReference(character: Character) {
  return normalizeBackendAssetUrl(
    character.target_reference_path
      || character.main_reference_image
      || character.source_reference_image
      || character.original_reference_image
      || character.reference_assets?.find((asset) => asset.is_primary)?.image,
  )
}

function candidateTimes(candidate: NonNullable<Episode['candidates']>[number]) {
  const values = [...(candidate.occurrence_timestamps || [])]
  if (Number.isFinite(candidate.representative_timestamp)) values.push(Number(candidate.representative_timestamp))
  return values.filter((value) => Number.isFinite(value))
}

export function buildSafeAutomaticMappings(
  plan: XmaxThirtySecondScenePlan,
  episode: Episode,
  targetCharacterId: string,
): Record<string, XmaxThirtySecondShotMapping> {
  const humanCandidates = (episode.candidates || []).filter((candidate) => candidate.entity_type === 'Human')
  return Object.fromEntries(sceneReviewUnits(plan).map((shot) => {
    const visibleCandidates = humanCandidates.filter((candidate) => candidateTimes(candidate).some((time) => time >= shot.source_start && time < shot.source_end))
    const targetVisible = visibleCandidates.some((candidate) => (candidate.character_id || candidate.matched_character_id) === targetCharacterId)
    const provenSingleTarget = visibleCandidates.length === 1 && targetVisible
    return [String(shot.sequence), provenSingleTarget
      ? { shotType: 'single_character', selectedCharacterId: targetCharacterId, referenceFaceConfirmed: true, preserveSource: false }
      : { shotType: visibleCandidates.length > 1 ? 'multi_character' : 'no_character', referenceFaceConfirmed: false, preserveSource: true }]
  }))
}

function versionedOutputUrl(task: OverseasEpisodeTask) {
  const value = normalizeBackendAssetUrl(task.final_output_url)
  if (!value) return ''
  const version = encodeURIComponent(task.completed_at || task.updated_at || task.task_id)
  return `${value}${value.includes('?') ? '&' : '?'}v=${version}`
}

export default function OneClickOverseasPanel({
  project,
  episode,
  languages,
  voices,
  onProjectChange,
}: {
  project: Project
  episode: Episode
  languages: Language[]
  voices: Voice[]
  onProjectChange: (project: Project) => void
}) {
  const validTargetCharacters = useMemo(
    () => validEpisodeTargetCharacters(project.characters, episode),
    [project.characters, episode],
  )
  const targetCharacter = validTargetCharacters[0]
  const [task, setTask] = useState<OverseasEpisodeTask | null>(null)
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [mergingFinal, setMergingFinal] = useState(false)
  const [error, setError] = useState('')
  const [statusSyncWarning, setStatusSyncWarning] = useState('')
  const targetLanguage = project.settings.target_language || 'english'
  const [voiceId, setVoiceId] = useState(targetCharacter?.voice_id || voices[0]?.id || 'default')
  const [prompt, setPrompt] = useState(DEFAULT_OVERSEAS_PROMPT)
  const [scenePlan, setScenePlan] = useState<XmaxThirtySecondScenePlan | null>(null)
  const [sceneMappings, setSceneMappings] = useState<Record<string, XmaxThirtySecondShotMapping>>({})
  const [sceneMappingError, setSceneMappingError] = useState('')
  const [scenePreflightValid, setScenePreflightValid] = useState(false)
  const [latestVisualTaskId, setLatestVisualTaskId] = useState('')
  const mountedRef = useRef(true)
  const taskRef = useRef<OverseasEpisodeTask | null>(null)
  const sourceReady = isEpisodeUploadComplete(episode)
  const sourceUrl = normalizeBackendAssetUrl(`/uploads/${episode.id}${episode.video_filename?.toLowerCase().endsWith('.mov') ? '.mov' : '.mp4'}`)
  const referenceUrl = targetCharacter ? characterReference(targetCharacter) : ''

  const updateTask = (next: OverseasEpisodeTask | null) => {
    taskRef.current = next
    setTask(next)
  }

  useEffect(() => {
    mountedRef.current = true
    if (!sourceReady) {
      setLoading(false)
      updateTask(null)
      return () => { mountedRef.current = false }
    }
    setLoading(true)
    setError('')
    void getLatestOverseasEpisodeTask(episode.id)
      .then((next) => {
        if (!mountedRef.current) return
        updateTask(next?.error_code === 'SHOT_MAPPING_REQUIRED' ? null : next)
      })
      .catch((reason) => {
        if (mountedRef.current) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (mountedRef.current) setLoading(false)
      })
    return () => {
      mountedRef.current = false
    }
  }, [episode.id, sourceReady])

  useEffect(() => {
    let active = true
    setScenePlan(null)
    setSceneMappings({})
    setSceneMappingError('')
    if (!sourceReady) return () => { active = false }
    void getXmaxThirtySecondScenePlan(episode.id)
      .then((plan) => {
        if (!active) return
        setScenePlan(plan)
        setSceneMappings(sceneReviewMappingsFromPlan(plan))
      })
      .catch((reason) => {
        if (active) setSceneMappingError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { active = false }
  }, [episode.id, sourceReady])

  useEffect(() => {
    let active = true
    if (!sourceReady) return () => { active = false }
    void getXmaxThirtySecondTasks(episode.id)
      .then((index) => {
        if (!active) return
        const latest = [...index.active_candidates, ...index.completed_results]
          .sort((left, right) => (right.updated_at || right.created_at).localeCompare(left.updated_at || left.created_at))[0]
        setLatestVisualTaskId(latest?.task_id || '')
      })
      .catch(() => {
        if (active) setLatestVisualTaskId('')
      })
    return () => { active = false }
  }, [episode.id, task?.visual_task_id, sourceReady])

  useEffect(() => {
    if (!task || task.status !== 'running' || task.requires_client_visual_execution) return
    const timer = window.setInterval(() => {
      void getOverseasEpisodeTask(task.task_id)
        .then((next) => {
          if (mountedRef.current) {
            updateTask(next)
            setStatusSyncWarning('')
          }
        })
        .catch(() => {
          if (mountedRef.current) setStatusSyncWarning('状态同步暂时不可用；当前镜头任务仍保持运行，可稍后刷新恢复。')
        })
    }, 1500)
    return () => window.clearInterval(timer)
  }, [task?.task_id, task?.status, task?.requires_client_visual_execution])

  const outputUrl = useMemo(() => task ? versionedOutputUrl(task) : '', [task])
  const finalDeliveryVisualTaskId = task?.visual_task_id || latestVisualTaskId
  const visualAwaitingAction = Boolean(task?.requires_client_visual_execution)
  const finalMergeReady = Boolean(task?.can_merge_final)
  const backendRunning = Boolean(
    task?.status === 'running'
    && !task.requires_client_visual_execution
    && !task.can_merge_final,
  )
  const canStart = sourceReady && !loading && !starting && !mergingFinal && !visualAwaitingAction && !backendRunning && !finalMergeReady
  const stage = task?.stage || 'preparing'
  const visualUnitCount = task?.transform_total || 0
  const visualCompletedCount = task?.transform_completed || 0
  const visualUnitsComplete = visualUnitCount > 0 && visualCompletedCount >= visualUnitCount
  const reportedProgress = Math.max(0, Math.min(100, task?.progress || 0))
  const progress = visualUnitsComplete ? 100 : Math.min(99, reportedProgress)
  const sceneMappingsConfirmed = sceneReviewMappingsReady(scenePlan, sceneMappings, validTargetCharacters)
  const visualPrerequisitesReady = Boolean(task?.visual_task_id) || (sourceReady && sceneMappingsConfirmed && scenePreflightValid)
  const referenceReadinessSignature = validTargetCharacters.map((character) => [
    character.id,
    character.target_reference_path,
    character.main_reference_image,
    character.source_reference_image,
    character.original_reference_image,
  ].join(':')).join('|')

  useEffect(() => {
    let active = true
    setScenePreflightValid(false)
    if (!sourceReady || !scenePlan || !sceneMappingsConfirmed) return () => { active = false }
    void preflightXmaxThirtySecondTask(episode.id, sceneMappings)
      .then((result) => {
        if (active) {
          setScenePreflightValid(result.allValid)
          setSceneMappingError(result.allValid ? '' : 'The persisted Scene mappings or character reference failed preflight.')
        }
      })
      .catch((reason) => {
        if (active) setSceneMappingError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { active = false }
  }, [episode.id, scenePlan?.mappings_updated_at, sceneMappingsConfirmed, referenceReadinessSignature, sourceReady])

  const handleStart = async () => {
    if (!canStart) return
    setStarting(true)
    setError('')
    try {
      if (!targetCharacter) {
        throw new Error('请先在“素材与角色”上传并确认一张目标角色参考图。')
      }
      if (!prompt.trim()) throw new Error('Prompt is required.')
      const nextSettings = { ...project.settings, target_language: targetLanguage }
      await api(`/projects/${project.id}/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(nextSettings),
      })
      let updatedCharacter = targetCharacter
      if (voiceId && targetCharacter.voice_id !== voiceId) {
        updatedCharacter = await api<Character>(`/projects/${project.id}/characters/${targetCharacter.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...targetCharacter, voice_id: voiceId }),
        })
      }
      onProjectChange({
        ...project,
        settings: nextSettings,
        target_language: targetLanguage,
        characters: project.characters.map((character) => character.id === updatedCharacter.id ? updatedCharacter : character),
      })
      const visualIndex = await getXmaxThirtySecondTasks(episode.id)
      if (!visualIndex.active_candidates.length && !visualIndex.completed_results.length) {
        const plan = await getXmaxThirtySecondScenePlan(episode.id)
        const mappings = sceneReviewMappingsFromPlan(plan)
        setScenePlan(plan)
        setSceneMappings(mappings)
        if (!sceneReviewMappingsReady(plan, mappings, validTargetCharacters)) {
          throw new Error('请先在“素材与角色 → 高级调整 → Scene Review”完成每个 Transform Unit 的角色或保留原画映射。')
        }
        const checked = await preflightXmaxThirtySecondTask(episode.id, mappings)
        if (!checked.allValid) {
          const invalid = checked.shots
            .filter((shot) => !shot.mappingValid)
            .map((shot) => `Unit ${shot.sequence}: ${shot.errorMessage || shot.errorCode || 'invalid mapping'}`)
          throw new Error(`镜头映射预检未通过。${invalid.join(' ')}`)
        }
        await createXmaxThirtySecondTask(episode.id, prompt.trim(), mappings)
      }
      const next = await createOverseasEpisodeTask(episode.id)
      updateTask(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setStarting(false)
    }
  }

  const handleTargetLanguageChange = async (value: string) => {
    const nextSettings = { ...project.settings, target_language: value }
    onProjectChange({ ...project, settings: nextSettings, target_language: value })
    try {
      await api(`/projects/${project.id}/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(nextSettings),
      })
    } catch (reason) {
      onProjectChange(project)
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const handleFinalMerge = async () => {
    const current = taskRef.current
    if (
      !current?.can_merge_final
      || !current.visual_task_id
      || (!current.final_assets_ready && !current.can_prepare_final_audio)
      || starting
      || mergingFinal
    ) return
    setMergingFinal(true)
    setError('')
    try {
      const visualTask = await getXmaxThirtySecondTask(current.visual_task_id, episode.id)
      if (visualTask.status !== 'completed' || !visualTask.final_output_url) {
        await mergeXmaxThirtySecondTask(current.visual_task_id)
      }
      const next = await completeOverseasEpisodeVisual(current.task_id, current.visual_task_id)
      if (mountedRef.current) updateTask(next)
    } catch (reason) {
      if (mountedRef.current) setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      if (mountedRef.current) setMergingFinal(false)
    }
  }

  const handleVisualProgress = async (value: {
    stage: 'analyzing_shots' | 'transforming_shots' | 'merging_visual'
    transformCompleted: number
    transformTotal: number
    fallbackCount: number
    providerCalls: number
    xmaxSessions: number
  }) => {
    const current = taskRef.current
    if (!current?.visual_task_id) return
    try {
      const next = await updateOverseasEpisodeVisualProgress(current.task_id, {
        visual_task_id: current.visual_task_id,
        stage: value.stage,
        transform_completed: value.transformCompleted,
        transform_total: value.transformTotal,
        fallback_count: value.fallbackCount,
        provider_calls: value.providerCalls,
        xmax_sessions: value.xmaxSessions,
      })
      if (mountedRef.current) {
        updateTask(next)
        setStatusSyncWarning('')
      }
    } catch {
      if (mountedRef.current) setStatusSyncWarning('状态同步暂时不可用；已保存的镜头结果不会被标记为失败。')
    }
  }

  const handleVisualComplete = async (visualTask: XmaxThirtySecondTask) => {
    const current = taskRef.current
    if (!current) return
    const next = await completeOverseasEpisodeVisual(current.task_id, visualTask.task_id)
    if (mountedRef.current) {
      updateTask(next)
    }
  }

  const handleVisualFailure = async (reason: Error) => {
    const current = taskRef.current
    if (!current) return
    const next = await failOverseasEpisodeTask(
      current.task_id,
      'VISUAL_ORCHESTRATION_FAILED',
      reason.message,
      current.stage,
    )
    if (mountedRef.current) {
      updateTask(next)
      setError(reason.message)
    }
  }

  return <div className="step-content one-click-overseas">
    <section className="one-click-overseas-card">
      <div className="one-click-overseas-heading">
        <div>
          <span className="section-label">OVERSEAS VERSION</span>
          <h2>一键生成海外版</h2>
          <p>系统会复用已完成资产，并自动完成镜头转绘、对白替换、英文配音、字幕和最终 MP4。</p>
        </div>
        <span className={`xmax-single-shot-status ${task?.status === 'failed' ? 'status-failed' : ''}`}>{stageDisplayLabel(stage)}</span>
      </div>
      {statusSyncWarning && <div className="xmax-warning">{statusSyncWarning}</div>}

      <div className="one-click-overseas-inputs">
        <div className="one-click-overseas-media">
          <span>原片预览</span>
          {sourceUrl ? <video controls playsInline preload="metadata" src={sourceUrl} /> : <div className="one-click-overseas-placeholder"><Film size={22} /> 未找到原片</div>}
        </div>
        <div className="one-click-overseas-media">
          <span>角色参考图</span>
          {referenceUrl ? <img src={referenceUrl} alt={targetCharacter?.name || 'Target character'} /> : <div className="one-click-overseas-placeholder"><UserRound size={22} /> 请先上传并确认一个目标角色</div>}
        </div>
      </div>
      <div className="one-click-overseas-controls">
        <label>目标语言<select value={targetLanguage} disabled={!canStart} onChange={(event) => void handleTargetLanguageChange(event.target.value)}>{targetLanguageOptions(languages).map((language) => <option key={language.id} value={language.id}>{language.label}</option>)}</select></label>
        <label>声音<select value={voiceId} disabled={!canStart} onChange={(event) => setVoiceId(event.target.value)}>{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}</select></label>
        <label className="one-click-overseas-prompt">提示词<textarea value={prompt} disabled={!canStart} onChange={(event) => setPrompt(event.target.value)} /></label>
      </div>

      <div className="one-click-overseas-progress" aria-label={`Progress ${progress}%`}>
        <span style={{ width: `${progress}%` }} />
      </div>
      <div className="one-click-overseas-summary">
        <div><span>当前阶段</span><strong>{stageDisplayLabel(stage)}</strong></div>
        <div><span>{task?.visual_ready ? '视觉转绘完成' : '转绘进度'}</span><strong>{task?.transform_completed || 0}/{task?.transform_total || 0}</strong></div>
        <div><span>原画回退</span><strong>{task?.fallback_count || 0}</strong></div>
      </div>
      {!task?.visual_task_id && <p className={sceneMappingsConfirmed ? 'xmax-thirty-second-valid' : 'xmax-thirty-second-invalid'}>
        {sceneMappingError
          ? `Scene Review failed: ${sceneMappingError}`
          : sceneMappingsConfirmed
            ? `Scene Review confirmed · ${scenePlan?.base_shot_count || 0} Base Shots → ${scenePlan?.transform_unit_count || scenePlan?.shots.length || 0} Transform Units`
            : '创建任务前，请先在“素材与角色 → 高级调整”完成 Scene Review。'}
      </p>}

      <button
        className="primary-button one-click-overseas-primary"
        disabled={
          task?.status === 'completed'
          || (finalMergeReady
            ? (!task?.final_assets_ready && !task?.can_prepare_final_audio) || mergingFinal
            : !canStart || !targetCharacter || !prompt.trim() || !visualPrerequisitesReady)
        }
        onClick={() => void (finalMergeReady ? handleFinalMerge() : handleStart())}
      >
        {(starting || backendRunning || mergingFinal) ? <LoaderCircle className="spin" size={17} /> : task?.status === 'completed' ? <Check size={17} /> : <Sparkles size={17} />}
        {task?.status === 'completed'
          ? '海外版已完成'
          : finalMergeReady
            ? '合成最终海外版'
            : visualAwaitingAction
              ? '请在下方选择镜头操作'
              : '生成海外版'}
      </button>

      {visualAwaitingAction && <p className="one-click-overseas-resume-note">视觉任务正在等待你的操作。选择单个镜头或全部重绘后才会启动 Xmax；刷新页面不会自动继续。</p>}
      {finalMergeReady && !task?.final_assets_ready && Boolean(task?.final_merge_blockers.length) && <div className="xmax-thirty-second-error">
        <strong>最终合成所需资产尚未就绪：</strong>
        {task?.final_merge_blockers.map((blocker) => (
          <div key={`${blocker.asset || 'asset'}-${blocker.segment_id || blocker.error_code || 'unknown'}`}>
            {blocker.segment_id || blocker.asset || 'asset'}: {blocker.message || blocker.error_code || 'not ready'}
          </div>
        ))}
      </div>}
      {finalMergeReady && task?.can_prepare_final_audio && Boolean(task.audio_assets_to_prepare?.length) && <p className="one-click-overseas-resume-note">
        点击后将自动完成：{task.audio_assets_to_prepare?.map((item) => item.asset).filter(Boolean).join('、')}。不会重新生成配音或视觉镜头。
      </p>}
      {(error || task?.error_message) && <div className="xmax-thirty-second-error">{error || task?.error_message}</div>}

      {task?.status === 'completed' && outputUrl && <div className="one-click-overseas-result">
        <div className="one-click-overseas-result-head"><strong>最终海外版</strong><span>{task.completed_at || task.updated_at}</span></div>
        <video controls playsInline preload="metadata" src={outputUrl} />
        <a className="primary-button" href={outputUrl} download={`overseas-${episode.id}.mp4`}><Download size={16} /> 下载 MP4</a>
      </div>}

      {task && <details className="xmax-thirty-second-diagnostics">
        <summary>技术详情</summary>
        <code>{JSON.stringify({
          taskId: task.task_id,
          visualTaskId: task.visual_task_id,
          audioTaskId: task.audio_task_id,
          status: task.status,
          stage: task.stage,
          progress: task.progress,
          providerCallsThisRun: task.provider_calls_this_run,
          xmaxSessionsThisRun: task.xmax_sessions_this_run,
          diagnostics: task.diagnostics,
        }, null, 2)}</code>
      </details>}
    </section>

    {finalDeliveryVisualTaskId && <XmaxThirtySecondPanel
      project={project}
      episode={episode}
      formalRedrawExecutorOnly={!task?.requires_client_visual_execution}
      orchestration={{
        orchestrationTaskId: task?.task_id || `formal-closure:${finalDeliveryVisualTaskId}`,
        visualTaskId: finalDeliveryVisualTaskId,
        providerCalls: task?.provider_calls_this_run || 0,
        xmaxSessions: task?.xmax_sessions_this_run || 0,
        onProgress: handleVisualProgress,
        onComplete: handleVisualComplete,
        onFailure: handleVisualFailure,
      }}
    />}
    {finalDeliveryVisualTaskId && <FormalEpisodeClosurePanel
      episodeId={episode.id}
      visualTaskId={finalDeliveryVisualTaskId}
    />}
  </div>
}
