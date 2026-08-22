import { classifyFetchFailure, classifyHttpFailure, invalidJsonResponse, prepareApiRequest, type ApiRequestInit } from './apiRequest'

const DEFAULT_API_BASE_URL = import.meta.env.DEV ? 'http://127.0.0.1:8001' : window.location.origin
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_URL || DEFAULT_API_BASE_URL).replace(/\/$/, '')
const API_URL = `${API_BASE_URL}/api`

const LOCAL_BACKEND_HOSTS = new Set(['127.0.0.1', 'localhost'])

/** Keep local upload assets same-origin so browser media/image fetches use Vite's proxy. */
export function normalizeBackendAssetUrl(value?: string | null): string {
  const raw = value?.trim() || ''
  if (!raw) return ''
  if (raw.startsWith('data:') || raw.startsWith('blob:')) return raw
  if (/^[A-Za-z]:[\\/]/.test(raw) || raw.startsWith('\\\\')) return ''
  if (/^\/(uploads|outputs|media)\//.test(raw) || raw.startsWith('/api/files/')) return raw
  if (/^(uploads|outputs|media)\//.test(raw)) return `/${raw}`

  try {
    const parsed = new URL(raw, 'http://127.0.0.1:5173')
    const isLocalBackend = (parsed.protocol === 'http:' || parsed.protocol === 'https:')
      && LOCAL_BACKEND_HOSTS.has(parsed.hostname)
      && parsed.port === '8001'
      && (/^\/(uploads|outputs|media)\//.test(parsed.pathname) || parsed.pathname.startsWith('/api/files/'))
    if (isLocalBackend) return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    // Leave unknown asset strings untouched for the caller to diagnose.
  }
  return raw
}

export async function api<T>(path: string, init?: ApiRequestInit): Promise<T> {
  const url = `${API_URL}${path}`
  const request = prepareApiRequest(init)
  const method = request?.method || 'GET'
  const projectId = path.match(/projects\/([^/]+)/)?.[1] || undefined
  const episodeId = path.match(/episodes\/([^/]+)/)?.[1] || undefined
  const characterId = path.match(/characters\/([^/]+)/)?.[1] || undefined
  let response: Response
  try {
    response = await fetch(url, request)
  } catch (error) {
    const failure = classifyFetchFailure(error, request?.signal)
    console.error('[API request failed before response]', { url, method, failureKind: failure.kind, project_id: projectId, episode_id: episodeId })
    failure.message = `${failure.message}\nAPI URL: ${url}\nHTTP method: ${method}\nfailure kind: ${failure.kind}\ncharacter_id: ${characterId || '-'}\nepisode_id: ${episodeId || '-'}\nproject_id: ${projectId || '-'}`
    throw failure
  }
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('auth:unauthorized'))
    const body = await response.text()
    const contentType = response.headers.get('content-type') || ''
    console.error('[API HTTP request failed]', { url, method, status: response.status, project_id: projectId, episode_id: episodeId })
    let detail = body
    try {
      const payload = JSON.parse(body) as { detail?: string | { code?: string; message?: string }; message?: string }
      if (typeof payload.detail === 'object' && payload.detail) {
        detail = [payload.detail.code, payload.detail.message].filter(Boolean).join(': ') || body
      } else {
        detail = payload.detail || payload.message || body
      }
    } catch {
      // Keep plain-text responses readable.
    }
    const failure = classifyHttpFailure(response.status, body, contentType)
    const safeDetail = failure.kind === 'origin_busy' ? '' : ` ${detail || 'Request failed'}`
    failure.message = `${failure.message}${safeDetail}\nAPI URL: ${url}\nHTTP method: ${method}\nstatus code: ${response.status}\ncharacter_id: ${characterId || '-'}\nepisode_id: ${episodeId || '-'}\nproject_id: ${projectId || '-'}`
    throw failure
  }
  const body = await response.text()
  try {
    return JSON.parse(body) as T
  } catch (error) {
    throw invalidJsonResponse(response.status, body, error)
  }
}

export async function uploadVideo(projectId: string, file: File) {
  const formData = new FormData()
  formData.append('file', file)
  return api<Project>(`/projects/${projectId}/upload`, { method: 'POST', body: formData })
}

export type ProjectStatus = 'Draft' | 'Uploaded' | 'Analyzing' | 'Character Review' | 'Ready' | 'Generating' | 'Completed' | 'Failed'
export type Project = {
  id: string; name: string; status: ProjectStatus; video_filename?: string; video_path?: string; video_duration?: number
  video_width?: number; video_height?: number; width?: number; height?: number; aspect_ratio?: number; orientation?: 'portrait' | 'landscape' | 'square'
  source_language?: string; target_language?: string; selected_style?: string; subtitle_enabled: boolean; lip_sync_enabled: boolean
  created_at: string; updated_at: string; characters: Character[]; scenes: Scene[]; settings: Settings
  processing_mode?: 'quick-transformation' | 'character-consistency' | 'batch-long-episode'
  active_episode_id?: string; use_character_library?: boolean; episodes?: Episode[]; voice_profiles?: VoiceProfile[]
  character_library?: { locked: Character[]; temporary: Character[]; background: Character[]; characters?: Character[] }
  defaults?: ProjectDefaults
}
export type Character = {
  id: string; project_id: string; name: string; gender: string; estimated_age: string; role: string; appearance_description: string
  hair_description: string; headwear_description: string; clothing_description: string; original_reference_image?: string; source_crop_path?: string; target_reference_path?: string
  front_view_image?: string; angle_view_image?: string; side_view_image?: string; full_body_image?: string; voice_id: string
  voice_settings: Record<string, string | number>; confirmed: boolean; character_code?: string; character_type?: 'Human' | 'Anime' | 'Comic' | '3D' | 'Creature' | 'Pet' | 'Animal' | 'Other'; primary_reference_type?: ReferenceField | null
  priority_level?: 'Locked' | 'Temporary' | 'Background'; identity_description?: string; face_description?: string
  body_description?: string; default_clothing_description?: string; voice_profile_id?: string; source?: string
  created_from_episode_id?: string; reference_assets?: ReferenceAsset[]; looks?: CharacterLook[]; entity_type?: 'Human' | 'Pet' | 'Animal' | 'Other'
  styled_front_view_image?: string; styled_angle_view_image?: string; styled_side_view_image?: string; styled_full_body_image?: string
  styled_asset_status?: string; confirmation_status?: string; styled_reference_assets?: ReferenceAsset[]
  source_reference_image?: string; main_reference_image?: string; confirmed_at?: string
}
export type Scene = { id: string; name: string; location: string; description: string; main_objects: string[]; reference_image?: string }
export type Settings = {
  project_id?: string; style_id: string; source_language: string; target_language: string; language_mode: string
  character_consistency: string; transformation_strength: string; scene_processing_mode: string; subtitle_enabled: boolean; lip_sync_enabled: boolean
  visual_scope?: string; character_appearance_policy?: string
}
export type Style = { id: string; name: string; description: string; accent: string; prompt: string[] }
export type Language = { id: string; name: string; type: string }
export type Voice = { id: string; name: string; base_voice: string; sample: string }
export type TaskStep = { step_index: number; name: string; status: string; progress: number; error_message?: string }
export type Task = { id: string; project_id: string; status: string; progress: number; current_step: string; error_message?: string; steps: TaskStep[] }
export type Result = { id?: string; task_id?: string; episode_id?: string; status?: string; mock?: boolean; project: Project; demo_label: string; original_video_url?: string; generated_video_url?: string; generated_at: string; comparison_note: string }
export type ReferenceField = 'original_reference_image' | 'front_view_image' | 'angle_view_image' | 'side_view_image' | 'full_body_image'
export type ReferenceAsset = { id?: string; asset_type: string; image?: string; is_primary?: boolean; field?: ReferenceField; source_type?: string; source_episode_id?: string; source_timestamp?: number }
export type CharacterLook = { id: string; character_id: string; name: string; description: string; reference_image?: string; clothing_description: string; hair_override: string; headwear_description: string; source_episode_id?: string; is_default: boolean }
export type EpisodeCandidate = { id: string; candidate_id?: string; episode_id: string; detected_name: string; entity_type: 'Human' | 'Pet' | 'Animal' | 'Other'; status: 'candidate' | 'retained' | 'matched' | 'temporary' | 'background' | 'ignored' | 'locked'; candidate_status?: string; character_id?: string; matched_character_id?: string; confidence: number; mock_confidence?: number; source_index: number; notes?: string; representative_frame?: string; representative_timestamp?: number; occurrence_frames?: string[]; occurrence_timestamps?: number[]; bounding_box?: { x?: number; y?: number; width?: number; height?: number } }
export type Episode = { id: string; project_id: string; episode_number: number; sort_order?: number; title: string; video_filename?: string; video_path?: string; video_url?: string | null; duration?: number; width?: number; height?: number; aspect_ratio?: number; orientation?: 'portrait' | 'landscape' | 'square'; thumbnail_path?: string; file_size?: number; upload_status?: string; upload_error?: string; analysis_status?: 'not_started' | 'processing' | 'completed' | 'warning' | 'failed'; analysis_error?: string; matched_count?: number; new_character_count?: number; pending_character_count?: number; candidate_count?: number; candidate_counts?: Record<string, number>; candidates?: EpisodeCandidate[]; conflict_count?: number; source_language?: string; status: string; character_handling: string; settings_override: Record<string, unknown>; created_at: string; updated_at: string; has_video?: boolean; matches?: EpisodeMatch[] }
export type EpisodeBatchItem = { id: string; job_id: string; episode_id?: string; filename?: string; item_order: number; status: 'queued' | 'processing' | 'uploading' | 'completed' | 'failed' | 'cancelled'; progress: number; error_message?: string }
export type EpisodeBatchJob = { id: string; project_id: string; job_type: 'upload' | 'analysis'; strategy: string; status: string; total_count: number; completed_count: number; failed_count: number; items: EpisodeBatchItem[]; created_at: string; updated_at: string }
export type EpisodeMatch = { id?: string; character_id?: string; character_code?: string; detected_name: string; match_status: string; confidence: number; use_project_asset: boolean }
export type VoiceProfile = { id: string; project_id: string; name: string; source_type: string; provider_id?: string; model_id?: string; external_voice_id?: string; uploaded_audio_path?: string; sample_url?: string; base_voice?: string; voice_age?: string; voice_style?: string; speech_speed: number; pitch: number; emotion_strength: number; status: string; bound_character?: { id: string; name: string }; created_at: string; updated_at: string }
export type ProjectDefaults = { project_id: string; processing_mode: string; style_id: string; source_language: string; target_language: string; language_mode: string; subtitle_enabled: boolean; lip_sync_enabled: boolean; character_consistency: string; transformation_strength: string; scene_processing_mode: string; visual_scope: string; character_appearance_policy: string; use_character_library: boolean }

export type XmaxShot = { id: string; batch_id: string; sequence: number; start_time: number; end_time: number; duration: number; source_path: string; source_url?: string; status: 'pending' | 'processing' | 'recording' | 'completed' | 'failed' | 'stopped'; output_path?: string; output_url?: string; output_duration?: number; file_size?: number; processed_seconds: number; error_code?: string; error_message?: string; character_ids?: string[]; reference_layout?: Array<{ character_id: string; position: string; order: number }>; reference_url?: string }
export type XmaxBatch = { id: string; project_id: string; episode_id: string; status: 'prepared' | 'starting' | 'pending' | 'running' | 'interrupted' | 'cancelled' | 'preparing' | 'splitting' | 'processing' | 'recording' | 'merging' | 'completed' | 'stopped' | 'failed'; reference_character_id?: string; prompt: string; total_shots: number; completed_shots: number; processed_seconds: number; estimated_credits: number; actual_credits?: number; output_path?: string; output_url?: string; output_duration?: number; output_file_size?: number; error_code?: string; error_message?: string; failed_stage?: string; session_id?: string; session_created_at?: string; started_at?: string; stop_requested_at?: string; stop_reason?: string; active_confirmation_source?: string; current_shot?: number; finished_at?: string; split_mode?: string; source_duration?: number; reused?: boolean; replaced_stale_batch_id?: string | null; session_id_exists?: boolean; shots: XmaxShot[]; created_at: string; updated_at: string; heartbeat_at?: string }
export type XmaxSingleShotTask = { task_id: string; attempt_id: string; project_id: string; episode_id: string; status: 'pending' | 'preparing' | 'running' | 'completed' | 'failed' | 'cancelled'; prompt: string; source_url?: string | null; source_duration: number; reference_url?: string | null; estimated_credits: number; billed_credits?: number | null; processed_seconds?: number | null; output_url?: string | null; output_duration?: number | null; file_size?: number | null; stop_reason?: string | null; error_code?: string | null; error_message?: string | null; created_at: string; completed_at?: string | null; idempotent?: boolean; persistence_diagnostics?: { remux_attempted: boolean; remux_succeeded: boolean; original_probe_error?: string | null; remux_probe_error?: string | null } }
export type XmaxSingleShotDiagnosticsPayload = {
  connectMs?: number | null
  referenceUploadMs?: number | null
  restartMs?: number | null
  firstOutputFrameMs?: number | null
  processedSeconds?: number
  stopReason?: string
  sessionId?: string
  events?: string[]
  recorderStartMode?: 'single-final-chunk'
  localBlobRetained?: boolean
  persistenceAttemptCount?: number
  remuxAttempted?: boolean
  remuxSucceeded?: boolean
  originalProbeError?: string | null
  remuxProbeError?: string | null
  [key: string]: unknown
}

export type XmaxThirtySecondSegment = {
  segment_id: string
  transform_unit_id?: string
  parent_shot_id?: string | null
  sequence: number
  start_time: number
  end_time: number
  duration: number
  content_duration?: number | null
  proxy_duration?: number | null
  proxy_preparation_status?: 'pending' | 'preparing' | 'ready' | 'failed' | string
  proxy_ready?: boolean
  pre_roll_duration?: number | null
  post_roll_duration?: number | null
  source_start?: number
  source_end?: number
  shot_type?: 'single_character' | 'multi_character' | 'no_character' | 'transition' | 'unknown' | string
  selected_character_id?: string | null
  mapping_status?: string | null
  reference_url?: string | null
  reference_preview_url?: string | null
  reference_filename?: string | null
  reference_type?: string | null
  preserve_original?: boolean
  scene_score?: number | null
  cut_method?: string | null
  risk_score?: number
  risk_level?: 'low' | 'medium' | 'high' | string
  first_frame_url?: string | null
  middle_frame_url?: string | null
  last_frame_url?: string | null
  timeline_status?: string | null
  timeline_error_ms?: number | null
  source_url: string
  processing_mode?: 'level1_direct' | 'level2_crop' | string
  preprocessed_source_url?: string | null
  original_source_sha256?: string | null
  preprocessed_source_sha256?: string | null
  crop?: { x: number; y: number; width: number; height: number } | null
  scale?: { width: number; height: number } | null
  preserve_current_unit_version?: boolean
  status: 'pending' | 'running' | 'local_ready' | 'uploading' | 'completed' | 'failed' | 'interrupted' | 'cancelled'
  execution_status?: 'pending' | 'running' | 'completed' | 'failed' | 'interrupted' | 'cancelled' | string
  quality_status?: 'review_required' | 'approved' | 'rejected_no_effect' | 'rejected_identity_drift' | 'rejected_timeline' | 'invalid_output' | 'not_applicable' | string
  quality_reason?: string | null
  quality_reviewed_at?: string | null
  quality_reviewed_by?: string | null
  retry_version?: number
  use_source_for_merge?: boolean
  preserve_source_by_user?: boolean
  output_url?: string | null
  output_duration?: number | null
  file_size?: number | null
  error_code?: string | null
  error_message?: string | null
  diagnostics_json?: string | null
}

export type XmaxThirtySecondTask = {
  task_id: string
  attempt_id: string
  project_id: string
  episode_id: string
  status: 'pending' | 'running' | 'local_ready' | 'uploading' | 'merge_pending' | 'merging' | 'completed' | 'failed' | 'merge_failed' | 'interrupted' | 'cancelled'
  action_state?: 'awaiting_user_action' | 'running' | 'ready_to_merge' | 'completed'
  prompt: string
  source_duration: number
  estimated_credits: number
  scene_threshold?: number | null
  scene_analysis_status?: string | null
  scene_analyzed_at?: string | null
  reference_url?: string | null
  final_output_url?: string | null
  result_valid: boolean
  result_classification: string
  final_duration?: number | null
  final_width?: number | null
  final_height?: number | null
  final_fps?: number | null
  final_has_audio?: boolean | null
  source_audio_restored?: boolean | null
  merged_at?: string | null
  file_size?: number | null
  error_code?: string | null
  error_message?: string | null
  created_at: string
  updated_at?: string | null
  completed_at?: string | null
  preparation_status: 'preparing' | 'ready' | 'failed'
  preparation_complete: boolean
  prepared_units: number
  preparation_total_units: number
  preparation_failed_units: number
  preparation_progress: number
  reused?: boolean
  existing_task_id?: string | null
  scene_plan?: XmaxThirtySecondScenePlan | null
  preflight_summary?: {
    transform_unit_count: number
    ai_transform_count: number
    source_fallback_count: number
    high_risk_count: number
    retry_count: number
    estimated_source_fallback_min: number
    estimated_source_fallback_max: number
  }
  segment_recovery?: {
    recovery_action: 'retry' | 'fallback_source' | 'awaiting_user_action'
    retry_allowed: boolean
    fallback_applied: boolean
    retry_version: number
  }
  segments: XmaxThirtySecondSegment[]
}
export type XmaxThirtySecondTaskSummary = {
  task_id: string
  attempt_id: string
  project_id: string
  episode_id: string
  status: XmaxThirtySecondTask['status']
  created_at: string
  updated_at?: string | null
  completed_at?: string | null
  merged_at?: string | null
  completed_shots: number
  merge_ready_shots?: number
  fallback_count?: number
  total_shots: number
  final_output_url?: string | null
  final_file_valid: boolean
  result_classification?: string
  real_output_count?: number
  explicit_source_count?: number
  automatic_fallback_count?: number
  final_duration?: number | null
  file_size?: number | null
  error_code?: string | null
  error_message?: string | null
  can_resume: boolean
  can_merge: boolean
  is_completed_result: boolean
  preparation_status?: 'preparing' | 'ready' | 'failed'
  preparation_complete?: boolean
  prepared_units?: number
  preparation_total_units?: number
  preparation_failed_units?: number
  preparation_progress?: number
}
export type XmaxThirtySecondTaskIndex = {
  episode_id: string
  active_candidates: XmaxThirtySecondTaskSummary[]
  completed_results: XmaxThirtySecondTaskSummary[]
  historical_tasks: XmaxThirtySecondTaskSummary[]
}
export type XmaxThirtySecondSceneShot = {
  sequence: number
  transform_unit_id?: string
  parent_shot_id?: string
  parent_sequence?: number
  source_start: number
  source_end: number
  target_duration: number
  scene_score: number
  cut_method: string
  risk_score?: number
  risk_level?: 'low' | 'medium' | 'high'
  risk_factors?: string[]
  transform_unit_count?: number
  transform_unit_ids?: string[]
  shot_type: 'single_character' | 'multi_character' | 'no_character' | 'transition' | 'unknown'
  selected_character_id?: string | null
  reference_face_confirmed?: boolean
  detected_people: string[]
  mapping_status: string
  preserve_original: boolean
  first_frame_url: string
  middle_frame_url: string
  last_frame_url: string
}
export type XmaxThirtySecondScenePlan = {
  episode_id: string
  project_id: string
  source_duration: number
  threshold: number
  persisted?: boolean
  cache_status?: 'created' | 'reused' | 'missing'
  base_shot_count?: number
  transform_unit_count?: number
  mappings_updated_at?: string
  active_task_id?: string | null
  synced_segment_ids?: string[]
  base_shots?: Array<{ source_start: number; source_end: number; scene_score: number; cut_method: string }>
  transform_units?: XmaxThirtySecondSceneShot[]
  candidate_cuts: Array<{ timestamp: number; scene_score: number; previous_frame_url: string; current_frame_url: string; next_frame_url: string }>
  shots: XmaxThirtySecondSceneShot[]
}
export type XmaxThirtySecondShotMapping = {
  shotType: 'single_character' | 'multi_character' | 'no_character' | 'transition' | 'unknown'
  selectedCharacterId?: string
  preserveSource?: boolean
  referenceFaceConfirmed?: boolean
  /** Compatibility-only input keys; the state adapter removes them before storage and serialization. */
  shot_type?: 'single_character' | 'multi_character' | 'no_character' | 'transition' | 'unknown'
  character_id?: string
  preserve_original?: boolean
  reference_confirmed?: boolean
}
export type XmaxThirtySecondPreflightShot = {
  sequence: number; shotType: XmaxThirtySecondShotMapping['shotType']; selectedCharacterId?: string | null; characterName?: string | null
  referenceFound: boolean; referenceReadable: boolean; referencePreviewUrl?: string | null; mappingValid: boolean; errorCode?: string | null; errorMessage?: string | null
  transformUnitId?: string | null; parentShotId?: string | null; riskScore?: number; riskLevel?: 'low' | 'medium' | 'high' | string
}
export type XmaxThirtySecondPreflight = {
  episodeId: string
  shotCount: number
  transformUnitCount: number
  aiTransformCount: number
  highRiskCount: number
  estimatedSourceFallbackMin: number
  estimatedSourceFallbackMax: number
  allValid: boolean
  shots: XmaxThirtySecondPreflightShot[]
}
export type XmaxAttempt = { id: string; episode_id: string; batch_id?: string | null; shot_id?: string | null; request_start_at: string; request_end_at?: string | null; session_id?: string | null; provider_request_method?: string | null; provider_request_url?: string | null; provider_http_status?: number | null; provider_response_body?: string | null; provider_error_name?: string | null; provider_error_code?: string | null; provider_error_message?: string | null; provider_error_stack_head?: string | null; classified_error_code?: string | null; classified_error_message?: string | null; processed_seconds: number; billed_credits?: number | null; estimated_credits: number; status: 'prepared' | 'starting' | 'running' | 'completed' | 'failed' | 'cancelled'; stop_reason?: string | null; page_hidden: boolean | number; tab_hidden: boolean | number; unload: boolean | number; output_stream_received: boolean | number; output_file_received: boolean | number; output_file_path?: string | null; final_input_stream_active?: boolean | number | null; final_input_video_track_count?: number | null; final_input_track_id?: string | null; final_input_track_ready_state?: string | null; final_input_track_enabled?: boolean | number | null; final_input_track_muted?: boolean | number | null; final_input_track_settings?: string | null; local_element_tag_name?: string | null; remote_element_tag_name?: string | null; canvas_draw_count_before_connect?: number | null; video_current_time_before_connect?: number | null; created_at: string; updated_at: string }

export type VisualProviderModel = { id: string; label: string; min_seconds?: number; max_seconds?: number; estimated_credits?: number | null; estimated_cost_usd?: number | null; video_to_video?: boolean; available_for_duration?: boolean }
export type VisualProviderStatus = { provider?: string; model?: string; configured: boolean; missing: string[]; max_test_seconds?: number; models?: VisualProviderModel[] }
export type VisualTransformationInput = { clip_url?: string; clip_path: string; start_time: number; end_time: number; duration: number; width: number; height: number; fps?: number; aspect_ratio: number; orientation: string; file_size: number }
export type VisualTransformationOutput = { local_output_url?: string; local_output_path?: string; output_duration?: number; width?: number; height?: number; aspect_ratio?: number; file_size?: number; output_hash?: string }
export type VisualTransformationSegment = { id: string; sequence: number; start_time: number; end_time: number; duration: number; status: string; error_code?: string; error_message?: string }
export type VisualTransformationTask = { id: string; project_id: string; episode_id: string; provider: string; model: string; provider_task_id?: string; start_time: number; end_time: number; duration: number; target_style: string; visual_scope: string; prompt_summary: string; prompt_summary_zh?: string; prompt_text: string; reference_character_ids: string[]; total_segments?: number; completed_segments?: number; skipped_segments?: number; segments?: VisualTransformationSegment[]; status: 'queued' | 'preparing' | 'submitting' | 'processing' | 'downloading' | 'completed' | 'failed' | 'cancelled' | 'timed_out'; progress?: number; estimated_credits?: number; estimated_cost_usd?: number; actual_credits?: number; actual_cost_usd?: number; error_code?: string; error_message?: string; started_at?: string; completed_at?: string; input?: VisualTransformationInput; output?: VisualTransformationOutput; quality_review?: Record<string, unknown> | null; provider_status?: VisualProviderStatus }
export type VisualTransformationState = { episode_id: string; project_id: string; project_mode: string; default_full_video?: boolean; video_duration?: number; video_width?: number; video_height?: number; video_aspect_ratio?: number; video_orientation?: string; video_url?: string; locked_characters: Array<{ id: string; name: string; character_code?: string; reference_image?: string }>; visual_scope: string; disabled_scopes: string[]; task?: VisualTransformationTask | null; provider: VisualProviderStatus }

export type ProviderStatus = { provider?: string; model?: string; configured: boolean; missing: string[] }
export type TranscriptionProviderOption = ProviderStatus & { id: 'tencent' | 'elevenlabs' | 'local_whisper_pyannote'; display_name: string }
export type MediaToolStatus = { available: boolean; ffmpeg: { available: boolean; path?: string; version?: string }; ffprobe: { available: boolean; path?: string; version?: string } }
export type AITask = { id: string; project_id: string; episode_id: string; task_type: string; provider?: string; model?: string; status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'queued' | 'disabled'; progress: number; stage?: string; error_code?: string; error_message?: string; stderr?: string; input_path?: string; output_path?: string; provider_request_id?: string; attempt_count?: number; usage?: Record<string, unknown>; started_at?: string; completed_at?: string; created_at: string; updated_at: string; heartbeat_at?: string }
export type SubtitleStream = { id: string; stream_index: number; codec_name?: string; language?: string; title?: string; disposition_default: number; metadata?: Record<string, unknown> }
export type SubtitleSource = { id: string; episode_id: string; source_type: 'embedded_soft_subtitle' | 'external_subtitle' | 'burned_in_subtitle' | 'speech_only' | 'unknown'; selected: boolean; priority: number; language?: string; file_path?: string; file_url?: string; file_format?: string; stream_count: number; metadata: Record<string, unknown>; manual_override: boolean; streams: SubtitleStream[] }
export type TranscriptDocument = { id: string; episode_id: string; source_id?: string; source_text_origin: string; source_language: string; source_priority?: 'speech_transcription' | 'subtitle_source' | 'side_by_side_review'; comparison_mode?: 'source_review' | 'side_by_side_review'; selected_source_mode: 'subtitle_primary' | 'speech_primary' | 'side_by_side'; revision: number; provider?: string; model?: string; status: string; detected_language?: string; transcript_text?: string; provider_request_id?: string; timestamp_source?: string; audio_duration?: number; speaker_count: number; segment_count: number; provider_metadata: Record<string, unknown>; user_edited: boolean; created_at: string; updated_at: string }
export type TranscriptSegment = { id: string; document_id: string; seq: number; start_ms: number; end_ms: number; source_text: string; source_text_origin: string; speaker: string; speaker_label?: string; segment_type: string; confidence?: number; timestamp_source?: string; literal_translation: string; localized_translation: string; final_translation: string; translation_status: string; subtitle_enabled: boolean; review_status: string; user_edited: boolean; translation_edited: boolean; stale_for_voice: boolean; reading_speed: number; reading_speed_warning: boolean }
export type TranslationDocument = { id: string; transcript_document_id: string; source_language: string; target_language: string; translation_style: string; provider?: string; model?: string; status: string; error_message?: string }
export type SubtitleStyle = { id: string; episode_id: string; render_mode: string; font_family: string; font_size: number; text_color: string; outline_color: string; outline_width: number; background_color: string; position: string; safe_margin_percent: number; max_lines: number; mask_height_percent: number; preview_stale?: boolean; preview_stale_reason?: string; font_fallback?: boolean }
export type SubtitleOutput = { id: string; output_type: string; file_path: string; file_url?: string; file_format: string; status: string; metadata: Record<string, unknown>; created_at: string }
export type TranscriptionSummary = { status: 'real' | 'disabled' | 'failed'; audio_duration?: number; detected_language?: string; segment_count: number; speaker_count: number; timestamp_source?: string }
export type TranscriptionSubtitleState = { episode_id: string; project_id: string; video_duration?: number; video_url?: string; sources: SubtitleSource[]; document?: TranscriptDocument; source_priority: 'speech_transcription' | 'subtitle_source' | 'side_by_side_review'; comparison_mode: 'source_review' | 'side_by_side_review'; active_source: { id: string; label: string }; subtitle_source_available: boolean; subtitle_source_reason?: string; segments: TranscriptSegment[]; translation?: TranslationDocument; style: SubtitleStyle; outputs: SubtitleOutput[]; latest_task?: AITask; media_tools: MediaToolStatus; transcription_provider: ProviderStatus; selected_transcription_provider: TranscriptionProviderOption['id']; transcription_providers: TranscriptionProviderOption[]; transcription_summary: TranscriptionSummary; translation_provider: ProviderStatus; tts_provider: ProviderStatus; render_modes: string[]; available_fonts?: { id: string; label: string }[]; a1_notice: string; batch_result?: { action: string; affected_count: number; exception_segment_ids: string[] } }

export type TencentVoiceEndpointProfile = { language_label: string; languages: string[]; sample_rates_hz: number[]; emotions: string[] }
export type TencentVoiceMetadata = {
  voice_type?: string
  catalog_version?: string
  recommended_scene?: string
  voice_tier?: string
  gender_or_role?: string
  supported_endpoints?: string[]
  endpoint_profiles?: Record<string, TencentVoiceEndpointProfile>
  text_to_voice_compatible?: boolean
  requires_billing_notice?: boolean
}
export type TTSVoice = { voice_id: string; provider: string; name: string; category?: string; labels: Record<string, string>; description?: string; preview_url?: string; metadata: TencentVoiceMetadata & Record<string, unknown>; fetched_at: string }
export type TtsProviderOption = ProviderStatus & { id: 'tencent' | 'elevenlabs' | 'edge'; display_name: string; output_format?: string; default_voice_configured?: boolean }
export type SpeakerVoiceBinding = { id: string; project_id: string; episode_id?: string; speaker_label: string; character_id?: string; provider: string; model: string; voice_id: string; voice_type?: string; voice_name?: string; catalog_version?: string; voice_known?: boolean; voice_resolution_source?: 'speaker_direct' | 'character' | 'voice_only_role' | 'language_default' | null; voice_language_compatible?: boolean; auto_rematched?: boolean; target_language?: string; is_project_default: boolean; is_episode_override: boolean }
export type DetectedSpeakerProfile = { speaker_id: string; source_provider: string; source_model: string; detected_language?: string; segment_count: number; total_duration_ms: number; first_appearance_ms: number; character_id?: string | null; tts_provider?: string | null; tts_model?: string | null; voice_id?: string | null; voice_name?: string | null; binding_status: 'bound' | 'unbound' }
export type TencentVoicePreview = { voice_id: string; voice_name: string; catalog_version: string; preview_text: string; audio_url: string; duration: number; cached: boolean; provider_calls: number; provider_request_id?: string | null }
export type TTSChildChunk = { chunk_id: string; index: number; parent_segment_id: string; chunk_hash: string; text: string; character_count: number; provider: 'tencent'; voice_type: string; speed_ratio: number; volume: number; status: 'pending' | 'ready' | 'failed'; duration?: number; provider_request_id?: string | null; character_cost?: number; cache_hit?: boolean; error_code?: string; error_message?: string }
export type VoiceSegmentOutput = { id: string; transcript_segment_id: string; output_kind: 'preview' | 'segment'; provider: string; provider_name?: string; model: string; voice_id: string; audio_path?: string; duration_ms?: number; provider_request_id?: string; character_cost?: number; target_language?: string; target_duration?: number; generated_duration?: number; aligned_duration?: number; speed_adjustment?: number; duration_warning: boolean; needs_shorter_text?: boolean; shorter_text?: string | null; regeneration_status?: string | null; original_duration?: number; available_duration?: number; speed_ratio?: number; duration_fit_status?: 'fit' | 'shortened_and_fit' | 'speed_adjusted_and_fit' | 'manual_review_required' | 'failed' | string; original_tts_duration?: number; final_tts_duration?: number; shortening_retry_count?: number; tts_child_chunk_count?: number; tts_child_chunks?: TTSChildChunk[]; status: string; raw_audio_url?: string; aligned_audio_url?: string; error_code?: string; error_message?: string; source_text_hash?: string; tts_input_hash?: string; usage: Record<string, unknown> }
export type DubbingVoiceStatus = 'ready' | 'missing_voice' | 'failed' | 'text_changed' | 'timing_exception' | 'omitted'
export type DubbingSegment = { id: string; transcript_segment_id: string; seq: number; start_ms: number; end_ms: number; speaker_label: string; segment_type: 'dialogue' | 'offscreen_dialogue' | 'narration' | 'inner_monologue' | string; source_text: string; final_translation: string; target_duration: number; available_duration?: number; voice_id?: string; tts_eligible: boolean; voice_status: DubbingVoiceStatus; mix_eligible?: boolean; mix_disabled_reason?: string | null; output?: VoiceSegmentOutput }
export type VoiceGenerationTask = { id: string; project_id: string; episode_id: string; task_type: string; status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | string; progress: number; current_segment_id?: string; total_count: number; completed_count: number; failed_count: number; error_code?: string; error_message?: string; control_requested?: string; last_heartbeat?: string; heartbeat_at?: string; segment_ids?: string[]; usage: { provider_calls?: number; cache_hits?: number; character_cost?: number; mixed_segments?: number; mixed_count?: number; eligible_count?: number; mix_duration?: number; preview_url?: string; audio_url?: string; output_path?: string; tts_only_url?: string; tts_only_path?: string; ducked_audio_path?: string; output_audio_path?: string; preview_video_path?: string; ducked_interval_count?: number; amix_inputs?: number; amix_normalize?: number }; started_at?: string; completed_at?: string }
export type AudioMixSettings = { episode_id: string; dialogue_original_volume: number; non_dialogue_original_volume: number; dub_voice_volume: number; fade_ms: number; include_target_subtitles: boolean }
export type DubbingMixManifest = { version: number; episode_id: string; mix_settings?: AudioMixSettings; segments: Array<{ transcript_segment_id: string; tts_input_hash: string | null; voice_output_id: string | null }> }
export type DubbedOutput = { id: string; status: string; mix_mode: string; target_language?: string; include_target_subtitles: boolean; actual_duration?: number; eligible_count?: number; mixed_count?: number; mix_revision?: string; mix_input_manifest?: DubbingMixManifest; audio_url?: string; preview_video_url?: string; created_at?: string; updated_at?: string }
export type AudioAssetsReadiness = { ready: boolean; can_auto_prepare: boolean; tts_ready: boolean; stems_ready: boolean; formal_mix_ready: boolean; dubbed_output_ready: boolean; quick_preview_ready: boolean; audio_asset_state: 'blocked' | 'preparing_background' | 'mix_required' | 'ready' | string; audio_revision?: string | null; subtitle_revision?: string | null; mix_settings_revision?: string | null; formal_mix_task_id?: string | null; formal_mix_source?: 'dubbing_preview' | 'audio_localization' | string | null; quick_preview_task_id?: string | null; blockers: Array<{ asset?: string; segment_id?: string | null; error_code?: string; message?: string }>; assets_to_prepare: Array<{ asset?: string; segment_id?: string | null; error_code?: string; message?: string }> }
export type DubbingState = { episode_id: string; project_id: string; processing_mode: string; target_language: string; target_locale: string; tts_provider: ProviderStatus & { output_format?: string; default_voice_configured?: boolean }; selected_tts_provider: TtsProviderOption['id']; tts_providers: TtsProviderOption[]; status: string; voices: TTSVoice[]; bindings: SpeakerVoiceBinding[]; speaker_profiles: DetectedSpeakerProfile[]; characters: Array<Pick<Character, 'id' | 'character_code' | 'name' | 'gender' | 'priority_level' | 'confirmed' | 'voice_id' | 'voice_profile_id'>>; speakers: string[]; segments: DubbingSegment[]; tts_unfinished_segment_ids: string[]; mix_required_segment_ids: string[]; mix_eligible_segment_ids: string[]; mix_missing_segment_ids: string[]; mix_input_manifest: DubbingMixManifest; mix_revision: string; mix_ready: boolean; mix_ineligible_reasons: Record<string, string>; mix_settings: AudioMixSettings; latest_task?: VoiceGenerationTask; summary: { total_count: number; completed_count: number; failed_count: number; duration_warning_count: number; needs_shorter_count: number; qualified_count: number; pending_count?: number; running_count?: number; interrupted_count?: number; transcript_count: number; eligible_count: number; speaker_count: number; dialogue_segment_count: number; voice_status_counts: Record<DubbingVoiceStatus, number>; all_speakers_have_voice: boolean; missing_voice_speakers: string[]; missing_translation_count: number; stale_count: number; orphan_count: number; provider_calls: number; cache_hits: number; character_cost: number }; dubbed_output?: DubbedOutput; audio_assets: AudioAssetsReadiness; capabilities: { tts: string; mixing: string; voice_separation: string; lip_sync: string } }
export type AudioLocalizationState = {
  episode_id: string
  project_id: string
  source: { kind: 'xmax_final' | 'episode_source'; task_id?: string | null; video_url: string; duration: number }
  settings: {
    target_language: string
    voice: string
    audio_mode: 'demucs_dialogue_replacement' | string
    original_audio_volume: number
    voice_over_volume: number
    keep_original_audio: boolean
    subtitles_enabled: boolean
    hide_original_subtitles: boolean
    original_subtitle_region_start_percent: number
    original_subtitle_region_height_percent: number
    subtitle_position: 'low' | 'standard' | 'high'
  }
  assets: { asr_ready: boolean; translation_ready: boolean; tts_ready: boolean; transcript_count: number; translated_count: number; tts_count: number; tts_timeline_url?: string | null; duration_fit?: { all_fit: boolean; segment_count: number; fit_count: number; blocked_segment_ids: string[]; segments: Array<{ segment_id: string; seq: number; fit_status: string; slot_duration: number; usable_duration: number; original_tts_duration?: number | null; final_tts_duration?: number | null; retry_count?: number }> } }
  interfaces: { asr: string; translation: string; tts: string; execution_mode: string }
  ready: boolean
  latest_task?: AudioLocalizationTask | null
  latest_output?: { output_url?: string | null; output_path?: string | null; size_bytes?: number | null; created_at?: string | null; file_name?: string | null; version?: string | null } | null
  provider_calls: number
  credits: number
}
export type AudioLocalizationTask = {
  id: string
  project_id: string
  episode_id: string
  source_task_id?: string | null
  mode: 'demucs_dialogue_replacement' | string
  status: 'pending' | 'preparing_assets' | 'rendering_subtitles' | 'separating_audio' | 'building_tts_timeline' | 'mixing_audio' | 'muxing_final' | 'completed' | 'failed' | 'interrupted' | string
  progress: number
  progress_message: string
  settings: AudioLocalizationState['settings']
  separation_output_dir?: string | null
  output_path?: string | null
  output_url?: string | null
  output_duration?: number | null
  output_width?: number | null
  output_height?: number | null
  output_size?: number | null
  error_code?: string | null
  error_message?: string | null
  created_at: string
  updated_at: string
  started_at?: string | null
  completed_at?: string | null
  finished_at?: string | null
  output_version?: string | null
  duration_fit?: { all_fit?: boolean; segment_count?: number; fit_count?: number; blocked_segment_ids?: string[]; translation_provider_calls?: number; tts_provider_calls?: number; provider_calls?: number }
  provider_calls: number
  xmax_sessions: number
  translation_provider_calls: number
  tts_provider_calls: number
  credits: number
}
export type AudioLocalizationResult = {
  episode_id: string
  project_id: string
  status: 'completed'
  source_task_id?: string | null
  source_video_url: string
  target_language: string
  voice: string
  settings: AudioLocalizationState['settings']
  asset_counts: { transcript_segments: number; translated_segments: number; tts_segments: number; subtitle_cues: number }
  audio: { source_had_audio: boolean; original_audio_path: string; tts_timeline_path: string; mixed_audio_path: string; amix_inputs: number; normalize: number }
  output_url: string
  output_path: string
  output_version: string
  duration: number
  width: number
  height: number
  has_audio: boolean
  size_bytes: number
  subtitles_rendered: boolean
  original_subtitles_hidden: boolean
  subtitle_mask?: { method: string; start_percent: number; height_percent: number; start_y: number; end_y: number; feather_pixels: number } | null
  created_at: string
  provider_calls: number
  credits: number
}

export type OverseasEpisodeStage =
  | 'preparing'
  | 'analyzing_shots'
  | 'transforming_shots'
  | 'merging_visual'
  | 'separating_audio'
  | 'adapting_dialogue'
  | 'rendering_subtitles'
  | 'muxing_final'
  | 'completed'
  | 'failed'

export type OverseasEpisodeTask = {
  task_id: string
  project_id: string
  episode_id: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  stage: OverseasEpisodeStage
  progress: number
  progress_message: string
  visual_task_id?: string | null
  audio_task_id?: string | null
  transform_completed: number
  transform_total: number
  fallback_count: number
  visual_status_counts?: {
    transformed: number
    failed: number
    awaiting_user_action: number
    source_by_user: number
  }
  final_output_url?: string | null
  result_valid: boolean
  result_classification: string
  current_visual_revision_id?: string | null
  visual_ready: boolean
  can_merge_final: boolean
  final_assets_ready: boolean
  can_prepare_final_audio: boolean
  audio_asset_state: 'blocked' | 'preparing_background' | 'mix_required' | 'ready' | string
  final_merge_blockers: Array<{
    asset?: string
    segment_id?: string | null
    error_code?: string
    message?: string
  }>
  audio_assets_to_prepare: Array<{
    asset?: string
    segment_id?: string | null
    error_code?: string
    message?: string
  }>
  audio_revision?: string | null
  subtitle_revision?: string | null
  background_audio_revision?: string | null
  mix_settings_revision?: string | null
  error_code?: string | null
  error_message?: string | null
  created_at: string
  updated_at: string
  completed_at?: string | null
  requires_client_visual_execution: boolean
  diagnostics: Record<string, unknown>
  provider_calls_this_run: number
  xmax_sessions_this_run: number
}

export type OverseasVisualProgress = {
  visual_task_id: string
  stage: Extract<OverseasEpisodeStage, 'analyzing_shots' | 'transforming_shots' | 'merging_visual'>
  transform_completed: number
  transform_total: number
  fallback_count: number
  provider_calls?: number
  xmax_sessions?: number
}

export const assetUrl = (path?: string) => path ? `${API_BASE_URL}${path}` : ''

export function createXmaxSingleShotTask(episodeId: string, prompt: string, referenceFile: File) {
  const form = new FormData()
  form.append('prompt', prompt)
  form.append('reference_image', referenceFile)
  return api<XmaxSingleShotTask>(`/episodes/${episodeId}/xmax-single-shot/tasks`, { method: 'POST', body: form })
}

export function uploadXmaxSingleShotOutput(taskId: string, outputBlob: Blob, diagnostics: XmaxSingleShotDiagnosticsPayload) {
  const form = new FormData()
  const fileName = `xmax-single-shot-${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 17)}.webm`
  form.append('output_file', new File([outputBlob], fileName, { type: outputBlob.type || 'video/webm' }))
  form.append('diagnostics_json', JSON.stringify(diagnostics))
  return api<XmaxSingleShotTask>(`/xmax-single-shot/${taskId}/output`, { method: 'POST', body: form })
}

export function getLatestXmaxSingleShotTask(episodeId: string) {
  return api<XmaxSingleShotTask | null>(`/episodes/${episodeId}/xmax-single-shot/latest`)
}

export function cancelXmaxSingleShotTask(taskId: string) {
  return api<XmaxSingleShotTask>(`/xmax-single-shot/${taskId}/cancel`, { method: 'POST' })
}

export function getXmaxThirtySecondScenePlan(episodeId: string, threshold = 0.35, createIfMissing = true) {
  return api<XmaxThirtySecondScenePlan>(`/episodes/${episodeId}/xmax-thirty-second/scene-plan?threshold=${threshold}&create_if_missing=${createIfMissing ? 'true' : 'false'}`)
}

export function createXmaxThirtySecondTask(episodeId: string, prompt: string, shotMappings: Record<string, XmaxThirtySecondShotMapping>, referenceFile?: File) {
  const form = new FormData()
  form.append('prompt', prompt)
  form.append('shot_mappings_json', JSON.stringify(serializeXmaxThirtySecondMappings(shotMappings)))
  if (referenceFile) form.append('reference_image', referenceFile)
  return api<XmaxThirtySecondTask>(`/episodes/${episodeId}/xmax-thirty-second/tasks`, { method: 'POST', body: form })
}

export function resumeXmaxThirtySecondPreparation(episodeId: string, taskId: string) {
  return api<XmaxThirtySecondTask>(
    `/episodes/${encodeURIComponent(episodeId)}/xmax-thirty-second/tasks/${encodeURIComponent(taskId)}/prepare`,
    { method: 'POST' },
  )
}

function serializeXmaxThirtySecondMappings(shotMappings: Record<string, XmaxThirtySecondShotMapping>) {
  return Object.entries(shotMappings).map(([sequence, mapping]) => ({
    sequence: Number(sequence),
    mapping_scope: 'transform_unit',
    shot_type: mapping.shotType,
    selected_character_id: mapping.selectedCharacterId || null,
    reference_face_confirmed: mapping.referenceFaceConfirmed === true,
    preserve_source: mapping.preserveSource === true,
  }))
}

export function preflightXmaxThirtySecondTask(episodeId: string, shotMappings: Record<string, XmaxThirtySecondShotMapping>) {
  const form = new FormData()
  form.append('shot_mappings_json', JSON.stringify(serializeXmaxThirtySecondMappings(shotMappings)))
  type WirePreflight = {
    episode_id: string
    shot_count: number
    transform_unit_count: number
    ai_transform_count: number
    high_risk_count: number
    estimated_source_fallback_min: number
    estimated_source_fallback_max: number
    all_valid: boolean
    shots: Array<{ sequence: number; transform_unit_id?: string | null; parent_shot_id?: string | null; shot_type: XmaxThirtySecondShotMapping['shotType']; selected_character_id?: string | null; character_name?: string | null; reference_found: boolean; reference_readable: boolean; reference_preview_url?: string | null; mapping_valid: boolean; risk_score?: number; risk_level?: 'low' | 'medium' | 'high'; error_code?: string | null; error_message?: string | null }>
  }
  return api<WirePreflight>(`/episodes/${episodeId}/xmax-thirty-second/preflight`, { method: 'POST', body: form }).then((payload) => ({
    episodeId: payload.episode_id,
    shotCount: payload.shot_count,
    transformUnitCount: payload.transform_unit_count,
    aiTransformCount: payload.ai_transform_count,
    highRiskCount: payload.high_risk_count,
    estimatedSourceFallbackMin: payload.estimated_source_fallback_min,
    estimatedSourceFallbackMax: payload.estimated_source_fallback_max,
    allValid: payload.all_valid,
    shots: payload.shots.map((shot) => ({
      sequence: shot.sequence,
      transformUnitId: shot.transform_unit_id,
      parentShotId: shot.parent_shot_id,
      shotType: shot.shot_type,
      selectedCharacterId: shot.selected_character_id,
      characterName: shot.character_name,
      referenceFound: shot.reference_found,
      referenceReadable: shot.reference_readable,
      referencePreviewUrl: shot.reference_preview_url,
      mappingValid: shot.mapping_valid,
      riskScore: shot.risk_score,
      riskLevel: shot.risk_level,
      errorCode: shot.error_code,
      errorMessage: shot.error_message,
    })),
  }))
}

export function saveXmaxThirtySecondSceneMappings(episodeId: string, shotMappings: Record<string, XmaxThirtySecondShotMapping>) {
  const form = new FormData()
  form.append('shot_mappings_json', JSON.stringify(serializeXmaxThirtySecondMappings(shotMappings)))
  return api<XmaxThirtySecondScenePlan>(`/episodes/${episodeId}/xmax-thirty-second/scene-mappings`, { method: 'PUT', body: form })
}

export type XmaxThirtySecondBatchMappingResponse = {
  episode_id: string
  selected_character_id?: string | null
  reference_face_confirmed: boolean
  preserve_source: boolean
  sequences: number[]
  provider_call_started: boolean
  mappings: Array<{ sequence: number; shot_type: XmaxThirtySecondShotMapping['shotType']; selected_character_id?: string | null; reference_face_confirmed: boolean; preserve_source: boolean }>
}

export function batchBindXmaxThirtySecondMappings(episodeId: string, sequences: number[], selectedCharacterId: string, preserveSource = false) {
  const form = new FormData()
  form.append('sequences', JSON.stringify(sequences))
  form.append('selected_character_id', selectedCharacterId)
  form.append('reference_face_confirmed', 'true')
  form.append('preserve_source', String(preserveSource))
  return api<XmaxThirtySecondBatchMappingResponse>(`/episodes/${episodeId}/xmax-thirty-second/mappings/batch`, { method: 'POST', body: form })
}

export type XmaxLocalPersistenceProbe = {
  probe_id: string
  status: 'raw_output_persisted' | 'normalized_output_ready' | 'normalization_failed'
  attempt_type: 'local_persistence_probe'
  provider_call_started: false
  temporary_key_created: false
  session_created: false
  credit_change: 0
  runtime: {
    feature: string
    module_loaded_at: string
    module_source_path: string
    module_sha256: string
  }
  raw_output: {
    status: 'persisted'
    path: string
    url: string
    sha256: string
    size: number
    mtime_ns: number
    unchanged_after_normalization?: boolean
  }
  normalization: {
    status: 'pending' | 'ready' | 'failed'
    path?: string
    url?: string
    sha256?: string
    size?: number
    diagnostics_path?: string
    diagnostics_url?: string
    error_code?: string
  }
  manifest_path: string
  manifest_url: string
}

export type XmaxLocalPersistenceProbeOptions = {
  probeId: string
  forceNormalizationFailure?: boolean
}

export function uploadXmaxThirtySecondSegment(
  taskId: string,
  segmentId: string,
  outputBlob: Blob,
  fileName: string,
  diagnostics: Record<string, unknown> | undefined,
  browserAttemptId: string | undefined,
  localProbe: XmaxLocalPersistenceProbeOptions,
): Promise<XmaxLocalPersistenceProbe>
export function uploadXmaxThirtySecondSegment(
  taskId: string,
  segmentId: string,
  outputBlob: Blob,
  fileName: string,
  diagnostics?: Record<string, unknown>,
  browserAttemptId?: string,
): Promise<XmaxThirtySecondTask>
export function uploadXmaxThirtySecondSegment(
  taskId: string,
  segmentId: string,
  outputBlob: Blob,
  fileName: string,
  diagnostics?: Record<string, unknown>,
  browserAttemptId?: string,
  localProbe?: XmaxLocalPersistenceProbeOptions,
) {
  const form = new FormData()
  form.append('output_file', new File([outputBlob], fileName, { type: outputBlob.type || 'video/webm' }))
  if (diagnostics) form.append('diagnostics_json', JSON.stringify(diagnostics))
  if (browserAttemptId) form.append('browser_attempt_id', browserAttemptId)
  if (localProbe) {
    form.append('local_persistence_probe_id', localProbe.probeId)
    form.append('local_persistence_probe_force_failure', String(Boolean(localProbe.forceNormalizationFailure)))
  }
  return api<XmaxThirtySecondTask | XmaxLocalPersistenceProbe>(`/xmax-thirty-second/${taskId}/segments/${segmentId}/output`, { method: 'POST', body: form })
}

export function getLatestXmaxThirtySecondTask(episodeId: string) {
  return api<XmaxThirtySecondTask | null>(`/episodes/${episodeId}/xmax-thirty-second/latest`)
}

export function getXmaxThirtySecondTasks(episodeId: string) {
  return api<XmaxThirtySecondTaskIndex>(`/episodes/${episodeId}/xmax-thirty-second/tasks`)
}

export function getXmaxThirtySecondTask(taskId: string, episodeId?: string) {
  return api<XmaxThirtySecondTask>(`/xmax-thirty-second/${encodeURIComponent(taskId)}${episodeId ? `?episode_id=${encodeURIComponent(episodeId)}` : ''}`)
}

export function interruptXmaxThirtySecondTask(taskId: string) {
  return api<XmaxThirtySecondTask>(`/xmax-thirty-second/${taskId}/interrupt`, { method: 'POST' })
}

export function interruptXmaxThirtySecondTaskOnPageExit(taskId: string) {
  const url = `${API_URL}/xmax-thirty-second/${encodeURIComponent(taskId)}/interrupt`
  if (typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function') {
    return navigator.sendBeacon(url)
  }
  void fetch(url, { method: 'POST', keepalive: true }).catch(() => undefined)
  return false
}

export function heartbeatXmaxThirtySecondTask(taskId: string) {
  return api<XmaxThirtySecondTask>(`/xmax-thirty-second/${taskId}/heartbeat`, { method: 'POST' })
}

export function startXmaxThirtySecondSegment(taskId: string, segmentId: string) {
  return api<XmaxThirtySecondTask>(`/xmax-thirty-second/${taskId}/segments/${segmentId}/browser-start`, { method: 'POST' })
}

export function failXmaxThirtySecondSegment(
  taskId: string,
  segmentId: string,
  errorCode: string,
  errorMessage: string,
  diagnostics?: Record<string, unknown>,
) {
  const form = new FormData()
  form.append('error_code', errorCode)
  form.append('error_message', errorMessage)
  if (diagnostics) form.append('diagnostics_json', JSON.stringify(diagnostics))
  return api<XmaxThirtySecondTask>(`/xmax-thirty-second/${taskId}/segments/${segmentId}/failed`, { method: 'POST', body: form })
}

export function reviewXmaxThirtySecondSegment(taskId: string, segmentId: string, qualityStatus: NonNullable<XmaxThirtySecondSegment['quality_status']>, qualityReason = '', browserAttemptId = '') {
  const form = new FormData()
  form.append('quality_status', qualityStatus)
  if (qualityReason) form.append('quality_reason', qualityReason)
  if (browserAttemptId) form.append('browser_attempt_id', browserAttemptId)
  return api<XmaxThirtySecondTask>(`/xmax-thirty-second/${taskId}/segments/${segmentId}/quality`, { method: 'POST', body: form })
}

export function setXmaxThirtySecondMergeSource(taskId: string, segmentId: string, useSourceForMerge: boolean) {
  const form = new FormData()
  form.append('use_source_for_merge', String(useSourceForMerge))
  return api<XmaxThirtySecondTask>(`/xmax-thirty-second/${taskId}/segments/${segmentId}/merge-source`, { method: 'POST', body: form })
}

export type XmaxThirtySecondSegmentRetryPlan = {
  task_id: string
  segment_id: string
  sequence: number
  retry_version: number
  execution_status: string
  quality_status: string
  selected_character_id?: string | null
  reference_path?: string | null
  reference_url?: string | null
  reference_preview_url?: string | null
  reference_filename?: string | null
  reference_type?: string | null
  automatic?: boolean
  provider_call_started: boolean
  message: string
}

export function prepareXmaxThirtySecondSegmentRetry(taskId: string, segmentId: string, automatic = false, explicitRedraw = false) {
  const form = new FormData()
  form.append('automatic', String(automatic))
  form.append('explicit_redraw', String(explicitRedraw))
  return api<XmaxThirtySecondSegmentRetryPlan>(`/xmax-thirty-second/${taskId}/segments/${segmentId}/retry`, { method: 'POST', body: form })
}

export function mergeXmaxThirtySecondTask(taskId: string) {
  return api<XmaxThirtySecondTask>(`/xmax-thirty-second/${taskId}/merge`, { method: 'POST' })
}

export function cancelXmaxThirtySecondTask(taskId: string) {
  return api<XmaxThirtySecondTask>(`/xmax-thirty-second/${taskId}/cancel`, { method: 'POST' })
}

export type XmaxTemporaryKeyRecord = {
  id: string
  user_id: string
  username?: string
  project_id: string
  episode_id: string
  unit_id: string
  attempt_id: string
  provider_uid?: string | null
  key_last4?: string | null
  points_limit: number
  consumed_points: number
  remaining_points: number
  released_points?: number
  expire_timestamp?: string | null
  provider_status?: string | null
  local_status: 'preparing' | 'active' | 'session_started' | 'pending_settlement' | 'settled' | 'settlement_conflict' | 'cancelled' | 'expired' | 'failed' | 'orphaned'
  session_uid?: string | null
  freeze_ledger_id?: string | null
  consume_ledger_id?: string | null
  release_ledger_id?: string | null
  issued_at?: string | null
  inactivated_at?: string | null
  settled_at?: string | null
  last_error_code?: string | null
  last_error_message?: string | null
  created_at: string
  updated_at: string
}

export type XmaxTemporaryKeyGrant = {
  internal_temporary_key_id: string
  temporaryApiKey: string
  expireTimestamp: string
  pointsLimit: number
  idempotent: boolean
}

export function prepareXmaxTemporaryKey(payload: {
  project_id: string
  episode_id: string
  unit_id: string
  attempt_id: string
  requested_points_limit: number
  expire_seconds?: number
  idempotency_key: string
}) {
  return api<XmaxTemporaryKeyGrant>('/xmax/temporary-keys/prepare', { method: 'POST', body: payload })
}

export function getXmaxTemporaryKeyStatus(temporaryKeyId: string) {
  return api<XmaxTemporaryKeyRecord>(`/xmax/temporary-keys/${encodeURIComponent(temporaryKeyId)}`)
}

export function bindXmaxTemporaryKeySession(temporaryKeyId: string, sessionUid: string) {
  return api<XmaxTemporaryKeyRecord>(`/xmax/temporary-keys/${encodeURIComponent(temporaryKeyId)}/bind-session`, {
    method: 'POST', body: { session_uid: sessionUid },
  })
}

export function settleXmaxTemporaryKey(temporaryKeyId: string) {
  return api<XmaxTemporaryKeyRecord>(`/xmax/temporary-keys/${encodeURIComponent(temporaryKeyId)}/settle`, { method: 'POST' })
}

export function abortXmaxTemporaryKey(temporaryKeyId: string) {
  return api<XmaxTemporaryKeyRecord>(`/xmax/temporary-keys/${encodeURIComponent(temporaryKeyId)}/abort`, { method: 'POST' })
}

export type XmaxBrowserAttempt = {
  id: string
  project_id: string
  episode_id: string
  task_id: string
  unit_id: string
  source_shot_ids: string[]
  character_id: string
  character_asset_version: string
  reference_url: string
  reference_sha256: string
  prompt_template_version: string
  prompt_hash: string
  model: string
  playback: { playbackRate?: number }
  temporary_key_record_id: string
  session_uid?: string | null
  output_id?: string | null
  output_url?: string | null
  output_duration?: number | null
  output_size?: number | null
  output_mime_type?: string | null
  output_sha256?: string | null
  output_saved: boolean
  raw_output_persisted: boolean
  raw_output_url?: string | null
  raw_output_sha256?: string | null
  raw_output_size?: number | null
  normalization_status?: 'pending' | 'ready' | 'failed' | null
  normalization_error_code?: string | null
  normalization_diagnostics_url?: string | null
  processing_mode?: 'level1_direct' | 'level2_crop' | string
  input_snapshot?: {
    original_source_url?: string | null
    original_source_sha256?: string | null
    selected_source_url?: string | null
    selected_source_sha256?: string | null
    preprocessed_source_url?: string | null
    preprocessed_source_sha256?: string | null
    crop?: { x: number; y: number; width: number; height: number } | null
    scale?: { width: number; height: number } | null
    preserve_current_unit_version?: boolean
  }
  routing_evaluation?: Record<string, unknown> | null
  status: 'prepared' | 'session_bound' | 'output_saved' | 'persistence_failed' | 'failed' | 'aborted' | 'settlement_pending' | 'settled'
  settlement_status: 'not_started' | 'pending' | 'settled' | 'conflict'
  consumed_points?: number | null
  released_points?: number | null
  last_error_code?: string | null
  last_error_message?: string | null
  failure_diagnostics?: {
    errorName: string
    errorCode?: string | null
    message: string
    phase: 'temporary_key' | 'reference_upload' | 'session_connect' | 'remote_stream'
    sanitizedStack?: string | null
  } | null
  temporary_key?: {
    id: string
    points_limit: number
    consumed_points: number
    remaining_points: number
    local_status: string
    provider_status?: string | null
    session_uid?: string | null
    last_error_code?: string | null
    last_error_message?: string | null
  }
  created_at: string
  updated_at: string
}

export type XmaxBrowserAttemptGrant = {
  attempt: XmaxBrowserAttempt
  temporaryApiKey: string
  expireTimestamp: string
  pointsLimit: number
  idempotent: boolean
  runtime: {
    model: 'x2.0' | string
    playback: { playbackRate: number }
    prompt: string
    refImageUrl: string
    characterAssetVersion: string
    processingMode: 'level1_direct' | 'level2_crop' | string
    sourceUrl: string
    sourceSha256: string
    sourceDuration: number
    contentDuration: number
    preRollDuration: number
    postRollDuration: number
    crop?: { x: number; y: number; width: number; height: number } | null
    scale?: { width: number; height: number } | null
    preserveCurrentUnitVersion: boolean
  }
}

export function prepareXmaxBrowserAttempt(payload: {
  task_id: string
  unit_id: string
  requested_points_limit: number
  expire_seconds?: number
  idempotency_key: string
}) {
  return api<XmaxBrowserAttemptGrant>('/xmax/browser-attempts/prepare', { method: 'POST', body: payload })
}

export type XmaxLevel2PreparedInput = {
  task_id: string
  unit_id: string
  original_source_url: string
  original_source_sha256: string
  source_width: number
  source_height: number
  preprocessed_source_url: string
  preprocessed_source_sha256: string
  crop: { x: number; y: number; width: number; height: number }
  scale: { width: number; height: number }
  source_duration: number
  content_duration: number
  decode_valid: boolean
}

export function prepareXmaxLevel2Input(payload: {
  task_id: string
  unit_id: string
  crop: { x: number; y: number; width: number; height: number }
  scale: { width: number; height: number }
}) {
  return api<XmaxLevel2PreparedInput>('/xmax/browser-attempts/level2/prepare', { method: 'POST', body: payload })
}

export function confirmXmaxLevel2Input(prepared: XmaxLevel2PreparedInput) {
  return api<{ route: Record<string, unknown>; provider_calls: 0; xmax_sessions: 0; credits_consumed: 0 }>(
    '/xmax/browser-attempts/level2/confirm',
    {
      method: 'POST',
      body: {
        task_id: prepared.task_id,
        unit_id: prepared.unit_id,
        crop: prepared.crop,
        scale: prepared.scale,
        preprocessed_source_sha256: prepared.preprocessed_source_sha256,
      },
    },
  )
}

export function confirmXmaxLevel1Input(taskId: string, unitId: string) {
  return api<{ processing_mode: 'level1_direct'; provider_calls: 0; xmax_sessions: 0; credits_consumed: 0 }>(
    '/xmax/browser-attempts/level1/confirm',
    { method: 'POST', body: { task_id: taskId, unit_id: unitId } },
  )
}

export function getXmaxBrowserAttempt(attemptId: string) {
  return api<XmaxBrowserAttempt>(`/xmax/browser-attempts/${encodeURIComponent(attemptId)}`)
}

export function bindXmaxBrowserAttemptSession(attemptId: string, sessionUid: string) {
  return api<XmaxBrowserAttempt>(`/xmax/browser-attempts/${encodeURIComponent(attemptId)}/bind-session`, {
    method: 'POST', body: { session_uid: sessionUid },
  })
}

export function failXmaxBrowserAttempt(
  attemptId: string,
  errorCode: string,
  errorMessage: string,
  persistenceFailed = false,
  diagnostics?: Record<string, unknown>,
) {
  return api<XmaxBrowserAttempt>(`/xmax/browser-attempts/${encodeURIComponent(attemptId)}/failed`, {
    method: 'POST',
    body: { error_code: errorCode, error_message: errorMessage, persistence_failed: persistenceFailed, diagnostics },
  })
}

export function finalizeXmaxBrowserAttempt(attemptId: string, keepalive = false) {
  return api<XmaxBrowserAttempt>(`/xmax/browser-attempts/${encodeURIComponent(attemptId)}/finalize`, {
    method: 'POST', keepalive,
  })
}

export function recoverXmaxBrowserAttemptQuality(attemptId: string, expectedSha256: string, sessionUid: string) {
  return api<{ attempt: XmaxBrowserAttempt; recovery: Record<string, unknown> }>(`/xmax/browser-attempts/${encodeURIComponent(attemptId)}/recover-quality`, {
    method: 'POST',
    body: {
      expected_sha256: expectedSha256,
      session_uid: sessionUid,
      quality_status: 'approved',
      quality_reason: 'Automated output preflight passed in the one-click workflow.',
    },
  })
}

export type FormalClosureAttempt = {
  id: string
  unit_id: string
  status: string
  settlement_status: string
  processing_mode: 'level1_direct' | 'level2_crop' | string
  output_url?: string | null
  output_duration?: number | null
  output_sha256?: string | null
  consumed_points?: number | null
  quality_status?: string | null
  raw_output_persisted: boolean
  normalization_status?: string | null
  normalization_error_code?: string | null
  output_saved: boolean
  playable: boolean
  created_at: string
}

export type FormalClosureUnit = {
  unit_id: string
  sequence: number
  start_time: number
  end_time: number
  duration: number
  status: string
  execution_status?: string | null
  quality_status?: string | null
  processing_mode: 'level1_direct' | 'level2_crop' | string
  source_url: string
  crop_preview_url?: string | null
  preprocessed_source_sha256?: string | null
  crop?: { x: number; y: number; width: number; height: number } | null
  scale?: { width: number; height: number } | null
  attempts: FormalClosureAttempt[]
  selection?: {
    selected_source_type: 'generated_attempt' | 'original'
    selected_attempt_id?: string | null
    selected_at: string
  } | null
}

export type FormalFinalOutput = {
  id: string
  episode_id: string
  task_id: string
  status: 'rendering' | 'ready' | 'failed'
  output_url?: string | null
  output_sha256?: string | null
  file_size?: number | null
  duration?: number | null
  width?: number | null
  height?: number | null
  fps?: number | null
  video_codec?: string | null
  audio_codec?: string | null
  audio_revision?: string | null
  subtitle_revision?: string | null
  error_code?: string | null
  error_message?: string | null
  created_at: string
  completed_at?: string | null
  unit_selection_snapshot: Array<Record<string, unknown>>
}

export type FormalClosureWorkspace = {
  project: { id: string; name: string }
  episode: { id: string; title: string; duration?: number | null }
  task_id: string
  units: FormalClosureUnit[]
  selection_complete: boolean
  final_outputs: FormalFinalOutput[]
  latest_final_output?: FormalFinalOutput | null
}

export function getFormalClosureWorkspace(episodeId: string, taskId?: string) {
  const query = taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''
  return api<FormalClosureWorkspace>(`/episodes/${encodeURIComponent(episodeId)}/formal-closure${query}`)
}

export function formalFinalOutputDownloadUrl(outputId: string) {
  return `/api/final-outputs/${encodeURIComponent(outputId)}/download`
}

export function selectFormalClosureUnit(
  episodeId: string,
  unitId: string,
  selectedSourceType: 'generated_attempt' | 'original',
  selectedAttemptId?: string,
) {
  return api<FormalClosureWorkspace>(
    `/episodes/${encodeURIComponent(episodeId)}/formal-closure/units/${encodeURIComponent(unitId)}/selection`,
    {
      method: 'PUT',
      body: {
        selected_source_type: selectedSourceType,
        selected_attempt_id: selectedSourceType === 'generated_attempt' ? selectedAttemptId : null,
      },
    },
  )
}

export function createFormalFinalOutput(episodeId: string, taskId: string) {
  return api<FormalFinalOutput>(`/episodes/${encodeURIComponent(episodeId)}/formal-closure/final-outputs`, {
    method: 'POST', body: { task_id: taskId },
  })
}

export function listAdminXmaxTemporaryKeys(query = '') {
  return api<{ items: XmaxTemporaryKeyRecord[]; total: number; page: number; page_size: number }>(`/admin/xmax/temporary-keys${query}`)
}

export function refreshAdminXmaxTemporaryKey(temporaryKeyId: string) {
  return api<XmaxTemporaryKeyRecord>(`/admin/xmax/temporary-keys/${encodeURIComponent(temporaryKeyId)}/refresh-status`, { method: 'POST' })
}

export function inactivateAdminXmaxTemporaryKey(temporaryKeyId: string) {
  return api<XmaxTemporaryKeyRecord>(`/admin/xmax/temporary-keys/${encodeURIComponent(temporaryKeyId)}/inactivate`, { method: 'POST' })
}

export function reconcileAdminXmaxTemporaryKey(temporaryKeyId: string) {
  return api<XmaxTemporaryKeyRecord>(`/admin/xmax/temporary-keys/${encodeURIComponent(temporaryKeyId)}/reconcile`, { method: 'POST' })
}

export function getLatestOverseasEpisodeTask(episodeId: string) {
  return api<OverseasEpisodeTask | null>(`/episodes/${episodeId}/overseas-version/latest`)
}

export function getOverseasEpisodeTask(taskId: string) {
  return api<OverseasEpisodeTask>(`/overseas-version/${encodeURIComponent(taskId)}`)
}

export function createOverseasEpisodeTask(episodeId: string) {
  return api<OverseasEpisodeTask>(`/episodes/${episodeId}/overseas-version`, { method: 'POST' })
}

export function updateOverseasEpisodeVisualProgress(taskId: string, progress: OverseasVisualProgress) {
  return api<OverseasEpisodeTask>(`/overseas-version/${encodeURIComponent(taskId)}/visual-progress`, {
    method: 'POST',
    body: progress,
  })
}

export function completeOverseasEpisodeVisual(taskId: string, visualTaskId: string) {
  return api<OverseasEpisodeTask>(`/overseas-version/${encodeURIComponent(taskId)}/visual-complete`, {
    method: 'POST',
    body: { visual_task_id: visualTaskId },
  })
}

export function failOverseasEpisodeTask(taskId: string, errorCode: string, errorMessage: string, failedStage?: string) {
  return api<OverseasEpisodeTask>(`/overseas-version/${encodeURIComponent(taskId)}/failed`, {
    method: 'POST',
    body: { error_code: errorCode, error_message: errorMessage, failed_stage: failedStage },
  })
}
