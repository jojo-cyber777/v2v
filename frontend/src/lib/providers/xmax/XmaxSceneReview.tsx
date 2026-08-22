import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  batchBindXmaxThirtySecondMappings,
  getXmaxThirtySecondScenePlan,
  normalizeBackendAssetUrl,
  preflightXmaxThirtySecondTask,
  saveXmaxThirtySecondSceneMappings,
  type Character,
  type Project,
  type XmaxThirtySecondScenePlan,
  type XmaxThirtySecondShotMapping,
} from '../../../api'
import { validEpisodeTargetCharacters } from '../../episodeCharacters'

type SceneShot = XmaxThirtySecondScenePlan['shots'][number]
type MappingStatus = { valid: boolean; label: string }

type SceneReviewProps = {
  plan: XmaxThirtySecondScenePlan | null
  mappings: Record<string, XmaxThirtySecondShotMapping>
  confirmedCharacters: Character[]
  disabled?: boolean
  selectedSequences?: number[]
  onToggleSequence?: (sequence: number) => void
  onMappingsChange: (mappings: Record<string, XmaxThirtySecondShotMapping>) => void
  statusForShot?: (shot: SceneShot) => MappingStatus
  reviewingPersistedUnits?: boolean
  children?: ReactNode
}

type StoredSceneReviewDraft = {
  version: 1
  episodeId: string
  planSignature: string
  mappings: Record<string, XmaxThirtySecondShotMapping>
}

export const XMAX_SCENE_MAPPINGS_SAVED_EVENT = 'xmax-scene-mappings-saved'

export function sceneReviewUnits(plan: XmaxThirtySecondScenePlan) {
  return plan.transform_units?.length ? plan.transform_units : plan.shots
}

export function sceneReviewDraftStorageKey(episodeId: string) {
  return `xmax-scene-review-draft:${episodeId}`
}

function scenePlanSignature(plan: XmaxThirtySecondScenePlan) {
  return JSON.stringify({
    mappingsUpdatedAt: plan.mappings_updated_at || '',
    units: sceneReviewUnits(plan).map((shot) => [
      shot.sequence,
      shot.transform_unit_id || '',
      Number(shot.source_start.toFixed(3)),
      Number(shot.source_end.toFixed(3)),
    ]),
  })
}

function normalizeMapping(value: XmaxThirtySecondShotMapping): XmaxThirtySecondShotMapping {
  return {
    shotType: value.shotType || value.shot_type || 'unknown',
    selectedCharacterId: value.selectedCharacterId || value.character_id || undefined,
    referenceFaceConfirmed: value.referenceFaceConfirmed ?? value.reference_confirmed ?? false,
    preserveSource: value.preserveSource ?? value.preserve_original ?? false,
  }
}

export function emptySceneReviewMappings(plan: XmaxThirtySecondScenePlan) {
  return Object.fromEntries(sceneReviewUnits(plan).map((shot) => [
    String(shot.sequence),
    {
      shotType: 'unknown' as const,
      preserveSource: false,
      referenceFaceConfirmed: false,
    },
  ]))
}

export function sceneReviewMappingsFromPlan(plan: XmaxThirtySecondScenePlan) {
  return Object.fromEntries(sceneReviewUnits(plan).map((shot) => [
    String(shot.sequence),
    {
      shotType: shot.shot_type || 'unknown',
      selectedCharacterId: shot.selected_character_id || undefined,
      referenceFaceConfirmed: shot.reference_face_confirmed === true,
      preserveSource: shot.preserve_original === true,
    },
  ]))
}

export function readSceneReviewDraft(plan: XmaxThirtySecondScenePlan) {
  const persisted = sceneReviewMappingsFromPlan(plan)
  const empty = emptySceneReviewMappings(plan)
  const fallback = Object.fromEntries(sceneReviewUnits(plan).map((shot) => {
    const saved = persisted[String(shot.sequence)]
    return [String(shot.sequence), saved?.shotType !== 'unknown' ? saved : empty[String(shot.sequence)]]
  }))
  try {
    const raw = window.localStorage.getItem(sceneReviewDraftStorageKey(plan.episode_id))
    if (!raw) return fallback
    const stored = JSON.parse(raw) as StoredSceneReviewDraft
    if (
      stored.version !== 1
      || stored.episodeId !== plan.episode_id
      || stored.planSignature !== scenePlanSignature(plan)
    ) {
      return fallback
    }
    return Object.fromEntries(sceneReviewUnits(plan).map((shot) => {
      const saved = stored.mappings[String(shot.sequence)]
      return [String(shot.sequence), saved ? normalizeMapping(saved) : fallback[String(shot.sequence)]]
    }))
  } catch {
    return fallback
  }
}

