import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, LoaderCircle, Play, Plus, RefreshCw, RotateCcw, Save, SlidersHorizontal, Trash2, Volume2 } from 'lucide-react'
import { api, assetUrl, type AudioMixSettings, type DubbingState, type TencentVoicePreview, type VoiceGenerationTask } from './api'
import { stage3a2Text, useI18n } from './i18n'
import { WORKFLOW_MODULE_IDS } from './workflowModuleNavigation'

type Props = { episodeId?: string; processingMode?: string; targetLanguage?: string }
type BindingDraft = { speaker_label: string; character_id: string; voice_id: string; scope: 'episode' | 'project'; saveStatus?: 'saving' | 'saved' | 'failed'; dirty?: boolean }
type CharacterVoiceDraft = { character_id: string; speaker_label: string; voice_id: string; saveStatus?: 'saving' | 'saved' | 'failed' }

const ACTIVE = new Set(['pending', 'running', 'queued', 'generating_voice', 'aligning', 'mixing', 'rendering_preview'])

function voiceGender(voice: DubbingState['voices'][number]) {
  const value = String(voice.metadata.gender_or_role || voice.labels.gender || '').toLowerCase()
  if (['male', 'female', 'boy', 'girl', 'child'].includes(value)) return value
  return 'unknown'
}

function voiceOptionLabel(voice: DubbingState['voices'][number]) {
  if (voice.provider === 'tencent') {
    const profile = voice.metadata.endpoint_profiles?.basic_synthesis
    const voiceType = voice.metadata.voice_type || voice.voice_id.split(':').pop() || ''
    return `${voice.name}｜${voice.metadata.recommended_scene || voice.description || '-'}｜${voice.metadata.voice_tier || voice.category || '-'}｜${profile?.language_label || voice.labels.language || '-'}｜ID ${voiceType}`
  }
  const gender = voiceGender(voice)
  const genderLabel = gender === 'female' ? 'Female' : gender === 'male' ? 'Male' : 'Voice'
  const accent = voice.labels.accent || voice.labels.language || 'Unknown accent'
  const style = voice.description || voice.category || 'Preset'
  return `${voice.name} — ${genderLabel} / ${accent} / ${style}`
}

function timecode(ms: number) {
  const total = Math.max(0, ms) / 1000
  const minutes = Math.floor(total / 60)
  return `${String(minutes).padStart(2, '0')}:${(total % 60).toFixed(1).padStart(4, '0')}`
}

function durationFitLabel(status: string | undefined, locale: string) {
  if (!status) return ''
  if (status === 'shortened_and_fit') return locale === 'zh-CN' ? '已自动缩短' : 'Automatically shortened'
  if (status === 'speed_adjusted_and_fit') return locale === 'zh-CN' ? '已适配' : 'Fitted'
  if (status === 'fit') return locale === 'zh-CN' ? '已适配' : 'Fitted'
  if (status === 'manual_review_required') return locale === 'zh-CN' ? '自动适配失败' : 'Automatic fit failed'
  return locale === 'zh-CN' ? '适配失败' : 'Fit failed'
}

function voiceStatusLabel(status: DubbingState['segments'][number]['voice_status'], locale: string) {
  const labels = {
    timing_exception: ['时长适配异常', 'Timing exception'],
    ready: ['已就绪', 'Ready'],
    missing_voice: ['缺少配音', 'Missing voice'],
    failed: ['生成失败', 'Failed'],
    text_changed: ['译文已修改', 'Text changed'],
    omitted: ['已忽略', 'Omitted'],
  } satisfies Record<DubbingState['segments'][number]['voice_status'], [string, string]>
  return labels[status][locale === 'zh-CN' ? 0 : 1]
}