function mappingsFingerprint(
  plan: XmaxThirtySecondScenePlan,
  mappings: Record<string, XmaxThirtySecondShotMapping>,
) {
  return JSON.stringify(sceneReviewUnits(plan).map((shot) => [
    shot.sequence,
    normalizeMapping(mappings[String(shot.sequence)] || {
      shotType: 'unknown',
      preserveSource: false,
      referenceFaceConfirmed: false,
    }),
  ]))
}

export function sceneReviewDraftIsUnsaved(
  plan: XmaxThirtySecondScenePlan,
  draft: Record<string, XmaxThirtySecondShotMapping>,
) {
  return mappingsFingerprint(plan, draft) !== mappingsFingerprint(plan, sceneReviewMappingsFromPlan(plan))
}

export function clearSceneReviewDraft(episodeId: string) {
  window.localStorage.removeItem(sceneReviewDraftStorageKey(episodeId))
}

export function writeSceneReviewDraft(
  plan: XmaxThirtySecondScenePlan,
  mappings: Record<string, XmaxThirtySecondShotMapping>,
) {
  const stored: StoredSceneReviewDraft = {
    version: 1,
    episodeId: plan.episode_id,
    planSignature: scenePlanSignature(plan),
    mappings: Object.fromEntries(Object.entries(mappings).map(([sequence, mapping]) => [
      sequence,
      normalizeMapping(mapping),
    ])),
  }
  window.localStorage.setItem(sceneReviewDraftStorageKey(plan.episode_id), JSON.stringify(stored))
}

export function sceneReviewMappingIsValid(
  mapping: XmaxThirtySecondShotMapping | undefined,
  confirmedCharacters: Character[],
) {
  if (!mapping) return false
  if (mapping.shotType === 'single_character') {
    return Boolean(
      mapping.selectedCharacterId
      && mapping.referenceFaceConfirmed
      && confirmedCharacters.some((character) => character.id === mapping.selectedCharacterId),
    )
  }
  if (mapping.shotType === 'multi_character') return mapping.preserveSource === true
  return mapping.shotType === 'no_character' || mapping.shotType === 'transition'
}

export function sceneReviewMappingsReady(
  plan: XmaxThirtySecondScenePlan | null,
  mappings: Record<string, XmaxThirtySecondShotMapping>,
  confirmedCharacters: Character[],
) {
  return Boolean(
    plan
    && sceneReviewUnits(plan).length
    && sceneReviewUnits(plan).every((shot) => sceneReviewMappingIsValid(mappings[String(shot.sequence)], confirmedCharacters)),
  )
}

export function XmaxSceneReview({
  plan,
  mappings,
  confirmedCharacters,
  disabled = false,
  selectedSequences,
  onToggleSequence,
  onMappingsChange,
  statusForShot,
  reviewingPersistedUnits = false,
  children,
}: SceneReviewProps) {
  const units = plan ? sceneReviewUnits(plan) : []
  const updateMapping = (sequence: number, patch: Partial<XmaxThirtySecondShotMapping>) => {
    const key = String(sequence)
    const current = mappings[key] || {
      shotType: 'unknown',
      preserveSource: false,
      referenceFaceConfirmed: false,
    }
    onMappingsChange({ ...mappings, [key]: normalizeMapping({ ...current, ...patch }) })
  }
  const singleCount = units.filter((shot) => mappings[String(shot.sequence)]?.shotType === 'single_character').length
  const preserveCount = units.filter((shot) => {
    const mapping = mappings[String(shot.sequence)]
    return Boolean(mapping?.preserveSource || ['no_character', 'transition', 'multi_character'].includes(mapping?.shotType || ''))
  }).length
  const manualCount = units.filter((shot) => !sceneReviewMappingIsValid(mappings[String(shot.sequence)], confirmedCharacters)).length

  return <div className="xmax-thirty-second-review">
    <div className="xmax-thirty-second-review-head">
      <strong>Scene Review</strong>
      <span>{plan ? `${plan.base_shot_count || plan.base_shots?.length || plan.shots.length} Base Shots · ${plan.transform_unit_count || units.length} Transform Units` : 'Analyzing locally...'}</span>
      {selectedSequences && <span>selected {selectedSequences.length}</span>}
      <span>single {singleCount}</span>
      <span>preserve {preserveCount}</span>
      <span>manual {manualCount}</span>
    </div>
    {children}
    {units.map((shot) => {
      const mapping = mappings[String(shot.sequence)]
      const status = statusForShot?.(shot) || {
        valid: sceneReviewMappingIsValid(mapping, confirmedCharacters),
        label: sceneReviewMappingIsValid(mapping, confirmedCharacters) ? 'Mapping confirmed' : 'Needs mapping',
      }
      const selected = selectedSequences?.includes(shot.sequence) || false
      return <div className="xmax-thirty-second-review-card" key={shot.transform_unit_id || shot.sequence}>
        <div className="xmax-thirty-second-review-images">
          {onToggleSequence
            ? <label className="xmax-thirty-second-shot-select"><input type="checkbox" checked={selected} disabled={disabled} onChange={() => onToggleSequence(shot.sequence)} /> Unit {shot.sequence}</label>
            : <strong>Unit {shot.sequence}</strong>}
          {shot.first_frame_url && <img src={normalizeBackendAssetUrl(shot.first_frame_url)} alt={`Unit ${shot.sequence} first frame`} />}
          {shot.middle_frame_url && <img src={normalizeBackendAssetUrl(shot.middle_frame_url)} alt={`Unit ${shot.sequence} middle frame`} />}
          {shot.last_frame_url && <img src={normalizeBackendAssetUrl(shot.last_frame_url)} alt={`Unit ${shot.sequence} last frame`} />}
        </div>
        <div className="xmax-thirty-second-review-body">
          <strong>{reviewingPersistedUnits ? 'Unit' : 'Transform Unit'} {shot.sequence}</strong>
          <span>{shot.source_start.toFixed(2)} - {shot.source_end.toFixed(2)}s · {shot.target_duration.toFixed(2)}s</span>
          <select
            value={mapping?.shotType || 'unknown'}
            disabled={disabled}
            onChange={(event) => {
              const shotType = event.target.value as XmaxThirtySecondShotMapping['shotType']
              updateMapping(shot.sequence, {
                shotType,
                selectedCharacterId: shotType === 'single_character' ? mapping?.selectedCharacterId : undefined,
                preserveSource: shotType !== 'single_character',
                referenceFaceConfirmed: false,
              })
            }}
          >
            <option value="unknown">Manual confirmation required</option>
            <option value="single_character">Single character</option>
            <option value="multi_character">Multi-character, preserve source</option>
            <option value="no_character">No character, preserve source</option>
            <option value="transition">Transition, preserve source</option>
          </select>
          {mapping?.shotType === 'single_character' && <>
            <select
              value={mapping.selectedCharacterId || ''}
              disabled={disabled}
              onChange={(event) => updateMapping(shot.sequence, {
                selectedCharacterId: event.target.value || undefined,
                preserveSource: false,
                referenceFaceConfirmed: false,
              })}
            >
              <option value="">Choose confirmed character</option>
              {confirmedCharacters.map((character) => <option key={character.id} value={character.id}>{character.character_code || character.name}</option>)}
            </select>
            <label className="xmax-thirty-second-preserve">
              <input
                type="checkbox"
                checked={mapping.referenceFaceConfirmed === true}
                disabled={disabled || !mapping.selectedCharacterId}
                onChange={(event) => updateMapping(shot.sequence, { referenceFaceConfirmed: event.target.checked })}
              />
              Confirm reference contains one person
            </label>
          </>}
          {mapping?.shotType === 'multi_character' && <label className="xmax-thirty-second-preserve">
            <input
              type="checkbox"
              checked={mapping.preserveSource === true}
              disabled={disabled}
              onChange={(event) => updateMapping(shot.sequence, { preserveSource: event.target.checked })}
            />
            Preserve original frame
          </label>}
          <span className={status.valid ? 'xmax-thirty-second-valid' : 'xmax-thirty-second-invalid'}>{status.label}</span>
          <details className="xmax-thirty-second-diagnostics">
            <summary>Advanced scene details</summary>
            <code>{JSON.stringify({
              transformUnitId: shot.transform_unit_id,
              parentShotId: shot.parent_shot_id,
              cutMethod: shot.cut_method,
              sceneScore: shot.scene_score,
              risk: shot.risk_level,
              riskScore: shot.risk_score,
            }, null, 2)}</code>
          </details>
        </div>
      </div>
    })}
  </div>
}