export default function DubbingMixPanel({ episodeId, processingMode, targetLanguage = 'en' }: Props) {
  const { locale } = useI18n()
  const tx = (key: string) => stage3a2Text(locale, key)
  const [state, setState] = useState<DubbingState | null>(null)
  const [drafts, setDrafts] = useState<BindingDraft[]>([])
  const [characterVoices, setCharacterVoices] = useState<CharacterVoiceDraft[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [mix, setMix] = useState<AudioMixSettings | null>(null)
  const [mixDirty, setMixDirty] = useState(false)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState<{ summary: string; details: string } | null>(null)
  const [voiceAvailability, setVoiceAvailability] = useState('compatible')
  const [voiceEndpoint, setVoiceEndpoint] = useState('basic_synthesis')
  const [voiceLanguage, setVoiceLanguage] = useState('en')
  const [voiceRole, setVoiceRole] = useState('all')
  const [voiceScene, setVoiceScene] = useState('all')
  const [voiceTier, setVoiceTier] = useState('all')
  const [voiceSearch, setVoiceSearch] = useState('')
  const [catalogPreviews, setCatalogPreviews] = useState<Record<string, TencentVoicePreview>>({})
  const [addingSpeaker, setAddingSpeaker] = useState(false)
  const [newSpeakerLabel, setNewSpeakerLabel] = useState('')
  const previewRef = useRef<HTMLVideoElement | null>(null)
  const autoMixAfterTtsRef = useRef(false)

  const active = Boolean(state?.latest_task && ACTIVE.has(state.latest_task.status))
  const voiceNames = useMemo(() => new Map((state?.voices || []).map((voice) => [voice.voice_id, voice.name])), [state?.voices])
  const voiceScenes = useMemo(() => [...new Set((state?.voices || []).map((voice) => String(voice.metadata.recommended_scene || '')).filter(Boolean))].sort(), [state?.voices])
  const voiceTiers = useMemo(() => [...new Set((state?.voices || []).map((voice) => String(voice.metadata.voice_tier || voice.category || '')).filter(Boolean))].sort(), [state?.voices])
  const filteredVoices = useMemo(() => (state?.voices || []).filter((voice) => {
    const isEdge = state?.tts_provider.provider === 'edge'
    const endpoints = voice.metadata.supported_endpoints || []
    const profiles = voice.metadata.endpoint_profiles || {}
    const languageProfiles = voiceEndpoint === 'all'
      ? Object.values(profiles)
      : profiles[voiceEndpoint]
        ? [profiles[voiceEndpoint]]
        : []
    const voiceLanguageValue = String(voice.labels.language || voice.metadata.language || voice.metadata.locale || '').toLowerCase()
    const searchable = `${voice.name} ${voice.metadata.voice_type || ''} ${voice.metadata.recommended_scene || ''} ${voice.metadata.voice_tier || ''}`.toLocaleLowerCase()
    return (isEdge || voiceAvailability === 'all' || Boolean(voice.metadata.text_to_voice_compatible))
      && (isEdge || voiceEndpoint === 'all' || endpoints.includes(voiceEndpoint))
      && (voiceLanguage === 'all' || voiceLanguageValue === voiceLanguage || voiceLanguageValue.startsWith(`${voiceLanguage}-`) || languageProfiles.some((profile) => profile.languages.includes(voiceLanguage)))
      && (voiceRole === 'all' || voiceGender(voice) === voiceRole)
      && (voiceScene === 'all' || voice.metadata.recommended_scene === voiceScene)
      && (voiceTier === 'all' || (voice.metadata.voice_tier || voice.category) === voiceTier)
      && (!voiceSearch.trim() || searchable.includes(voiceSearch.trim().toLocaleLowerCase()))
  }), [state?.voices, state?.tts_provider.provider, voiceAvailability, voiceEndpoint, voiceLanguage, voiceRole, voiceScene, voiceTier, voiceSearch])

  function presentError(value: unknown) {
    const details = value instanceof Error ? value.message : String(value)
    const codes = [...details.matchAll(/\b([A-Z][A-Z0-9_]{3,})\b/g)].map((match) => match[1])
    const code = codes.find((candidate) => !['HTTP', 'ERROR', 'FAILED'].includes(candidate))
    setError({ summary: code ? tx(`error_${code}`) : tx('operationFailed'), details })
  }

  async function load(silent = false) {
    if (!episodeId) return
    if (!silent) setBusy('load')
    try {
      const next = await api<DubbingState>(`/episodes/${episodeId}/dubbing`)
      setState(next)
      window.dispatchEvent(new CustomEvent('localization-assets-updated', { detail: { episodeId } }))
      setSelected(new Set(next.mix_eligible_segment_ids))
      setCharacterVoices(next.characters.map((character) => {
        const saved = next.bindings.find((item) => item.character_id === character.id)
        return {
          character_id: character.id,
          speaker_label: character.character_code || character.name,
          voice_id: saved?.voice_id || character.voice_profile_id || character.voice_id || '',
          saveStatus: saved?.voice_id || character.voice_profile_id || character.voice_id ? 'saved' : undefined,
        }
      }))
      setDrafts((current) => [...new Set([...next.speakers, ...current.map((item) => item.speaker_label)])].map((speaker) => {
        const retained = current.find((item) => item.speaker_label === speaker)
        if (retained) return retained
        const saved = next.bindings.find((item) => item.speaker_label === speaker)
        return {
          speaker_label: speaker,
          character_id: saved?.character_id || '',
          voice_id: saved?.voice_id || '',
          scope: saved?.is_project_default ? 'project' : 'episode', saveStatus: saved?.voice_id ? 'saved' : undefined, dirty: false,
        }
      }))
      if (!mixDirty) setMix(next.mix_settings)
      setError(null)
    } catch (value) {
      presentError(value)
    } finally {
      if (!silent) setBusy('')
    }
  }

  useEffect(() => {
    setState(null); setDrafts([]); setCharacterVoices([]); setSelected(new Set()); setMix(null); setMixDirty(false); setMessage(''); setError(null); setCatalogPreviews({})
    autoMixAfterTtsRef.current = false
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeId])

  useEffect(() => {
    if (!episodeId || !active) return
    const deadline = Date.now() + 120_000
    let expired = false
    const timer = window.setInterval(() => {
      if (Date.now() >= deadline) {
        if (!expired) {
          expired = true
          void load(true)
        }
        window.clearInterval(timer)
        return
      }
      void load(true)
    }, 900)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeId, active])

  const latestPreview = state?.dubbed_output?.preview_video_url ? `${assetUrl(state.dubbed_output.preview_video_url)}?v=${encodeURIComponent(state.dubbed_output.updated_at || state.dubbed_output.created_at || '')}` : ''
  const formalMixReady = Boolean(state?.audio_assets.formal_mix_ready && state?.dubbed_output)
  const formalMixVoiceSummary = state?.bindings
    .filter((binding) => binding.provider === state.selected_tts_provider && binding.voice_id)
    .map((binding) => `${binding.speaker_label}: ${binding.voice_name || binding.voice_id}`)
    .join(' · ') || ''
  const audioAssetMessage = !state
    ? ''
    : state.audio_assets.formal_mix_ready
      ? (locale === 'zh-CN' ? '正式混音已就绪' : 'Formal mix ready')
      : state.audio_assets.quick_preview_ready
        ? (locale === 'zh-CN' ? '快速预览已生成，正式混音未就绪' : 'Quick preview ready; formal mix is not ready')
        : state.audio_assets.stems_ready
          ? (locale === 'zh-CN' ? '背景音已就绪，正式混音未生成' : 'Background stems ready; formal mix not generated')
          : (locale === 'zh-CN' ? '正式音频资产未就绪' : 'Formal audio assets are not ready')

  useEffect(() => {
    if (!latestPreview || !previewRef.current) return
    previewRef.current.load()
    void previewRef.current.play().catch(() => undefined)
  }, [latestPreview])

  useEffect(() => {
    if (
      !autoMixAfterTtsRef.current
      || state?.latest_task?.status !== 'completed'
      || busy
    ) return
    autoMixAfterTtsRef.current = false
    if (state.mix_missing_segment_ids.length || !state.mix_eligible_segment_ids.length) return
    void createMixPreview()
    // createMixPreview intentionally runs once for the TTS batch initiated in this panel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.latest_task?.id, state?.latest_task?.status, state?.mix_revision, busy])

  async function run(label: string, action: () => Promise<unknown>, success = '') {
    setBusy(label); setMessage(''); setError(null)
    try {
      await action()
      if (success) setMessage(success)
      await load(true)
      return true
    } catch (value) {
      presentError(value)
      return false
    } finally {
      setBusy('')
    }
  }

  async function selectTtsProvider(provider: DubbingState['selected_tts_provider']) {
    if (!episodeId || provider === state?.selected_tts_provider) return
    await run('tts-provider', () => api(`/episodes/${episodeId}/tts-provider`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider }),
    }), locale === 'zh-CN' ? '当前剧集配音服务已保存' : 'Episode TTS provider saved')
  }

  function updateDraft(speaker: string, patch: Partial<BindingDraft>) {
    setDrafts((items) => items.map((item) => item.speaker_label === speaker ? { ...item, ...patch } : item))
  }

  function updateCharacterVoice(characterId: string, patch: Partial<CharacterVoiceDraft>) {
    setCharacterVoices((items) => items.map((item) => item.character_id === characterId ? { ...item, ...patch } : item))
  }

  function addSpeakerRole() {
    const speakerLabel = newSpeakerLabel.trim()
    if (!speakerLabel) return
    if (drafts.some((item) => item.speaker_label.toLocaleLowerCase() === speakerLabel.toLocaleLowerCase())) {
      setError({ summary: locale === 'zh-CN' ? '声音角色已存在' : 'Voice role already exists', details: speakerLabel })
      return
    }
    setDrafts((items) => [...items, {
      speaker_label: speakerLabel,
      character_id: '',
      voice_id: '',
      scope: 'episode',
      dirty: true,
    }])
    setNewSpeakerLabel('')
    setAddingSpeaker(false)
    setError(null)
  }

  async function saveVoiceImmediately(draft: BindingDraft, voiceId: string) {
    if (!episodeId || !voiceId) return
    const next = { ...draft, voice_id: voiceId, saveStatus: 'saving' as const }
    updateDraft(draft.speaker_label, next)
    try {
      await api(`/episodes/${episodeId}/speaker-voice-bindings`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bindings: [{ speaker_label: next.speaker_label, character_id: next.character_id || null, voice_id: next.voice_id, scope: next.scope }] }),
      })
      updateDraft(draft.speaker_label, { voice_id: voiceId, saveStatus: 'saved', dirty: false })
      setMessage(tx('voiceBindingSaved'))
      await load(true)
    } catch (value) {
      updateDraft(draft.speaker_label, { voice_id: voiceId, saveStatus: 'failed' })
      presentError(value)
    }
  }

  async function saveCharacterVoiceImmediately(draft: CharacterVoiceDraft, voiceId: string) {
    if (!episodeId || !voiceId) return
    updateCharacterVoice(draft.character_id, { voice_id: voiceId, saveStatus: 'saving' })
    try {
      await api(`/episodes/${episodeId}/speaker-voice-bindings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bindings: [{
            speaker_label: draft.speaker_label,
            character_id: draft.character_id,
            voice_id: voiceId,
            scope: 'episode',
          }],
        }),
      })
      updateCharacterVoice(draft.character_id, { voice_id: voiceId, saveStatus: 'saved' })
      setDrafts((items) => items.map((item) =>
        item.character_id === draft.character_id ? { ...item, voice_id: voiceId, saveStatus: 'saved' } : item,
      ))
      setMessage(tx('voiceBindingSaved'))
      await load(true)
    } catch (value) {
      updateCharacterVoice(draft.character_id, { voice_id: voiceId, saveStatus: 'failed' })
      presentError(value)
    }
  }

  async function saveBindings() {
    if (!episodeId) return
    const characterVoiceMap = new Map(characterVoices.map((item) => [item.character_id, item.voice_id]))
    const resolvedDrafts = drafts.map((item) => ({
      ...item,
      voice_id: item.character_id ? characterVoiceMap.get(item.character_id) || '' : item.voice_id,
    }))
    const dirtyDrafts = resolvedDrafts.filter((item) => item.dirty)
    const incomplete = dirtyDrafts.some((item) => !item.voice_id)
    if (incomplete) { setError({ summary: tx('missingBinding'), details: tx('missingBinding') }); return }
    const payloadBySpeaker = new Map<string, Omit<BindingDraft, 'saveStatus' | 'dirty'>>()
    dirtyDrafts.forEach(({ saveStatus: _saveStatus, dirty: _dirty, ...item }) => payloadBySpeaker.set(item.speaker_label, item))
    if (!payloadBySpeaker.size) {
      setMessage(locale === 'zh-CN' ? '映射没有变化' : 'No mapping changes')
      return
    }
    await run('bindings', () => api(`/episodes/${episodeId}/speaker-voice-bindings`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bindings: [...payloadBySpeaker.values()].map((item) => ({ ...item, character_id: item.character_id || null })) }),
    }), tx('bindingsSaved'))
  }

  async function resolveVoiceSegment(segmentId: string, action: 'use_current' | 'omit' | 'restore') {
    if (!episodeId) return
    await run(`resolve-${segmentId}`, () => api(`/episodes/${episodeId}/dubbing-segments/${segmentId}/resolution`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    }))
  }

  async function reassignSegmentSpeaker(segmentId: string, speakerLabel: string) {
    if (!episodeId) return
    await run(`speaker-${segmentId}`, () => api(`/episodes/${episodeId}/dubbing-segments/${segmentId}/speaker`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speaker_label: speakerLabel }),
    }))
  }

  async function taskAction(action: 'pause' | 'resume' | 'cancel' | 'reset-stuck') {
    if (!task) return
    await run(action, () => api(`/voice-generation-tasks/${task.id}/${action}`, { method: 'POST' }))
  }

  async function generate(mode: 'selected' | 'all_unfinished' | 'failed', segmentIds: string[] = []) {
    if (!episodeId) return
    if (mode === 'selected' && !segmentIds.length) { setError({ summary: tx('selectSegment'), details: tx('selectSegment') }); return }
    autoMixAfterTtsRef.current = true
    const started = await run('generate', () => api<VoiceGenerationTask>(`/episodes/${episodeId}/tts-generate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode, segment_ids: segmentIds }),
    }))
    if (!started) autoMixAfterTtsRef.current = false
  }

  async function preview(segmentId: string, voiceId?: string) {
    if (!episodeId) return
    if (!voiceId) { setError({ summary: tx('missingBinding'), details: tx('missingBinding') }); return }
    await run(`preview-${segmentId}`, () => api<VoiceGenerationTask>(`/episodes/${episodeId}/tts-preview`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ segment_id: segmentId, voice_id: voiceId }),
    }))
  }

  async function previewCatalogVoice(voiceId: string) {
    if (!episodeId) return
    const voiceType = voiceId.split(':').pop() || ''
    if (!/^\d+$/.test(voiceType)) {
      setError({ summary: locale === 'zh-CN' ? '音色ID无效' : 'Invalid voice ID', details: voiceId })
      return
    }
    const label = `catalog-preview-${voiceId}`
    setBusy(label)
    setMessage('')
    setError(null)
    try {
      const result = await api<TencentVoicePreview>(`/episodes/${episodeId}/tts-voices/${voiceType}/preview`, { method: 'POST' })
      setCatalogPreviews((items) => ({ ...items, [voiceId]: result }))
    } catch (value) {
      presentError(value)
    } finally {
      setBusy('')
    }
  }

  async function createMixPreview() {
    if (!episodeId) return
    const ids = state?.mix_eligible_segment_ids || []
    const missingIds = state?.mix_missing_segment_ids || []
    if (missingIds.length) {
      setError({
        summary: locale === 'zh-CN' ? '最新配音尚未齐全' : 'Latest TTS is incomplete',
        details: `${locale === 'zh-CN' ? '缺少片段' : 'Missing segments'}: ${missingIds.join(', ')}`,
      })
      return
    }
    if (!ids.length) {
      const reasons = [...new Set(Object.values(state?.mix_ineligible_reasons || {}))]
      setError({
        summary: locale === 'zh-CN' ? '没有可用配音' : 'No voice output is ready for mixing',
        details: reasons.length ? reasons.join('; ') : (locale === 'zh-CN' ? '没有已完成且可读取的配音文件。' : 'No completed, readable voice file is available.'),
      })
      return
    }
    await run('mix', () => api(`/episodes/${episodeId}/dubbing-preview`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        dialogue_original_volume: mix?.dialogue_original_volume ?? 0.05,
        non_dialogue_original_volume: mix?.non_dialogue_original_volume ?? 0.95,
        dub_voice_volume: mix?.dub_voice_volume ?? 1,
        fade_ms: Math.max(10, Math.min(30, mix?.fade_ms ?? 20)),
        include_target_subtitles: mix?.include_target_subtitles ?? true,
      }),
    }))
  }

  async function saveMix() {
    if (!episodeId || !mix) return
    await run('mix-settings', () => api(`/episodes/${episodeId}/audio-mix-settings`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(mix),
    }), tx('mixSaved'))
    setMixDirty(false)
  }

  if (!episodeId) return <section className="settings-section a2-panel"><h2>{tx('title')}</h2><p className="muted-copy">{tx('episodeRequired')}</p></section>
  if (!state || !mix) return <section className="settings-section a2-panel"><div className="a2-loading"><LoaderCircle className="spin" size={18} /> {tx('title')}</div>{error && <A2Error error={error} technical={tx('technical')} />}</section>

  const task = state.latest_task
  const isConsistency = (processingMode || state.processing_mode) === 'character-consistency'
  const providerStatus = state.tts_provider.configured ? tx('real') : tx('disabled')
  const providerName = state.tts_provider.provider === 'tencent'
    ? 'Tencent Cloud'
    : state.tts_provider.provider === 'elevenlabs'
      ? 'ElevenLabs'
      : state.tts_provider.provider === 'edge'
        ? 'Edge TTS'
        : state.tts_provider.provider || 'TTS'
  const voiceRequiredLabel = state.tts_provider.provider === 'tencent'
    ? (locale === 'zh-CN' ? '请选择腾讯音色' : 'Select a Tencent voice')
    : (locale === 'zh-CN' ? '请先为角色绑定声音' : 'Select a voice')
  const providerNotice = state.tts_provider.provider === 'tencent'
    ? (locale === 'zh-CN'
        ? 'A2 使用腾讯云真实 TTS 和现有混音链路。人声分离与口型同步状态保持不变。'
        : 'A2 uses Tencent Cloud TTS with the existing mixing pipeline. Voice separation and lip sync remain unchanged.')
    : tx('notice')
  const characterVoiceMap = new Map(characterVoices.map((item) => [item.character_id, item.voice_id]))
  const formalReady = state.summary.eligible_count > 0 && state.summary.all_speakers_have_voice
  const needsShorterCount = state.summary.needs_shorter_count ?? state.summary.duration_warning_count
  const qualifiedCount = state.summary.qualified_count ?? Math.max(0, state.summary.completed_count - needsShorterCount)
  const mixEligibleIds = state.mix_eligible_segment_ids
  const selectedEligibleCount = mixEligibleIds.length
  const batchGenerateCount = state.tts_unfinished_segment_ids.length
  const isTencentCatalog = state.tts_provider.provider === 'tencent'
  const isEdgeCatalog = state.tts_provider.provider === 'edge'
  const catalogTitle = isTencentCatalog
    ? (locale === 'zh-CN' ? '腾讯云音色列表' : 'Tencent Cloud voice catalog')
    : isEdgeCatalog ? 'Edge TTS voice catalog' : tx('voiceCatalog')
  const catalogVersion = String(state.voices.find((voice) => voice.metadata.catalog_version)?.metadata.catalog_version || '-')
  const voiceCatalogCount = `${filteredVoices.length}/${state.voices.length}`
  const detectedSpeakerIds = new Set(state.speaker_profiles.map((profile) => profile.speaker_id))
  const voiceOnlyDrafts = drafts.filter((draft) => !detectedSpeakerIds.has(draft.speaker_label))
  const legacyVoiceName = (voiceId: string) => {
    const knownVoice = state.voices.find((voice) => voice.voice_id === voiceId)
    if (knownVoice) return voiceOptionLabel(knownVoice)
    const binding = state.bindings.find((item) => item.voice_id === voiceId)
    if (binding?.voice_name) return binding.voice_name
    if (voiceId.startsWith('tencent:')) return `${locale === 'zh-CN' ? '未知腾讯音色 ID' : 'Unknown Tencent voice ID'} ${voiceId.split(':').pop()}`
    return voiceId
  }
  const selectOptions = (selectedVoiceId: string) => {
    const selectedVisible = filteredVoices.some((voice) => voice.voice_id === selectedVoiceId)
    return <>
      {selectedVoiceId && !selectedVisible && <option value={selectedVoiceId}>{legacyVoiceName(selectedVoiceId)}</option>}
      {filteredVoices.map((voice) => <option
        key={voice.voice_id}
        value={voice.voice_id}
        disabled={isTencentCatalog && !voice.metadata.text_to_voice_compatible}
      >{voiceOptionLabel(voice)}{isTencentCatalog && !voice.metadata.text_to_voice_compatible ? ' · TextToVoice 不可用' : ''}</option>)}
    </>
  }

  return <section id="a2-dubbing-panel" className="settings-section a2-panel">
    <div className="section-heading-inline"><div><span className="section-label">A2-1</span><h2>{tx('title')}</h2></div><span className={`selection-count a2-status ${state.status}`}>{tx(state.status)}</span></div>
    <div className="demo-notice a2-notice"><Volume2 size={16} /><span>{providerNotice}</span></div>
    <div className="a2-capability-grid">
      <div><span>{tx('provider')}</span><select aria-label="TTS Provider" value={state.selected_tts_provider} disabled={active || Boolean(busy)} onChange={(event) => void selectTtsProvider(event.target.value as DubbingState['selected_tts_provider'])}>{state.tts_providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.display_name}</option>)}</select><small>{providerName} · {providerStatus}</small></div>
      <div><span>{tx('model')}</span><strong>{state.tts_provider.model || '-'}</strong></div>
      <div><span>{tx('outputFormat')}</span><strong>{state.tts_provider.output_format || '-'}</strong></div>
      <div><span>{tx('mixMode')}</span><strong>{tx('real')}</strong></div>
    </div>
    <div className="a2-boundary"><span>{tx('separation')}</span><span>{tx('lipSync')}</span></div>

    <div id={WORKFLOW_MODULE_IDS.voice} className="workflow-module-anchor">
    <div className="a2-subhead"><div><span className="section-label">{locale === 'zh-CN' ? '原视频说话人' : 'Source video speakers'}</span><p>{locale === 'zh-CN' ? `检测到 ${state.speaker_profiles.length} 个 Speaker` : `${state.speaker_profiles.length} detected speakers`} · {isConsistency ? tx('consistencyScope') : tx('quickScope')}</p></div><div className="a2-actions"><button className="secondary-button" disabled={active || Boolean(busy)} onClick={() => setAddingSpeaker((value) => !value)}><Plus size={15} /> {locale === 'zh-CN' ? '新增仅声音角色' : 'Add voice-only role'}</button><button className="secondary-button" disabled={active || Boolean(busy) || !state.voices.length} onClick={saveBindings}><Save size={15} /> {tx('saveBindings')}</button></div></div>
    {addingSpeaker && <div className="a2-add-speaker"><input value={newSpeakerLabel} maxLength={64} autoFocus placeholder={locale === 'zh-CN' ? '例如：旁白、电话声音' : 'e.g. Narrator, Phone voice'} onChange={(event) => setNewSpeakerLabel(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') addSpeakerRole() }} /><button className="primary-button compact" disabled={!newSpeakerLabel.trim()} onClick={addSpeakerRole}>{locale === 'zh-CN' ? '添加' : 'Add'}</button><button className="quiet-button" onClick={() => { setAddingSpeaker(false); setNewSpeakerLabel('') }}>{locale === 'zh-CN' ? '取消' : 'Cancel'}</button></div>}
    {!state.speaker_profiles.length ? <p className="empty-copy">{locale === 'zh-CN' ? '当前转录尚未检测到 Speaker' : 'No detected speakers in the current transcript'}</p> : <div className="a2-speaker-profiles">
      {state.speaker_profiles.map((profile) => {
        const draft = drafts.find((item) => item.speaker_label === profile.speaker_id)
        if (!draft) return null
        const resolvedVoiceId = draft.character_id ? characterVoiceMap.get(draft.character_id) || draft.voice_id : draft.voice_id
        const character = state.characters.find((item) => item.id === draft.character_id)
        return <article className="a2-speaker-profile" key={profile.speaker_id}>
          <div className="a2-speaker-profile-heading"><div><strong>{profile.speaker_id}</strong><span>{profile.source_provider || '-'} · {profile.source_model || '-'}</span></div><span className={`a2-speaker-binding-state ${resolvedVoiceId ? 'bound' : 'unbound'}`}>{resolvedVoiceId ? (locale === 'zh-CN' ? '已绑定' : 'Bound') : (locale === 'zh-CN' ? '未绑定' : 'Unbound')}</span></div>
          <div className="a2-speaker-profile-facts"><span>{locale === 'zh-CN' ? '片段' : 'Segments'} {profile.segment_count}</span><span>{locale === 'zh-CN' ? '说话时长' : 'Speaking time'} {(profile.total_duration_ms / 1000).toFixed(2)}s</span><span>{locale === 'zh-CN' ? '首次出现' : 'First seen'} {timecode(profile.first_appearance_ms)}</span><span>{character ? `${character.character_code || character.name}` : (locale === 'zh-CN' ? '未绑定角色' : 'No character')}</span><span>{resolvedVoiceId ? (voiceNames.get(resolvedVoiceId) || legacyVoiceName(resolvedVoiceId)) : (locale === 'zh-CN' ? '未绑定声音' : 'No voice')}</span></div>
          <div className="a2-speaker-profile-controls">
            <label><span>{locale === 'zh-CN' ? '绑定角色' : 'Character'}</span><select value={draft.character_id} disabled={active || Boolean(busy)} onChange={(event) => updateDraft(draft.speaker_label, { character_id: event.target.value, voice_id: event.target.value ? characterVoiceMap.get(event.target.value) || '' : draft.voice_id, dirty: true, saveStatus: undefined })}><option value="">{locale === 'zh-CN' ? '仅声音角色' : 'Voice-only role'}</option>{state.characters.map((item) => <option key={item.id} value={item.id}>{item.character_code ? `${item.character_code} · ` : ''}{item.name}</option>)}</select></label>
            <label><span>{locale === 'zh-CN' ? '配音声音' : 'TTS voice'} {draft.saveStatus && <em className={`a2-binding-status ${draft.saveStatus}`}>{tx(draft.saveStatus === 'saving' ? 'bindingSaving' : draft.saveStatus === 'saved' ? 'bindingSaved' : 'bindingFailed')}</em>}</span><select value={resolvedVoiceId} disabled={active || Boolean(busy)} onChange={(event) => void saveVoiceImmediately(draft, event.target.value)}><option value="">{locale === 'zh-CN' ? '未绑定' : 'Unbound'}</option>{selectOptions(resolvedVoiceId)}</select></label>
          </div>
        </article>
      })}
    </div>}
    {!!voiceOnlyDrafts.length && <details className="a2-speaker-assignments">
      <summary>{locale === 'zh-CN' ? '仅声音角色' : 'Voice-only roles'} · {voiceOnlyDrafts.length}</summary>
      <div className="a2-bindings">
        {voiceOnlyDrafts.map((draft) => {
          const resolvedVoiceId = draft.character_id ? characterVoiceMap.get(draft.character_id) || '' : draft.voice_id
          return <div className="a2-speaker-assignment-row" key={draft.speaker_label}>
            <label><span>{tx('speaker')}</span><strong>{draft.speaker_label}</strong></label>
            <label><span>归属角色</span><select value={draft.character_id} onChange={(event) => updateDraft(draft.speaker_label, { character_id: event.target.value, voice_id: event.target.value ? characterVoiceMap.get(event.target.value) || '' : draft.voice_id, dirty: true, saveStatus: undefined })}><option value="">仅声音角色</option>{state.characters.map((character) => <option key={character.id} value={character.id}>{character.character_code ? `${character.character_code} · ` : ''}{character.name}</option>)}</select></label>
            {draft.character_id
              ? <div><span>复用声音</span><strong>{voiceNames.get(resolvedVoiceId) || voiceRequiredLabel}</strong></div>
              : <label><span>{tx('voice')}</span><select value={draft.voice_id} onChange={(event) => void saveVoiceImmediately(draft, event.target.value)}><option value="">{tx('voice')}</option>{selectOptions(draft.voice_id)}</select></label>}
          </div>
        })}
      </div>
    </details>}

    <div className="a2-subhead"><div><span className="section-label">{locale === 'zh-CN' ? '配音声音' : 'TTS voices'}</span><p>{catalogTitle} · {state.voices.length ? `${voiceCatalogCount} · catalog ${catalogVersion}` : tx('noVoices')}</p></div><button className="secondary-button" disabled={active || Boolean(busy)} onClick={() => run('voices', () => api(`/episodes/${episodeId}/tts-voices/refresh`, { method: 'POST' }))}>{busy === 'voices' ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />} {busy === 'voices' ? tx('refreshingVoices') : tx('refreshVoices')}</button></div>
    <div className="a2-character-voices">
      {characterVoices.map((draft) => {
        const character = state.characters.find((item) => item.id === draft.character_id)
        const mappedSpeakers = drafts.filter((item) => item.character_id === draft.character_id).map((item) => item.speaker_label)
        const selectedVoice = state.voices.find((voice) => voice.voice_id === draft.voice_id)
        return <div className="a2-character-voice-row" key={draft.character_id}>
          <div><span>角色</span><strong>{character?.character_code || character?.name || draft.speaker_label}</strong><small>{mappedSpeakers.length ? `Speaker：${mappedSpeakers.join('、')}` : '尚未分配对白'}</small></div>
          <label><span>{tx('voice')} {draft.saveStatus && <em className={`a2-binding-status ${draft.saveStatus}`}>{tx(draft.saveStatus === 'saving' ? 'bindingSaving' : draft.saveStatus === 'saved' ? 'bindingSaved' : 'bindingFailed')}</em>}</span><select value={draft.voice_id} onChange={(event) => void saveCharacterVoiceImmediately(draft, event.target.value)}><option value="">{tx('voice')}</option>{selectOptions(draft.voice_id)}</select>{selectedVoice && <small>{voiceGender(selectedVoice)} · {selectedVoice.metadata.voice_tier || selectedVoice.labels.language || tx('unknownAccent')}</small>}</label>
        </div>
      })}
    </div>
    {!!state.voices.length && <details className="a2-voice-catalog">
      <summary>{locale === 'zh-CN' ? `浏览与筛选音色 · ${voiceCatalogCount}` : `Browse and filter voices · ${voiceCatalogCount}`}</summary>
      <div className="a2-voice-filters">
        <label><span>{locale === 'zh-CN' ? '可用性' : 'Availability'}</span><select value={voiceAvailability} onChange={(event) => setVoiceAvailability(event.target.value)}><option value="compatible">{locale === 'zh-CN' ? '当前接口可用' : 'Available for current API'}</option><option value="all">{locale === 'zh-CN' ? '全部音色' : 'All voices'}</option></select></label>
        <label><span>{locale === 'zh-CN' ? '接口' : 'Endpoint'}</span><select value={voiceEndpoint} onChange={(event) => setVoiceEndpoint(event.target.value)}><option value="basic_synthesis">TextToVoice</option><option value="realtime_synthesis">{locale === 'zh-CN' ? '实时语音合成' : 'Realtime synthesis'}</option><option value="long_text_synthesis">{locale === 'zh-CN' ? '长文本语音合成' : 'Long-text synthesis'}</option><option value="all">{locale === 'zh-CN' ? '全部接口' : 'All endpoints'}</option></select></label>
        <label><span>{locale === 'zh-CN' ? '语言' : 'Language'}</span><select value={voiceLanguage} onChange={(event) => setVoiceLanguage(event.target.value)}><option value="en">{locale === 'zh-CN' ? '英文' : 'English'}</option><option value="zh">{locale === 'zh-CN' ? '中文' : 'Chinese'}</option><option value="all">{locale === 'zh-CN' ? '全部语言' : 'All languages'}</option></select></label>
        <label><span>{locale === 'zh-CN' ? '角色类型' : 'Role'}</span><select value={voiceRole} onChange={(event) => setVoiceRole(event.target.value)}><option value="all">{locale === 'zh-CN' ? '全部' : 'All'}</option><option value="male">{locale === 'zh-CN' ? '男声' : 'Male'}</option><option value="female">{locale === 'zh-CN' ? '女声' : 'Female'}</option><option value="boy">{locale === 'zh-CN' ? '男童' : 'Boy'}</option><option value="girl">{locale === 'zh-CN' ? '女童' : 'Girl'}</option><option value="child">{locale === 'zh-CN' ? '儿童' : 'Child'}</option><option value="unknown">{locale === 'zh-CN' ? '未知' : 'Unknown'}</option></select></label>
        <label><span>{locale === 'zh-CN' ? '推荐场景' : 'Scene'}</span><select value={voiceScene} onChange={(event) => setVoiceScene(event.target.value)}><option value="all">{locale === 'zh-CN' ? '全部场景' : 'All scenes'}</option>{voiceScenes.map((scene) => <option key={scene} value={scene}>{scene}</option>)}</select></label>
        <label><span>{locale === 'zh-CN' ? '音色档位' : 'Tier'}</span><select value={voiceTier} onChange={(event) => setVoiceTier(event.target.value)}><option value="all">{locale === 'zh-CN' ? '全部档位' : 'All tiers'}</option>{voiceTiers.map((tier) => <option key={tier} value={tier}>{tier}</option>)}</select></label>
        <label className="a2-voice-search"><span>{locale === 'zh-CN' ? '搜索' : 'Search'}</span><input value={voiceSearch} onChange={(event) => setVoiceSearch(event.target.value)} placeholder={locale === 'zh-CN' ? '名称、ID或场景' : 'Name, ID, or scene'} /></label>
      </div>
      <p className="a2-catalog-callout">{locale === 'zh-CN' ? '页面加载、搜索和筛选不会调用腾讯云。试听会产生一次腾讯云语音合成调用。' : 'Loading, searching, and filtering do not call Tencent Cloud. Previewing makes one Tencent synthesis call.'}</p>
      <div className="a2-voice-catalog-list">
        {filteredVoices.map((voice) => {
          const metadata = voice.metadata
          const voiceType = metadata.voice_type || voice.voice_id.split(':').pop() || ''
          const profile = metadata.endpoint_profiles?.[voiceEndpoint === 'all' ? 'basic_synthesis' : voiceEndpoint] || metadata.endpoint_profiles?.basic_synthesis
          const compatible = isEdgeCatalog || Boolean(metadata.text_to_voice_compatible)
          const previewResult = catalogPreviews[voice.voice_id]
          const previewBusy = busy === `catalog-preview-${voice.voice_id}`
          return <article className={`a2-voice-card ${compatible ? '' : 'disabled'}`} key={voice.voice_id}>
            <div className="a2-voice-card-heading"><div><strong>{voiceOptionLabel(voice)}</strong><span>{metadata.recommended_scene || '-'}</span></div><button className="secondary-button compact" disabled={!compatible || Boolean(busy)} title={compatible ? '' : 'TextToVoice unavailable'} onClick={() => void previewCatalogVoice(voice.voice_id)}>{previewBusy ? <LoaderCircle className="spin" size={14} /> : <Play size={14} />} {locale === 'zh-CN' ? '试听' : 'Preview'}</button></div>
            <div className="a2-voice-facts"><span>VoiceType {voiceType}</span><span>{metadata.voice_tier || '-'}</span><span>{profile?.language_label || '-'}</span><span>{profile?.sample_rates_hz?.map((rate) => `${rate}Hz`).join(' / ') || '-'}</span><span>{profile?.emotions?.join(' / ') || '-'}</span><span>{metadata.supported_endpoints?.join(' / ') || '-'}</span></div>
            {!compatible && <p className="a2-warning-copy">{locale === 'zh-CN' ? '当前 TextToVoice 接口不可用。' : 'Not available for the current TextToVoice API.'}</p>}
            {metadata.requires_billing_notice && <p className="a2-catalog-cost">{locale === 'zh-CN' ? '资源包覆盖及费用需以腾讯云账户为准。' : 'Resource package coverage and charges depend on the Tencent Cloud account.'}</p>}
            {previewResult && <div className="a2-catalog-preview"><audio controls preload="metadata" src={`${assetUrl(previewResult.audio_url)}?v=${encodeURIComponent(previewResult.catalog_version)}`} /><small>{previewResult.cached ? (locale === 'zh-CN' ? '已使用试听缓存' : 'Cached preview') : (locale === 'zh-CN' ? '本次调用腾讯云 1 次' : 'One Tencent call')}</small></div>}
          </article>
        })}
      </div>
    </details>}

    <div className="a2-subhead"><div><span className="section-label">{tx('segments')} · {targetLanguage}</span><p>{locale === 'zh-CN' ? 'Speaker 数量' : 'Speakers'}: {state.summary.speaker_count} · {locale === 'zh-CN' ? '对白片段数量' : 'Dialogue segments'}: {state.summary.dialogue_segment_count} · {tx('eligibleCount')}: {state.summary.eligible_count} · {tx('generatedCount')}: {state.summary.completed_count} · {tx('missingTranslation')}: {state.summary.missing_translation_count}</p></div><div className="a2-actions"><button className="secondary-button" disabled={active || Boolean(busy)} onClick={() => generate('selected', [...selected])}><Volume2 size={15} /> {tx('generateSelected')}</button><button className="primary-button" disabled={active || Boolean(busy) || !formalReady || !batchGenerateCount} onClick={() => generate('all_unfinished')}><Play size={15} /> {tx('generateAll')} · {batchGenerateCount}</button><button className="icon-button" title={tx('refreshTranscript')} disabled={Boolean(busy)} onClick={() => void load()}><RefreshCw size={15} /></button></div></div>
    <p className={formalReady ? 'a2-ready-copy' : 'a2-warning-copy'}>
      {formalReady
        ? tx('voiceReady')
        : `${tx('voiceRequired')}${state.summary.missing_voice_speakers.length ? `: ${state.summary.missing_voice_speakers.join(', ')}` : ''}`}
    </p>
    <p className="a2-duration-summary">{tx('durationSummary').replace('{{total}}', String(state.summary.eligible_count)).replace('{{ready}}', String(qualifiedCount)).replace('{{shorter}}', String(needsShorterCount))} · {locale === 'zh-CN' ? `失败 ${state.summary.failed_count} · 中断 ${state.summary.interrupted_count || 0} · 运行 ${state.summary.running_count || 0}` : `Failed ${state.summary.failed_count} · Interrupted ${state.summary.interrupted_count || 0} · Running ${state.summary.running_count || 0}`}</p>
    <p className="a2-cache-note">{tx('cacheNotice')}</p>
    <div className="a2-selection-toolbar"><span>{tx('mixEligible')}: {mixEligibleIds.length} · {tx('select')}: {selectedEligibleCount}</span><span>{locale === 'zh-CN' ? '已自动包含全部可用配音' : 'All eligible voice outputs are included automatically'}</span></div>
    {!state.segments.length ? <p className="empty-copy">{tx('noSegments')}</p> : <div className="a2-segments">
      {state.segments.map((segment) => {
        const draft = drafts.find((item) => item.speaker_label === segment.speaker_label)
        const output = segment.output
        const currentAudio = segment.mix_eligible && output?.status === 'completed'
          ? output.aligned_audio_url
          : undefined
        const historicalAudio = !currentAudio
          ? output?.raw_audio_url || output?.aligned_audio_url
          : undefined
        const needsShorter = Boolean(output?.needs_shorter_text || output?.duration_warning)
        const fitLabel = durationFitLabel(output?.duration_fit_status, locale)
        const safeDuration = segment.segment_type === 'dialogue'
          ? segment.target_duration
          : segment.available_duration ?? segment.target_duration
        const canUseCurrent = Boolean(historicalAudio && (output?.generated_duration || 0) <= safeDuration * 2)
        const requiresResolution = ['failed', 'timing_exception'].includes(segment.voice_status) || output?.duration_fit_status === 'manual_review_required'
        return <article className={`a2-segment ${needsShorter ? 'warning' : ''}`} key={segment.transcript_segment_id}>
          <div className="a2-segment-main"><input aria-label={tx('select')} type="checkbox" disabled={!segment.mix_eligible} checked={Boolean(segment.mix_eligible && selected.has(segment.id))} onChange={(event) => setSelected((old) => { const next = new Set(old); event.target.checked ? next.add(segment.id) : next.delete(segment.id); return next })} /><span className="a2-seq">{String(segment.seq).padStart(2, '0')}</span><div><strong>{timecode(segment.start_ms)}-{timecode(segment.end_ms)}</strong><p>{segment.final_translation || segment.source_text}</p><div className="a2-segment-routing"><label className="field-label">Speaker<select aria-label={`Speaker for segment ${segment.seq}`} value={segment.speaker_label} disabled={active || Boolean(busy)} onChange={(event) => void reassignSegmentSpeaker(segment.id, event.target.value)}>{[...new Set([segment.speaker_label, ...state.speakers])].map((speaker) => <option key={speaker} value={speaker}>{speaker}</option>)}</select></label></div><small>{voiceNames.get(segment.voice_id || draft?.voice_id || '') || tx('voice')} · {tx('targetDuration')} {segment.target_duration.toFixed(2)}s{output?.generated_duration ? ` · ${tx('generatedDuration')} ${output.generated_duration.toFixed(2)}s` : ''}</small><details className="a2-segment-metadata"><summary>后台标签</summary><span>{segment.segment_type}</span>{output && <span>provider={output.provider_name || output.provider}</span>}{historicalAudio && <div className="a2-stale-audio"><strong>{locale === 'zh-CN' ? '旧音频，不参与混音' : 'Historical audio, excluded from mixing'}</strong><audio controls preload="none" src={`${assetUrl(historicalAudio)}?v=${encodeURIComponent(output?.tts_input_hash || output?.id || '')}`} /></div>}</details></div></div>
          <div className="a2-segment-actions">{needsShorter && <span title={tx('needsShorter')}><AlertTriangle size={16} /></span>}<span className={`a2-output-status ${segment.voice_status}`}>{voiceStatusLabel(segment.voice_status, locale)}</span>{fitLabel && <span className={`a2-output-status ${output?.duration_fit_status || ''}`}>{fitLabel}</span>}{currentAudio && <audio controls preload="none" src={`${assetUrl(currentAudio)}?v=${encodeURIComponent(output?.tts_input_hash || output?.id || '')}`} />}{!currentAudio && segment.voice_status !== 'omitted' && <button className="icon-button" title={tx('preview')} disabled={active || Boolean(busy) || !segment.tts_eligible} onClick={() => preview(segment.id, draft?.voice_id)}>{busy === `preview-${segment.id}` ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}</button>}{output && segment.voice_status !== 'omitted' && <button className="icon-button" title={tx('regenerate')} disabled={active || Boolean(busy) || !segment.tts_eligible} onClick={() => generate('selected', [segment.id])}><RotateCcw size={16} /></button>}</div>
          {segment.voice_status === 'text_changed' && <p className="a2-warning-copy">{locale === 'zh-CN' ? '译文已修改，需要重新生成。' : 'The translation changed and this line must be regenerated.'}</p>}
          {output?.duration_fit_status && <details className="a2-duration-fit-details"><summary>{locale === 'zh-CN' ? '时长适配详情' : 'Duration fit details'}</summary><p>{locale === 'zh-CN' ? '可用时长' : 'Usable'}: {(output.available_duration ?? segment.target_duration).toFixed(2)}s · {locale === 'zh-CN' ? '原始TTS' : 'Original TTS'}: {(output.original_tts_duration ?? output.generated_duration ?? 0).toFixed(2)}s · {locale === 'zh-CN' ? '最终TTS' : 'Final TTS'}: {(output.final_tts_duration ?? output.aligned_duration ?? 0).toFixed(2)}s · {locale === 'zh-CN' ? '重试' : 'Retries'}: {output.shortening_retry_count ?? 0}</p></details>}
          {Boolean(output?.tts_child_chunk_count) && <details className="a2-duration-fit-details"><summary>{locale === 'zh-CN' ? `自动拆分 ${output?.tts_child_chunk_count} 段` : `Automatically split into ${output?.tts_child_chunk_count} chunks`}</summary><div className="a2-chunk-status-list">{output?.tts_child_chunks?.map((chunk) => <p key={chunk.chunk_hash}><strong>{chunk.index + 1}/{output.tts_child_chunk_count}</strong> · {chunk.character_count} chars · <span className={`a2-output-status ${chunk.status}`}>{chunk.status}</span>{typeof chunk.duration === 'number' ? ` · ${chunk.duration.toFixed(2)}s` : ''}{chunk.error_message ? ` · ${chunk.error_message}` : ''}</p>)}</div></details>}
          {needsShorter && <div className="a2-duration-detail"><p className="a2-warning-copy">{locale === 'zh-CN' ? '自动适配未通过；其余句子会继续处理。' : 'Automatic fitting failed; other lines continue.'}</p><p>{tx('targetDuration')}: {segment.target_duration.toFixed(2)}s · {tx('availableDuration')}: {(segment.available_duration ?? segment.target_duration).toFixed(2)}s · {tx('generatedDuration')}: {(output?.generated_duration ?? 0).toFixed(2)}s · {tx('speedRatio')}: {(output?.speed_ratio ?? 0).toFixed(2)}x</p></div>}
          {requiresResolution && <details className="a2-resolution-actions"><summary>高级处理</summary><div className="a2-actions"><button className="secondary-button compact" disabled={active || Boolean(busy) || !segment.tts_eligible} onClick={() => generate('selected', [segment.id])}>重新生成</button><button className="secondary-button compact" title={canUseCurrent ? '' : '当前音频无法在安全时间边界内适配'} disabled={active || Boolean(busy) || !canUseCurrent} onClick={() => void resolveVoiceSegment(segment.id, 'use_current')}>使用当前音频</button><button className="quiet-button compact" disabled={active || Boolean(busy)} onClick={() => void resolveVoiceSegment(segment.id, 'omit')}>忽略该句</button></div></details>}
          {segment.voice_status === 'omitted' && <div className="a2-actions"><button className="secondary-button compact" disabled={active || Boolean(busy)} onClick={() => void resolveVoiceSegment(segment.id, 'restore')}>{locale === 'zh-CN' ? '恢复该句' : 'Restore line'}</button><span className="a2-warning-copy">{locale === 'zh-CN' ? '恢复后需生成当前版本配音；旧音频不会参与混音。' : 'Restoring requires current TTS; historical audio will not be mixed.'}</span></div>}
          {output?.error_message && <p className="a2-warning-copy">{output.error_code}: {output.error_message}</p>}
        </article>
      })}
    </div>}
    <div className="a2-actions a2-actions-footer"><button className="secondary-button" disabled={active || Boolean(busy) || !needsShorterCount} onClick={() => generate('failed')}><RotateCcw size={15} /> {tx('retryFailed')}</button><button className="ghost-button" disabled={active || Boolean(busy)} onClick={() => { if (window.confirm(tx('confirmClear'))) void run('clear', () => api(`/episodes/${episodeId}/tts-previews`, { method: 'DELETE' })) }}><Trash2 size={15} /> {tx('clearPreviews')}</button></div>

    {task && <div className="a2-task"><div><strong>{tx('task')} · {tx(`status_${task.status}`) || task.status}</strong><span>{task.completed_count}/{task.total_count}{task.status !== 'interrupted' && task.status !== 'failed' ? ` · ${task.progress}%` : ''}</span><button className="icon-button" title={tx('refreshStatus')} onClick={() => void load()}><RefreshCw size={14} /></button></div><progress max="100" value={task.progress} /><p>{tx('providerCalls')}: {state.summary.provider_calls} · {tx('cacheHits')}: {state.summary.cache_hits} · {tx('characterCost')}: {state.summary.character_cost} · {tx('staleCount')}: {state.summary.stale_count}</p>{task.error_message && <p className="a2-warning-copy">{task.error_code}: {task.error_message}</p>}<div className="a2-task-controls">{ACTIVE.has(task.status) && <button className="secondary-button compact" onClick={() => void taskAction('pause')}>{tx('pauseQueue')}</button>}{['paused', 'failed', 'interrupted'].includes(task.status) && <button className="primary-button compact" onClick={() => void taskAction('resume')}>{task.status === 'interrupted' ? tx('continueFailed') : tx('resumeQueue')}</button>}{ACTIVE.has(task.status) && <button className="quiet-button" onClick={() => void taskAction('cancel')}>{tx('cancelRemaining')}</button>}{['paused', 'failed'].includes(task.status) && <button className="secondary-button compact" onClick={() => void taskAction('reset-stuck')}>{tx('resetStuck')}</button>}</div></div>}
    </div>

    <div id={WORKFLOW_MODULE_IDS.mix} className="workflow-module-anchor">
    <div className="a2-subhead"><div><span className="section-label">{formalMixReady ? (locale === 'zh-CN' ? '正式完整混音' : 'Formal full mix') : tx('audioMix')}</span><p>{formalMixReady ? (locale === 'zh-CN' ? 'Demucs 背景声 + 当前英文配音时间线' : 'Demucs accompaniment + current English TTS timeline') : tx('mixMode')}</p></div><SlidersHorizontal size={18} /></div>
    <div className="a2-mix-grid">
      <MixControl label={tx('originalDialogue')} value={mix.dialogue_original_volume} min={0} max={1} step={0.05} onChange={(value) => { setMix({ ...mix, dialogue_original_volume: value }); setMixDirty(true) }} />
      <MixControl label={tx('originalElsewhere')} value={mix.non_dialogue_original_volume} min={0} max={1} step={0.05} onChange={(value) => { setMix({ ...mix, non_dialogue_original_volume: value }); setMixDirty(true) }} />
      <MixControl label={tx('dubVolume')} value={mix.dub_voice_volume} min={0} max={2} step={0.05} onChange={(value) => { setMix({ ...mix, dub_voice_volume: value }); setMixDirty(true) }} />
      <MixControl label={tx('fade')} value={mix.fade_ms} min={0} max={500} step={10} onChange={(value) => { setMix({ ...mix, fade_ms: value }); setMixDirty(true) }} />
    </div>
    <label className="a2-checkbox"><input type="checkbox" checked={mix.include_target_subtitles} onChange={(event) => { setMix({ ...mix, include_target_subtitles: event.target.checked }); setMixDirty(true) }} /> {tx('targetSubtitles')}</label>
    <div className="a2-actions"><button className="secondary-button" disabled={active || Boolean(busy)} onClick={saveMix}><Save size={15} /> {tx('saveMix')}</button><button className="primary-button" disabled={active || Boolean(busy) || !state.mix_ready} onClick={() => void createMixPreview()}><Play size={15} /> {tx('createPreview')}</button></div>
    {message && <div className="toast-line"><CheckCircle2 size={15} /> {message}</div>}
    <div className="a2-preview-output"><div className="a2-subhead"><div><span className="section-label">{tx('mixPreviewReady')}</span><p>{audioAssetMessage}{state.dubbed_output?.mixed_count !== undefined ? ` · ${tx('mixedCount').replace('{{mixed}}', String(state.dubbed_output.mixed_count)).replace('{{total}}', String(state.dubbed_output.eligible_count ?? selectedEligibleCount))}` : ''} {state.dubbed_output?.updated_at || state.dubbed_output?.created_at ? ` · ${state.dubbed_output.updated_at || state.dubbed_output.created_at}` : ''}</p></div></div>{formalMixReady && <p className="a2-cache-note">{locale === 'zh-CN' ? '背景音 Stem 已就绪' : 'Background stems ready'} · {state.dubbed_output?.actual_duration?.toFixed(3)}s · mix revision {state.dubbed_output?.mix_revision || state.mix_revision}{formalMixVoiceSummary ? ` · Edge TTS · ${formalMixVoiceSummary}` : ''}</p>}{latestPreview && <video ref={previewRef} key={latestPreview} autoPlay controls loop muted preload="metadata" src={latestPreview} />}{state.dubbed_output?.audio_url && <audio controls loop preload="none" src={`${assetUrl(state.dubbed_output.audio_url)}?v=${encodeURIComponent(state.dubbed_output.updated_at || state.dubbed_output.created_at || '')}`} />}</div>
    </div>
    {error && <A2Error error={error} technical={tx('technical')} />}
  </section>
}

function MixControl({ label, value, min, max, step, onChange }: { label: string; value: number; min: number; max: number; step: number; onChange: (value: number) => void }) {
  return <label><span>{label}</span><div><input type="range" value={value} min={min} max={max} step={step} onChange={(event) => onChange(Number(event.target.value))} /><input type="number" value={value} min={min} max={max} step={step} onChange={(event) => onChange(Number(event.target.value))} /></div></label>
}

function A2Error({ error, technical }: { error: { summary: string; details: string }; technical: string }) {
  return <div className="inline-error a2-error"><AlertTriangle size={17} /><div><strong>{error.summary}</strong><details><summary>{technical}</summary><pre>{error.details}</pre></details></div></div>
}