export function XmaxSceneReviewEditor({
  project,
  episodeId,
  onPlanChange,
  onProjectRefresh,
}: {
  project: Project
  episodeId?: string
  onPlanChange?: (plan: XmaxThirtySecondScenePlan) => void
  onProjectRefresh?: () => Promise<void>
}) {
  const [plan, setPlan] = useState<XmaxThirtySecondScenePlan | null>(null)
  const [persistedMappings, setPersistedMappings] = useState<Record<string, XmaxThirtySecondShotMapping>>({})
  const [mappings, setMappings] = useState<Record<string, XmaxThirtySecondShotMapping>>({})
  const [error, setError] = useState('')
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved'>('idle')
  const [preflight, setPreflight] = useState<Awaited<ReturnType<typeof preflightXmaxThirtySecondTask>> | null>(null)
  const [selectedSequences, setSelectedSequences] = useState<number[]>([])
  const [batchCharacterId, setBatchCharacterId] = useState('')
  const episode = project.episodes?.find((item) => item.id === episodeId)
  const selectableCharacters = useMemo(
    () => project.characters.filter((character) =>
      character.confirmed
      && character.priority_level === 'Locked'
      && Boolean(character.target_reference_path || character.main_reference_image || character.source_reference_image),
    ),
    [project.characters],
  )
  const episodeCharacters = useMemo(
    () => validEpisodeTargetCharacters(project.characters, episode),
    [project.characters, episode],
  )

  useEffect(() => {
    let active = true
    setPlan(null)
    setPersistedMappings({})
    setMappings({})
    setError('')
    setSaveStatus('idle')
    setPreflight(null)
    setSelectedSequences([])
    setBatchCharacterId('')
    if (!episodeId) return () => { active = false }
    void getXmaxThirtySecondScenePlan(episodeId)
      .then((nextPlan) => {
        if (!active) return
        const persisted = sceneReviewMappingsFromPlan(nextPlan)
        setPlan(nextPlan)
        setPersistedMappings(persisted)
        setMappings(readSceneReviewDraft(nextPlan))
        onPlanChange?.(nextPlan)
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { active = false }
  }, [episodeId])

  useEffect(() => {
    let active = true
    setPreflight(null)
    if (!episodeId || !plan) return () => { active = false }
    void preflightXmaxThirtySecondTask(episodeId, persistedMappings)
      .then((result) => {
        if (active) setPreflight(result)
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { active = false }
  }, [episodeId, plan?.mappings_updated_at, persistedMappings])

  const updateMappings = (next: Record<string, XmaxThirtySecondShotMapping>) => {
    setMappings(next)
    setSaveStatus('idle')
    if (plan) writeSceneReviewDraft(plan, next)
  }

  if (!episodeId) return <p className="empty-copy">Select an Episode to review its Scene mappings.</p>
  if (error) return <div className="xmax-thirty-second-error">Scene Review failed: {error}</div>
  if (!plan) return <p className="empty-copy">Loading persisted Base Shots and Transform Units...</p>

  const hasUnsavedDraft = sceneReviewDraftIsUnsaved(plan, mappings)
  const draftReady = sceneReviewMappingsReady(plan, mappings, selectableCharacters)
  const persistedReady = !hasUnsavedDraft
    && sceneReviewMappingsReady(plan, persistedMappings, episodeCharacters)
    && preflight?.allValid === true
  const toggleSequence = (sequence: number) => setSelectedSequences((current) => (
    current.includes(sequence) ? current.filter((item) => item !== sequence) : [...current, sequence]
  ))
  const applyBatchMapping = async (preserveSource: boolean) => {
    if (!episodeId || selectedSequences.length === 0 || (!preserveSource && !batchCharacterId)) return
    setError('')
    try {
      const result = await batchBindXmaxThirtySecondMappings(
        episodeId,
        selectedSequences,
        preserveSource ? '' : batchCharacterId,
        preserveSource,
      )
      if (result.provider_call_started) throw new Error('Batch mapping unexpectedly started a Provider call.')
      const next = { ...mappings }
      for (const mapping of result.mappings) {
        next[String(mapping.sequence)] = {
          shotType: mapping.shot_type,
          selectedCharacterId: mapping.selected_character_id || undefined,
          referenceFaceConfirmed: mapping.reference_face_confirmed,
          preserveSource: mapping.preserve_source,
        }
      }
      updateMappings(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }
  const saveMappings = async () => {
    if (!episodeId || !draftReady || !hasUnsavedDraft || saveStatus === 'saving') return
    setSaveStatus('saving')
    setError('')
    try {
      await saveXmaxThirtySecondSceneMappings(episodeId, mappings)
      const savedPlan = await getXmaxThirtySecondScenePlan(episodeId, 0.35, false)
      const savedMappings = sceneReviewMappingsFromPlan(savedPlan)
      clearSceneReviewDraft(episodeId)
      setPlan(savedPlan)
      setPersistedMappings(savedMappings)
      setMappings(savedMappings)
      await onProjectRefresh?.()
      setSaveStatus('saved')
      onPlanChange?.(savedPlan)
      window.dispatchEvent(new CustomEvent(XMAX_SCENE_MAPPINGS_SAVED_EVENT, {
        detail: {
          episodeId,
          activeTaskId: savedPlan.active_task_id || null,
          syncedSegmentIds: savedPlan.synced_segment_ids || [],
        },
      }))
    } catch (reason) {
      setSaveStatus('idle')
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }
  return <div className="xmax-scene-review-editor">
    <p className={persistedReady ? 'xmax-thirty-second-valid' : 'xmax-thirty-second-invalid'}>
      {persistedReady
        ? 'All Transform Unit mappings are confirmed for the next Xmax task.'
        : hasUnsavedDraft && draftReady
          ? 'Unsaved Scene Review draft. Save Scene mappings before creating an Xmax task.'
          : 'Saved mappings contain an invalid character or still need mapping.'}
    </p>
    <XmaxSceneReview
      plan={plan}
      mappings={mappings}
      confirmedCharacters={selectableCharacters}
      selectedSequences={selectedSequences}
      onToggleSequence={toggleSequence}
      onMappingsChange={updateMappings}
      statusForShot={(shot) => {
        const draft = mappings[String(shot.sequence)]
        if (draft?.selectedCharacterId && !selectableCharacters.some((character) => character.id === draft.selectedCharacterId)) {
          return { valid: false, label: 'Character is no longer available; remap this Unit.' }
        }
        if (hasUnsavedDraft) {
          return sceneReviewMappingIsValid(draft, selectableCharacters)
            ? { valid: false, label: 'Draft mapping; save required.' }
            : { valid: false, label: 'Needs mapping' }
        }
        const checked = preflight?.shots.find((item) => item.sequence === shot.sequence)
        return checked?.mappingValid
          ? { valid: true, label: 'Mapping confirmed' }
          : { valid: false, label: checked?.errorMessage || checked?.errorCode || 'Needs mapping' }
      }}
    >
      <div className="xmax-thirty-second-batch-controls">
        <select value={batchCharacterId} disabled={saveStatus === 'saving'} onChange={(event) => setBatchCharacterId(event.target.value)}>
          <option value="">Choose character for batch</option>
          {selectableCharacters.map((character) => <option key={character.id} value={character.id}>{character.character_code || character.name}</option>)}
        </select>
        <button className="text-button" disabled={saveStatus === 'saving'} onClick={() => setSelectedSequences(sceneReviewUnits(plan).map((shot) => shot.sequence))}>Select all</button>
        <button className="text-button" disabled={saveStatus === 'saving' || selectedSequences.length === 0} onClick={() => setSelectedSequences([])}>Clear</button>
        <button className="text-button" disabled={saveStatus === 'saving'} onClick={() => setSelectedSequences(sceneReviewUnits(plan).filter((shot) => mappings[String(shot.sequence)]?.shotType === 'single_character').map((shot) => shot.sequence))}>Only single-character</button>
        <button className="secondary-button" disabled={saveStatus === 'saving' || selectedSequences.length === 0 || !batchCharacterId} onClick={() => void applyBatchMapping(false)}>Apply character to selected</button>
        <button className="secondary-button" disabled={saveStatus === 'saving' || selectedSequences.length === 0} onClick={() => void applyBatchMapping(true)}>Keep selected source</button>
      </div>
      <div className="xmax-thirty-second-batch-summary"><span>selected {selectedSequences.length}</span><strong>{hasUnsavedDraft ? 'Unsaved Scene Review draft' : 'No unsaved changes'}</strong></div>
    </XmaxSceneReview>
    <div className="xmax-single-shot-actions">
      <button className="secondary-button" disabled={!draftReady || !hasUnsavedDraft || saveStatus === 'saving'} onClick={() => void saveMappings()}>
        {saveStatus === 'saving' ? 'Saving mappings...' : 'Save Scene mappings'}
      </button>
      {saveStatus === 'saved' && <span className="xmax-thirty-second-valid">Mappings saved for this Episode.</span>}
    </div>
  </div>
}
