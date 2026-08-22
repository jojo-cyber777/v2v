import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, ArrowRight, ArrowUp, Check, ChevronDown, ChevronRight, Clapperboard, CloudUpload, Download, Film, FolderOpen, Gauge, Languages, Library, ListVideo, LoaderCircle, Mic2, MoreHorizontal, Pause, Play, Plus, RefreshCw, RotateCcw, Save, Settings2, SlidersHorizontal, Sparkles, Trash2, Upload, UploadCloud, UserRound, Users, Volume2, WandSparkles, X } from 'lucide-react'
import { api, assetUrl, Character, CharacterLook, Episode, EpisodeBatchItem, EpisodeBatchJob, EpisodeMatch, formalFinalOutputDownloadUrl, getFormalClosureWorkspace, getLatestOverseasEpisodeTask, getXmaxThirtySecondScenePlan, getXmaxThirtySecondTask, getXmaxThirtySecondTasks, Language, preflightXmaxThirtySecondTask, Project, ProjectDefaults, ReferenceField, Result, Settings, Style, Task, TaskStep, Voice, VoiceProfile, VisualTransformationState, VisualTransformationTask, XmaxBatch, type FormalClosureWorkspace, type FormalFinalOutput, type OverseasEpisodeTask, type XmaxThirtySecondScenePlan, type XmaxThirtySecondTask, uploadVideo } from './api'
import { catalogLabels, roundTwoText, styleDescriptions, targetLanguageOptions, useI18n as useI18nHook } from './i18n'
import TranscriptionSubtitlesPanel from './TranscriptionSubtitlesPanel'
import DubbingMixPanel from './DubbingMixPanel'
import CurrentEffectPreview from './CurrentEffectPreview'
import AudioLocalizationPanel from './AudioLocalizationPanel'
import LocalizationWorkflowSummary from './LocalizationWorkflowSummary'
import TransformUnitPlanSummary from './TransformUnitPlanSummary'
import { getXmaxResult, setXmaxResult, xmaxResultKey, XmaxRealtimeAdapter, type XmaxRealtimeResult, type XmaxRealtimeStatus } from './lib/providers/xmax/XmaxRealtimeAdapter'
import XmaxShotPipelinePanel from './lib/providers/xmax/XmaxShotPipelinePanel'
import OneClickOverseasPanel from './lib/providers/xmax/OneClickOverseasPanel'
import { sceneReviewMappingsFromPlan, sceneReviewMappingsReady, sceneReviewUnits, XmaxSceneReviewEditor } from './lib/providers/xmax/XmaxSceneReview'
import { episodeBoundCharacters, validEpisodeTargetCharacters } from './lib/episodeCharacters'
import XmaxOfficialBaselineDemo from './dev/XmaxOfficialBaselineDemo'
import P4RawFirstPersistenceProbe from './dev/P4RawFirstPersistenceProbe'
import { useAuth } from './AuthContext'
import { AccountControls, AdminPage, InviteRegistrationPage, LoginPage } from './AuthPages'
import { CreditsPage, RechargePage } from './BillingPages'
import { browserXmaxCredentialConfigured, XMAX_TEMP_KEY_REQUIRED } from './lib/providers/xmax/xmaxBrowserCredential'
import { isEpisodeUploadComplete } from './apiRequest'

const useI18n = useI18nHook

const steps = [
  { id: 'upload', label: 'Assets & Character', icon: CloudUpload },
  { id: 'generate', label: 'Generate Overseas Version', icon: Sparkles },
  { id: 'result', label: 'Result', icon: Play },
]
const languageName = (id: string | undefined, languages: Language[]) => languages.find((item) => item.id === id)?.name || id || 'Not set'
const styleName = (id: string | undefined, styles: Style[]) => styles.find((item) => item.id === id)?.name || id || 'Not set'
const stepLabel = (id: string, t: ReturnType<typeof useI18n>['t']) => t(`steps.${id}` as Parameters<typeof t>[0])
const workflowStepLabel = (id: string, locale: 'zh-CN' | 'en') => ({
  upload: locale === 'zh-CN' ? '素材与角色' : 'Assets & Character',
  generate: locale === 'zh-CN' ? '生成海外版' : 'Generate Overseas Version',
  result: locale === 'zh-CN' ? '生成结果' : 'Result',
}[id] || id)
const catalogLabel = (id: string | undefined, locale: 'zh-CN' | 'en') => id ? catalogLabels[locale][id] || id : ''
const flowRoute = (projectId: string, step: string, episodeId?: string | null) => `/project/${projectId}/${step}${episodeId ? `?episode_id=${encodeURIComponent(episodeId)}` : ''}`
const queryEpisodeId = () => new URLSearchParams(window.location.search).get('episode_id') || undefined

function LanguageToggle() {
  const { locale, setLocale, t } = useI18n()
  return <div className="language-toggle" aria-label="Language"><button className={locale === 'zh-CN' ? 'active' : ''} onClick={() => setLocale('zh-CN')}>{t('common.languageChinese')}</button><button className={locale === 'en' ? 'active' : ''} onClick={() => setLocale('en')}>{t('common.languageEnglish')}</button></div>
}

function DemoNotice({ children }: { children: React.ReactNode }) { return <div className="demo-notice"><Sparkles size={15} /><span>{children}</span></div> }

type ProcessingMode = 'quick-transformation' | 'character-consistency' | 'batch-long-episode'
function ProcessingModePicker({ value, onChange, compact = false }: { value: ProcessingMode; onChange: (value: ProcessingMode) => void; compact?: boolean }) {
  const { locale } = useI18n()
  const options: Array<[ProcessingMode, string, string, string]> = [['quick-transformation', 'quick', 'Zap', roundTwoText(locale, 'quickCopy')], ['character-consistency', 'consistency', 'Users', roundTwoText(locale, 'consistencyCopy')], ['batch-long-episode', 'batch', 'Soon', roundTwoText(locale, 'batchCopy')]]
  return <section className={`mode-picker ${compact ? 'compact-mode-picker' : ''}`}><div className="section-label">{roundTwoText(locale, 'modeTitle')}</div><div className="mode-picker-grid">{options.map(([mode, key, badge, copy]) => <button key={mode} className={`mode-option ${value === mode ? 'selected' : ''} ${mode === 'batch-long-episode' ? 'disabled' : ''}`} disabled={mode === 'batch-long-episode'} onClick={() => onChange(mode)}><div className="mode-option-head"><strong>{roundTwoText(locale, key)}</strong><span>{badge}</span></div><p>{copy}</p></button>)}</div>{!compact && <p className="mode-notice">{roundTwoText(locale, 'modeNotice')}</p>}</section>
}

function App() {
  const { user, loading } = useAuth()
  if (loading) return <div className="app-loading"><LoaderCircle className="spin" size={22} /> 正在恢复登录状态...</div>
  if (!user) return <Routes><Route path="/login" element={<LoginPage />} /><Route path="/register" element={<InviteRegistrationPage />} /><Route path="*" element={<Navigate to="/login" replace />} /></Routes>
  if (import.meta.env.DEV && new URLSearchParams(window.location.search).get('p4_raw_first_probe') === '1') {
    return <P4RawFirstPersistenceProbe />
  }
  if (import.meta.env.DEV && new URLSearchParams(window.location.search).get('xmax_official_demo') === '1') {
    return <XmaxOfficialBaselineDemo />
  }
  return <><Routes><Route path="/login" element={<Navigate to="/" replace />} /><Route path="/register" element={<Navigate to="/" replace />} /><Route path="/admin" element={<AdminPage />} /><Route path="/credits" element={<CreditsPage />} /><Route path="/recharge" element={<RechargePage />} /><Route path="/" element={<Dashboard />} /><Route path="/project/:projectId/*" element={<ProjectWorkspace />} /><Route path="*" element={<Navigate to="/" replace />} /></Routes><BackToTopButton /></>
}

function BackToTopButton() {
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const update = () => setVisible(window.scrollY > 480)
    update()
    window.addEventListener('scroll', update, { passive: true })
    return () => window.removeEventListener('scroll', update)
  }, [])
  if (!visible) return null
  return <button className="back-to-top" title="返回顶部" aria-label="返回顶部" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}><ArrowUp size={19} /></button>
}

function Dashboard() {
  const navigate = useNavigate()
  const { t } = useI18n()
  const [projects, setProjects] = useState<Project[]>([])
  const [showNew, setShowNew] = useState(false)
  const [name, setName] = useState('')
  const [processingMode, setProcessingMode] = useState<'quick-transformation' | 'character-consistency' | 'batch-long-episode'>('quick-transformation')
  const [editingProject, setEditingProject] = useState<Project | null>(null)
  const [deletingProject, setDeletingProject] = useState<Project | null>(null)
  const [actionError, setActionError] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => { api<Project[]>('/projects').then(setProjects).catch((e) => setError(e.message)).finally(() => setLoading(false)) }, [])
  async function createProject() {
    if (!name.trim()) return
    try {
      const project = await api<Project>('/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim(), processing_mode: processingMode }) })
      navigate(`/project/${project.id}/upload`)
    } catch (e) {
      setActionError((e as Error).message)
    }
  }
  async function renameProject() {
    if (!editingProject || !name.trim()) return
    try {
      const updated = await api<Project>(`/projects/${editingProject.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }) })
      setProjects((items) => items.map((item) => item.id === updated.id ? updated : item))
      setEditingProject(null)
    } catch (e) {
      setActionError((e as Error).message)
    }
  }
  async function deleteProject() {
    if (!deletingProject) return
    try {
      await api(`/projects/${deletingProject.id}`, { method: 'DELETE' })
      setProjects((items) => items.filter((item) => item.id !== deletingProject.id))
      setDeletingProject(null)
    } catch (e) {
      setActionError((e as Error).message)
    }
  }
  return <div className="app-shell dashboard-shell">
    <header className="topbar"><Link to="/" className="brand"><span className="brand-mark"><Clapperboard size={17} /></span><span>{t('brand.studio')}</span></Link><div className="topbar-right"><LanguageToggle /><AccountControls /></div></header>
    <main className="dashboard-content">
      <DemoNotice>{t('dashboard.demoNotice')}</DemoNotice>
      <section className="dashboard-hero"><div><p className="eyebrow"><Sparkles size={14} /> {t('dashboard.eyebrow')}</p><h1>{t('dashboard.title')}<br /><span>{t('dashboard.titleAccent')}</span></h1><p className="hero-copy">{t('dashboard.copy')}</p><button className="primary-button" onClick={() => setShowNew(true)}><Plus size={17} /> {t('dashboard.newProject')}</button></div><div className="hero-showcase"><div className="showcase-glow" /><MockFrame label={t('dashboard.eyebrow')} variant="source" /><div className="showcase-arrow"><ArrowRight size={18} /></div><MockFrame label={t('result.demo')} variant="result" /></div></section>
      <section className="section-heading"><div><p className="eyebrow">{t('dashboard.titleZh')}</p><h2>{t('dashboard.recentProjects')}</h2></div><button className="quiet-button" onClick={() => setShowNew(true)}><Plus size={16} /> {t('dashboard.newProject')}</button></section>
      {error && <InlineError message={error} />}
      {loading ? <div className="loading-line"><LoaderCircle className="spin" size={17} /> Loading workspace...</div> : projects.length === 0 ? <EmptyProjects onCreate={() => setShowNew(true)} /> : <div className="project-grid">{projects.map((project) => <ProjectCardWithActions key={project.id} project={project} onOpen={() => navigate(`/project/${project.id}/${project.status === 'Completed' ? 'result' : 'upload'}`)} onRename={() => { setName(project.name); setActionError(''); setEditingProject(project) }} onDelete={() => { setActionError(''); setDeletingProject(project) }} />)}</div>}
    </main>
    {showNew && <Modal title={t('dashboard.createModal')} onClose={() => { setShowNew(false); setActionError('') }}><label className="field-label">{t('dashboard.projectName')}<input autoFocus value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && createProject()} placeholder={t('dashboard.namePlaceholder')} /></label><ProcessingModePicker value={processingMode} onChange={setProcessingMode} />{actionError && <InlineError message={actionError} />}<div className="modal-actions"><button className="quiet-button" onClick={() => setShowNew(false)}>{t('common.cancel')}</button><button className="primary-button" onClick={createProject}><ArrowRight size={16} /> {t('common.continue')}</button></div></Modal>}
    {editingProject && <Modal title={t('dashboard.renameModal')} onClose={() => setEditingProject(null)}><label className="field-label">{t('dashboard.projectName')}<input autoFocus value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && renameProject()} /></label>{actionError && <InlineError message={actionError} />}<div className="modal-actions"><button className="quiet-button" onClick={() => setEditingProject(null)}>{t('common.cancel')}</button><button className="primary-button" onClick={renameProject}><Save size={16} /> {t('common.save')}</button></div></Modal>}
    {deletingProject && <Modal title={t('dashboard.deleteModal')} onClose={() => setDeletingProject(null)}><p className="modal-copy">{t('dashboard.deleteCopy')}</p>{actionError && <InlineError message={actionError} />}<div className="modal-actions"><button className="quiet-button" onClick={() => setDeletingProject(null)}>{t('common.cancel')}</button><button className="danger-button" onClick={deleteProject}><Trash2 size={16} /> {t('common.delete')}</button></div></Modal>}
  </div>
}

function ProjectCard({ project, onOpen, onRename, onDelete }: { project: Project; onOpen: () => void; onRename: () => void; onDelete: () => void }) {
  return <button className="project-card" onClick={onOpen}><div className="project-poster"><MockFrame label={project.video_filename ? 'UPLOADED VIDEO' : 'READY TO START'} variant={project.selected_style === 'japanese-anime' ? 'anime' : 'source'} /></div><div className="project-card-body"><div className="card-row"><h3>{project.name}</h3><StatusBadge status={project.status} /></div><p>{project.video_filename || 'No source video yet'}</p><div className="card-meta"><span>{project.video_duration ? `${project.video_duration}s` : '—'}</span><span>Updated {formatDate(project.updated_at)}</span></div></div></button>
}

function EmptyProjects({ onCreate }: { onCreate: () => void }) {
  const { t } = useI18n()
  return <div className="empty-state"><div className="empty-icon"><Film size={25} /></div><h3>{t('dashboard.emptyTitle')}</h3><p>{t('dashboard.emptyCopy')}</p><button className="primary-button" onClick={onCreate}><Plus size={17} /> {t('dashboard.createProject')}</button></div>
}

function ProjectCardWithActions({ project, onOpen, onRename, onDelete }: { project: Project; onOpen: () => void; onRename: () => void; onDelete: () => void }) {
  const { t, formatDuration, formatDate: formatLocalizedDate } = useI18n()
  return <article className="project-card"><button className="project-card-main" onClick={onOpen}><div className="project-poster"><MockFrame label={project.video_filename ? t('dashboard.uploadedVideo') : t('dashboard.readyToStart')} variant={project.selected_style === 'japanese-anime' ? 'anime' : 'source'} /></div><div className="project-card-body"><div className="card-row"><h3>{project.name}</h3><StatusBadge status={project.status} /></div><p>{project.video_filename || t('upload.start')}</p><div className="card-meta"><span>{project.video_duration ? formatDuration(project.video_duration) : t('dashboard.noDuration')}</span><span>{t('dashboard.updated', { date: formatLocalizedDate(project.updated_at) })}</span></div></div></button><div className="project-card-actions"><button className="icon-button" title={t('common.rename')} onClick={onRename}><Save size={15} /></button><button className="icon-button danger-icon" title={t('common.delete')} onClick={onDelete}><Trash2 size={15} /></button></div></article>
}

type VideoMetadata = { width: number; height: number; aspect_ratio: number; orientation: 'portrait' | 'landscape' | 'square'; duration: number }

function readVideoMetadata(video: HTMLVideoElement): VideoMetadata | null {
  if (!video.videoWidth || !video.videoHeight) return null
  const aspectRatio = video.videoWidth / video.videoHeight
  return {
    width: video.videoWidth,
    height: video.videoHeight,
    aspect_ratio: Number(aspectRatio.toFixed(6)),
    orientation: aspectRatio > 1.03 ? 'landscape' : aspectRatio < 0.97 ? 'portrait' : 'square',
    duration: Number.isFinite(video.duration) ? Number(video.duration.toFixed(3)) : 0,
  }
}

async function persistVideoMetadata(project: Project, metadata: VideoMetadata, onChange: (project: Project) => void) {
  const width = project.video_width ?? project.width
  const height = project.video_height ?? project.height
  const ratio = project.aspect_ratio
  const durationMatches = typeof project.video_duration === 'number' && Math.abs(project.video_duration - metadata.duration) < 0.05
  if (width === metadata.width && height === metadata.height && ratio === metadata.aspect_ratio && project.orientation === metadata.orientation && durationMatches) return
  try {
    const updated = await api<Project>(`/projects/${project.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ video_duration: metadata.duration, video_width: metadata.width, video_height: metadata.height, aspect_ratio: metadata.aspect_ratio, orientation: metadata.orientation }),
    })
    onChange(updated)
  } catch {
    // Metadata is best-effort for older local projects; playback still uses the browser's intrinsic dimensions.
  }
}

function ResponsiveVideo({ src, project, onChange }: { src: string; project: Project; onChange: (project: Project) => void }) {
  const [metadata, setMetadata] = useState<VideoMetadata | null>(null)
  const savedKey = useRef('')
  function handleMetadata(event: React.SyntheticEvent<HTMLVideoElement>) {
    const next = readVideoMetadata(event.currentTarget)
    if (!next) return
    setMetadata(next)
    const key = `${next.width}x${next.height}:${next.duration}`
    if (savedKey.current === key) return
    savedKey.current = key
    void persistVideoMetadata(project, next, onChange)
  }
  return <div className={`responsive-video-shell orientation-${metadata?.orientation || project.orientation || 'unknown'}`}>
    <video className="responsive-video" controls preload="metadata" playsInline src={src} onLoadedMetadata={handleMetadata} />
  </div>
}

function videoUrl(project: Project) {
  if (!project.video_filename) return ''
  const suffix = project.video_filename.toLowerCase().endsWith('.mov') ? '.mov' : '.mp4'
  return assetUrl(`/uploads/${project.id}${suffix}`)
}

function formatDuration(value?: number) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Reading duration…'
  const totalSeconds = Math.max(0, Math.round(value))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = String(totalSeconds % 60).padStart(2, '0')
  return `${minutes}:${seconds}`
}

function formatTimestamp(value: number) {
  const total = Math.max(0, Number(value) || 0)
  const minutes = Math.floor(total / 60)
  const seconds = total - minutes * 60
  const secondLabel = Number.isInteger(seconds) ? String(seconds).padStart(2, '0') : seconds.toFixed(1).padStart(4, '0')
  return `${String(minutes).padStart(2, '0')}:${secondLabel}`
}

function ProjectWorkspace() {
  const { projectId } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const i18n = useI18nHook()
  const { t, locale } = i18n
  const useI18n = () => i18n
  const episodeId = new URLSearchParams(location.search).get('episode_id') || undefined
  const [project, setProject] = useState<Project | null>(null)
  const [styles, setStyles] = useState<Style[]>([])
  const [languages, setLanguages] = useState<Language[]>([])
  const [voices, setVoices] = useState<Voice[]>([])
  const [error, setError] = useState('')
  useEffect(() => { if (!projectId) return; Promise.all([api<Project>(`/projects/${projectId}`), api<Style[]>('/styles'), api<Language[]>('/languages'), api<Voice[]>('/voices')]).then(([p, s, l, v]) => { setProject(p); setStyles(s); setLanguages(l); setVoices(v) }).catch((e) => setError(e.message)) }, [projectId])
  useEffect(() => { if (!project?.active_episode_id || episodeId) return; const route = location.pathname.split('/').pop() || 'upload'; if (steps.some((step) => step.id === route)) navigate(flowRoute(project.id, route, project.active_episode_id), { replace: true }) }, [project?.active_episode_id, episodeId, location.pathname])
  useEffect(() => {
    if (!project) return
    const route = location.pathname.split('/').pop() || 'upload'
    const replacement = route === 'analyze' || route === 'characters' ? 'upload' : route === 'settings' ? 'generate' : null
    if (replacement) navigate(flowRoute(project.id, replacement, episodeId || project.active_episode_id), { replace: true })
  }, [project?.id, project?.active_episode_id, episodeId, location.pathname])
  if (!projectId) return <Navigate to="/" replace />
  if (!project) return <div className="loading-screen"><LoaderCircle className="spin" size={20} /> Loading project...</div>
  const rawActive = location.pathname.split('/').pop() || 'upload'
  const active = rawActive === 'analyze' || rawActive === 'characters' ? 'upload' : rawActive === 'settings' ? 'generate' : rawActive
  const activeIndex = Math.max(0, steps.findIndex((step) => step.id === active))
  const toolRoute = ['episodes', 'character-library', 'voice-library', 'defaults'].includes(active)
  const toolLabel = active === 'episodes' ? roundTwoText(useI18n().locale, 'episodes') : active === 'character-library' ? roundTwoText(useI18n().locale, 'library') : active === 'voice-library' ? roundTwoText(useI18n().locale, 'voices') : roundTwoText(useI18n().locale, 'defaults')
  const saveProject = (next: Project) => setProject(next)
  return <div className="studio-shell">
    <header className="topbar">
      <Link to="/" className="brand"><span className="brand-mark"><Clapperboard size={17} /></span><span>{t('brand.studio')}</span></Link>
      <div className="project-title"><span className="project-kicker">{t('workspace.project')}</span><span>{project.name}</span><StatusBadge status={project.status} /></div>
      <div className="topbar-right"><LanguageToggle /><button className="icon-button" title={t('common.openDashboard')} onClick={() => navigate('/')}><FolderOpen size={17} /></button><AccountControls /></div>
    </header>
    <div className="studio-layout">
      <aside className="side-nav">
        <div className="side-nav-heading">{t('workspace.productionFlow')}</div>
        {steps.map((step, index) => {
          const Icon = step.icon
          const isActive = active === step.id
          return <button key={step.id} className={`step-link ${isActive ? 'active' : ''} ${index < activeIndex && !toolRoute ? 'visited' : ''}`} onClick={() => navigate(flowRoute(project.id, step.id, episodeId))}>
            <span className="step-number">{index < activeIndex && !toolRoute ? <Check size={13} /> : `0${index + 1}`}</span>
            <Icon size={16} />
            <span>{workflowStepLabel(step.id, locale)}</span>
            {isActive && <ChevronRight className="step-chevron" size={15} />}
          </button>
        })}
        <div className="tool-nav">
          <div className="side-nav-heading">{roundTwoText(locale, 'tools')}</div>
          <ToolNavLink to="episodes" active={active === 'episodes'} icon={<ListVideo size={15} />} label={roundTwoText(locale, 'episodes')} />
          <ToolNavLink to="character-library" active={active === 'character-library'} icon={<Library size={15} />} label={roundTwoText(locale, 'library')} />
          <ToolNavLink to="voice-library" active={active === 'voice-library'} icon={<Volume2 size={15} />} label={roundTwoText(locale, 'voices')} />
          <ToolNavLink to="defaults" active={active === 'defaults'} icon={<SlidersHorizontal size={15} />} label={roundTwoText(locale, 'defaults')} />
        </div>
      </aside>
      <main className="workspace">
        <div className="workspace-head">
          <div>
            <p className="eyebrow">{toolRoute ? roundTwoText(locale, 'tools') : locale === 'zh-CN' ? `第 ${String(activeIndex + 1).padStart(2, '0')} 步 / 共 03 步` : `Step ${String(activeIndex + 1).padStart(2, '0')} of 03`}</p>
            <h1>{toolRoute ? toolLabel : workflowStepLabel(steps[activeIndex]?.id || 'upload', locale)}</h1>
            {episodeId && <span className="active-episode-chip">{locale === 'zh-CN' ? '当前处理' : 'Processing'}: Episode {String(project.episodes?.find((item) => item.id === episodeId)?.episode_number || '').padStart(2, '0')}</span>}
          </div>
          <div className="head-actions"><span className="save-state"><Check size={14} /> {t('common.savedLocally')}</span></div>
        </div>
        {error && <InlineError message={error} />}
        {active === 'upload' && <AssetsAndCharactersStep project={project} episodeId={episodeId} onChange={saveProject} />}
        {active === 'generate' && <GenerateWorkspaceStep project={project} styles={styles} languages={languages} voices={voices} onChange={saveProject} />}
        {active === 'result' && <ResultStep project={project} styles={styles} languages={languages} voices={voices} onChange={saveProject} />}
        {active === 'episodes' && <EpisodesPage project={project} onChange={saveProject} />}
        {active === 'character-library' && <CharacterLibraryPage project={project} onChange={saveProject} />}
        {active === 'voice-library' && <VoiceLibraryPage project={project} onChange={saveProject} />}
        {active === 'defaults' && <ProjectDefaultsPage project={project} onChange={saveProject} />}
      </main>
    </div>
  </div>
}

function readEpisodeVideoMetadata(file: File): Promise<VideoMetadata & { thumbnail_data?: string }> {
  return new Promise((resolve, reject) => {
    const video = document.createElement('video')
    const source = URL.createObjectURL(file)
    video.preload = 'metadata'; video.muted = true; video.playsInline = true
    const finish = (metadata: VideoMetadata, thumbnail_data?: string) => { URL.revokeObjectURL(source); resolve({ ...metadata, thumbnail_data }) }
    video.onloadedmetadata = () => {
      if (!video.videoWidth || !video.videoHeight) { reject(new Error('Unable to read video dimensions')); return }
      const ratio = video.videoWidth / video.videoHeight
      const metadata: VideoMetadata = { width: video.videoWidth, height: video.videoHeight, aspect_ratio: Number(ratio.toFixed(6)), orientation: ratio > 1.03 ? 'landscape' : ratio < 0.97 ? 'portrait' : 'square', duration: Number.isFinite(video.duration) ? Number(video.duration.toFixed(3)) : 0 }
      video.onloadeddata = () => { try { video.currentTime = Math.min(0.1, Math.max(0, video.duration / 2)) } catch { finish(metadata) } }
      video.onseeked = () => { try { const canvas = document.createElement('canvas'); canvas.width = video.videoWidth; canvas.height = video.videoHeight; canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height); finish(metadata, canvas.toDataURL('image/jpeg', 0.78)) } catch { finish(metadata) } }
      video.onerror = () => finish(metadata)
    }
    video.onerror = () => { URL.revokeObjectURL(source); reject(new Error('Unable to read video metadata')) }
    video.src = source
  })
}

function episodeVideoUrl(episode?: Episode) {
  if (!episode) return ''
  if (episode.video_url) return assetUrl(episode.video_url)
  if (!episode.video_filename) return ''
  if (episode.video_path?.startsWith('/uploads/')) return assetUrl(episode.video_path)
  const suffix = episode.video_filename.toLowerCase().endsWith('.mov') ? '.mov' : '.mp4'
  return assetUrl(`/uploads/${episode.project_id}/${episode.id}${suffix}`)
}

function characterReferenceUrl(character: Character) {
  const asset = character.target_reference_path || character.main_reference_image || character.source_reference_image || character.original_reference_image || character.reference_assets?.find((item) => item.is_primary)?.image
  if (!asset) return ''
  return asset.startsWith('data:') || asset.startsWith('blob:') || asset.startsWith('http') ? asset : assetUrl(asset)
}

function primaryTargetCharacter(characters: Character[]) {
  const ordered = [...characters].sort((left, right) =>
    (left.character_code || left.name || left.id).localeCompare(right.character_code || right.name || right.id),
  )
  return ordered.find((character) => character.confirmed && Boolean(character.target_reference_path))
    || ordered.find((character) => character.confirmed && Boolean(characterReferenceUrl(character)))
}

function AssetsAndCharactersStep({ project, episodeId, onChange }: { project: Project; episodeId?: string; onChange: (project: Project) => void }) {
  const navigate = useNavigate()
  const { locale } = useI18n()
  const sourceVideoRef = useRef<HTMLVideoElement>(null)
  const episode = project.episodes?.find((item) => item.id === (episodeId || project.active_episode_id))
  const [autoAnalysisState, setAutoAnalysisState] = useState<'idle' | 'running' | 'completed' | 'failed'>('idle')
  const [error, setError] = useState('')
  const [referenceName, setReferenceName] = useState('')
  const [scenePlan, setScenePlan] = useState<XmaxThirtySecondScenePlan | null>(null)
  const [scenePlanError, setScenePlanError] = useState('')
  const [scenePreflightValid, setScenePreflightValid] = useState(false)
  const analysisAttemptRef = useRef('')
  const episodeCharacters = episodeBoundCharacters(project.characters, episode)
  const validTargetCharacters = validEpisodeTargetCharacters(project.characters, episode)
  const targetCharacter = primaryTargetCharacter(validTargetCharacters)
  const persistedMappings = scenePlan ? sceneReviewMappingsFromPlan(scenePlan) : {}
  const mappingsReady = sceneReviewMappingsReady(scenePlan, persistedMappings, validTargetCharacters)
  const matchedCharacterIds = new Set(
    scenePlan
      ? sceneReviewUnits(scenePlan)
        .map((unit) => persistedMappings[String(unit.sequence)]?.selectedCharacterId)
        .filter((characterId): characterId is string =>
          Boolean(characterId && validTargetCharacters.some((character) => character.id === characterId)),
        )
      : [],
  )
  const detectedCount = validTargetCharacters.length
  const matchedCount = matchedCharacterIds.size
  const pendingCount = scenePlan
    ? sceneReviewUnits(scenePlan).filter((unit) =>
      !sceneReviewMappingsReady(
        { ...scenePlan, transform_units: [unit], shots: [unit] },
        persistedMappings,
        validTargetCharacters,
      ),
    ).length
    : 0
  const sourceReady = isEpisodeUploadComplete(episode)
  const prerequisitesReady = sourceReady && validTargetCharacters.length > 0 && mappingsReady && scenePreflightValid
  const referenceReadinessSignature = validTargetCharacters.map((character) => [
    character.id,
    character.confirmed,
    character.priority_level,
    character.target_reference_path,
    character.main_reference_image,
    character.source_reference_image,
    character.original_reference_image,
  ].join(':')).join('|')

  useEffect(() => {
    let active = true
    setScenePlan(null)
    setScenePlanError('')
    if (!episode?.id || !sourceReady) return () => { active = false }
    void getXmaxThirtySecondScenePlan(episode.id, 0.35, false)
      .then((nextPlan) => {
        if (active) setScenePlan(nextPlan)
      })
      .catch((reason) => {
        if (active) setScenePlanError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { active = false }
  }, [episode?.id, episode?.video_path, episode?.updated_at, sourceReady])

  useEffect(() => {
    let active = true
    setScenePreflightValid(false)
    if (!episode?.id || !scenePlan || !mappingsReady) return () => { active = false }
    void preflightXmaxThirtySecondTask(episode.id, persistedMappings)
      .then((result) => {
        if (active) {
          setScenePreflightValid(result.allValid)
          setScenePlanError(result.allValid ? '' : 'The persisted Scene mappings or character reference failed preflight.')
        }
      })
      .catch((reason) => {
        if (active) setScenePlanError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { active = false }
  }, [episode?.id, scenePlan?.mappings_updated_at, mappingsReady, referenceReadinessSignature])

  useEffect(() => {
    if (!episode?.video_filename || !['not_started', 'failed'].includes(episode.analysis_status || 'not_started')) return
    if (analysisAttemptRef.current === episode.id) return
    const lockKey = `episode-auto-analysis:${episode.id}`
    if (window.sessionStorage.getItem(lockKey) === 'running') return
    analysisAttemptRef.current = episode.id
    window.sessionStorage.setItem(lockKey, 'running')
    setAutoAnalysisState('running')
    setError('')
    void api<{ project: Project }>(`/episodes/${episode.id}/analyze`, { method: 'POST' })
      .then((response) => {
        onChange(response.project)
        setAutoAnalysisState('completed')
      })
      .catch((reason) => {
        setError(reason instanceof Error ? reason.message : String(reason))
        setAutoAnalysisState('failed')
      })
      .finally(() => window.sessionStorage.removeItem(lockKey))
  }, [episode?.id, episode?.video_filename, episode?.analysis_status])

  async function uploadTargetReference(file?: File) {
    if (!file) return
    if (!episode) {
      setError(locale === 'zh-CN' ? '请先选择当前剧集。' : 'Select the current Episode first.')
      return
    }
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      setError(locale === 'zh-CN' ? '请选择 PNG、JPEG 或 WebP 参考图。' : 'Choose a PNG, JPEG, or WebP reference image.')
      return
    }
    setError('')
    setReferenceName(file.name)
    const dataUrl = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result || ''))
      reader.onerror = () => reject(new Error('Unable to read the reference image.'))
      reader.readAsDataURL(file)
    })
    try {
      let character = targetCharacter
      const createdForEpisode = !character
      if (!character) {
        character = await api<Character>(`/projects/${project.id}/characters`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'Character 01', role: 'Lead', character_type: 'Human', priority_level: 'Locked', created_from_episode_id: episode.id }),
        })
      }
      const saved = await api<Character>(`/projects/${project.id}/characters/${character.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...character, target_reference_path: dataUrl, confirmed: true, priority_level: 'Locked' }),
      })
      const unresolvedCandidates = (episode?.candidates || []).filter((candidate) =>
        !candidate.character_id
        && !candidate.matched_character_id
        && !['ignored', 'background'].includes(candidate.status),
      )
      if (createdForEpisode && episode && unresolvedCandidates.length === 1) {
        await api(`/episodes/${episode.id}/character-candidates/${unresolvedCandidates[0].id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'matched', character_id: saved.id }),
        })
      }
      onChange(await api<Project>(`/projects/${project.id}`))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  return <div className="assets-and-characters">
    <div className="assets-source">
      {episode ? <EpisodeUploadStep project={project} episodeId={episode.id} onChange={onChange} videoRef={sourceVideoRef} /> : <UploadStep project={project} onChange={onChange} />}
    </div>
    <section className="asset-character-summary">
      <div className="section-heading-inline">
        <div><span className="section-label">{locale === 'zh-CN' ? '角色准备' : 'Character readiness'}</span><h2>{locale === 'zh-CN' ? '上传目标角色参考图' : 'Upload the target character reference'}</h2></div>
        <label className="secondary-button compact"><Upload size={15} /> {targetCharacter ? (locale === 'zh-CN' ? '替换参考图' : 'Replace reference') : (locale === 'zh-CN' ? '上传参考图' : 'Upload reference')}<input type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => void uploadTargetReference(event.target.files?.[0])} /></label>
      </div>
      <div className="asset-character-grid">
        <div className="asset-reference-preview">
          {targetCharacter && characterReferenceUrl(targetCharacter) ? <img src={characterReferenceUrl(targetCharacter)} alt={targetCharacter.name} /> : <div><UserRound size={24} /><span>{locale === 'zh-CN' ? '尚未上传参考图' : 'No reference uploaded'}</span></div>}
          <span>{referenceName || targetCharacter?.name || (locale === 'zh-CN' ? '目标角色' : 'Target character')}</span>
        </div>
        <div className="asset-analysis-summary">
          <div><span>{locale === 'zh-CN' ? '识别角色数' : 'Detected'}</span><strong>{detectedCount}</strong></div>
          <div><span>{locale === 'zh-CN' ? '已匹配数' : 'Matched'}</span><strong>{matchedCount}</strong></div>
          <div><span>{locale === 'zh-CN' ? '待确认数' : 'Pending'}</span><strong>{pendingCount}</strong></div>
        </div>
      </div>
      {!!episodeCharacters.length && <div className="episode-character-list" aria-label="Episode characters">
        {episodeCharacters.map((character) => <span key={character.id}>
          <strong>{character.character_code || character.name}</strong>
          {character.character_code && character.name !== character.character_code ? ` · ${character.name}` : ''}
        </span>)}
      </div>}
      {(autoAnalysisState === 'running' || episode?.analysis_status === 'processing') && <p className="asset-auto-status"><LoaderCircle className="spin" size={14} /> {locale === 'zh-CN' ? '视频上传完成，正在自动分析人物…' : 'Upload complete. Analyzing characters automatically…'}</p>}
      {error && <InlineError message={error} />}
    </section>
    <details className="workflow-advanced">
      <summary><Settings2 size={15} /> {locale === 'zh-CN' ? '高级调整' : 'Advanced adjustments'}</summary>
      <p>{locale === 'zh-CN' ? '检测框、候选截图、角色确认和处理模式仅在需要人工修正时使用。' : 'Detection details, candidate frames, character confirmation, and processing mode are available only when manual correction is needed.'}</p>
      <TransformUnitPlanSummary episodeId={episode?.id} />
      <XmaxSceneReviewEditor
        project={project}
        episodeId={episode?.id}
        onPlanChange={setScenePlan}
        onProjectRefresh={async () => onChange(await api<Project>(`/projects/${project.id}`))}
      />
      <ProcessingModeControl project={project} onChange={onChange} />
      <AnalyzeStepCore project={project} episodeId={episode?.id} onChange={onChange} />
      <CharactersStep project={project} episode={episode} sourceVideoRef={sourceVideoRef} onChange={onChange} />
    </details>
    <div className="step-footer">
      <span>{prerequisitesReady
        ? <><Check size={15} /> {locale === 'zh-CN' ? '原片、目标角色和镜头映射已就绪' : 'Source, target character, and Scene mappings are ready'}</>
        : scenePlanError
          ? (locale === 'zh-CN' ? `镜头计划读取失败：${scenePlanError}` : `Scene Plan failed: ${scenePlanError}`)
          : !sourceReady
            ? (locale === 'zh-CN' ? '请先上传可读取的原片' : 'Upload a readable source video first')
            : !targetCharacter
              ? (locale === 'zh-CN' ? '请先上传并确认目标角色参考图' : 'Upload and confirm a target character reference')
              : (locale === 'zh-CN' ? '请在高级调整中保存全部镜头映射' : 'Save every Scene mapping in Advanced adjustments')}</span>
      <button className="primary-button" disabled={!prerequisitesReady} onClick={() => navigate(flowRoute(project.id, 'generate', episode?.id))}>{locale === 'zh-CN' ? '继续生成海外版' : 'Continue'} <ArrowRight size={16} /></button>
    </div>
  </div>
}

function EpisodeUploadStep({ project, episodeId, onChange, videoRef }: { project: Project; episodeId: string; onChange: (p: Project) => void; videoRef?: React.RefObject<HTMLVideoElement> }) {
  const navigate = useNavigate(); const { t, formatDuration } = useI18n(); const [uploading, setUploading] = useState(false); const [localUrl, setLocalUrl] = useState(''); const [error, setError] = useState('')
  const episode = project.episodes?.find((item) => item.id === episodeId)
  const selectedEpisode = episode
  const uploadComplete = isEpisodeUploadComplete(episode)
  useEffect(() => { const episodeForSync = selectedEpisode; if (!episodeForSync?.video_filename || episodeForSync.duration) return; const video = document.createElement('video'); video.preload = 'metadata'; video.src = episodeVideoUrl(episodeForSync); video.onloadedmetadata = () => { const metadata = readVideoMetadata(video); if (!metadata) return; void api<Episode>(`/episodes/${episodeForSync.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(metadata) }).then((updated) => onChange({ ...project, episodes: project.episodes?.map((item) => item.id === updated.id ? updated : item) })).catch(() => undefined) }; return () => { video.onloadedmetadata = null } }, [selectedEpisode?.id, selectedEpisode?.video_filename, selectedEpisode?.duration])
  useEffect(() => { const episodeForSync = selectedEpisode; if (!episodeForSync?.video_filename || episodeForSync.duration) return; const player = document.querySelector('.video-preview video') as HTMLVideoElement | null; if (!player) return; const sync = () => { const metadata = readVideoMetadata(player); if (!metadata) return; void api<Episode>(`/episodes/${episodeForSync.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(metadata) }).then((updated) => onChange({ ...project, episodes: project.episodes?.map((item) => item.id === updated.id ? updated : item) })).catch(() => undefined) }; player.addEventListener('loadedmetadata', sync); return () => player.removeEventListener('loadedmetadata', sync) }, [selectedEpisode?.id, selectedEpisode?.video_filename, selectedEpisode?.duration, localUrl])
  useEffect(() => { const player = document.querySelector('.video-preview video') as HTMLVideoElement | null; if (player && player.readyState >= 1) player.dispatchEvent(new Event('loadedmetadata')) }, [selectedEpisode?.id, localUrl])
  useEffect(() => { const episodeForSync = selectedEpisode; const timer = window.setTimeout(() => { const player = document.querySelector('.video-preview video') as HTMLVideoElement | null; const metadata = player ? readVideoMetadata(player) : null; if (!episodeForSync?.id || !metadata || episodeForSync.duration) return; void api<Episode>(`/episodes/${episodeForSync.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(metadata) }).then((updated) => onChange({ ...project, episodes: project.episodes?.map((item) => item.id === updated.id ? updated : item) })).catch(() => undefined) }, 250); return () => window.clearTimeout(timer) }, [selectedEpisode?.id, selectedEpisode?.duration, localUrl])
  if (!episode) return <InlineError message={`Episode ${episodeId} was not found in project ${project.id}.`} />
  const currentEpisode = episode
  async function uploadEpisodeFile(file?: File) { if (!file) return; setError(''); setUploading(true); setLocalUrl(URL.createObjectURL(file)); try { const metadata = await readEpisodeVideoMetadata(file); if (metadata.duration > 180) throw new Error('当前MVP单集支持3分钟以内，请分集上传。'); const form = new FormData(); form.append('file', file); Object.entries(metadata).forEach(([key, value]) => { if (key !== 'thumbnail_data') form.append(key, String(value)) }); if (metadata.thumbnail_data) form.append('thumbnail_data', metadata.thumbnail_data); const response = await api<{ project: Project }>(`/episodes/${currentEpisode.id}/upload`, { method: 'POST', body: form }); onChange(response.project) } catch (e) { setError((e as Error).message) } finally { setUploading(false) } }
  const source = localUrl || episodeVideoUrl(episode)
  return <div className="step-content"><DemoNotice>{t('dashboard.demoNotice')}</DemoNotice><div className="upload-layout"><section className="video-panel"><div className="panel-topline"><span className="panel-label">{t('upload.sourceVideo')}</span><span className="file-rule">{t('upload.fileRule')}</span></div>{source ? <div className="video-preview"><video ref={videoRef} className="responsive-video" controls preload="metadata" playsInline crossOrigin="use-credentials" src={source} /><div className="video-overlay-label">{t('upload.originalSource')} · EP {String(episode.episode_number).padStart(2, '0')}</div></div> : <label className="dropzone"><input type="file" accept="video/mp4,.mp4,.mov" onChange={(e) => uploadEpisodeFile(e.target.files?.[0])} /><div className="drop-icon"><CloudUpload size={25} /></div><strong>{t('upload.dropTitle')}</strong><span>{t('upload.dropCopy')}</span><small>{t('upload.dropMeta')}</small></label>}{episode.video_filename && <div className="file-summary"><div className="file-type"><Film size={17} /></div><div><strong>{episode.video_filename}</strong><span>{formatDuration(episode.duration)} · {episode.width || '—'} × {episode.height || '—'}</span></div><label className="replace-link">{t('upload.replace')}<input type="file" accept="video/mp4,.mp4,.mov" onChange={(e) => uploadEpisodeFile(e.target.files?.[0])} /></label></div>}{error && <InlineError message={error} />}</section><aside className="upload-aside"><div className="section-label">{t('upload.next')}</div><div className="mini-flow"><FlowItem number="01" title={t('upload.flowAnalyze')} copy={t('upload.flowAnalyzeCopy')} /><FlowItem number="02" title={t('upload.flowCharacters')} copy={t('upload.flowCharactersCopy')} /><FlowItem number="03" title={t('upload.flowWorld')} copy={t('upload.flowWorldCopy')} /></div></aside></div><div className="step-footer"><span>{uploading ? <><LoaderCircle className="spin" size={15} /> {t('upload.uploading')}</> : uploadComplete ? <><Check size={15} /> {t('upload.sourceReady')}</> : t('upload.start')}</span><button className="primary-button" disabled={!uploadComplete || uploading} onClick={() => navigate(flowRoute(project.id, 'analyze', episode.id))}>{t('upload.continue')} <ArrowRight size={16} /></button></div></div>
}

function UploadStep({ project, onChange }: { project: Project; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const { t, formatDuration } = useI18n(); const [uploading, setUploading] = useState(false); const [localUrl, setLocalUrl] = useState(''); const [error, setError] = useState('')
  async function handleFile(file?: File) { if (!file) return; if (!['video/mp4', 'video/quicktime'].includes(file.type) && !/\.(mp4|mov)$/i.test(file.name)) { setError(t('upload.errorType')); return }; setError(''); setLocalUrl(URL.createObjectURL(file)); setUploading(true); try { onChange(await uploadVideo(project.id, file)) } catch (e) { setError((e as Error).message) } finally { setUploading(false) } }
  const videoSrc = localUrl || videoUrl(project)
  return <div className="step-content"><DemoNotice>{t('dashboard.demoNotice')}</DemoNotice><div className="upload-layout"><section className="video-panel"><div className="panel-topline"><span className="panel-label">{t('upload.sourceVideo')}</span><span className="file-rule">{t('upload.fileRule')}</span></div>{videoSrc ? <div className="video-preview"><ResponsiveVideo src={videoSrc} project={project} onChange={onChange} /><div className="video-overlay-label">{t('upload.originalSource')}</div></div> : <label className="dropzone"><input type="file" accept="video/mp4,.mp4,.mov" onChange={(e) => handleFile(e.target.files?.[0])} /><div className="drop-icon"><CloudUpload size={25} /></div><strong>{t('upload.dropTitle')}</strong><span>{t('upload.dropCopy')}</span><small>{t('upload.dropMeta')}</small></label>}{project.video_filename && <div className="file-summary"><div className="file-type"><Film size={17} /></div><div><strong>{project.video_filename}</strong><span>{formatDuration(project.video_duration)} · {t('upload.originalDimensions')}</span></div><label className="replace-link">{t('upload.replace')}<input type="file" accept="video/mp4,.mp4,.mov" onChange={(e) => handleFile(e.target.files?.[0])} /></label></div>}{error && <InlineError message={error} />}</section><aside className="upload-aside"><div className="section-label">{t('upload.next')}</div><div className="mini-flow"><FlowItem number="01" title={t('upload.flowAnalyze')} copy={t('upload.flowAnalyzeCopy')} /><FlowItem number="02" title={t('upload.flowCharacters')} copy={t('upload.flowCharactersCopy')} /><FlowItem number="03" title={t('upload.flowWorld')} copy={t('upload.flowWorldCopy')} /></div><div className="privacy-note"><span className="status-dot" /> {t('upload.localFiles')}</div></aside></div><div className="step-footer"><span>{uploading ? <><LoaderCircle className="spin" size={15} /> {t('upload.uploading')}</> : project.video_filename ? <><Check size={15} /> {t('upload.sourceReady')}</> : t('upload.start')}</span><button className="primary-button" disabled={!project.video_filename || uploading} onClick={() => navigate(`/project/${project.id}/analyze`)}>{t('upload.continue')} <ArrowRight size={16} /></button></div></div>
}

function LegacyUploadStep({ project, onChange }: { project: Project; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const [uploading, setUploading] = useState(false); const [localUrl, setLocalUrl] = useState(''); const [error, setError] = useState('')
  const { t, formatDuration: formatLocalizedDuration } = useI18n()
  async function handleFile(file?: File) { if (!file) return; if (!['video/mp4', 'video/quicktime'].includes(file.type) && !/\.(mp4|mov)$/i.test(file.name)) { setError('Please choose an MP4 or MOV file.'); return }; setError(''); setLocalUrl(URL.createObjectURL(file)); setUploading(true); try { onChange(await uploadVideo(project.id, file)) } catch (e) { setError((e as Error).message) } finally { setUploading(false) } }
  const videoSrc = localUrl || videoUrl(project)
  return <div className="step-content"><div className="upload-layout"><section className="video-panel"><div className="panel-topline"><span className="panel-label">SOURCE VIDEO</span><span className="file-rule">MP4 / MOV · up to 3 min</span></div>{videoSrc ? <div className="video-preview"><ResponsiveVideo src={videoSrc} project={project} onChange={onChange} /><div className="video-overlay-label">ORIGINAL SOURCE</div></div> : <label className="dropzone"><input type="file" accept="video/mp4,.mp4,.mov" onChange={(e) => handleFile(e.target.files?.[0])} /><div className="drop-icon"><CloudUpload size={25} /></div><strong>Drop your drama here</strong><span>or click to browse from this computer</span><small>One file · max 500 MB · recommended under 3 minutes</small></label>}{project.video_filename && <div className="file-summary"><div className="file-type"><Film size={17} /></div><div><strong>{project.video_filename}</strong><span>{formatDuration(project.video_duration)} · original dimensions are detected in-browser</span></div><label className="replace-link">Replace<input type="file" accept="video/mp4,.mp4,.mov" onChange={(e) => handleFile(e.target.files?.[0])} /></label></div>}{error && <InlineError message={error} />}</section><aside className="upload-aside"><div className="section-label">What happens next</div><div className="mini-flow"><FlowItem number="01" title="Analyze the source" copy="Detect characters, scenes and language." /><FlowItem number="02" title="Lock character assets" copy="Confirm the references that stay consistent." /><FlowItem number="03" title="Choose your world" copy="Pick style, voices and output language." /></div><div className="privacy-note"><span className="status-dot" /> Files stay on this local workspace</div></aside></div><div className="step-footer"><span>{uploading ? <><LoaderCircle className="spin" size={15} /> Uploading source...</> : project.video_filename ? <><Check size={15} /> Source ready for analysis</> : 'Start with a source video'}</span><button className="primary-button" disabled={!project.video_filename || uploading} onClick={() => navigate(`/project/${project.id}/analyze`)}>Continue to analysis <ArrowRight size={16} /></button></div></div>
}

function AnalyzeStep({ project, onChange }: { project: Project; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const [analyzing, setAnalyzing] = useState(false); const [error, setError] = useState('')
  async function analyze() { setAnalyzing(true); setError(''); try { const response = await api<{ project: Project }>(`/projects/${project.id}/analyze`, { method: 'POST' }); onChange(response.project) } catch (e) { setError((e as Error).message) } finally { setAnalyzing(false) } }
  return <div className="step-content"><div className="analysis-head"><div><p className="muted-copy">A quick mock pass identifies the cast and the visual environments your story moves through.</p></div><button className="secondary-button" disabled={analyzing || !project.video_filename} onClick={analyze}>{analyzing ? <LoaderCircle className="spin" size={16} /> : <Gauge size={16} />} {analyzing ? 'Analyzing...' : project.characters.length ? 'Run analysis again' : 'Analyze source'}</button></div>{error && <InlineError message={error} />}<div className="analysis-grid"><section className="analysis-video"><div className="panel-topline"><span className="panel-label">VIDEO ANALYSIS</span><span className="analysis-status"><span className="status-dot" /> Mock vision pass</span></div>{project.video_filename ? <ResponsiveVideo src={videoUrl(project)} project={project} onChange={onChange} /> : <div className="empty-video"><Film size={23} /> Upload a source video first</div>}<div className="video-stats"><Stat label="Duration" value={formatDuration(project.video_duration)} /><Stat label="Language" value={project.source_language === 'mandarin' ? 'Mandarin' : project.source_language || 'Pending'} /><Stat label="Aspect" value={project.aspect_ratio ? `${project.aspect_ratio.toFixed(3)} · ${project.orientation}` : 'Reading…'} /></div></section><section className="analysis-results"><div className="result-section-heading"><div><span className="section-label">CAST DETECTION</span><h3>{project.characters.length || '—'} key characters</h3></div><span className="confidence-chip">94% confidence</span></div>{project.characters.length ? <div className="character-mini-list">{project.characters.map((character, index) => <div className="character-mini" key={character.id}><MockAvatar index={index} /><div><strong>{character.name}</strong><span>{character.role} · {character.gender} · {character.estimated_age}</span></div><Check className="green-check" size={16} /></div>)}</div> : <div className="empty-result">Run analysis to populate the character review.</div>}<div className="result-section-heading scene-heading"><div><span className="section-label">SCENE DETECTION</span><h3>{project.scenes.length || '—'} environments</h3></div></div>{project.scenes.length ? <div className="scene-list">{project.scenes.map((scene) => <div className="scene-row" key={scene.id}><MockFrame variant={scene.id.includes('street') ? 'street' : 'cafe'} label={scene.location.toUpperCase()} /><div><strong>{scene.name}</strong><span>{scene.description}</span><small>{scene.main_objects.join(' · ')}</small></div></div>)}</div> : <div className="empty-result">Scenes will appear with the character pass.</div>}</section></div><div className="step-footer"><span>{project.characters.length ? <><Check size={15} /> Analysis ready for review</> : 'Mock analysis takes about a second'}</span><button className="primary-button" disabled={!project.characters.length} onClick={() => navigate(`/project/${project.id}/characters`)}>Review characters <ArrowRight size={16} /></button></div></div>
}

type AnalyzeStatus = 'not_started' | 'processing' | 'completed' | 'warning' | 'failed'

function EpisodeAnalyzeStep({ project, episodeId, onChange }: { project: Project; episodeId: string; onChange: (p: Project) => void }) {
  const navigate = useNavigate()
  const { locale, t, formatDuration } = useI18n()
  const videoRef = useRef<HTMLVideoElement>(null)
  const episode = project.episodes?.find((item) => item.id === episodeId)
  const [status, setStatus] = useState<AnalyzeStatus>(episode?.analysis_status || 'not_started')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [activeId, setActiveId] = useState('')
  useEffect(() => setStatus(episode?.analysis_status || 'not_started'), [episode?.analysis_status])
  if (!episode) return <InlineError message={`Episode ${episodeId} was not found in project ${project.id}.`} />
  const selectedEpisode = episode

  const isConsistency = project.processing_mode === 'character-consistency'
  const candidates = (episode.candidates || []).filter((item) => !['background'].includes(item.status))
  const active = candidates.find((item) => item.id === activeId) || candidates[0]
  const matched = candidates.filter((item) => item.status === 'matched')
  const review = candidates.filter((item) => !['matched', 'locked'].includes(item.status))
  const retainedCount = review.filter((item) => item.status === 'retained' || item.status === 'temporary').length
  const undecidedCount = review.filter((item) => item.status === 'candidate').length
  const ignoredCount = review.filter((item) => item.status === 'ignored').length
  const lockedCount = project.characters.filter((item) => item.priority_level === 'Locked').length
  const statusLabels: Record<AnalyzeStatus, string> = { not_started: t('status.not_started'), processing: t('status.processing'), completed: t('status.completed'), warning: t('status.warning'), failed: t('status.failed') }

  async function refresh() {
    onChange(await api<Project>(`/projects/${project.id}`))
  }
  async function analyze() {
    setStatus('processing')
    setError('')
    try {
      const response = await api<{ project: Project }>(`/episodes/${selectedEpisode.id}/analyze`, { method: 'POST' })
      onChange(response.project)
      setStatus('completed')
      setActiveId(response.project.episodes?.find((item) => item.id === selectedEpisode.id)?.candidates?.[0]?.id || '')
    } catch (reason) {
      setStatus('failed')
      setError((reason as Error).message)
    }
  }
  async function decide(candidateId: string, nextStatus: 'candidate' | 'retained' | 'ignored') {
    setBusy(candidateId)
    setError('')
    try {
      await api(`/episodes/${selectedEpisode.id}/character-candidates/${candidateId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: nextStatus }) })
      await refresh()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy('')
    }
  }
  async function createCards() {
    setBusy('all')
    setError('')
    try {
      const response = await api<{ project: Project }>(`/episodes/${selectedEpisode.id}/character-candidates/confirm-retained`, { method: 'POST' })
      onChange(response.project)
      navigate(flowRoute(project.id, 'characters', selectedEpisode.id))
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy('')
    }
  }
  function selectCandidate(candidate: typeof candidates[number]) {
    setActiveId(candidate.id)
    if (videoRef.current) {
      videoRef.current.currentTime = candidate.representative_timestamp || 0
      void videoRef.current.pause()
    }
  }
  const label = (value: string) => locale === 'zh-CN'
    ? ({ candidate: '待选择', retained: '已保留', temporary: '角色卡待锁定', matched: '自动复用', locked: '已锁定', ignored: '已忽略' }[value] || value)
    : ({ candidate: 'Undecided', retained: 'Kept', temporary: 'Card needs locking', matched: 'Auto reused', locked: 'Locked', ignored: 'Ignored' }[value] || value)

  return <div className="step-content">
    <DemoNotice>{isConsistency
      ? (locale === 'zh-CN' ? '当前仅展示预设的演示候选 / Mock候选、静态人物框和演示时间点，不代表真实人物识别。普通路人不进入项目角色库。' : 'Only preset Demo / Mock candidates, static boxes, and demo timestamps are shown. They are not real person recognition results.')
      : (locale === 'zh-CN' ? 'Quick 模式仅处理当前单集，不建立或复用跨集角色库；当前也不会生成项目级人物识别结果。' : 'Quick mode handles only this episode and does not build, reuse, or claim project-level person recognition.')}</DemoNotice>
    <div className="analysis-head"><p className="muted-copy">{isConsistency
      ? (locale === 'zh-CN' ? '每个演示人物只对应一个候选、一张裁剪头像和一个演示时间点，只需做一次“保留 / 忽略”决定。' : 'Each demo person maps to one candidate, one cropped portrait, and one demo timestamp, requiring one Keep / Ignore decision.')
      : (locale === 'zh-CN' ? '可直接完成当前单集分析并继续设置，不要求挑选角色。' : 'Analyze this episode and continue without selecting project characters.')}</p><button className="secondary-button" disabled={status === 'processing' || !episode.video_filename} onClick={analyze}>{status === 'processing' ? <LoaderCircle className="spin" size={16} /> : <Gauge size={16} />} {status === 'processing' ? t('analyze.analyzing') : status === 'not_started' ? t('analyze.analyzeSource') : t('analyze.runAgain')}</button></div>
    {error && <InlineError message={error} />}
    <div className={`analysis-grid ${isConsistency ? 'unified-candidate-analysis' : ''}`}>
      <section className="analysis-video"><div className="panel-topline"><span className="panel-label">{t('analyze.videoAnalysis')}</span><span className={`analysis-status analysis-status-${status}`}><span className="status-dot" /> {statusLabels[status]}</span></div><div className="candidate-video-stage"><video ref={videoRef} className="responsive-video" controls preload="metadata" playsInline src={episodeVideoUrl(episode)} />{isConsistency && candidates.filter((item) => item.status !== 'ignored').map((candidate) => <button key={candidate.id} className={`mock-detection-box ${active?.id === candidate.id ? 'active' : ''}`} style={{ left: `${candidate.bounding_box?.x || 10}%`, top: `${candidate.bounding_box?.y || 10}%`, width: `${candidate.bounding_box?.width || 20}%`, height: `${candidate.bounding_box?.height || 60}%` }} onClick={() => selectCandidate(candidate)} title={`${candidate.detected_name} · Mock候选`}><span>{candidate.detected_name}</span></button>)}</div><div className="video-stats"><Stat label={t('analyze.duration')} value={formatDuration(episode.duration)} /><Stat label={t('analyze.aspect')} value={episode.aspect_ratio ? `${episode.aspect_ratio.toFixed(3)} · ${episode.orientation}` : t('analyze.reading')} />{isConsistency ? <Stat label={locale === 'zh-CN' ? '项目核心角色' : 'Project core roles'} value={`${lockedCount} / 5`} /> : <Stat label={locale === 'zh-CN' ? '处理范围' : 'Processing scope'} value={locale === 'zh-CN' ? '当前单集' : 'Current episode'} />}</div></section>
      <aside className="analysis-results candidate-rail">
        <div className="result-section-heading"><div><span className="section-label">{isConsistency ? (locale === 'zh-CN' ? '演示候选 / Mock候选' : 'Demo / Mock candidates') : (locale === 'zh-CN' ? '单集临时处理' : 'Temporary episode processing')}</span><h3>{isConsistency ? (locale === 'zh-CN' ? `${review.length} 个待处理 · ${matched.length} 个演示自动复用` : `${review.length} to review · ${matched.length} demo auto-reused`) : (locale === 'zh-CN' ? '无需建立项目角色库' : 'No project character library required')}</h3></div></div>
        {isConsistency ? <div className="candidate-rail-list">{candidates.map((candidate) => <button key={candidate.id} className={`candidate-rail-item ${active?.id === candidate.id ? 'active' : ''}`} onClick={() => selectCandidate(candidate)}>{candidate.representative_frame ? <img src={assetUrl(candidate.representative_frame)} alt={`${candidate.detected_name} Mock crop`} /> : <span className="candidate-crop-missing">{locale === 'zh-CN' ? '无演示裁剪' : 'No demo crop'}</span>}<span><strong>{candidate.detected_name}</strong><small>{candidate.entity_type} · {locale === 'zh-CN' ? 'Mock候选' : 'Mock candidate'} {Math.round(candidate.confidence * 100)}%</small><em>{locale === 'zh-CN' ? '演示时间 ' : 'Demo time '}{candidate.occurrence_timestamps?.map(formatTimestamp).join('、')}</em></span><i>{label(candidate.status)}</i></button>)}</div> : <div className="empty-result">{locale === 'zh-CN' ? 'Quick 模式只保存当前单集设置，不会显示跨集角色复用已生效。' : 'Quick mode only saves episode settings and never shows cross-episode reuse as active.'}</div>}
      </aside>
    </div>
    {status === 'completed' && isConsistency && <section className="incremental-review core-character-review"><div className="section-heading-inline"><div><span className="section-label">{locale === 'zh-CN' ? '核心角色轻筛选' : 'Core character selection'}</span><h2>{lockedCount ? (locale === 'zh-CN' ? '已锁定角色自动复用，只处理新增长期角色' : 'Locked roles are reused; review only possible new long-term roles') : (locale === 'zh-CN' ? '先保留 1–5 个重要角色' : 'Keep 1–5 important roles first')}</h2></div>{retainedCount > 0 && <button className="primary-button" disabled={busy === 'all'} onClick={createCards}><Check size={15} /> {locale === 'zh-CN' ? '一键确认已保留角色' : 'Create cards for kept roles'}</button>}</div><div className="candidate-stats"><span>{locale === 'zh-CN' ? '自动复用' : 'Auto reused'}<strong>{matched.length}</strong></span><span>{locale === 'zh-CN' ? '待选择' : 'Undecided'}<strong>{undecidedCount}</strong></span><span>{locale === 'zh-CN' ? '已保留' : 'Kept'}<strong>{retainedCount}</strong></span><span>{locale === 'zh-CN' ? '已忽略' : 'Ignored'}<strong>{ignoredCount}</strong></span></div>{matched.length > 0 && <div className="reuse-summary"><Check size={15} /><span>{locale === 'zh-CN' ? `${matched.length} 个项目角色已自动关联本集，无需重新确认。` : `${matched.length} project roles are linked automatically; no repeat confirmation is needed.`}</span></div>}<div className="candidate-list">{review.map((candidate) => <article className={`candidate-card candidate-${candidate.status}`} key={candidate.id} onClick={() => selectCandidate(candidate)}><div className="candidate-mock-frame">{candidate.representative_frame ? <img src={assetUrl(candidate.representative_frame)} alt="" /> : <><span>{String(candidate.source_index + 1).padStart(2, '0')}</span><i /></>}</div><div className="candidate-copy"><div className="card-row"><strong>{candidate.detected_name}</strong><span className={`priority-chip priority-${candidate.status}`}>{label(candidate.status)}</span></div><span>{locale === 'zh-CN' ? `本集唯一候选 · 出现 ${candidate.occurrence_timestamps?.length || 0} 次` : `Unique episode candidate · ${candidate.occurrence_timestamps?.length || 0} grouped occurrences`}</span><div className="occurrence-times">{candidate.occurrence_timestamps?.map((time) => <button key={time} onClick={(event) => { event.stopPropagation(); selectCandidate({ ...candidate, representative_timestamp: time }) }}>{formatTimestamp(time)}</button>)}</div></div><div className="candidate-actions simple-candidate-actions">{candidate.status === 'temporary' ? <span className="confirm-state"><Check size={14} /> {locale === 'zh-CN' ? '角色卡已建立，待锁定' : 'Card created; lock it next'}</span> : candidate.status === 'ignored' ? <button className="secondary-button compact" disabled={busy === candidate.id} onClick={(event) => { event.stopPropagation(); void decide(candidate.id, 'candidate') }}><RotateCcw size={13} /> {locale === 'zh-CN' ? '重新选择' : 'Review again'}</button> : <><button className={`secondary-button compact ${candidate.status === 'retained' ? 'active-button' : ''}`} disabled={busy === candidate.id} onClick={(event) => { event.stopPropagation(); void decide(candidate.id, 'retained') }}><Check size={13} /> {locale === 'zh-CN' ? '保留' : 'Keep'}</button><button className="text-button" disabled={busy === candidate.id} onClick={(event) => { event.stopPropagation(); void decide(candidate.id, 'ignored') }}><X size={13} /> {locale === 'zh-CN' ? '忽略' : 'Ignore'}</button></>}</div></article>)}</div>{review.length === 0 && <div className="empty-result">{locale === 'zh-CN' ? '本集没有新增长期角色候选，已锁定角色将直接复用。' : 'No new long-term candidate in this episode. Locked roles will be reused directly.'}</div>}<div className="step-footer"><span>{locale === 'zh-CN' ? '普通路人和群演不进入长期角色库。' : 'Background people and extras stay outside the long-term library.'}</span><button className="secondary-button" onClick={() => navigate(flowRoute(project.id, 'characters', episode.id))}>{locale === 'zh-CN' ? '查看项目角色库' : 'Review project library'} <ArrowRight size={15} /></button></div></section>}
    {status === 'completed' && !isConsistency && <div className="step-footer"><span>{locale === 'zh-CN' ? 'Quick 模式分析完成，不要求建立角色卡。' : 'Quick analysis is complete; no character card is required.'}</span><button className="primary-button" onClick={() => navigate(flowRoute(project.id, 'settings', episode.id))}>{locale === 'zh-CN' ? '继续单集设置' : 'Continue episode settings'} <ArrowRight size={16} /></button></div>}
  </div>
}

function LegacyOverloadedEpisodeAnalyzeStep({ project, episodeId, onChange }: { project: Project; episodeId: string; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const { locale, t, formatDuration } = useI18n(); const videoRef = useRef<HTMLVideoElement>(null)
  const episode = project.episodes?.find((item) => item.id === episodeId); const candidates = episode?.candidates || []
  const [status, setStatus] = useState<AnalyzeStatus>(episode?.analysis_status || 'not_started'); const [error, setError] = useState(''); const [busy, setBusy] = useState(''); const [activeId, setActiveId] = useState('')
  useEffect(() => setStatus(episode?.analysis_status || 'not_started'), [episode?.analysis_status])
  if (!episode) return <InlineError message={`Episode ${episodeId} was not found in project ${project.id}.`} />
  const selectedEpisode = episode
  const active = candidates.find((item) => item.id === activeId) || candidates[0]
  const counts = { retained: candidates.filter((item) => item.status === 'candidate' || item.status === 'temporary').length, matched: candidates.filter((item) => item.status === 'matched' || item.status === 'locked').length, background: candidates.filter((item) => item.status === 'background').length, ignored: candidates.filter((item) => item.status === 'ignored').length }
  const locked = project.characters.filter((item) => item.priority_level === 'Locked')
  const label = (value: string) => locale === 'zh-CN' ? ({ candidate: '已保留', temporary: '待确认', matched: '已匹配', locked: '已锁定', background: '背景人物', ignored: '已忽略' }[value] || value) : ({ candidate: 'Retained', temporary: 'Needs confirmation', matched: 'Matched', locked: 'Locked', background: 'Background', ignored: 'Ignored' }[value] || value)
  async function refresh() { const next = await api<Project>(`/projects/${project.id}`); onChange(next) }
  async function analyze() { setStatus('processing'); setError(''); try { const response = await api<{ project: Project }>(`/episodes/${selectedEpisode.id}/analyze`, { method: 'POST' }); onChange(response.project); setStatus('completed'); setActiveId(response.project.episodes?.find((item) => item.id === selectedEpisode.id)?.candidates?.[0]?.id || '') } catch (reason) { setStatus('failed'); setError((reason as Error).message) } }
  async function act(candidateId: string, candidateStatus: 'candidate' | 'matched' | 'temporary' | 'background' | 'ignored', characterId?: string) { setBusy(candidateId); setError(''); try { await api(`/episodes/${selectedEpisode.id}/character-candidates/${candidateId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: candidateStatus, character_id: characterId || null }) }); await refresh() } catch (reason) { setError((reason as Error).message) } finally { setBusy('') } }
  async function confirmRetained() { setBusy('all'); setError(''); try { const response = await api<{ project: Project }>(`/episodes/${selectedEpisode.id}/character-candidates/confirm-retained`, { method: 'POST' }); onChange(response.project); navigate(flowRoute(project.id, 'characters', selectedEpisode.id)) } catch (reason) { setError((reason as Error).message) } finally { setBusy('') } }
  function selectCandidate(candidate: typeof candidates[number]) { setActiveId(candidate.id); if (videoRef.current) { videoRef.current.currentTime = candidate.representative_timestamp || 0; void videoRef.current.pause() } }
  const statusLabels: Record<AnalyzeStatus, string> = { not_started: t('status.not_started'), processing: t('status.processing'), completed: t('status.completed'), warning: t('status.warning'), failed: t('status.failed') }
  return <div className="step-content"><DemoNotice>{locale === 'zh-CN' ? '当前关键帧、人物框、跨时间归并和置信度均为 Mock，不代表真实视觉识别。' : 'Keyframes, boxes, cross-time grouping, and confidence are Mock data, not real vision results.'}</DemoNotice><div className="analysis-head"><p className="muted-copy">{locale === 'zh-CN' ? '系统按整段视频归并候选；每个角色只需保留或排除一次。' : 'Candidates are grouped across the full episode. Review each identity once.'}</p><button className="secondary-button" disabled={status === 'processing' || !episode.video_filename} onClick={analyze}>{status === 'processing' ? <LoaderCircle className="spin" size={16} /> : <Gauge size={16} />} {status === 'processing' ? t('analyze.analyzing') : status === 'not_started' ? t('analyze.analyzeSource') : t('analyze.runAgain')}</button></div>{error && <InlineError message={error} />}<div className="analysis-grid unified-candidate-analysis"><section className="analysis-video"><div className="panel-topline"><span className="panel-label">{t('analyze.videoAnalysis')}</span><span className={`analysis-status analysis-status-${status}`}><span className="status-dot" /> {statusLabels[status]}</span></div><div className="candidate-video-stage"><video ref={videoRef} className="responsive-video" controls preload="metadata" playsInline src={episodeVideoUrl(episode)} />{candidates.filter((item) => item.status !== 'ignored').map((candidate) => <button key={candidate.id} className={`mock-detection-box ${active?.id === candidate.id ? 'active' : ''}`} style={{ left: `${candidate.bounding_box?.x || 10}%`, top: `${candidate.bounding_box?.y || 10}%`, width: `${candidate.bounding_box?.width || 20}%`, height: `${candidate.bounding_box?.height || 60}%` }} onClick={() => selectCandidate(candidate)} title={`${candidate.detected_name} · Mock`}><span>{candidate.detected_name.split(' ')[0]}</span></button>)}</div><div className="video-stats"><Stat label={t('analyze.duration')} value={formatDuration(episode.duration)} /><Stat label={t('analyze.castDetection')} value={status === 'not_started' ? t('status.not_started') : String(candidates.length)} /><Stat label={locale === 'zh-CN' ? '当前代表帧' : 'Representative frame'} value={formatTimestamp(active?.representative_timestamp || 0)} /></div></section><aside className="analysis-results candidate-rail"><div className="result-section-heading"><div><span className="section-label">{locale === 'zh-CN' ? '候选角色（统一数据源）' : 'Candidates (single source)'}</span><h3>{locale === 'zh-CN' ? `${candidates.length} 个唯一候选` : `${candidates.length} unique candidates`}</h3></div></div><div className="candidate-rail-list">{candidates.map((candidate) => <button key={candidate.id} className={`candidate-rail-item ${active?.id === candidate.id ? 'active' : ''}`} onClick={() => selectCandidate(candidate)}>{candidate.representative_frame ? <img src={assetUrl(candidate.representative_frame)} alt="" /> : <MockAvatar index={candidate.source_index} />}<span><strong>{candidate.detected_name}</strong><small>{candidate.entity_type} · Mock {Math.round(candidate.confidence * 100)}% · {candidate.occurrence_timestamps?.length || 0}×</small><em>{candidate.occurrence_timestamps?.map(formatTimestamp).join('、')}</em></span><i>{label(candidate.status)}</i></button>)}</div></aside></div>{status === 'completed' && <section className="incremental-review"><div className="section-heading-inline"><div><span className="section-label">{locale === 'zh-CN' ? '候选去向' : 'Candidate outcomes'}</span><h2>{locale === 'zh-CN' ? '所有候选均可追溯且只计数一次' : 'Every candidate is traceable and counted once'}</h2></div><button className="primary-button" disabled={busy === 'all' || counts.retained === 0} onClick={confirmRetained}><Check size={15} /> {locale === 'zh-CN' ? '一键确认已保留角色' : 'Confirm retained candidates'}</button></div><div className="candidate-stats"><span>{locale === 'zh-CN' ? '候选总数' : 'Total'}<strong>{candidates.length}</strong></span><span>{locale === 'zh-CN' ? '已保留 / 待确认' : 'Retained / pending'}<strong>{counts.retained}</strong></span><span>{locale === 'zh-CN' ? '已匹配已有角色' : 'Matched'}<strong>{counts.matched}</strong></span><span>{locale === 'zh-CN' ? '背景人物' : 'Background'}<strong>{counts.background}</strong></span><span>{locale === 'zh-CN' ? '已忽略' : 'Ignored'}<strong>{counts.ignored}</strong></span></div><div className="candidate-list">{candidates.map((candidate) => <article className={`candidate-card candidate-${candidate.status}`} key={candidate.id} onClick={() => selectCandidate(candidate)}><div className="candidate-mock-frame">{candidate.representative_frame ? <img src={assetUrl(candidate.representative_frame)} alt="" /> : <><span>{String(candidate.source_index + 1).padStart(2, '0')}</span><i /></>}</div><div className="candidate-copy"><div className="card-row"><strong>{candidate.detected_name}</strong><span className={`priority-chip priority-${candidate.status}`}>{label(candidate.status)}</span></div><span>{candidate.entity_type} · Mock {Math.round(candidate.confidence * 100)}% · {locale === 'zh-CN' ? `出现 ${candidate.occurrence_timestamps?.length || 0} 次` : `${candidate.occurrence_timestamps?.length || 0} occurrences`}</span><div className="occurrence-times">{candidate.occurrence_timestamps?.map((time) => <button key={time} onClick={(event) => { event.stopPropagation(); selectCandidate({ ...candidate, representative_timestamp: time }) }}>{formatTimestamp(time)}</button>)}</div></div><div className="candidate-actions">{candidate.status === 'background' || candidate.status === 'ignored' ? <button className="secondary-button compact" disabled={busy === candidate.id} onClick={(event) => { event.stopPropagation(); void act(candidate.id, 'candidate') }}><RotateCcw size={13} /> {locale === 'zh-CN' ? '恢复为角色' : 'Restore'}</button> : <button className="secondary-button compact active-button" disabled={busy === candidate.id || candidate.status === 'locked' || candidate.status === 'matched'} onClick={(event) => { event.stopPropagation(); void act(candidate.id, 'candidate') }}><Check size={13} /> {locale === 'zh-CN' ? '保留为角色' : 'Keep as character'}</button>}<label className="field-label compact-field">{locale === 'zh-CN' ? '匹配到已有角色' : 'Match existing'}<select value="" disabled={busy === candidate.id || !locked.length || candidate.status === 'locked'} onClick={(event) => event.stopPropagation()} onChange={(event) => event.target.value && act(candidate.id, 'matched', event.target.value)}><option value="">{locale === 'zh-CN' ? '选择已锁定角色' : 'Choose locked character'}</option>{locked.map((character) => <option key={character.id} value={character.id}>{character.character_code} · {character.name}</option>)}</select></label><div className="candidate-exclude"><span><X size={13} /> {locale === 'zh-CN' ? '排除' : 'Exclude'}</span><button disabled={busy === candidate.id || candidate.status === 'locked'} onClick={(event) => { event.stopPropagation(); void act(candidate.id, 'background') }}>{locale === 'zh-CN' ? '背景人物' : 'Background'}</button><button disabled={busy === candidate.id || candidate.status === 'locked'} onClick={(event) => { event.stopPropagation(); void act(candidate.id, 'ignored') }}>{locale === 'zh-CN' ? '忽略本集' : 'Ignore'}</button></div></div></article>)}</div></section>}</div>
}

function LegacyUnifiedEpisodeAnalyzeStep({ project, episodeId, onChange }: { project: Project; episodeId: string; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const { locale } = useI18n(); const [candidateError, setCandidateError] = useState(''); const [busyCandidate, setBusyCandidate] = useState('')
  const episode = project.episodes?.find((item) => item.id === episodeId); const candidates = episode?.candidates || []; const counts = episode?.candidate_counts || {}; const lockedCharacters = project.characters.filter((character) => character.priority_level === 'Locked')
  async function candidateAction(candidateId: string, status: 'matched' | 'temporary' | 'background' | 'ignored', characterId?: string, entityType?: string) { setBusyCandidate(candidateId); setCandidateError(''); try { await api(`/episodes/${episodeId}/character-candidates/${candidateId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status, character_id: characterId || null, entity_type: entityType || null }) }); const updated = await api<Project>(`/projects/${project.id}`); onChange(updated); if (status === 'temporary') navigate(flowRoute(project.id, 'characters', episodeId)) } catch (reason) { setCandidateError((reason as Error).message) } finally { setBusyCandidate('') } }
  const statusLabel = (status: string) => locale === 'zh-CN' ? ({ candidate: '候选', matched: '已匹配', temporary: '待确认', background: '背景', ignored: '已忽略', locked: '已锁定' }[status] || status) : status
  return <><CoreEpisodeAnalyzeStep project={project} episodeId={episodeId} onChange={onChange} />{episode?.analysis_status === 'completed' && <section className="incremental-review"><div className="section-heading-inline"><div><span className="section-label">{locale === 'zh-CN' ? '本集角色候选（Mock）' : 'Episode Candidates (Mock)'}</span><h2>{locale === 'zh-CN' ? '每个候选都有明确去向' : 'Every candidate has an explicit outcome'}</h2></div></div><p className="reference-guidance">{locale === 'zh-CN' ? '候选框、身份和置信度均为 Mock 数据，不代表真实人物识别。候选操作不会永久删除项目角色。' : 'Candidate boxes, identities, and confidence are Mock data. Candidate actions never delete project characters.'}</p><div className="candidate-stats"><span>{locale === 'zh-CN' ? '候选总数' : 'Total'} <strong>{candidates.length}</strong></span><span>{locale === 'zh-CN' ? '已匹配已有角色' : 'Matched'} <strong>{(counts.matched || 0) + (counts.locked || 0)}</strong></span><span>{locale === 'zh-CN' ? '新增待确认' : 'New pending'} <strong>{(counts.candidate || 0) + (counts.temporary || 0)}</strong></span><span>{locale === 'zh-CN' ? '背景人物' : 'Background'} <strong>{counts.background || 0}</strong></span><span>{locale === 'zh-CN' ? '已忽略' : 'Ignored'} <strong>{counts.ignored || 0}</strong></span></div>{candidateError && <InlineError message={candidateError} />}<div className="candidate-list">{candidates.map((candidate) => <article className={`candidate-card candidate-${candidate.status}`} key={candidate.id}><div className="candidate-mock-frame"><span>{String(candidate.source_index + 1).padStart(2, '0')}</span><i /></div><div className="candidate-copy"><div className="card-row"><strong>{candidate.detected_name}</strong><span className={`priority-chip priority-${candidate.status}`}>{statusLabel(candidate.status)}</span></div><span>{candidate.entity_type} · Mock {Math.round(candidate.confidence * 100)}%</span><small>{candidate.character_id ? `${locale === 'zh-CN' ? '关联角色' : 'Linked'}: ${candidate.character_id}` : (locale === 'zh-CN' ? '未关联项目角色' : 'No project character linked')}</small></div><div className="candidate-actions"><label className="field-label compact-field">{locale === 'zh-CN' ? '匹配到已有角色' : 'Match existing'}<select value="" disabled={busyCandidate === candidate.id || !lockedCharacters.length} onChange={(event) => event.target.value && candidateAction(candidate.id, 'matched', event.target.value)}><option value="">{locale === 'zh-CN' ? '选择已锁定角色' : 'Choose locked character'}</option>{lockedCharacters.map((character) => <option key={character.id} value={character.id}>{character.character_code} · {character.name}</option>)}</select></label><button className="secondary-button compact" disabled={busyCandidate === candidate.id || candidate.status === 'locked'} onClick={() => candidateAction(candidate.id, 'temporary', undefined, candidate.entity_type)}>{locale === 'zh-CN' ? '进入角色资产确认' : 'Review character asset'}</button><button className="text-button" disabled={busyCandidate === candidate.id || candidate.status === 'locked'} onClick={() => candidateAction(candidate.id, 'background')}>{locale === 'zh-CN' ? '标记为背景' : 'Mark background'}</button><button className="text-button" disabled={busyCandidate === candidate.id || candidate.status === 'locked'} onClick={() => candidateAction(candidate.id, 'ignored')}>{locale === 'zh-CN' ? '忽略本集候选' : 'Ignore candidate'}</button></div></article>)}</div>{!candidates.length && <div className="empty-result">{locale === 'zh-CN' ? '本集没有候选记录，请重新运行分析。' : 'No persisted candidates. Run analysis again.'}</div>}</section>}</>
}

function LegacyEpisodeAnalyzeStep({ project, episodeId, onChange }: { project: Project; episodeId: string; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const { locale } = useI18n(); const [candidateError, setCandidateError] = useState(''); const episode = project.episodes?.find((item) => item.id === episodeId); const candidates = project.characters.filter((character) => character.created_from_episode_id === episodeId && character.priority_level === 'Temporary')
  async function updateCandidate(character: Character, patch: Partial<Character>) { try { const updated = await api<Character>(`/characters/${character.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(characterPayload(character, patch)) }); onChange({ ...project, characters: project.characters.map((item) => item.id === updated.id ? updated : item) }) } catch (reason) { setCandidateError((reason as Error).message) } }
  return <><CoreEpisodeAnalyzeStep project={project} episodeId={episodeId} onChange={onChange} />{episode?.analysis_status === 'completed' && <section className="incremental-review"><div className="section-heading-inline"><div><span className="section-label">{locale === 'zh-CN' ? '本集增量角色复核（Mock）' : 'Incremental Character Review (Mock)'}</span><h2>{locale === 'zh-CN' ? '只处理新增或不确定实体' : 'Review only new or uncertain entities'}</h2></div><button className="secondary-button compact" onClick={() => navigate(flowRoute(project.id, 'characters', episodeId))}><Plus size={14} /> {locale === 'zh-CN' ? '手动添加角色' : 'Add manually'}</button></div><p className="reference-guidance">{locale === 'zh-CN' ? '框选和身份匹配为 Mock 数据，不代表真实人物识别。已锁定角色不会重复创建 ID。' : 'Boxes and identity matches are Mock data. Locked identities are not duplicated.'}</p>{candidateError && <InlineError message={candidateError} />}<div className="candidate-grid">{(episode.matches || []).map((match) => <div className="candidate-row matched" key={match.id || match.character_id}><span>{match.detected_name}</span><strong>{match.character_code || match.character_id}</strong><small>{locale === 'zh-CN' ? 'Mock 匹配' : 'Mock match'} {Math.round(match.confidence * 100)}%</small></div>)}{candidates.map((character) => <div className="candidate-row" key={character.id}><span>{character.name}</span><select value={character.entity_type || (character.character_type === 'Creature' ? 'Animal' : 'Human')} onChange={(event) => updateCandidate(character, { character_type: event.target.value as Character['character_type'], entity_type: event.target.value as Character['entity_type'] })}><option value="Human">Human</option><option value="Pet">Pet</option><option value="Animal">Animal</option><option value="Other">Other</option></select><button className="text-button" onClick={() => updateCandidate(character, { priority_level: 'Background' })}>{locale === 'zh-CN' ? '标为背景' : 'Background'}</button><button className="text-button" onClick={() => navigate(flowRoute(project.id, 'characters', episodeId))}>{locale === 'zh-CN' ? '进入角色资产确认' : 'Review asset'}</button></div>)}</div>{!candidates.length && !(episode.matches || []).length && <div className="empty-result">{locale === 'zh-CN' ? '本集没有新增候选或匹配结果。' : 'No new candidates or matches in this episode.'}</div>}</section>}</>
}

function CoreEpisodeAnalyzeStep({ project, episodeId, onChange }: { project: Project; episodeId: string; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const { t, formatDuration } = useI18n(); const episode = project.episodes?.find((item) => item.id === episodeId); const [status, setStatus] = useState<AnalyzeStatus>(episode?.analysis_status || 'not_started'); const [error, setError] = useState('')
  if (!episode) return <InlineError message={`Episode ${episodeId} was not found in project ${project.id}.`} />
  const selectedEpisode = episode
  async function analyze() { setStatus('processing'); setError(''); try { const response = await api<{ project: Project }>(`/episodes/${selectedEpisode.id}/analyze`, { method: 'POST' }); onChange(response.project); setStatus(response.project.characters.length ? 'completed' : 'warning') } catch (e) { setStatus('failed'); setError((e as Error).message) } }
  const labels: Record<AnalyzeStatus, string> = { not_started: t('status.not_started'), processing: t('status.processing'), completed: t('status.completed'), warning: t('status.warning'), failed: t('status.failed') }
  const hasCharacters = project.characters.length > 0
  return <div className="step-content"><DemoNotice>{t('analyze.demoNotice')}</DemoNotice><div className="analysis-head"><p className="muted-copy">{t('analyze.copy')}</p><button className="secondary-button" disabled={status === 'processing' || !episode.video_filename} onClick={analyze}>{status === 'processing' ? <LoaderCircle className="spin" size={16} /> : <Gauge size={16} />} {status === 'processing' ? t('analyze.analyzing') : status === 'not_started' ? t('analyze.analyzeSource') : t('analyze.runAgain')}</button></div>{error && <InlineError message={error} />}<div className="analysis-grid"><section className="analysis-video"><div className="panel-topline"><span className="panel-label">{t('analyze.videoAnalysis')}</span><span className={`analysis-status analysis-status-${status}`}><span className="status-dot" /> {labels[status]}</span></div><video className="responsive-video" controls preload="metadata" playsInline src={episodeVideoUrl(episode)} /><div className="video-stats"><Stat label={t('analyze.duration')} value={formatDuration(episode.duration)} /><Stat label={t('analyze.aspect')} value={episode.aspect_ratio ? `${episode.aspect_ratio.toFixed(3)} · ${episode.orientation}` : t('analyze.reading')} /><Stat label={t('analyze.castDetection')} value={status === 'not_started' ? (t('status.not_started')) : String(episode.matched_count ?? 0)} /></div></section><section className="analysis-results"><div className="result-section-heading"><div><span className="section-label">{t('analyze.castDetection')}</span><h3>{hasCharacters ? `${project.characters.length} characters` : t('analyze.runToPopulate')}</h3></div></div>{hasCharacters ? <div className="character-mini-list">{project.characters.map((character, index) => <div className="character-mini" key={character.id}><MockAvatar index={index} /><div><strong>{character.name}</strong><span>{character.role}</span></div>{status === 'completed' && <Check className="green-check" size={16} />}</div>)}</div> : <div className="empty-result">{status === 'warning' ? t('analyze.noCharacters') : t('analyze.runToPopulate')}</div>}</section></div><div className="step-footer"><span className={`analysis-footer analysis-footer-${status}`}>{labels[status]}</span><button className="primary-button" disabled={!hasCharacters || status !== 'completed'} onClick={() => navigate(flowRoute(project.id, 'characters', episode.id))}>{t('analyze.reviewCharacters')} <ArrowRight size={16} /></button></div></div>
}

function AnalyzeStepCore({ project, episodeId, onChange }: { project: Project; episodeId?: string; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const { t, formatDuration } = useI18n(); const episode = episodeId ? project.episodes?.find((item) => item.id === episodeId) : undefined; const initialStatus: AnalyzeStatus = episode?.analysis_status || (project.characters.length ? 'completed' : 'not_started'); const [status, setStatus] = useState<AnalyzeStatus>(initialStatus); const [error, setError] = useState(''); const hasCharacters = project.characters.length > 0
  if (episodeId) return <EpisodeAnalyzeStep project={project} episodeId={episodeId} onChange={onChange} />
  async function analyze() { setStatus('processing'); setError(''); try { const response = await api<{ project: Project }>(episodeId ? `/episodes/${episodeId}/analyze` : `/projects/${project.id}/analyze`, { method: 'POST' }); onChange(response.project); setStatus(response.project.characters.length ? 'completed' : 'warning') } catch (e) { setStatus('failed'); setError((e as Error).message) } }
  const labels: Record<AnalyzeStatus, string> = { not_started: t('status.not_started'), processing: t('status.processing'), completed: t('status.completed'), warning: t('status.warning'), failed: t('status.failed') }
  const footer: Record<AnalyzeStatus, string> = { not_started: t('analyze.footerNotStarted'), processing: t('analyze.footerProcessing'), completed: t('analyze.footerReady'), warning: t('analyze.footerWarning'), failed: t('analyze.footerFailed') }
  return <div className="step-content"><DemoNotice>{t('analyze.demoNotice')}</DemoNotice><div className="analysis-head"><p className="muted-copy">{t('analyze.copy')}</p><button className="secondary-button" disabled={status === 'processing' || !project.video_filename} onClick={analyze}>{status === 'processing' ? <LoaderCircle className="spin" size={16} /> : status === 'failed' || status === 'warning' ? <RotateCcw size={16} /> : <Gauge size={16} />} {status === 'processing' ? t('analyze.analyzing') : hasCharacters ? t('analyze.runAgain') : status === 'failed' || status === 'warning' ? t('analyze.retry') : t('analyze.analyzeSource')}</button></div>{error && <InlineError message={error} />}<div className="analysis-grid"><section className="analysis-video"><div className="panel-topline"><span className="panel-label">{t('analyze.videoAnalysis')}</span><span className={`analysis-status analysis-status-${status}`}><span className="status-dot" /> {labels[status]}</span></div>{project.video_filename ? <ResponsiveVideo src={videoUrl(project)} project={project} onChange={onChange} /> : <div className="empty-video"><Film size={23} /> {t('upload.start')}</div>}<div className="video-stats"><Stat label={t('analyze.duration')} value={formatDuration(project.video_duration)} /><Stat label={t('analyze.language')} value={project.source_language || t('common.unknown')} /><Stat label={t('analyze.aspect')} value={project.aspect_ratio ? `${project.aspect_ratio.toFixed(3)} · ${project.orientation}` : t('analyze.reading')} /></div></section><section className="analysis-results"><div className="result-section-heading"><div><span className="section-label">{t('analyze.castDetection')}</span><h3>{t('analyze.characters', { count: project.characters.length || '—' })}</h3></div><span className="confidence-chip">{status === 'completed' ? t('analyze.confidence') : t('analyze.pending')}</span></div>{hasCharacters ? <div className="character-mini-list">{project.characters.map((character, index) => <div className="character-mini" key={character.id}><MockAvatar index={index} /><div><strong>{character.name}</strong><span>{character.role} · {character.gender} · {character.estimated_age}</span></div>{status === 'completed' && <Check className="green-check" size={16} />}</div>)}</div> : <div className="empty-result">{status === 'warning' ? t('analyze.noCharacters') : t('analyze.runToPopulate')}</div>}<div className="result-section-heading scene-heading"><div><span className="section-label">{t('analyze.sceneDetection')}</span><h3>{t('analyze.environments', { count: project.scenes.length || '—' })}</h3></div></div>{project.scenes.length ? <div className="scene-list">{project.scenes.map((scene) => <div className="scene-row" key={scene.id}><MockFrame variant={scene.id.includes('street') ? 'street' : 'cafe'} label={scene.location.toUpperCase()} /><div><strong>{scene.name}</strong><span>{scene.description}</span><small>{scene.main_objects.join(' · ')}</small></div></div>)}</div> : <div className="empty-result">{t('analyze.scenesAppear')}</div>}</section></div><div className="step-footer"><span className={`analysis-footer analysis-footer-${status}`}>{status === 'completed' && <Check size={15} />} {footer[status]}</span><button className="primary-button" disabled={!hasCharacters || status !== 'completed'} onClick={() => navigate(`/project/${project.id}/characters`)}>{t('analyze.reviewCharacters')} <ArrowRight size={16} /></button></div></div>
}

function LegacyAnalyzeStepCore({ project, onChange }: { project: Project; onChange: (p: Project) => void }) {
  const navigate = useNavigate()
  const [status, setStatus] = useState<AnalyzeStatus>(project.characters.length ? 'completed' : 'not_started')
  const [error, setError] = useState('')
  const hasCharacters = project.characters.length > 0
  async function analyze() {
    setStatus('processing')
    setError('')
    try {
      const response = await api<{ project: Project }>(`/projects/${project.id}/analyze`, { method: 'POST' })
      onChange(response.project)
      setStatus(response.project.characters.length ? 'completed' : 'warning')
    } catch (e) {
      setStatus('failed')
      setError((e as Error).message)
    }
  }
  const statusLabels: Record<AnalyzeStatus, string> = { not_started: 'Not started', processing: 'Processing', completed: 'Completed', warning: 'Needs attention', failed: 'Failed' }
  const statusMessage = status === 'warning' ? 'No characters were detected. Review the source and retry.' : status === 'failed' ? 'Analysis failed. Retry after checking the error details.' : status === 'completed' ? 'Mock analysis completed with characters ready for review.' : status === 'processing' ? 'The mock analysis is processing this source.' : 'Run the mock analysis to detect characters and scenes.'
  return <div className="step-content"><div className="analysis-head"><div><p className="muted-copy">A quick mock pass identifies the cast and the visual environments your story moves through.</p></div><button className="secondary-button" disabled={status === 'processing' || !project.video_filename} onClick={analyze}>{status === 'processing' ? <LoaderCircle className="spin" size={16} /> : status === 'failed' || status === 'warning' ? <RotateCcw size={16} /> : <Gauge size={16} />} {status === 'processing' ? 'Analyzing...' : hasCharacters ? 'Run analysis again' : status === 'failed' || status === 'warning' ? 'Retry analysis' : 'Analyze source'}</button></div>{error && <InlineError message={error} />}<div className="analysis-grid"><section className="analysis-video"><div className="panel-topline"><span className="panel-label">VIDEO ANALYSIS</span><span className={`analysis-status analysis-status-${status}`}><span className="status-dot" /> {statusLabels[status]}</span></div>{project.video_filename ? <ResponsiveVideo src={videoUrl(project)} project={project} onChange={onChange} /> : <div className="empty-video"><Film size={23} /> Upload a source video first</div>}<div className="video-stats"><Stat label="Duration" value={formatDuration(project.video_duration)} /><Stat label="Language" value={project.source_language === 'mandarin' ? 'Mandarin' : project.source_language || 'Pending'} /><Stat label="Aspect" value={project.aspect_ratio ? `${project.aspect_ratio.toFixed(3)} · ${project.orientation}` : 'Reading…'} /></div></section><section className="analysis-results"><div className="result-section-heading"><div><span className="section-label">CAST DETECTION</span><h3>{project.characters.length || '—'} key characters</h3></div><span className="confidence-chip">{status === 'completed' ? '94% confidence' : 'Pending'}</span></div>{hasCharacters ? <div className="character-mini-list">{project.characters.map((character, index) => <div className="character-mini" key={character.id}><MockAvatar index={index} /><div><strong>{character.name}</strong><span>{character.role} · {character.gender} · {character.estimated_age}</span></div>{status === 'completed' && <Check className="green-check" size={16} />}</div>)}</div> : <div className="empty-result">{status === 'warning' ? 'No characters detected in this pass.' : 'Run analysis to populate the character review.'}</div>}<div className="result-section-heading scene-heading"><div><span className="section-label">SCENE DETECTION</span><h3>{project.scenes.length || '—'} environments</h3></div></div>{project.scenes.length ? <div className="scene-list">{project.scenes.map((scene) => <div className="scene-row" key={scene.id}><MockFrame variant={scene.id.includes('street') ? 'street' : 'cafe'} label={scene.location.toUpperCase()} /><div><strong>{scene.name}</strong><span>{scene.description}</span><small>{scene.main_objects.join(' · ')}</small></div></div>)}</div> : <div className="empty-result">Scenes will appear with the character pass.</div>}</section></div><div className="step-footer"><span className={`analysis-footer analysis-footer-${status}`}>{status === 'completed' ? <><Check size={15} /> {statusMessage}</> : statusMessage}</span><button className="primary-button" disabled={!hasCharacters || status !== 'completed'} onClick={() => navigate(`/project/${project.id}/characters`)}>Review characters <ArrowRight size={16} /></button></div></div>
}

async function captureCurrentVideoFrame(video: HTMLVideoElement): Promise<{ dataUrl: string; timestamp: number; filename: string }> {
  if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || !video.videoWidth || !video.videoHeight || video.seeking) {
    throw new Error('CURRENT_VIDEO_FRAME_NOT_READY')
  }
  video.pause()
  const timestamp = video.currentTime
  const canvas = document.createElement('canvas')
  canvas.width = video.videoWidth
  canvas.height = video.videoHeight
  const context = canvas.getContext('2d')
  if (!context) throw new Error('CURRENT_VIDEO_FRAME_CAPTURE_FAILED')
  context.drawImage(video, 0, 0, canvas.width, canvas.height)
  const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob((value) => value ? resolve(value) : reject(new Error('CURRENT_VIDEO_FRAME_CAPTURE_FAILED')), 'image/png'))
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('CURRENT_VIDEO_FRAME_CAPTURE_FAILED'))
    reader.readAsDataURL(blob)
  })
  return { dataUrl, timestamp, filename: `current-frame-${timestamp.toFixed(3).replace('.', '-')}.png` }
}

function CharactersStep({ project, episode, sourceVideoRef, onChange }: { project: Project; episode?: Episode; sourceVideoRef: React.RefObject<HTMLVideoElement>; onChange: (p: Project) => void }) {
  const { locale } = useI18n(); const [assetError, setAssetError] = useState(''); const [working, setWorking] = useState(false)
  async function generateAllStyled() { setWorking(true); setAssetError(''); try { const response = await api<{ characters: Character[] }>(`/projects/${project.id}/characters/mock-styled-assets`, { method: 'POST' }); onChange({ ...project, characters: response.characters }) } catch (reason) { setAssetError((reason as Error).message) } finally { setWorking(false) } }
  async function confirmAll() { setWorking(true); setAssetError(''); try { const response = await api<{ characters: Character[]; skipped: unknown[] }>(`/projects/${project.id}/characters/confirm-all`, { method: 'POST' }); onChange({ ...project, characters: response.characters }); if (response.skipped.length) setAssetError(locale === 'zh-CN' ? '部分角色缺少有效名称或原视频主参考图，未被锁定。' : 'Some characters still need a valid name or primary source reference.') } catch (reason) { setAssetError((reason as Error).message) } finally { setWorking(false) } }
  async function addFromCurrentFrame(character: Character) {
    const video = sourceVideoRef.current
    if (!episode || !video) {
      setAssetError(locale === 'zh-CN' ? '请先加载当前单集的原视频。' : 'Load the current Episode source video first.')
      return
    }
    setWorking(true)
    setAssetError('')
    try {
      const frame = await captureCurrentVideoFrame(video)
      const saved = await api<Character>(`/projects/${project.id}/characters/${character.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...character, original_reference_image: frame.dataUrl, primary_reference_type: character.primary_reference_type || 'original_reference_image' }),
      })
      onChange({ ...project, characters: project.characters.map((item) => item.id === saved.id ? saved : item) })
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason)
      setAssetError(message === 'CURRENT_VIDEO_FRAME_NOT_READY'
        ? (locale === 'zh-CN' ? '视频尚未加载到可截帧状态，请等待加载完成并重新选择时间点。' : 'The video is not ready to capture. Wait for it to load and choose the time again.')
        : (locale === 'zh-CN' ? '无法截取当前画面，请确认原视频已加载后重试。' : 'Unable to capture the current frame. Make sure the source video is loaded and try again.'))
    } finally {
      setWorking(false)
    }
  }
  return <><DemoNotice>{project.processing_mode === 'character-consistency' ? (locale === 'zh-CN' ? '角色卡以原视频主参考图作为身份基础。请锁定 1–5 个核心角色，后续剧集将默认复用。' : 'Character cards use source frames as identity anchors. Lock 1–5 core roles for later episode reuse.') : (locale === 'zh-CN' ? 'Quick 模式中的角色卡仅服务当前单集，不启用跨集角色复用。' : 'Character cards in Quick mode apply only to this episode and are not reused across episodes.')}</DemoNotice><section className="character-asset-toolbar"><div><strong>{locale === 'zh-CN' ? '主参考图 + 名称 + 锁定' : 'Primary reference + name + lock'}</strong><span>{locale === 'zh-CN' ? '备注、发型、服装和多视角参考均为可选补充，不是确认门槛。' : 'Notes, hair, clothing, and multi-view references are optional and do not block confirmation.'}</span></div><div><button className="secondary-button compact" disabled={working} onClick={generateAllStyled}><WandSparkles size={14} /> {locale === 'zh-CN' ? 'Mock 生成全部风格参考' : 'Mock generate all'}</button><button className="secondary-button compact" disabled={working} onClick={confirmAll}><Check size={14} /> {locale === 'zh-CN' ? '锁定全部合格角色' : 'Lock valid characters'}</button></div></section>{assetError && <InlineError message={assetError} />}<CoreCharactersStep project={project} onCaptureCurrentFrame={addFromCurrentFrame} onChange={onChange} /></>
}

function CoreCharactersStep({ project, onCaptureCurrentFrame, onChange }: { project: Project; onCaptureCurrentFrame: (character: Character) => Promise<void>; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const { t } = useI18n(); const [saving, setSaving] = useState(''); const [error, setError] = useState('')
  async function saveCharacter(character: Character, patch: Partial<Character> = {}) { const next = { ...character, ...patch }; setSaving(character.id); setError(''); try { const saved = await api<Character>(`/projects/${project.id}/characters/${character.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(next) }); onChange({ ...project, characters: project.characters.map((item) => item.id === saved.id ? saved : item) }); return saved } catch (e) { setError((e as Error).message); return undefined } finally { setSaving('') } }
  async function addCharacter() { const number = project.characters.length + 1; try { const added = await api<Character>(`/projects/${project.id}/characters`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: `Character ${String(number).padStart(2, '0')}`, appearance_description: '', identity_description: '', clothing_description: '' }) }); onChange({ ...project, characters: [...project.characters, added] }) } catch (e) { setError((e as Error).message) } }
  async function removeCharacter(id: string) { try { await api(`/projects/${project.id}/characters/${id}`, { method: 'DELETE' }); onChange({ ...project, characters: project.characters.filter((item) => item.id !== id) }) } catch (e) { setError((e as Error).message) } }
  const confirmed = project.characters.filter((item) => item.confirmed).length
  return <div className="step-content"><div className="analysis-head"><p className="muted-copy">{t('characters.copy')}</p><button className="secondary-button" onClick={addCharacter}><Plus size={16} /> {t('characters.add')}</button></div>{error && <InlineError message={error} />}<div className="character-stack">{project.characters.map((character, index) => <CharacterCardWithTarget key={character.id} character={character} index={index} saving={saving === character.id} onSave={saveCharacter} onRemove={removeCharacter} onCaptureCurrentFrame={() => onCaptureCurrentFrame(character)} />)}</div>{project.characters.length === 0 && <div className="empty-result character-empty"><Users size={20} /><span>{t('characters.empty')}</span><button className="secondary-button" onClick={addCharacter}><Plus size={15} /> {t('characters.add')}</button></div>}<QuickSkipAction project={project} /><div className="step-footer"><span>{t('characters.confirmedCount', { confirmed, total: project.characters.length })}</span><button className="primary-button" disabled={project.processing_mode === 'character-consistency' && confirmed < 1} onClick={() => navigate(`/project/${project.id}/settings`)}>{t('characters.continue')} <ArrowRight size={16} /></button></div></div>
}

function QuickSkipAction({ project }: { project: Project }) {
  const navigate = useNavigate(); const { locale } = useI18n()
  if (project.processing_mode !== 'quick-transformation') return null
  return <div className="quick-skip-row"><div><strong>{roundTwoText(locale, 'skipCharacters')}</strong><span>{roundTwoText(locale, 'skipCharactersCopy')}</span></div><button className="secondary-button" onClick={() => navigate(`/project/${project.id}/settings`)}><ArrowRight size={15} /> {roundTwoText(locale, 'skipCharacters')}</button></div>
}

function CharacterEditorLocalized({ character, index, saving, onSave, onRemove }: { character: Character; index: number; saving: boolean; onSave: (c: Character, patch?: Partial<Character>) => void; onRemove: (id: string) => void }) {
  const { t } = useI18n(); const [draft, setDraft] = useState(character); const [expanded, setExpanded] = useState(true)
  useEffect(() => setDraft(character), [character])
  function patch(key: keyof Character, value: string | boolean) { setDraft((old) => ({ ...old, [key]: value })) }
  function upload(field: 'original_reference_image' | 'front_view_image' | 'angle_view_image' | 'side_view_image' | 'full_body_image', file?: File) { if (!file) return; const reader = new FileReader(); reader.onload = () => { const value = String(reader.result); patch(field, value); onSave({ ...draft, [field]: value }) }; reader.readAsDataURL(file) }
  const ref = (field: 'front_view_image' | 'angle_view_image' | 'side_view_image' | 'full_body_image', label: string) => <ReferenceUploader label={label} value={draft[field]} onUpload={(value) => { patch(field, value); onSave({ ...draft, [field]: value }) }} />
  return <article className={`character-card ${draft.confirmed ? 'confirmed' : ''}`}><div className="character-card-head"><div className="character-heading"><MockAvatar index={index} /><div><div className="card-id">{t('characters.referenceAsset')} {String(index + 1).padStart(2, '0')} <span>/{draft.id}</span></div><h3>{draft.name}</h3><span>{draft.role} · {draft.gender} · {draft.estimated_age}</span></div></div><div className="character-actions"><span className={`confirm-state ${draft.confirmed ? 'is-confirmed' : ''}`}>{draft.confirmed ? <Check size={14} /> : <span className="hollow-dot" />}{draft.confirmed ? t('characters.confirmed') : t('characters.needsReview')}</span><button className="icon-button" title={t('characters.remove')} onClick={() => onRemove(draft.id)}><Trash2 size={16} /></button><button className="icon-button" title={expanded ? t('characters.collapse') : t('characters.expand')} onClick={() => setExpanded(!expanded)}>{expanded ? <ChevronDown size={17} /> : <ChevronRight size={17} />}</button></div></div>{expanded && <div className="character-card-body"><div className="asset-column"><span className="section-label">{t('characters.referenceAsset')}</span><div className="reference-grid"><MockFrame label={t('characters.originalFrame')} variant={index === 0 ? 'portrait' : 'portrait-alt'} /><label className="reference-upload">{draft.original_reference_image ? <img src={draft.original_reference_image} alt={t('characters.uploadReference')} /> : <><Upload size={17} /><span>{t('characters.uploadReference')}</span><small>PNG, JPG</small></>}<input type="file" accept="image/png,image/jpeg" onChange={(e) => upload('original_reference_image', e.target.files?.[0])} /></label></div><div className="asset-views"><MockFrame label={t('characters.front')} variant={index === 0 ? 'portrait' : 'portrait-alt'} /><MockFrame label={t('characters.angle')} variant={index === 0 ? 'angle' : 'angle-alt'} /><MockFrame label={t('characters.side')} variant="side" /><MockFrame label={t('characters.fullBody')} variant="full" /></div><div className="reference-upload-grid">{ref('front_view_image', t('characters.front'))}{ref('angle_view_image', t('characters.angle'))}{ref('side_view_image', t('characters.side'))}{ref('full_body_image', t('characters.fullBody'))}</div></div><div className="character-fields"><div className="field-grid"><label className="field-label">{t('characters.name')}<input value={draft.name} onChange={(e) => patch('name', e.target.value)} /></label><label className="field-label">{t('characters.role')}<select value={draft.role} onChange={(e) => patch('role', e.target.value)}><option>Lead</option><option>Supporting</option><option>Extra</option></select></label></div><label className="field-label">{t('characters.appearance')}<input value={draft.appearance_description} onChange={(e) => patch('appearance_description', e.target.value)} /></label><label className="field-label">{t('characters.hair')}<input value={draft.hair_description} onChange={(e) => patch('hair_description', e.target.value)} /></label><label className="field-label">{t('characters.clothing')}<input value={draft.clothing_description} onChange={(e) => patch('clothing_description', e.target.value)} /></label><div className="inline-save"><button className={`secondary-button ${draft.confirmed ? 'active-button' : ''}`} onClick={() => { patch('confirmed', !draft.confirmed); onSave(draft, { confirmed: !draft.confirmed }) }}>{draft.confirmed ? <Check size={15} /> : <UserRound size={15} />} {draft.confirmed ? t('characters.confirmed') : t('characters.confirm')}</button><button className="text-button" disabled={saving} onClick={() => onSave(draft)}>{saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />} {t('characters.saveChanges')}</button></div></div></div>}</article>
}

function ReferenceAssetEditor({ character, index, saving, onSave, onRemove }: { character: Character; index: number; saving: boolean; onSave: (c: Character, patch?: Partial<Character>) => Promise<Character | undefined> | void; onRemove: (id: string) => void }) {
  const { t, locale } = useI18n(); const [draft, setDraft] = useState(character); const [validationError, setValidationError] = useState('')
  useEffect(() => setDraft(character), [character])
  const slots: Array<{ field: ReferenceField; label: string }> = [{ field: 'original_reference_image', label: t('characters.originalFrame') }, { field: 'front_view_image', label: t('characters.front') }, { field: 'angle_view_image', label: t('characters.angle') }, { field: 'side_view_image', label: t('characters.side') }, { field: 'full_body_image', label: t('characters.fullBody') }]
  const rawEntityType = draft.entity_type || draft.character_type || 'Other'; const entityTypeValue = ['Human', 'Pet', 'Animal', 'Other'].includes(rawEntityType) ? rawEntityType : 'Other'
  function save(patch: Partial<Character>) { const next = { ...draft, ...patch }; setDraft(next); void onSave(next, patch) }
  function uploadTarget(value: string, filename: string) { save({ target_reference_path: value }); if ((draft.character_type || 'Human') === 'Human' && /(?:creature|animal|dog|cat|wolf|fox|monster|beast|bird|horse|lion|tiger|猫|狗|动物|生物)/i.test(filename)) setValidationError(t('characters.creatureWarning')); else setValidationError('') }
  function upload(field: ReferenceField, value: string, filename: string) { if (draft.confirmed && draft.primary_reference_type === field && !window.confirm(t('characters.primaryChangeWarning'))) return; save({ [field]: value, primary_reference_type: draft.primary_reference_type || field }); if ((draft.character_type || 'Human') === 'Human' && /(?:creature|animal|dog|cat|wolf|fox|monster|beast|bird|horse|lion|tiger|\u732b|\u72d7|\u52a8\u7269|\u751f\u7269)/i.test(filename)) setValidationError(t('characters.creatureWarning')); else setValidationError('') }
  function removeImage(field: ReferenceField) { const nextPrimary = draft.primary_reference_type === field ? slots.map((slot) => slot.field).find((candidate) => candidate !== field && isRenderableReference(draft[candidate])) || null : draft.primary_reference_type; save({ [field]: null, primary_reference_type: nextPrimary as ReferenceField | null }) }
  function setPrimary(field: ReferenceField) { if (!isRenderableReference(draft[field])) return; if (draft.confirmed && draft.primary_reference_type !== field && !window.confirm(t('characters.primaryChangeWarning'))) return; save({ primary_reference_type: field }) }
  function validateConfirmation(target = draft) { const name = target.name.trim(); const entity = target.entity_type || target.character_type; const targetReference = target.target_reference_path || target.main_reference_image; if (!name || ['name', '姓名', '角色名', 'new character', '新角色'].includes(name.toLowerCase()) || name.length < 2) return locale === 'zh-CN' ? '请填写有效角色名称。' : 'Enter a valid character name.'; if (!['Human', 'Pet', 'Animal', 'Other'].includes(entity || '')) return locale === 'zh-CN' ? '实体类型只能是 Human、Pet、Animal 或 Other。' : 'Entity type must be Human, Pet, Animal, or Other.'; if (!isRenderableReference(targetReference)) return locale === 'zh-CN' ? '请先上传目标角色参考图。' : 'Please upload a target reference image before confirming this character.'; return '' }
  async function toggleConfirmed() { if (draft.confirmed) { try { const updated = await api<Character>(`/characters/${draft.id}/unconfirm`, { method: 'POST' }); setDraft(updated); await onSave(updated, {}) } catch (reason) { setValidationError((reason as Error).message) }; return }; const saved = await onSave(draft, { confirmed: false, priority_level: 'Temporary' }); if (!saved) return; setDraft(saved); const message = validateConfirmation(saved); if (message) { setValidationError(message); return }; try { const updated = await api<Character>(`/characters/${saved.id}/confirm`, { method: 'POST' }); setDraft(updated); setValidationError(''); await onSave(updated, {}) } catch (reason) { setValidationError((reason as Error).message) } }
  async function generateStyled() { try { const updated = await api<Character>(`/characters/${draft.id}/mock-styled-assets`, { method: 'POST' }); setDraft(updated); setValidationError('') } catch (reason) { setValidationError((reason as Error).message) } }
  return <article className={`character-card ${draft.confirmed ? 'confirmed' : ''}`}><div className="character-card-head"><div className="character-heading"><MockAvatar index={index} /><div><div className="card-id">{t('characters.referenceAsset')} {String(index + 1).padStart(2, '0')} <span>/{draft.character_code || draft.id}</span></div><h3>{draft.name}</h3><span>{draft.role} · {draft.gender} · {draft.estimated_age}</span></div></div><div className="character-actions"><span className={`confirm-state ${draft.confirmed ? 'is-confirmed' : ''}`}>{draft.confirmed ? <Check size={14} /> : <span className="hollow-dot" />}{draft.confirmed ? t('characters.confirmed') : t('characters.needsReview')}</span><button className="icon-button" title={t('characters.remove')} onClick={() => onRemove(draft.id)}><Trash2 size={16} /></button></div></div><div className="character-card-body"><div className="asset-column"><span className="section-label">{t('characters.referenceAsset')}</span><p className="reference-guidance">{t('characters.referenceGuidance')}</p><div className="reference-upload-grid reference-assets-grid">{slots.map((slot) => <ReferenceAssetUploader key={slot.field} label={slot.label} value={draft[slot.field]} isPrimary={draft.primary_reference_type === slot.field} characterType={draft.character_type || 'Human'} onUpload={(value, filename) => upload(slot.field, value, filename)} onDelete={() => removeImage(slot.field)} onSetPrimary={() => setPrimary(slot.field)} t={t} />)}</div></div><div className="character-fields"><div className="field-grid"><label className="field-label">{t('characters.name')}<input value={draft.name} onChange={(e) => setDraft((old) => ({ ...old, name: e.target.value }))} /></label><label className="field-label">{t('characters.role')}<select value={draft.role} onChange={(e) => setDraft((old) => ({ ...old, role: e.target.value }))}><option value="Lead">{locale === 'zh-CN' ? '主角' : 'Lead'}</option><option value="Supporting">{locale === 'zh-CN' ? '配角' : 'Supporting'}</option><option value="Other">{locale === 'zh-CN' ? '其他' : 'Other'}</option></select></label></div><label className="field-label">{t('characters.characterType')}<select value={entityTypeValue} onChange={(e) => setDraft((old) => ({ ...old, character_type: e.target.value as Character['character_type'], entity_type: e.target.value as Character['entity_type'] }))}><option value="Human">{t('characters.typeHuman')}</option><option value="Pet">Pet</option><option value="Animal">Animal</option><option value="Other">{t('characters.typeOther')}</option></select></label><label className="field-label">{t('characters.appearance')}<textarea value={draft.appearance_description} onChange={(e) => setDraft((old) => ({ ...old, appearance_description: e.target.value, identity_description: e.target.value }))} /></label><label className="field-label">{t('characters.hair')}<input value={draft.hair_description} onChange={(e) => setDraft((old) => ({ ...old, hair_description: e.target.value }))} /></label><label className="field-label">{t('characters.clothing')}<input value={draft.clothing_description} onChange={(e) => setDraft((old) => ({ ...old, clothing_description: e.target.value }))} /></label><div className="inline-save"><button className={`secondary-button ${draft.confirmed ? 'active-button' : ''}`} onClick={toggleConfirmed}>{draft.confirmed ? <Check size={15} /> : <UserRound size={15} />} {draft.confirmed ? t('characters.confirmed') : t('characters.confirm')}</button><button className="text-button" disabled={saving} onClick={() => save({})}>{saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />} {t('characters.saveChanges')}</button></div>{validationError && <div className="asset-warning">{validationError}</div>}<small className="confirmation-requirements">{t('characters.confirmRequirements')}</small></div></div></article>
}

function CharacterCardWithTarget({ character, index, saving, onSave, onRemove, onCaptureCurrentFrame }: { character: Character; index: number; saving: boolean; onSave: (c: Character, patch?: Partial<Character>) => Promise<Character | undefined> | void; onRemove: (id: string) => void; onCaptureCurrentFrame: () => Promise<void> }) {
  const { locale, t } = useI18n(); const [target, setTarget] = useState(character.target_reference_path || ''); const [error, setError] = useState('')
  useEffect(() => setTarget(character.target_reference_path || ''), [character.target_reference_path])
  function upload(value: string, filename: string) { setTarget(value); setError(''); void onSave({ ...character, target_reference_path: value }, { target_reference_path: value }) }
  function remove() { setTarget(''); void onSave({ ...character, target_reference_path: undefined }, { target_reference_path: undefined }) }
  return <div className="character-card-with-target"><div className="character-current-frame-action"><button className="secondary-button compact" disabled={saving} onClick={() => void onCaptureCurrentFrame()}><Clapperboard size={14} /> {locale === 'zh-CN' ? '从当前帧添加' : 'Add from current frame'}</button><span>{locale === 'zh-CN' ? '保存播放器当前时间点到这个角色，不会替换目标参考图。' : 'Save the player\'s current frame to this character without replacing the target reference.'}</span></div><ReferenceAssetEditor character={character} index={index} saving={saving} onSave={onSave} onRemove={onRemove} /><div className="target-reference-panel"><div><strong>{locale === 'zh-CN' ? '目标参考图' : 'Target reference image'}</strong><span>{locale === 'zh-CN' ? '上传你希望 Xmax 使用的可信参考图。' : 'Upload the trusted reference image Xmax should use.'}</span></div><ReferenceAssetUploader label={locale === 'zh-CN' ? '上传目标图' : 'Upload target'} value={target} isPrimary={true} characterType={character.character_type || 'Human'} onUpload={upload} onDelete={remove} onSetPrimary={() => undefined} t={t} />{error && <small className="asset-warning">{error}</small>}</div></div>
}

function isRenderableReference(value?: string | null) { return Boolean(value && (/^data:image\//i.test(value) || /^https?:\/\//i.test(value) || /^\/uploads\//i.test(value))) }

function ReferenceAssetUploader({ label, value, isPrimary, characterType, onUpload, onDelete, onSetPrimary, t }: { label: string; value?: string; isPrimary: boolean; characterType: string; onUpload: (value: string, filename: string) => void; onDelete: () => void; onSetPrimary: () => void; t: ReturnType<typeof useI18n>['t'] }) {
  const [preview, setPreview] = useState(value || ''); const [imageFailed, setImageFailed] = useState(false); const renderable = isRenderableReference(preview) && !imageFailed
  useEffect(() => { setPreview(value?.startsWith('/uploads/') ? assetUrl(value) : value || ''); setImageFailed(false) }, [value])
  function handle(file?: File) { if (!file) return; const reader = new FileReader(); reader.onload = () => { const next = String(reader.result); setPreview(next); onUpload(next, file.name) }; reader.readAsDataURL(file) }
  return <div className="reference-asset-slot"><div className="reference-asset-preview">{renderable ? <img src={preview} alt={`${label} reference`} onError={() => setImageFailed(true)} /> : <><Upload size={15} /><span>{t('characters.unuploaded')}</span></>}</div><div className="reference-asset-title"><span>{label}</span>{isPrimary && <strong>{t('characters.primary')}</strong>}</div><div className="reference-asset-actions"><label className="text-button"><Upload size={13} /> {renderable ? t('characters.replace') : t('characters.uploadReference')}<input type="file" accept="image/png,image/jpeg" onChange={(e) => handle(e.target.files?.[0])} /></label>{renderable && <><button className="text-button" onClick={onDelete}><Trash2 size={13} /> {t('characters.deleteImage')}</button>{!isPrimary && <button className="text-button" onClick={onSetPrimary}>{t('characters.setPrimary')}</button>}</>}</div>{characterType === 'Creature' && <small className="asset-type-hint">{t('characters.typeCreature')}</small>}</div>
}

function LegacyCharactersStep({ project, onChange }: { project: Project; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const [saving, setSaving] = useState(''); const [error, setError] = useState('')
  async function saveCharacter(character: Character, patch: Partial<Character> = {}) { const next = { ...character, ...patch }; setSaving(character.id); try { const saved = await api<Character>(`/projects/${project.id}/characters/${character.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(next) }); onChange({ ...project, characters: project.characters.map((item) => item.id === saved.id ? saved : item) }) } catch (e) { setError((e as Error).message) } finally { setSaving('') } }
  async function addCharacter() { try { const added = await api<Character>(`/projects/${project.id}/characters`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'New Character', appearance_description: 'Add a clear reference description', clothing_description: 'Describe the signature wardrobe' }) }); onChange({ ...project, characters: [...project.characters, added] }) } catch (e) { setError((e as Error).message) } }
  async function removeCharacter(id: string) { try { await api(`/projects/${project.id}/characters/${id}`, { method: 'DELETE' }); onChange({ ...project, characters: project.characters.filter((item) => item.id !== id) }) } catch (e) { setError((e as Error).message) } }
  const confirmedCount = project.characters.filter((item) => item.confirmed).length
  return <div className="step-content"><div className="analysis-head"><div><p className="muted-copy">These references anchor identity across every future segment. Confirm the people you want to keep consistent.</p></div><button className="secondary-button" onClick={addCharacter}><Plus size={16} /> Add character</button></div>{error && <InlineError message={error} />}<div className="character-stack">{project.characters.map((character, index) => <CharacterEditor key={character.id} character={character} index={index} saving={saving === character.id} onSave={saveCharacter} onRemove={removeCharacter} />)}</div>{project.characters.length === 0 && <div className="empty-result character-empty"><Users size={20} /><span>No characters detected yet. Add one to continue.</span><button className="secondary-button" onClick={addCharacter}><Plus size={15} /> Add character</button></div>}<div className="step-footer"><span>{confirmedCount} of {project.characters.length} characters confirmed</span><button className="primary-button" disabled={confirmedCount < 1} onClick={() => navigate(`/project/${project.id}/settings`)}>Continue to style & voice <ArrowRight size={16} /></button></div></div>
}

function ReferenceUploader({ label, value, onUpload }: { label: string; value?: string; onUpload: (value: string) => void }) {
  const [preview, setPreview] = useState(value || '')
  useEffect(() => setPreview(value || ''), [value])
  function handle(file?: File) {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      const next = String(reader.result)
      setPreview(next)
      onUpload(next)
    }
    reader.readAsDataURL(file)
  }
  return <label className="reference-upload reference-upload-small">{preview ? <img src={preview} alt={`${label} reference`} /> : <><Upload size={15} /><span>{label}</span><small>PNG, JPG</small></>}<input type="file" accept="image/png,image/jpeg" onChange={(e) => handle(e.target.files?.[0])} /></label>
}

function CharacterEditor({ character, index, saving, onSave, onRemove }: { character: Character; index: number; saving: boolean; onSave: (c: Character, patch?: Partial<Character>) => void; onRemove: (id: string) => void }) {
  function saveView(field: 'front_view_image' | 'angle_view_image' | 'side_view_image' | 'full_body_image', value: string) {
    onSave({ ...character, [field]: value })
  }
  return <div className="character-editor-wrap"><CharacterEditorBase character={character} index={index} saving={saving} onSave={onSave} onRemove={onRemove} /><div className="reference-upload-grid"><ReferenceUploader label="Front view" value={character.front_view_image} onUpload={(value) => saveView('front_view_image', value)} /><ReferenceUploader label="45 degree" value={character.angle_view_image} onUpload={(value) => saveView('angle_view_image', value)} /><ReferenceUploader label="Side view" value={character.side_view_image} onUpload={(value) => saveView('side_view_image', value)} /><ReferenceUploader label="Full body" value={character.full_body_image} onUpload={(value) => saveView('full_body_image', value)} /></div></div>
}

function CharacterEditorBase({ character, index, saving, onSave, onRemove }: { character: Character; index: number; saving: boolean; onSave: (c: Character, patch?: Partial<Character>) => void; onRemove: (id: string) => void }) {
  const [expanded, setExpanded] = useState(true); const [draft, setDraft] = useState(character); const [preview, setPreview] = useState('')
  useEffect(() => setDraft(character), [character])
  function patch(key: keyof Character, value: string | boolean) { setDraft((old) => ({ ...old, [key]: value })) }
  function reference(file?: File) { if (!file) return; const reader = new FileReader(); reader.onload = () => { const value = String(reader.result); setPreview(value); patch('original_reference_image', value); onSave({ ...draft, original_reference_image: value }) }; reader.readAsDataURL(file) }
  function saveView(field: 'front_view_image' | 'angle_view_image' | 'side_view_image' | 'full_body_image', value: string) { patch(field, value); onSave({ ...draft, [field]: value }) }
  return <article className={`character-card ${draft.confirmed ? 'confirmed' : ''}`}><div className="character-card-head"><div className="character-heading"><MockAvatar index={index} /><div><div className="card-id">CHARACTER {String(index + 1).padStart(2, '0')} <span>/{draft.id}</span></div><h3>{draft.name}</h3><span>{draft.role} · {draft.gender} · {draft.estimated_age}</span></div></div><div className="character-actions"><span className={`confirm-state ${draft.confirmed ? 'is-confirmed' : ''}`}>{draft.confirmed ? <Check size={14} /> : <span className="hollow-dot" />}{draft.confirmed ? 'Confirmed' : 'Needs review'}</span><button className="icon-button" title="Remove character" onClick={() => onRemove(draft.id)}><Trash2 size={16} /></button><button className="icon-button" title={expanded ? 'Collapse card' : 'Expand card'} onClick={() => setExpanded(!expanded)}>{expanded ? <ChevronDown size={17} /> : <ChevronRight size={17} />}</button></div></div>{expanded && <div className="character-card-body"><div className="asset-column"><span className="section-label">REFERENCE ASSET</span><div className="reference-grid"><MockFrame label="ORIGINAL FRAME" variant={index === 0 ? 'portrait' : 'portrait-alt'} /><label className="reference-upload">{preview ? <img src={preview} alt="Uploaded character reference" /> : <><Upload size={17} /><span>Upload reference</span><small>PNG, JPG</small></>}<input type="file" accept="image/png,image/jpeg" onChange={(e) => reference(e.target.files?.[0])} /></label></div><div className="asset-views"><MockFrame label="FRONT" variant={index === 0 ? 'portrait' : 'portrait-alt'} /><MockFrame label="45°" variant={index === 0 ? 'angle' : 'angle-alt'} /><MockFrame label="SIDE" variant="side" /><MockFrame label="FULL BODY" variant="full" /></div></div><div className="character-fields"><div className="field-grid"><label className="field-label">Name<input value={draft.name} onChange={(e) => patch('name', e.target.value)} /></label><label className="field-label">Role<select value={draft.role} onChange={(e) => patch('role', e.target.value)}><option>Lead</option><option>Supporting</option><option>Extra</option></select></label></div><label className="field-label">Appearance<input value={draft.appearance_description} onChange={(e) => patch('appearance_description', e.target.value)} /></label><label className="field-label">Hair<input value={draft.hair_description} onChange={(e) => patch('hair_description', e.target.value)} /></label><label className="field-label">Clothing<input value={draft.clothing_description} onChange={(e) => patch('clothing_description', e.target.value)} /></label><div className="inline-save"><button className={`secondary-button ${draft.confirmed ? 'active-button' : ''}`} onClick={() => { patch('confirmed', !draft.confirmed); onSave(draft, { confirmed: !draft.confirmed }) }}>{draft.confirmed ? <Check size={15} /> : <UserRound size={15} />} {draft.confirmed ? 'Character confirmed' : 'Confirm character'}</button><button className="text-button" disabled={saving} onClick={() => onSave(draft)}>{saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />} Save changes</button></div></div></div>}</article>
}

function LegacyBrokenSettingsStep({ project, styles, languages, voices, onChange }: { project: Project; styles: Style[]; languages: Language[]; voices: Voice[]; onChange: (p: Project) => void }) { /*
  const navigate = useNavigate(); const { t, locale } = useI18n(); const [settings, setSettings] = useState<Settings>(project.settings); const [saving, setSaving] = useState(false); const [message, setMessage] = useState('')
  useEffect(() => setSettings(project.settings), [project.settings])
  function change(key: keyof Settings, value: string | boolean) { setSettings((old) => ({ ...old, [key]: value })) }
  async function save() { setSaving(true); setMessage(''); try { await api(`/projects/${project.id}/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings) }); onChange({ ...project, settings, selected_style: settings.style_id, source_language: settings.source_language, target_language: settings.target_language, subtitle_enabled: settings.subtitle_enabled, lip_sync_enabled: settings.lip_sync_enabled }); setMessage(t('settings.saved')) } catch (e) { setMessage((e as Error).message) } finally { setSaving(false) } }
  async function updateVoice(character: Character, patch: Partial<Character>) { const next = { ...character, ...patch }; try { const saved = await api<Character>(`/projects/${project.id}/characters/${character.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(next) }); onChange({ ...project, characters: project.characters.map((item) => item.id === saved.id ? saved : item) }) } catch (e) { setMessage((e as Error).message) } }
  return <div className="step-content"><DemoNotice>{t('settings.demoNotice')}</DemoNotice><div className="settings-intro"><p className="muted-copy">{t('settings.copy')}</p><button className="secondary-button" onClick={save} disabled={saving}>{saving ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />} {saving ? t('common.loading') : t('settings.save')}</button></div>{message && <div className="toast-line"><Check size={15} /> {message}</div>}<section className="settings-section"><div className="section-heading-inline"><div><span className="section-label">{t('settings.visualWorld')}</span><h2>{t('settings.chooseStyle')}</h2></div><span className="selection-count">{catalogLabel(settings.style_id, locale)}</span></div><div className="style-grid">{styles.map((style, index) => <button key={style.id} className={`style-card ${settings.style_id === style.id ? 'selected' : ''}`} onClick={() => change('style_id', style.id)}><div className={`style-visual style-${index}`}><span>{index === 0 ? 'LIVE' : index === 1 ? '2D' : index === 2 ? 'WEB' : index === 3 ? 'INK' : index === 4 ? '3D' : 'FILM'}</span><div className="style-silhouette" /></div><div className="style-card-copy"><div className="card-row"><strong>{catalogLabel(style.id, locale)}</strong>{settings.style_id === style.id && <span className="selected-mark"><Check size={12} /></span>}</div><p>{styleDescriptions[locale][style.id] || t('settings.demoNotice')}</p></div></button>)}</div></section><section className="settings-section language-section"><div className="section-heading-inline"><div><span className="section-label">{t('settings.languageRoute')}</span><h2>{t('settings.dialogue')}</h2></div></div><div className="language-route"><div className="language-field"><span className="field-icon"><Languages size={16} /></span><label className="field-label">{t('settings.sourceLanguage')}<select value={settings.source_language} onChange={(e) => change('source_language', e.target.value)}>{languages.map((item) => <option key={item.id} value={item.id}>{catalogLabel(item.id, locale)}</option>)}</select></label></div><ArrowRight className="route-arrow" size={18} /><div className="language-field target"><span className="field-icon"><Languages size={16} /></span><label className="field-label">{t('settings.targetLanguage')}<select value={settings.target_language} onChange={(e) => change('target_language', e.target.value)}>{languages.filter((item) => item.id !== 'auto').map((item) => <option key={item.id} value={item.id}>{catalogLabel(item.id, locale)}</option>)}</select></label></div></div><div className="mode-row"><span className="field-label plain-label">{t('settings.audioMode')}</span>{[['keep-original', t('settings.keepOriginal')], ['translate-dub', t('settings.translateDub')], ['dub-only', t('settings.dubOnly')], ['subtitle-only', t('settings.subtitleOnly')].map(([value, label]) => <button key={value} className={`seg-button ${settings.language_mode === value ? 'active' : ''}`} onClick={() => change('language_mode', value)}>{label}</button>)}</div></section><section className="settings-section"><div className="section-heading-inline"><div><span className="section-label">{t('settings.characterVoices')}</span><h2>{t('settings.voiceTitle')}</h2></div><span className="selection-count">{t('settings.voicesAssigned', { count: project.characters.length })}</span></div><div className="voice-list">{project.characters.map((character, index) => <VoiceEditorLocalized key={character.id} character={character} index={index} voices={voices} onChange={(patch) => updateVoice(character, patch)} />)}</div></section><section className="settings-section final-settings"><div className="section-heading-inline"><div><span className="section-label">{t('settings.outputControls')}</span><h2>{t('settings.outputTitle')}</h2></div></div><div className="control-grid"><SettingSelect label={t('settings.characterConsistency')} value={settings.character_consistency} options={[['standard', locale === 'zh-CN' ? '标准' : 'Standard'], ['strong', locale === 'zh-CN' ? '强' : 'Strong'], ['maximum', locale === 'zh-CN' ? '最高' : 'Maximum']]} onChange={(v) => change('character_consistency', v)} /><SettingSelect label={t('settings.visualStrength')} value={settings.transformation_strength} options={[['low', locale === 'zh-CN' ? '低' : 'Low'], ['medium', locale === 'zh-CN' ? '中' : 'Medium'], ['high', locale === 'zh-CN' ? '高' : 'High']]} onChange={(v) => change('transformation_strength', v)} /><SettingSelect label={t('settings.sceneProcessing')} value={settings.scene_processing_mode} options={[['all-elements', t('settings.allElements')], ['keep-background', t('settings.keepBackground')], ['characters-only', t('settings.charactersOnly')]} onChange={(v) => change('scene_processing_mode', v)} /></div><div className="toggle-row"><Toggle label={t('settings.aiSubtitles')} copy={t('settings.aiSubtitlesCopy')} checked={settings.subtitle_enabled} onChange={(v) => change('subtitle_enabled', v)} /><Toggle label={t('settings.lipSync')} copy={t('settings.lipSyncCopy')} checked={settings.lip_sync_enabled} onChange={(v) => change('lip_sync_enabled', v)} /></div></section><div className="step-footer"><span>{t('settings.defaultsSaved')}</span><button className="primary-button" onClick={async () => { await save(); navigate(`/project/${project.id}/generate`) }}>{t('settings.continue')} <ArrowRight size={16} /></button></div></div>
}

*/ }

function SettingsStep({ project, languages, voices, onChange }: { project: Project; styles: Style[]; languages: Language[]; voices: Voice[]; onChange: (p: Project) => void }) {
  const navigate = useNavigate()
  const { t, locale } = useI18n()
  const [settings, setSettings] = useState<Settings>(project.settings)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  useEffect(() => setSettings(project.settings), [project.settings])
  function change(key: keyof Settings, value: string | boolean) { setSettings((old) => ({ ...old, [key]: value })) }
  async function changeTargetLanguage(value: string) {
    const previousSettings = settings
    const nextSettings = { ...settings, target_language: value }
    setSettings(nextSettings)
    onChange({ ...project, settings: nextSettings, target_language: value })
    try {
      await api(`/projects/${project.id}/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...nextSettings, lip_sync_enabled: false }),
      })
    } catch (error) {
      setSettings(previousSettings)
      onChange({ ...project, settings: previousSettings, target_language: previousSettings.target_language })
      setMessage((error as Error).message)
    }
  }
  async function save() {
    setSaving(true); setMessage('')
    try {
      const supportedSettings = { ...settings, lip_sync_enabled: false }
      await api(`/projects/${project.id}/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(supportedSettings) })
      setSettings(supportedSettings)
      onChange({ ...project, settings: supportedSettings, selected_style: supportedSettings.style_id, source_language: supportedSettings.source_language, target_language: supportedSettings.target_language, subtitle_enabled: supportedSettings.subtitle_enabled, lip_sync_enabled: false })
      setMessage(t('settings.saved'))
    } catch (e) { setMessage((e as Error).message) } finally { setSaving(false) }
  }
  async function updateVoice(character: Character, patch: Partial<Character>) {
    try { const saved = await api<Character>(`/projects/${project.id}/characters/${character.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...character, ...patch }) }); onChange({ ...project, characters: project.characters.map((item) => item.id === saved.id ? saved : item) }) } catch (e) { setMessage((e as Error).message) }
  }
  const option = (_key: string, en: string, zh: string) => locale === 'zh-CN' ? zh : en
  const modes: Array<[string, string]> = [['keep-original', t('settings.keepOriginal')], ['translate-dub', t('settings.translateDub')], ['dub-only', t('settings.dubOnly')], ['subtitle-only', t('settings.subtitleOnly')]]
  return <div className="step-content">
    <div className="settings-intro"><p className="muted-copy">{t('settings.copy')}</p><button className="secondary-button" onClick={save} disabled={saving}>{saving ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />} {saving ? t('common.loading') : t('common.save')}</button></div>
    {message && <div className="toast-line"><Check size={15} /> {message}</div>}
     <section className="settings-section language-section"><div className="section-heading-inline"><div><span className="section-label">{t('settings.languageRoute')}</span><h2>{t('settings.dialogue')}</h2></div></div><div className="language-route"><div className="language-field"><span className="field-icon"><Languages size={16} /></span><label className="field-label">{t('settings.sourceLanguage')}<select value={settings.source_language} onChange={(e) => change('source_language', e.target.value)}>{languages.map((item) => <option key={item.id} value={item.id}>{catalogLabel(item.id, locale)}</option>)}</select></label></div><ArrowRight className="route-arrow" size={18} /><div className="language-field target"><span className="field-icon"><Languages size={16} /></span><label className="field-label">{t('settings.targetLanguage')}<select value={settings.target_language} onChange={(e) => void changeTargetLanguage(e.target.value)}>{targetLanguageOptions(languages).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label></div></div><div className="mode-row"><span className="field-label plain-label">{t('settings.audioMode')}</span>{modes.map(([value, label]) => <button key={value} className={`seg-button ${settings.language_mode === value ? 'active' : ''}`} onClick={() => change('language_mode', value)}>{label}</button>)}</div></section>
    <TranscriptionSubtitlesPanel episodeId={queryEpisodeId() || project.active_episode_id} sourceLanguage={settings.source_language} targetLanguage={settings.target_language} />
    <DubbingMixPanel episodeId={queryEpisodeId() || project.active_episode_id} processingMode={project.processing_mode} targetLanguage={settings.target_language} />
    <CurrentEffectPreview episodeId={queryEpisodeId() || project.active_episode_id} />
    <section className="settings-section final-settings">
      <div className="section-heading-inline"><div><span className="section-label">{t('settings.outputControls')}</span><h2>{t('settings.outputTitle')}</h2></div></div>
      <div className="control-grid">
        <SettingSelect label={t('settings.characterConsistency')} value={settings.character_consistency} options={[['standard', option('standard', 'Standard', '\u6807\u51c6')], ['strong', option('strong', 'Strong', '\u5f3a')], ['maximum', option('maximum', 'Maximum', '\u6700\u9ad8')]]} onChange={(v) => change('character_consistency', v)} />
        <SettingSelect label={t('settings.visualStrength')} value={settings.transformation_strength} options={[['low', option('low', 'Low', '\u4f4e')], ['medium', option('medium', 'Medium', '\u4e2d')], ['high', option('high', 'High', '\u9ad8')]]} onChange={(v) => change('transformation_strength', v)} />
        <SettingSelect label={t('settings.sceneProcessing')} value={settings.scene_processing_mode} options={[['all-elements', t('settings.allElements')], ['keep-background', t('settings.keepBackground')], ['characters-only', t('settings.charactersOnly')]]} onChange={(v) => change('scene_processing_mode', v)} />
      </div>
      <div className="scope-setting"><div><span className="section-label">{locale === 'zh-CN' ? '转绘范围（第二阶段仅保存）' : 'Transformation Scope (saved only in Phase 2)'}</span><p>{locale === 'zh-CN' ? '当前不会执行真实 AI 转绘。' : 'No real AI transformation is executed in this phase.'}</p></div><div className="mode-row">{[['characters-only', locale === 'zh-CN' ? '仅人物' : 'Characters only'], ['characters-background', locale === 'zh-CN' ? '人物与背景' : 'Characters & background'], ['full-scene', locale === 'zh-CN' ? '全场景' : 'Full scene'], ['partial-redraw', locale === 'zh-CN' ? '局部重绘' : 'Partial redraw']].map(([value, label]) => <button key={value} className={`seg-button ${settings.visual_scope === value ? 'active' : ''}`} onClick={() => change('visual_scope', value)}>{label}</button>)}</div></div>
      <div className="toggle-row"><Toggle label={t('settings.aiSubtitles')} copy={t('settings.aiSubtitlesCopy')} checked={settings.subtitle_enabled} onChange={(v) => change('subtitle_enabled', v)} /><Toggle label={t('settings.lipSync')} copy={locale === 'zh-CN' ? '口型同步尚未接入' : 'Lip sync is not available yet'} checked={false} disabled onChange={() => undefined} /></div>
    </section>
    <div className="step-footer"><span>{t('settings.defaultsSaved')}</span><button className="primary-button" onClick={async () => { await save(); navigate(`/project/${project.id}/generate`) }}>{t('settings.continue')} <ArrowRight size={16} /></button></div>
  </div>
}

function VoiceEditorLocalized({ character, index, voices, onChange }: { character: Character; index: number; voices: Voice[]; onChange: (patch: Partial<Character>) => void }) {
  const { locale } = useI18n(); const settings = { base_voice: 'Auto Match', voice_age: 'Young Adult', voice_style: 'Warm', speech_speed: 1, pitch: 0, emotion_strength: 60, ...character.voice_settings }
  const patchSetting = (key: string, value: string | number) => onChange({ voice_settings: { ...settings, [key]: value } })
  return <div className="voice-card"><div className="voice-card-head"><div className="voice-person"><MockAvatar index={index} /><div><div className="card-id">CHARACTER {String(index + 1).padStart(2, '0')}</div><strong>{character.name}</strong><span>{character.role} · Voice ID <code>{character.voice_id}</code></span></div></div><div className="voice-actions"><button className="secondary-button compact" disabled title={locale === 'zh-CN' ? '真实声音将在下一阶段接入' : 'Real voice is planned for the next stage'}><Play size={14} /> {locale === 'zh-CN' ? '试听声音（下一阶段）' : 'Voice preview (Next stage)'}</button><button className="text-button" onClick={() => onChange({ voice_id: 'auto-match', voice_settings: { ...settings, base_voice: 'Auto Match' } })}><RotateCcw size={14} /> {locale === 'zh-CN' ? '自动匹配' : 'Auto match'}</button></div></div><div className="mock-capability-label">{locale === 'zh-CN' ? 'A1 不生成声音，相关能力将在下一阶段接入' : 'A1 does not generate audio; voice capabilities arrive in the next stage'}</div><div className="voice-controls"><label className="field-label">{locale === 'zh-CN' ? '声音模型' : 'Voice model'}<select value={character.voice_id} onChange={(event) => onChange({ voice_id: event.target.value })}>{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name} · {voice.base_voice}</option>)}</select></label><label className="field-label">{locale === 'zh-CN' ? '基础声音' : 'Base voice'}<select value={String(settings.base_voice)} onChange={(event) => patchSetting('base_voice', event.target.value)}>{['Auto Match', 'Male', 'Female', 'Neutral'].map((value) => <option key={value}>{value}</option>)}</select></label><label className="field-label">{locale === 'zh-CN' ? '声音年龄' : 'Voice age'}<select value={String(settings.voice_age)} onChange={(event) => patchSetting('voice_age', event.target.value)}>{['Child', 'Teen', 'Young Adult', 'Adult', 'Middle-aged', 'Elderly'].map((value) => <option key={value}>{value}</option>)}</select></label><label className="field-label">{locale === 'zh-CN' ? '声音风格' : 'Voice style'}<select value={String(settings.voice_style)} onChange={(event) => patchSetting('voice_style', event.target.value)}>{['Deep', 'Soft', 'Warm', 'Calm', 'Energetic', 'Serious'].map((value) => <option key={value}>{value}</option>)}</select></label></div><div className="range-controls"><RangeControl label={locale === 'zh-CN' ? '语速' : 'Speech speed'} value={Number(settings.speech_speed)} min={0.7} max={1.3} step={0.01} display={`${Number(settings.speech_speed).toFixed(2)}x`} onChange={(value) => patchSetting('speech_speed', value)} /><RangeControl label={locale === 'zh-CN' ? '音调' : 'Pitch'} value={Number(settings.pitch)} min={-6} max={6} step={1} display={String(settings.pitch)} onChange={(value) => patchSetting('pitch', value)} /><RangeControl label={locale === 'zh-CN' ? '情绪强度' : 'Emotion strength'} value={Number(settings.emotion_strength)} min={0} max={100} step={1} display={`${settings.emotion_strength}%`} onChange={(value) => patchSetting('emotion_strength', value)} /></div></div>
}

function LegacyVoiceEditorLocalized({ character, index, voices, onChange }: { character: Character; index: number; voices: Voice[]; onChange: (patch: Partial<Character>) => void }) {
  const { t, locale } = useI18n(); const defaults = { base_voice: 'Auto Match', voice_age: 'Young Adult', voice_style: 'Warm', speech_speed: 1, pitch: 0, emotion_strength: 60, ...character.voice_settings }; const [playing, setPlaying] = useState(false)
  function settingsPatch(key: string, value: string | number) { onChange({ voice_settings: { ...defaults, [key]: value } }) }
  function previewVoice() { setPlaying(true); window.setTimeout(() => setPlaying(false), 900) }
  const options = locale === 'zh-CN' ? { base: ['自动匹配', '男声', '女声', '中性'], age: ['儿童', '青少年', '青年', '成年', '中年', '老年'], style: ['深沉', '磁性', '柔和', '温暖', '平静', '活泼', '严肃', '温柔', '冷峻', '欢快', '权威'] } : { base: ['Auto Match', 'Male', 'Female', 'Neutral'], age: ['Child', 'Teen', 'Young Adult', 'Adult', 'Middle-aged', 'Elderly'], style: ['Deep', 'Magnetic', 'Soft', 'Warm', 'Calm', 'Energetic', 'Serious', 'Gentle', 'Cold', 'Cheerful', 'Authoritative'] }
  return <div className="voice-card"><div className="voice-card-head"><div className="voice-person"><MockAvatar index={index} /><div><div className="card-id">{t('characters.referenceAsset')} {String(index + 1).padStart(2, '0')}</div><strong>{character.name}</strong><span>{character.role} · Voice ID <code>{character.voice_id}</code></span></div></div><div className="voice-actions"><button className="secondary-button compact" onClick={previewVoice}>{playing ? <Pause size={14} /> : <Play size={14} />} {playing ? t('settings.playing') : t('settings.previewVoice')}</button><button className="text-button" onClick={() => onChange({ voice_id: 'auto-match', voice_settings: { ...defaults, base_voice: 'Auto Match' } })}><RotateCcw size={14} /> {t('settings.autoMatch')}</button></div></div><div className="voice-controls"><label className="field-label">{t('settings.voiceModel')}<select value={character.voice_id} onChange={(e) => onChange({ voice_id: e.target.value })}>{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name} · {voice.base_voice}</option>)}</select></label><label className="field-label">{t('settings.baseVoice')}<select value={String(defaults.base_voice)} onChange={(e) => settingsPatch('base_voice', e.target.value)}>{options.base.map((item) => <option key={item}>{item}</option>)}</select></label><label className="field-label">{t('settings.voiceAge')}<select value={String(defaults.voice_age)} onChange={(e) => settingsPatch('voice_age', e.target.value)}>{options.age.map((item) => <option key={item}>{item}</option>)}</select></label><label className="field-label">{t('settings.voiceStyle')}<select value={String(defaults.voice_style)} onChange={(e) => settingsPatch('voice_style', e.target.value)}>{options.style.map((item) => <option key={item}>{item}</option>)}</select></label></div><div className="range-controls"><RangeControl label={t('settings.speechSpeed')} value={Number(defaults.speech_speed)} min={0.7} max={1.3} step={0.01} display={`${Number(defaults.speech_speed).toFixed(2)}x`} onChange={(v) => settingsPatch('speech_speed', v)} /><RangeControl label={t('settings.pitch')} value={Number(defaults.pitch)} min={-6} max={6} step={1} display={`${Number(defaults.pitch) > 0 ? '+' : ''}${defaults.pitch}`} onChange={(v) => settingsPatch('pitch', v)} /><RangeControl label={t('settings.emotionStrength')} value={Number(defaults.emotion_strength)} min={0} max={100} step={1} display={`${defaults.emotion_strength}%`} onChange={(v) => settingsPatch('emotion_strength', v)} /></div></div>
}

function LegacySettingsStep({ project, styles, languages, voices, onChange }: { project: Project; styles: Style[]; languages: Language[]; voices: Voice[]; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const [settings, setSettings] = useState<Settings>(project.settings); const [saving, setSaving] = useState(false); const [message, setMessage] = useState('')
  useEffect(() => setSettings(project.settings), [project.settings])
  function change(key: keyof Settings, value: string | boolean) { setSettings((old) => ({ ...old, [key]: value })) }
  async function save() { setSaving(true); setMessage(''); try { await api(`/projects/${project.id}/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings) }); onChange({ ...project, settings, selected_style: settings.style_id, source_language: settings.source_language, target_language: settings.target_language, subtitle_enabled: settings.subtitle_enabled, lip_sync_enabled: settings.lip_sync_enabled }); setMessage('Settings saved locally') } catch (e) { setMessage((e as Error).message) } finally { setSaving(false) } }
  async function updateVoice(character: Character, patch: Partial<Character>) { const next = { ...character, ...patch }; const saved = await api<Character>(`/projects/${project.id}/characters/${character.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(next) }); onChange({ ...project, characters: project.characters.map((item) => item.id === saved.id ? saved : item) }) }
  return <div className="step-content"><div className="settings-intro"><div><p className="muted-copy">Set the transformation language and the sound of every recurring character. The defaults are tuned for a quick cinematic pass.</p></div><button className="secondary-button" onClick={save} disabled={saving}>{saving ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />} {saving ? 'Saving...' : 'Save settings'}</button></div>{message && <div className="toast-line"><Check size={15} /> {message}</div>}<section className="settings-section"><div className="section-heading-inline"><div><span className="section-label">01 / VISUAL WORLD</span><h2>Choose a transformation style</h2></div><span className="selection-count">{styleName(settings.style_id, styles)}</span></div><div className="style-grid">{styles.map((style, index) => <button key={style.id} className={`style-card ${settings.style_id === style.id ? 'selected' : ''}`} onClick={() => change('style_id', style.id)}><div className={`style-visual style-${index}`}><span>{index === 0 ? 'LIVE' : index === 1 ? '2D' : index === 2 ? 'WEB' : index === 3 ? 'INK' : index === 4 ? '3D' : 'FILM'}</span><div className="style-silhouette" /></div><div className="style-card-copy"><div className="card-row"><strong>{style.name}</strong>{settings.style_id === style.id && <span className="selected-mark"><Check size={12} /></span>}</div><p>{style.description}</p><div className="prompt-tags">{style.prompt.slice(0, 3).map((item) => <span key={item}>{item}</span>)}</div></div></button>)}</div></section><section className="settings-section language-section"><div className="section-heading-inline"><div><span className="section-label">02 / LANGUAGE ROUTE</span><h2>Decide how the dialogue travels</h2></div></div><div className="language-route"><div className="language-field"><span className="field-icon"><Languages size={16} /></span><label className="field-label">Source language<select value={settings.source_language} onChange={(e) => change('source_language', e.target.value)}>{languages.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></div><ArrowRight className="route-arrow" size={18} /><div className="language-field target"><span className="field-icon"><Languages size={16} /></span><label className="field-label">Target language<select value={settings.target_language} onChange={(e) => change('target_language', e.target.value)}>{languages.filter((item) => item.id !== 'auto').map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></div></div><div className="mode-row"><span className="field-label plain-label">Audio mode</span>{[['keep-original', 'Keep original audio'], ['translate-dub', 'Translate & dub'], ['dub-only', 'Dub without translation'], ['subtitle-only', 'Subtitle only']].map(([value, label]) => <button key={value} className={`seg-button ${settings.language_mode === value ? 'active' : ''}`} onClick={() => change('language_mode', value)}>{label}</button>)}</div></section><section className="settings-section"><div className="section-heading-inline"><div><span className="section-label">03 / CHARACTER VOICES</span><h2>Give every character a consistent voice</h2></div><span className="selection-count">{project.characters.length} voices assigned</span></div><div className="voice-list">{project.characters.map((character, index) => <VoiceEditor key={character.id} character={character} index={index} voices={voices} onChange={(patch) => updateVoice(character, patch)} />)}</div></section><section className="settings-section final-settings"><div className="section-heading-inline"><div><span className="section-label">04 / OUTPUT CONTROLS</span><h2>Keep the transformation in your hands</h2></div></div><div className="control-grid"><SettingSelect label="Character consistency" value={settings.character_consistency} options={[['standard', 'Standard'], ['strong', 'Strong'], ['maximum', 'Maximum']]} onChange={(v) => change('character_consistency', v)} /><SettingSelect label="Visual transformation strength" value={settings.transformation_strength} options={[['low', 'Low'], ['medium', 'Medium'], ['high', 'High']]} onChange={(v) => change('transformation_strength', v)} /><SettingSelect label="Scene processing" value={settings.scene_processing_mode} options={[['all-elements', 'Transform all visual elements'], ['keep-background', 'Keep original background'], ['characters-only', 'Transform characters only']]} onChange={(v) => change('scene_processing_mode', v)} /></div><div className="toggle-row"><Toggle label="AI subtitles" copy="Auto-generate target language subtitles" checked={settings.subtitle_enabled} onChange={(v) => change('subtitle_enabled', v)} /><Toggle label="Lip sync" copy="Align transformed dialogue to each face" checked={settings.lip_sync_enabled} onChange={(v) => change('lip_sync_enabled', v)} /></div></section><div className="step-footer"><span>Defaults are saved to this project</span><button className="primary-button" onClick={async () => { await save(); navigate(`/project/${project.id}/generate`) }}>Continue to generation <ArrowRight size={16} /></button></div></div>
}

function VoiceEditor({ character, index, voices, onChange }: { character: Character; index: number; voices: Voice[]; onChange: (patch: Partial<Character>) => void }) {
  const defaults = { base_voice: 'Auto Match', voice_age: 'Young Adult', voice_style: 'Warm', speech_speed: 1, pitch: 0, emotion_strength: 60, ...character.voice_settings }; const [playing, setPlaying] = useState(false)
  function settingsPatch(key: string, value: string | number) { onChange({ voice_settings: { ...defaults, [key]: value } }) }
  function previewVoice() { setPlaying(true); if ('speechSynthesis' in window) { window.speechSynthesis.cancel(); const utterance = new SpeechSynthesisUtterance(`This is a mock voice preview for ${character.name}.`); utterance.onend = () => setPlaying(false); window.speechSynthesis.speak(utterance) } else setTimeout(() => setPlaying(false), 1400) }
  return <div className="voice-card"><div className="voice-card-head"><div className="voice-person"><MockAvatar index={index} /><div><div className="card-id">CHARACTER {String(index + 1).padStart(2, '0')}</div><strong>{character.name}</strong><span>{character.role} · Voice ID <code>{character.voice_id}</code></span></div></div><div className="voice-actions"><button className="secondary-button compact" onClick={previewVoice}>{playing ? <Pause size={14} /> : <Play size={14} />} {playing ? 'Playing' : 'Preview voice'}</button><button className="text-button" onClick={() => onChange({ voice_id: 'auto-match', voice_settings: { ...defaults, base_voice: 'Auto Match' } })}><RotateCcw size={14} /> Auto match</button></div></div><div className="voice-controls"><label className="field-label">Voice model<select value={character.voice_id} onChange={(e) => onChange({ voice_id: e.target.value })}>{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name} · {voice.base_voice}</option>)}</select></label><label className="field-label">Base voice<select value={String(defaults.base_voice)} onChange={(e) => settingsPatch('base_voice', e.target.value)}><option>Auto Match</option><option>Male</option><option>Female</option><option>Neutral</option></select></label><label className="field-label">Voice age<select value={String(defaults.voice_age)} onChange={(e) => settingsPatch('voice_age', e.target.value)}>{['Child', 'Teen', 'Young Adult', 'Adult', 'Middle-aged', 'Elderly'].map((item) => <option key={item}>{item}</option>)}</select></label><label className="field-label">Voice style<select value={String(defaults.voice_style)} onChange={(e) => settingsPatch('voice_style', e.target.value)}>{['Deep', 'Magnetic', 'Soft', 'Warm', 'Calm', 'Energetic', 'Serious', 'Gentle', 'Cold', 'Cheerful', 'Authoritative'].map((item) => <option key={item}>{item}</option>)}</select></label></div><div className="range-controls"><RangeControl label="Speech speed" value={Number(defaults.speech_speed)} min={0.7} max={1.3} step={0.01} display={`${Number(defaults.speech_speed).toFixed(2)}×`} onChange={(v) => settingsPatch('speech_speed', v)} /><RangeControl label="Pitch" value={Number(defaults.pitch)} min={-6} max={6} step={1} display={`${Number(defaults.pitch) > 0 ? '+' : ''}${defaults.pitch}`} onChange={(v) => settingsPatch('pitch', v)} /><RangeControl label="Emotion strength" value={Number(defaults.emotion_strength)} min={0} max={100} step={1} display={`${defaults.emotion_strength}%`} onChange={(v) => settingsPatch('emotion_strength', v)} /></div></div>
}

type XmaxWorkspaceMode = 'unified_scene_workflow' | 'historical_legacy_task' | 'unsupported_duration'

function resolveXmaxWorkspaceMode({ duration, featureEnabled, hasHistoricalTaskEntry }: { duration: number; featureEnabled: boolean; hasHistoricalTaskEntry: boolean }): XmaxWorkspaceMode {
  if (hasHistoricalTaskEntry) return 'historical_legacy_task'
  if (!featureEnabled || !Number.isFinite(duration) || duration <= 0 || duration > 180) return 'unsupported_duration'
  return 'unified_scene_workflow'
}

function GenerateWorkspaceStep({ project, styles, languages, voices, onChange }: { project: Project; styles: Style[]; languages: Language[]; voices: Voice[]; onChange: (project: Project) => void }) {
  const { locale } = useI18n()
  const showAdvanced = new URLSearchParams(window.location.search).get('advanced') === '1'
  const episodeId = queryEpisodeId() || project.active_episode_id
  return <div className="generation-workspace">
    <LocalizationWorkflowSummary
      episodeId={episodeId}
      sourceLanguage={project.settings.source_language}
      targetLanguage={project.settings.target_language}
    />
    <GuardedGenerateStep project={project} languages={languages} voices={voices} onChange={onChange} />
    <details className="workflow-advanced generation-advanced" open={showAdvanced || undefined}>
      <summary><Settings2 size={15} /> {locale === 'zh-CN' ? '高级调整' : 'Advanced adjustments'}</summary>
      <p>{locale === 'zh-CN' ? '时间轴、逐句字幕、单句配音、混音、处理模式和技术信息默认隐藏。' : 'Timeline, sentence-level subtitles, individual voice clips, mixing, processing mode, and technical information are hidden by default.'}</p>
      <TransformUnitPlanSummary episodeId={episodeId} />
      <ProcessingModeControl project={project} onChange={onChange} />
      <SettingsStep project={project} styles={styles} languages={languages} voices={voices} onChange={onChange} />
    </details>
  </div>
}

function GuardedGenerateStep({ project, languages, voices, onChange }: { project: Project; languages: Language[]; voices: Voice[]; onChange: (p: Project) => void }) {
  const navigate = useNavigate()
  const { locale } = useI18n()
  const episodeId = queryEpisodeId() || project.active_episode_id
  const episode = project.episodes?.find((item) => item.id === episodeId)
  if (!episode) {
    return <div className="step-content"><div className="empty-state"><div className="empty-icon"><Film size={25} /></div><h3>Select an Episode first.</h3><p>Open an Episode before starting the unified Scene Analysis and Shot Review workflow.</p></div></div>
  }
  if (!isEpisodeUploadComplete(episode)) {
    const message = locale === 'zh-CN'
      ? '\u89c6\u9891\u5c1a\u672a\u4e0a\u4f20\u6210\u529f\uff0c\u8bf7\u91cd\u65b0\u4e0a\u4f20\u540e\u518d\u7ee7\u7eed\u3002'
      : 'The video has not uploaded successfully. Upload it again before continuing.'
    return <div className="step-content"><div className="empty-state"><div className="empty-icon"><UploadCloud size={25} /></div><h3>{message}</h3><button className="primary-button" onClick={() => navigate(flowRoute(project.id, 'upload', episode.id))}><Upload size={16} /> {locale === 'zh-CN' ? '\u91cd\u65b0\u4e0a\u4f20' : 'Upload again'}</button></div></div>
  }
  if (project.processing_mode !== 'quick-transformation' && !project.characters.some((item) => item.confirmed && item.priority_level === 'Locked')) {
    return <div className="step-content"><div className="empty-state"><div className="empty-icon"><Users size={25} /></div><h3>Confirm at least one character first.</h3><p>Return to Characters and confirm the cast before starting the mock transformation.</p><button className="primary-button" onClick={() => navigate(`/project/${project.id}/characters`)}><Users size={16} /> Review characters</button></div></div>
  }
  const singleShotEnabled = import.meta.env.VITE_XMAX_SINGLE_SHOT_MVP === 'true'
  const rawDuration: unknown = episode?.duration
  const normalizedDuration = rawDuration == null || rawDuration === '' ? Number.NaN : Number(rawDuration)
  const hasHistoricalTaskEntry = new URLSearchParams(window.location.search).has('legacy_task_id')
  const workspaceMode = resolveXmaxWorkspaceMode({ duration: normalizedDuration, featureEnabled: singleShotEnabled, hasHistoricalTaskEntry })
  const diagnostic = import.meta.env.DEV ? <div className="xmax-single-shot-gate-diagnostic">flag={singleShotEnabled ? 'true' : 'false'} · episodeFound={episode ? 'true' : 'false'} · rawDuration={String(rawDuration ?? 'null')} · normalizedDuration={Number.isFinite(normalizedDuration) ? normalizedDuration : 'NaN'} · workspaceMode={workspaceMode}</div> : null
  if (workspaceMode === 'historical_legacy_task') {
    return <><div className="xmax-single-shot-gate-diagnostic">Historical legacy task</div><XmaxShotPipelinePanel project={project} onChange={onChange} />{diagnostic}</>
  }
  if (workspaceMode === 'unsupported_duration') {
    return <>{diagnostic}<div className="step-content"><div className="empty-state"><div className="empty-icon"><Film size={25} /></div><h3>Episode duration is not supported.</h3><p>For reliable processing, split this Episode into shorter Episodes before starting a transformation.</p></div></div></>
  }
  return <OneClickOverseasPanel project={project} episode={{ ...episode, duration: normalizedDuration }} languages={languages} voices={voices} onProjectChange={onChange} />
}

function XmaxGeneratePanel({ project, onChange }: { project: Project; onChange: (p: Project) => void }) {
  const navigate = useNavigate()
  const { locale } = useI18n()
  const episodeId = queryEpisodeId() || project.active_episode_id
  const episode = project.episodes?.find((item) => item.id === episodeId)
  const source = episode ? episodeVideoUrl(episode) : videoUrl(project)
  const inputRef = useRef<HTMLVideoElement>(null)
  const outputRef = useRef<HTMLVideoElement>(null)
  const localContainerRef = useRef<HTMLDivElement>(null)
  const remoteContainerRef = useRef<HTMLDivElement>(null)
  const previewRef = useRef<HTMLVideoElement>(null)
  const remoteStreamRef = useRef<MediaStream | null>(null)
  const adapterRef = useRef<XmaxRealtimeAdapter | null>(null)
  const [status, setStatus] = useState<XmaxRealtimeStatus | 'idle'>('idle')
  const [error, setError] = useState('')
  const [sourceDuration, setSourceDuration] = useState(() => episode?.duration || project.video_duration || 0)
  const [processedSeconds, setProcessedSeconds] = useState(0)
  const [result, setResult] = useState<XmaxRealtimeResult | undefined>(() => getXmaxResult(xmaxResultKey(project.id, episodeId)))
  const [style, setStyle] = useState(project.settings.style_id || 'western-live-action')
  const [targetLanguage, setTargetLanguage] = useState(project.settings.target_language || 'english')
  const zh = locale === 'zh-CN'
  const xmaxConfigured = browserXmaxCredentialConfigured()
  const referenceCharacter = project.characters.find((character) => character.confirmed && characterReferenceUrl(character))
  const referenceImage = referenceCharacter ? characterReferenceUrl(referenceCharacter) : ''
  const styleOptions: Array<[string, string, string]> = [
    ['western-live-action', 'Western Live Action', '欧美仿真人'],
    ['western-fantasy', 'Western Fantasy', '西幻真人'],
    ['cinematic-realism', 'Cinematic Realism', '电影写实'],
    ['japanese-anime', 'Japanese Anime', '日漫'],
    ['korean-webtoon', 'Korean Webtoon', '韩漫'],
    ['american-comic', 'American Comic', '美漫'],
    ['3d-animation', '3D Animation', '3D动画'],
  ]
  const languageOptions: Array<[string, string, string]> = [['english', 'English', '英语'], ['japanese', 'Japanese', '日语'], ['korean', 'Korean', '韩语'], ['spanish', 'Spanish', '西班牙语'], ['portuguese', 'Portuguese', '葡萄牙语'], ['french', 'French', '法语']]
  const styleLabel = styleOptions.find(([value]) => value === style)?.[zh ? 2 : 1] || style
  const languageLabel = languageOptions.find(([value]) => value === targetLanguage)?.[zh ? 2 : 1] || targetLanguage
  const prompt = "视频中角色替换成参考图中角色"
  const running = ['preparing', 'connecting', 'generating', 'recording'].includes(status)

  useEffect(() => {
    const stored = getXmaxResult(xmaxResultKey(project.id, episodeId))
    setResult(stored)
  }, [project.id, episodeId])

  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => {
      const current = inputRef.current?.currentTime || 0
      setProcessedSeconds(Math.min(sourceDuration || current, current))
    }, 200)
    return () => window.clearInterval(timer)
  }, [running, sourceDuration])

  useEffect(() => {
    if (!previewRef.current || !remoteStreamRef.current) return
    previewRef.current.srcObject = remoteStreamRef.current
    void previewRef.current.play().catch(() => undefined)
  }, [status, result])

  useEffect(() => () => { void adapterRef.current?.dispose() }, [])

  function handleSourceMetadata(event: React.SyntheticEvent<HTMLVideoElement>) {
    const duration = event.currentTarget.duration
    if (Number.isFinite(duration) && duration > 0) setSourceDuration(duration)
  }

  async function saveSelection() {
    const settings = { ...project.settings, style_id: style, target_language: targetLanguage }
    await api(`/projects/${project.id}/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings) })
    onChange({ ...project, settings, selected_style: style, target_language: targetLanguage })
  }

  async function start() {
    if (!xmaxConfigured) {
      setError(XMAX_TEMP_KEY_REQUIRED)
      return
    }
    if (!referenceCharacter || !referenceImage) {
      setError(zh ? '请先确认角色卡。' : 'Please confirm a character card first.')
      return
    }
    const input = inputRef.current
    const output = outputRef.current
    const localContainer = localContainerRef.current
    const remoteContainer = remoteContainerRef.current
    if (!input || !output || !localContainer || !remoteContainer || !source) {
      setError(zh ? '请先上传当前视频。' : 'Upload the current video first.')
      return
    }
    setError('')
    setResult(undefined)
    setProcessedSeconds(0)
    try {
      await saveSelection()
      input.currentTime = 0
      await input.play()
      const adapter = new XmaxRealtimeAdapter()
      adapterRef.current = adapter
      setStatus('preparing')
      await adapter.start({
        inputVideo: input,
        outputVideo: output,
        localElement: localContainer,
        remoteElement: remoteContainer,
        referenceImage,
        prompt,
        sourceDuration,
        style: styleLabel,
        targetLanguage: languageLabel,
        characterName: referenceCharacter.name,
        onStatus: setStatus,
        onProgress: setProcessedSeconds,
        onRemoteStream: (stream) => {
          remoteStreamRef.current = stream
          if (previewRef.current) { previewRef.current.srcObject = stream; void previewRef.current.play().catch(() => undefined) }
        },
        onError: (message) => { setError(message); setStatus('failed') },
        onResult: (next) => {
          setResult(next)
          setXmaxResult(xmaxResultKey(project.id, episodeId), next)
          onChange({ ...project, status: 'Completed' })
        },
      })
    } catch (reason) {
      setStatus('failed')
      setError((reason as Error).message)
    }
  }

  async function stop() {
    await adapterRef.current?.stop('stopped')
    setStatus('stopped')
  }

  const statusLabel: Partial<Record<XmaxRealtimeStatus | 'idle', string>> = {
    idle: zh ? '准备就绪' : 'Ready', preparing: zh ? '准备中' : 'Preparing', connecting: zh ? '连接中' : 'Connecting', generating: zh ? '生成中' : 'Generating', recording: zh ? '录制中' : 'Recording', completed: zh ? '已完成' : 'Completed', stopped: zh ? '已停止' : 'Stopped', failed: zh ? '失败' : 'Failed',
  }
  const displaySeconds = result?.processedSeconds ?? processedSeconds
  return <div className="step-content"><DemoNotice>{zh ? '实时海外版生成会使用当前视频和已确认角色卡。语言只保存到任务状态，本轮不执行翻译、配音或口型同步。' : 'Realtime overseas generation uses the current video and a confirmed character card. Language is stored with the task; translation, dubbing and lip sync are not run in this step.'}</DemoNotice><section className="xmax-panel"><div className="xmax-heading"><div><span className="section-label">GENERATE OVERSEAS VERSION</span><h2>{zh ? '生成海外版' : 'Generate overseas version'}</h2><p>{zh ? '保持现有动作、镜头和背景，只调整角色身份与目标视觉风格。' : 'Preserve action, camera and background while changing the character identity and target visual style.'}</p></div><div className="xmax-heading-status"><span className={`xmax-status xmax-status-${status}`}>{statusLabel[status]}</span><span className="xmax-configured">Xmax configured: {String(xmaxConfigured)}</span></div></div>{!xmaxConfigured && <div className="xmax-warning">{zh ? 'Xmax 尚未配置' : 'Xmax is not configured'}</div>}{error && <InlineError message={error} />}<div className="xmax-media-grid"><div className="xmax-media-card"><span className="media-label">{zh ? '原视频' : 'Original video'}</span>{source ? <video controls playsInline preload="metadata" src={source} onLoadedMetadata={handleSourceMetadata} /> : <div className="empty-video"><Film size={20} /> {zh ? '请先上传视频' : 'Upload a video first'}</div>}</div><div className="xmax-media-card"><span className="media-label">{zh ? '转绘结果' : 'Transformation result'}</span>{result?.objectUrl || status === 'recording' || status === 'generating' ? <video ref={previewRef} controls playsInline src={result?.objectUrl} className="xmax-result-video" /> : <div className="empty-video"><Sparkles size={20} /> {zh ? '生成后显示实时结果' : 'The live result will appear after generation'}</div>}</div></div><video ref={inputRef} className="xmax-hidden-video" muted playsInline preload="auto" src={source} onLoadedMetadata={handleSourceMetadata} onEnded={() => void adapterRef.current?.stop('completed')} /><video ref={outputRef} className="xmax-hidden-video" muted playsInline autoPlay /><div ref={localContainerRef} className="xmax-sdk-container"><span className="xmax-sdk-container-label">Xmax Local Container</span></div><div ref={remoteContainerRef} className="xmax-sdk-container"><span className="xmax-sdk-container-label">Xmax Remote Container</span></div><div className="xmax-control-grid"><label className="field-label">{zh ? '目标画风' : 'Target style'}<select value={style} onChange={(event) => setStyle(event.target.value)} disabled={running}>{styleOptions.map(([value, en, cn]) => <option key={value} value={value}>{zh ? cn : en}</option>)}</select></label><label className="field-label">{zh ? '目标语言' : 'Target language'}<select value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value)} disabled={running}>{languageOptions.map(([value, en, cn]) => <option key={value} value={value}>{zh ? cn : en}</option>)}</select></label></div><div className="xmax-reference"><div>{referenceImage ? <img src={referenceImage} alt={referenceCharacter?.name || 'Character reference'} /> : <div className="xmax-reference-empty"><Users size={18} /> {zh ? '没有已确认的主参考图' : 'No confirmed primary reference'}</div>}</div><div><span className="section-label">{zh ? '确认角色卡' : 'Confirmed character card'}</span><strong>{referenceCharacter?.name || (zh ? '未确认' : 'Not confirmed')}</strong><p>{referenceImage ? (zh ? '将使用此主参考图作为实时转绘参考。' : 'This primary reference will be sent as the realtime reference image.') : (zh ? '请先确认角色卡。' : 'Please confirm a character card first.')}</p></div></div><div className="xmax-summary"><div><span>{zh ? '当前状态' : 'Status'}</span><strong>{statusLabel[status]}</strong></div><div><span>{zh ? '已处理时长' : 'Processed'}</span><strong>{displaySeconds.toFixed(1)}s{sourceDuration ? ` / ${sourceDuration.toFixed(1)}s` : ''}</strong></div><div><span>{zh ? '预计 credits' : 'Estimated credits'}</span><strong>{displaySeconds.toFixed(1)}</strong><small>{zh ? '按已处理秒数 × 1' : 'Processed seconds × 1'}</small></div></div><div className="xmax-actions">{running ? <button className="danger-button" onClick={() => void stop()}><X size={16} /> {zh ? '停止' : 'Stop'}</button> : <button className="primary-button" onClick={() => void start()} disabled={!source || !referenceImage || !xmaxConfigured}><Sparkles size={17} /> {zh ? '生成海外版' : 'Generate overseas version'}</button>}{result && <><a className="secondary-button" href={result.objectUrl} download="overseas-version.webm"><Download size={16} /> {zh ? '下载结果' : 'Download result'}</a><button className="secondary-button" onClick={() => navigate(flowRoute(project.id, 'result', episodeId))}><ArrowRight size={16} /> {zh ? '查看结果' : 'View result'}</button></>}<span className="xmax-disclaimer">{zh ? '本地开发配置只用于验证流程；生产部署密钥方案待 Xmax 确认。' : 'Local development configuration only; the production credential flow requires confirmation with Xmax.'}</span></div></section></div>
}

function OneClickVisualPanelV2({ project }: { project: Project }) {
  const { locale } = useI18n()
  const episodeId = queryEpisodeId() || project.active_episode_id
  const zh = locale === 'zh-CN'
  const [state, setState] = useState<VisualTransformationState | null>(null)
  const [style, setStyle] = useState(project.settings.style_id || 'cinematic-realism')
  const [targetLanguage, setTargetLanguage] = useState(project.settings.target_language || 'english')
  const [extra, setExtra] = useState('')
  const [model, setModel] = useState('')
  const [estimate, setEstimate] = useState<{ estimated_credits?: number; estimated_cost_usd?: number; duration_seconds?: number; note?: string; model?: string; prompt_summary_zh?: string; prompt_text?: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [fineOpen, setFineOpen] = useState(false)

  function load() {
    if (!episodeId) return
    void api<VisualTransformationState>(`/episodes/${episodeId}/visual-transformation`).then((next) => {
      setState(next)
      setModel((current) => current || next.provider.model || next.provider.models?.[0]?.id || '')
    }).catch((reason) => setError((reason as Error).message))
  }
  useEffect(load, [episodeId])
  const task = state?.task || null
  const running = Boolean(task && ['queued', 'preparing', 'submitting', 'processing', 'downloading'].includes(task.status))
  const failed = Boolean(task && ['failed', 'cancelled', 'timed_out'].includes(task.status))
  useEffect(() => { if (!running) return; const timer = window.setInterval(load, 4000); return () => window.clearInterval(timer) }, [running, task?.id])
  if (!episodeId) return null

  const selectedModel = model || state?.provider.model || ''
  const modelInfo = state?.provider.models?.find((item) => item.id === selectedModel)
  const styles: Array<[string, string]> = [['western-live-action', zh ? '\u6b27\u7f8e\u4eff\u771f\u4eba' : 'Western Live Action'], ['japanese-anime', zh ? '\u65e5\u6f2b' : 'Japanese Anime'], ['korean-webtoon', zh ? '\u97e9\u6f2b' : 'Korean Webtoon'], ['american-comic', zh ? '\u7f8e\u6f2b' : 'American Comic'], ['3d-animation', '3D Animation'], ['cinematic-realism', zh ? '\u7535\u5f71\u5199\u5b9e' : 'Cinematic Realism']]
  const languages: Array<[string, string]> = [['english', zh ? '\u82f1\u8bed' : 'English'], ['japanese', zh ? '\u65e5\u8bed' : 'Japanese'], ['korean', zh ? '\u97e9\u8bed' : 'Korean'], ['spanish', zh ? '\u897f\u73ed\u7259\u8bed' : 'Spanish'], ['french', zh ? '\u6cd5\u8bed' : 'French']]
  const status = task?.status === 'completed' ? (zh ? '\u5df2\u5b8c\u6210' : 'Completed') : task?.status === 'failed' ? (zh ? '\u5931\u8d25\uff0c\u53ef\u91cd\u65b0\u51c6\u5907' : 'Failed; ready to prepare again') : running ? (zh ? '\u5904\u7406\u4e2d' : 'Processing') : (zh ? '\u5f85\u5f00\u59cb' : 'Ready')
  async function saveChoices() { await api(`/projects/${project.id}/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...project.settings, style_id: style, target_language: targetLanguage }) }) }
  async function prepare() {
    setLoading(true); setError('')
    try {
      await saveChoices()
      const next = await api<{ estimate?: typeof estimate }>(`/episodes/${episodeId}/visual-transformation/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target_style: style, visual_scope: 'full-scene', extra_description: extra, model: selectedModel || undefined, confirm_fee: false }) })
      setEstimate(next.estimate || null)
    } catch (reason) { setError((reason as Error).message) } finally { setLoading(false) }
  }
  async function submit() {
    if (!estimate || !window.confirm(zh ? '\u8fd9\u662f\u4e00\u6b21\u4ed8\u8d39\u7684 Runway \u4efb\u52a1\u3002\u786e\u8ba4\u8981\u63d0\u4ea4\u5417\uff1f' : 'This starts a paid Runway task. Submit it now?')) return
    setLoading(true); setError('')
    try {
      await saveChoices()
      const next = await api<VisualTransformationTask>(`/episodes/${episodeId}/visual-transformation/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target_style: style, visual_scope: 'full-scene', extra_description: extra, model: selectedModel || undefined, confirm_fee: true }) })
      setEstimate(null); setState((current) => current ? { ...current, task: next } : current)
    } catch (reason) { setError((reason as Error).message) } finally { setLoading(false) }
  }
  async function cancel() { if (!task) return; try { await api(`/visual-transformation-tasks/${task.id}/cancel`, { method: 'POST' }); load() } catch (reason) { setError((reason as Error).message) } }
  return <section className="b1-panel one-click-visual-panel">
    <div className="b1-heading"><div><span className="section-label">B1 / ONE-CLICK TRANSFORMATION</span><h2>{zh ? '\u4e00\u952e\u8f6c\u7ed8' : 'One-click transformation'}</h2><p>{zh ? '\u4f7f\u7528\u5f53\u524d\u5267\u96c6\u7684\u5b8c\u6574\u89c6\u9891\uff0c\u7edf\u4e00\u8f6c\u6362\u4eba\u7269\u3001\u670d\u88c5\u3001\u80cc\u666f\u3001\u9053\u5177\u548c\u5149\u5f71\uff0c\u5e76\u4fdd\u7559\u52a8\u4f5c\u4e0e\u6784\u56fe\u3002' : 'Transform the full scene including people, wardrobe, background, props and lighting while preserving action and composition.'}</p></div><span className={`b1-provider ${state?.provider.configured ? 'ready' : 'disabled'}`}>{state?.provider.configured ? (zh ? '\u5df2\u914d\u7f6e' : 'Ready') : (zh ? '\u672a\u914d\u7f6e' : 'Disabled')}</span></div>
    {error && <InlineError message={error} />}
    <div className="one-click-summary"><div><span>{zh ? '\u5f53\u524d\u5267\u96c6' : 'Current episode'}</span><strong>{project.episodes?.find((item) => item.id === episodeId)?.title || episodeId}</strong></div><div><span>{zh ? '\u5b8c\u6574\u8f93\u5165\u65f6\u957f' : 'Full input duration'}</span><strong>{state?.video_duration ? `${state.video_duration.toFixed(2)}s` : (zh ? '\u8bfb\u53d6\u4e2d' : 'Reading')}</strong></div><div><span>{zh ? '\u81ea\u52a8\u9009\u62e9\u6a21\u578b' : 'Auto-selected model'}</span><strong>{modelInfo?.label || selectedModel || (zh ? '\u8bfb\u53d6\u4e2d' : 'Reading')}</strong></div><div><span>{zh ? '\u4efb\u52a1\u72b6\u6001' : 'Task status'}</span><strong>{status}</strong></div></div>
    <div className="b1-grid"><label className="field-label">{zh ? '\u76ee\u6807\u98ce\u683c' : 'Target style'}<select value={style} onChange={(event) => setStyle(event.target.value)} disabled={running}>{styles.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="field-label">{zh ? '\u76ee\u6807\u8bed\u8a00' : 'Target language'}<select value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value)} disabled={running}>{languages.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="field-label">{zh ? '\u8f6c\u7ed8\u8303\u56f4' : 'Transformation scope'}<input value={zh ? '\u5168\u573a\u666f' : 'Full scene'} readOnly /></label></div>
    <label className="field-label b1-extra">{zh ? '\u7279\u6b8a\u8981\u6c42\uff08\u53ef\u9009\uff09' : 'Special requirement (optional)'}<textarea value={extra} onChange={(event) => setExtra(event.target.value)} placeholder={zh ? '\u53ef\u7559\u7a7a\uff1b\u7cfb\u7edf\u4f1a\u81ea\u52a8\u751f\u6210\u65b9\u6848' : 'Optional; the system generates the prompt automatically.'} disabled={running} /></label>
    <div className="one-click-actions"><button className="primary-button" disabled={loading || running || !state?.provider.configured || !state?.video_duration} onClick={() => void prepare()}>{loading ? <LoaderCircle className="spin" size={17} /> : <Gauge size={17} />} {failed ? (zh ? '\u91cd\u65b0\u51c6\u5907\u5e76\u91cd\u65b0\u8ba1\u4ef7' : 'Prepare again') : (zh ? '\u8bfb\u53d6\u8d39\u7528\u5e76\u9884\u89c8' : 'Review cost and prepare')}</button>{running && <button className="secondary-button" onClick={() => void cancel()}><X size={15} /> {zh ? '\u53d6\u6d88\u4efb\u52a1' : 'Cancel task'}</button>}</div>
    {estimate && <div className="b1-estimate"><strong>{zh ? '\u8d39\u7528\u786e\u8ba4' : 'Fee confirmation'}</strong><span>{zh ? `\u672c\u6b21 ${estimate.duration_seconds?.toFixed(2) || '-'} \u79d2\uff0c\u9884\u8ba1 ${estimate.estimated_credits ?? '-'} credits\uff0c\u9884\u8ba1\u8d39\u7528 ${estimate.estimated_cost_usd == null ? '\u4ee5 Provider \u8d26\u5355\u4e3a\u51c6' : `$${estimate.estimated_cost_usd.toFixed(2)}`}\u3002` : `${estimate.duration_seconds?.toFixed(2) || '-'} seconds, estimated ${estimate.estimated_credits ?? '-'} credits, estimated cost ${estimate.estimated_cost_usd == null ? 'Provider invoice' : `$${estimate.estimated_cost_usd.toFixed(2)}`}.`}</span><small>{estimate.note}</small><button className="primary-button" disabled={loading} onClick={() => void submit()}><Check size={15} /> {zh ? '\u786e\u8ba4\u8d39\u7528\u5e76\u63d0\u4ea4' : 'Confirm fee and submit'}</button></div>}
    <details className="b1-prompt-details"><summary>{zh ? '\u67e5\u770b\u81ea\u52a8\u65b9\u6848\u4e0e Prompt' : 'View automatic plan and Prompt'}</summary><p>{estimate?.prompt_summary_zh || state?.task?.prompt_summary_zh || (zh ? '\u5c06\u6839\u636e\u89c6\u9891\u3001\u98ce\u683c\u548c\u9879\u76ee\u89d2\u8272\u751f\u6210\u5168\u573a\u666f\u65b9\u6848\u3002' : 'Built from the full video, style and project characters.')}</p><code>{estimate?.prompt_text || state?.task?.prompt_text || (zh ? '\u9884\u4f30\u524d\u4f1a\u751f\u6210\u82f1\u6587 Prompt' : 'The English Prompt is generated before the estimate.')}</code></details>
    <details className="b1-fine-adjustment" open={fineOpen} onToggle={(event) => setFineOpen((event.currentTarget as HTMLDetailsElement).open)}><summary>{zh ? '\u5931\u8d25\u540e\u5207\u6362\u6a21\u578b / \u7ec6\u8c03' : 'Switch model after failure / fine adjustment'}</summary><label className="field-label">{zh ? '\u6a21\u578b' : 'Model'}<select value={selectedModel} onChange={(event) => setModel(event.target.value)} disabled={running}>{(state?.provider.models || []).map((item) => <option key={item.id} value={item.id}>{`${item.label} · ${item.min_seconds || 0}-${item.max_seconds || 0}s · ${item.estimated_credits ?? '-'} credits · ${item.video_to_video ? 'video-to-video' : 'unavailable'}`}</option>)}</select></label><p className="muted-copy">{zh ? '\u6a21\u578b\u5207\u6362\u53ea\u4f1a\u6539\u53d8\u4e0b\u4e00\u6b21\u4f30\u8d39\uff0c\u4e0d\u4f1a\u81ea\u52a8\u91cd\u65b0\u63d0\u4ea4\u6216\u4ea7\u751f\u7b2c\u4e8c\u6b21\u8d39\u7528\u3002' : 'Switching only changes the next estimate; it never auto-submits or creates a second paid task.'}</p></details>
    {task && <div className="b1-task-status"><span>{status}</span>{task.provider_task_id && <span>Provider task: {task.provider_task_id}</span>}{task.error_message && <details><summary>{zh ? '\u67e5\u770b\u6280\u672f\u8be6\u60c5' : 'Technical details'}</summary><code>{task.error_code || ''} {task.error_message}</code></details>}{task.output?.local_output_url && <video className="b1-output-video" controls src={assetUrl(task.output.local_output_url)} />}</div>}
  </section>
}

function OneClickVisualPanel({ project }: { project: Project }) {
  const { locale } = useI18n()
  const episodeId = queryEpisodeId() || project.active_episode_id
  const zh = locale === 'zh-CN'
  const [state, setState] = useState<VisualTransformationState | null>(null)
  const [style, setStyle] = useState(project.settings.style_id || 'cinematic-realism')
  const [targetLanguage, setTargetLanguage] = useState(project.settings.target_language || 'english')
  const [extra, setExtra] = useState('')
  const [start, setStart] = useState(0)
  const [end, setEnd] = useState(0)
  const [model, setModel] = useState('')
  const [estimate, setEstimate] = useState<{ estimated_credits?: number; estimated_cost_usd?: number; duration_seconds?: number; note?: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [fineOpen, setFineOpen] = useState(false)
  const [developerOpen, setDeveloperOpen] = useState(false)

  const load = () => { if (!episodeId) return; api<VisualTransformationState>(`/episodes/${episodeId}/visual-transformation`).then((next) => { setState(next); setEnd(next.video_duration || next.task?.duration || 0); if (!model) setModel(next.provider.model || next.provider.models?.[0]?.id || '') }).catch((reason) => setError((reason as Error).message)) }
  useEffect(() => { load() }, [episodeId])
  useEffect(() => { if (!state?.task || !['queued', 'preparing', 'submitting', 'processing', 'downloading'].includes(state.task.status)) return; const timer = window.setInterval(load, 4000); return () => window.clearInterval(timer) }, [state?.task?.id, state?.task?.status])

  if (!episodeId) return null
  const task = state?.task || null
  const running = Boolean(task && ['queued', 'preparing', 'submitting', 'processing', 'downloading'].includes(task.status))
  const failed = Boolean(task && ['failed', 'cancelled', 'timed_out'].includes(task.status))
  const styles: Array<[string, string]> = [['western-live-action', zh ? '\u6b27\u7f8e\u4eff\u771f\u4eba' : 'Western Live Action'], ['japanese-anime', zh ? '\u65e5\u6f2b' : 'Japanese Anime'], ['korean-webtoon', zh ? '\u97e9\u6f2b' : 'Korean Webtoon'], ['american-comic', zh ? '\u7f8e\u6f2b' : 'American Comic'], ['3d-animation', '3D Animation'], ['cinematic-realism', zh ? '\u7535\u5f71\u5199\u5b9e' : 'Cinematic Realism']]
  const languages: Array<[string, string]> = [['english', zh ? '\u82f1\u8bed' : 'English'], ['japanese', zh ? '\u65e5\u8bed' : 'Japanese'], ['korean', zh ? '\u97e9\u8bed' : 'Korean'], ['spanish', zh ? '\u897f\u73ed\u7259\u8bed' : 'Spanish'], ['french', zh ? '\u6cd5\u8bed' : 'French']]
  const episode = project.episodes?.find((item) => item.id === episodeId)
  async function saveChoices() { await api(`/projects/${project.id}/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...project.settings, style_id: style, target_language: targetLanguage }) }) }
  async function prepare() { setLoading(true); setError(''); try { await saveChoices(); const payload: Record<string, unknown> = { target_style: style, visual_scope: 'full-scene', extra_description: extra, model: model || undefined, confirm_fee: false }; if (developerOpen) { payload.start_time = start; payload.end_time = end || undefined }; const next = await api<{ estimate?: typeof estimate }>(`/episodes/${episodeId}/visual-transformation/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); setEstimate(next.estimate || null) } catch (reason) { setError((reason as Error).message) } finally { setLoading(false) } }
  async function submit() { if (!estimate || !window.confirm(zh ? '\u8fd9\u662f\u4e00\u6b21\u4ed8\u8d39\u7684 Runway \u4efb\u52a1\u3002\u786e\u8ba4\u8981\u63d0\u4ea4\u5417\uff1f' : 'This starts a paid Runway task. Submit it now?')) return; setLoading(true); setError(''); try { await saveChoices(); const payload: Record<string, unknown> = { target_style: style, visual_scope: 'full-scene', extra_description: extra, model: model || undefined, confirm_fee: true }; if (developerOpen) { payload.start_time = start; payload.end_time = end || undefined }; const next = await api<VisualTransformationTask>(`/episodes/${episodeId}/visual-transformation/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); setEstimate(null); setState((current) => current ? { ...current, task: next } : current) } catch (reason) { setError((reason as Error).message) } finally { setLoading(false) } }
  async function cancel() { if (!task) return; try { await api(`/visual-transformation-tasks/${task.id}/cancel`, { method: 'POST' }); load() } catch (reason) { setError((reason as Error).message) } }
  const statusLabel = task?.status === 'completed' ? (zh ? '\u5df2\u5b8c\u6210' : 'Completed') : task?.status === 'failed' ? (zh ? '\u5931\u8d25\uff0c\u53ef\u91cd\u65b0\u51c6\u5907' : 'Failed; ready to prepare again') : task?.status === 'cancelled' ? (zh ? '\u5df2\u53d6\u6d88' : 'Cancelled') : task?.status === 'timed_out' ? (zh ? '\u8d85\u65f6' : 'Timed out') : task ? (zh ? '\u5904\u7406\u4e2d' : 'Processing') : (zh ? '\u5f85\u5f00\u59cb' : 'Ready')
  const selectedModelLabel = state?.provider.models?.find((item) => item.id === (model || state?.provider.model))?.label || model || state?.provider.model || (zh ? '\u8bfb\u53d6\u4e2d' : 'Reading')
  return <section className="b1-panel one-click-visual-panel"><div className="b1-heading"><div><span className="section-label">B1 / ONE-CLICK TRANSFORMATION</span><h2>{zh ? '\u4e00\u952e\u8f6c\u7ed8' : 'One-click transformation'}</h2><p>{zh ? '\u9ed8\u8ba4\u4f7f\u7528\u5f53\u524d\u5267\u96c6\u7684\u5b8c\u6574\u89c6\u9891\uff0c\u53ea\u6539\u53d8\u89c6\u89c9\u98ce\u683c\uff0c\u4fdd\u7559\u52a8\u4f5c\u3001\u955c\u5934\u548c\u6784\u56fe\u3002' : 'The full current episode is used by default. Change the visual style while preserving action, camera and composition.'}</p></div><span className={`b1-provider ${state?.provider.configured ? 'ready' : 'disabled'}`}>{state?.provider.configured ? (zh ? '\u5df2\u914d\u7f6e' : 'Ready') : (zh ? '\u672a\u914d\u7f6e' : 'Disabled')}</span></div>{error && <InlineError message={error} />}{!state?.provider.configured && <div className="b1-warning">{zh ? '\u5f53\u524d\u6ca1\u6709\u53ef\u7528\u7684\u89c6\u89c9 Provider\uff0c\u4e0d\u4f1a\u4f7f\u7528 Mock \u56de\u9000\u3002' : 'No visual provider is configured. There is no Mock fallback.'}</div>}<div className="one-click-summary"><div><span>{zh ? '\u5f53\u524d\u5267\u96c6' : 'Current episode'}</span><strong>{episode?.title || episodeId}</strong></div><div><span>{zh ? '\u8f93\u5165\u65f6\u957f' : 'Input duration'}</span><strong>{state?.video_duration ? `${state.video_duration.toFixed(2)}s` : (zh ? '\u8bfb\u53d6\u4e2d' : 'Reading')}</strong></div><div><span>{zh ? '\u4efb\u52a1\u72b6\u6001' : 'Task status'}</span><strong>{statusLabel}</strong></div></div><div className="b1-grid"><label className="field-label">{zh ? '\u76ee\u6807\u98ce\u683c' : 'Target style'}<select value={style} onChange={(e) => setStyle(e.target.value)} disabled={running}>{styles.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label className="field-label">{zh ? '\u76ee\u6807\u8bed\u8a00' : 'Target language'}<select value={targetLanguage} onChange={(e) => setTargetLanguage(e.target.value)} disabled={running}>{languages.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label className="field-label"><span>{zh ? '\u8f6c\u7ed8\u8303\u56f4' : 'Transformation scope'}</span><input value={zh ? '\u5168\u573a\u666f\uff08\u5f53\u524d\u53ef\u7528\uff09' : 'Full scene (available)'} readOnly /></label></div><label className="field-label b1-extra">{zh ? '\u7279\u6b8a\u8981\u6c42\uff08\u53ef\u9009\uff09' : 'Special requirement (optional)'}<textarea value={extra} onChange={(e) => setExtra(e.target.value)} placeholder={zh ? '\u53ef\u4ee5\u7559\u7a7a\uff1b\u81ea\u52a8\u63d0\u793a\u4f1a\u6839\u636e\u89c6\u9891\u548c\u98ce\u683c\u751f\u6210' : 'Optional. The automatic prompt is built from the video and style.'} disabled={running} /></label><div className="one-click-actions"><button className="primary-button" disabled={loading || running || !state?.provider.configured || !state?.video_duration} onClick={() => void prepare()}>{loading ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />} {failed ? (zh ? '\u91cd\u65b0\u51c6\u5907\u8f6c\u7ed8' : 'Prepare again') : (zh ? '\u83b7\u53d6\u8d39\u7528\u5e76\u9884\u89c8' : 'Review cost and prepare')}</button>{task && running && <button className="secondary-button" onClick={() => void cancel()}><X size={15} /> {zh ? '\u53d6\u6d88\u4efb\u52a1' : 'Cancel task'}</button>}</div>{estimate && <div className="b1-estimate"><strong>{zh ? '\u8d39\u7528\u786e\u8ba4' : 'Fee confirmation'}</strong><span>{zh ? `\u672c\u6b21\u5c06\u5904\u7406 ${estimate.duration_seconds?.toFixed(2) || '-'} \u79d2\uff0c\u8d39\u7528\u4ee5 Provider \u5b9e\u9645\u8d26\u5355\u4e3a\u51c6\u3002` : `This task will process ${estimate.duration_seconds?.toFixed(2) || '-'} seconds; the Provider invoice is authoritative.`}</span><button className="primary-button" disabled={loading} onClick={() => void submit()}><Check size={15} /> {zh ? '\u786e\u8ba4\u8d39\u7528\u5e76\u63d0\u4ea4' : 'Confirm fee and submit'}</button></div>}<details className="b1-prompt-details"><summary>{zh ? '\u67e5\u770b\u81ea\u52a8\u63d0\u793a' : 'View automatic prompt'}</summary><p>{state?.task?.prompt_summary_zh || (zh ? '\u5c06\u6839\u636e\u89c6\u9891\u3001\u98ce\u683c\u548c\u5df2\u9501\u5b9a\u89d2\u8272\u751f\u6210\u3002' : 'Built from the video, style and locked project characters.')}</p><code>{state?.task?.prompt_text || (zh ? '\u63d0\u4ea4\u524d\u4f1a\u81ea\u52a8\u751f\u6210\u82f1\u6587\u63d0\u793a\u3002' : 'The English prompt is generated when the task is prepared.')}</code></details><details className="b1-fine-adjustment" open={fineOpen} onToggle={(event) => setFineOpen((event.currentTarget as HTMLDetailsElement).open)}><summary>{zh ? '\u7ec6\u8c03\u4e0e\u6280\u672f\u8bbe\u7f6e' : 'Fine adjustment and technical settings'}</summary><div className="b1-grid"><label className="field-label">{zh ? '\u4f7f\u7528\u7684\u6a21\u578b' : 'Model'}<select value={model} onChange={(e) => setModel(e.target.value)} disabled={running}>{(state?.provider.models || []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><label className="field-label">{zh ? '\u5206\u6bb5\u5904\u7406' : 'Long-video handling'}<input value={zh ? '\u8d85\u8fc7\u6a21\u578b\u9650\u5236\u65f6\u540e\u7aef\u6309\u5206\u6bb5\u6392\u961f' : 'Backend queues segments when provider limits require it'} readOnly /></label></div><details open={developerOpen} onToggle={(event) => setDeveloperOpen((event.currentTarget as HTMLDetailsElement).open)}><summary>{zh ? '\u5f00\u53d1\u8005\uff1a\u624b\u52a8\u65f6\u95f4\u8303\u56f4' : 'Developer: manual time range'}</summary><div className="b1-grid"><label className="field-label">Start (seconds)<input type="number" min="0" step="0.1" value={start} onChange={(e) => setStart(Number(e.target.value))} disabled={running} /></label><label className="field-label">End (seconds)<input type="number" min="4" step="0.1" value={end} onChange={(e) => setEnd(Number(e.target.value))} disabled={running} /></label></div></details></details>{task && <div className="b1-task-status"><span>{statusLabel}</span>{task.progress != null && <strong>{task.progress}%</strong>}{task.error_message && <details><summary>{zh ? '\u67e5\u770b\u6280\u672f\u8be6\u60c5' : 'Technical details'}</summary><code>{task.error_code || ''} {task.error_message}</code></details>}{task.output?.local_output_url && <video className="b1-output-video" controls src={assetUrl(task.output.local_output_url)} />}</div>}</section>
}

function B1RecoveryPanel({ project }: { project: Project }) {
  const { locale } = useI18n()
  const episodeId = queryEpisodeId() || project.active_episode_id
  const [state, setState] = useState<VisualTransformationState | null>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  async function load() { if (!episodeId) return; try { setState(await api<VisualTransformationState>(`/episodes/${episodeId}/visual-transformation`)); setError('') } catch (reason) { setError((reason as Error).message) } }
  useEffect(() => { void load() }, [episodeId])
  if (!episodeId || !state?.task) return null
  const task = state.task
  async function refreshProvider() { try { const next = await api<VisualTransformationTask>(`/visual-transformation-tasks/${task.id}/refresh`, { method: 'POST' }); setState((current) => current ? { ...current, task: next } : current); setMessage(locale === 'zh-CN' ? '\u5df2\u91cd\u65b0\u8bfb\u53d6 Provider \u72b6\u6001' : 'Provider status refreshed without creating a new task.') } catch (reason) { setError((reason as Error).message) } }
  async function removeTask() { if (!window.confirm(locale === 'zh-CN' ? '\u5220\u9664\u5f53\u524d\u4efb\u52a1\u8bb0\u5f55\uff1f' : 'Delete this task record?')) return; try { await api(`/visual-transformation-tasks/${task.id}`, { method: 'DELETE' }); setMessage(locale === 'zh-CN' ? '\u4efb\u52a1\u8bb0\u5f55\u5df2\u5220\u9664' : 'Task record deleted.'); setState((current) => current ? { ...current, task: null } : current) } catch (reason) { setError((reason as Error).message) } }
  return <section className="b1-recovery"><div><strong>{locale === 'zh-CN' ? '\u4efb\u52a1\u6062\u590d' : 'Task recovery'}</strong><span>{locale === 'zh-CN' ? '\u4ec5\u91cd\u8bfb Provider \u72b6\u6001\uff0c\u4e0d\u4f1a\u81ea\u52a8\u521b\u5efa\u4ed8\u8d39\u4efb\u52a1\u3002' : 'Refreshes Provider state only; it never submits a new paid task automatically.'}</span></div><div className="b1-recovery-actions"><button className="secondary-button compact" onClick={() => void load()}><RefreshCw size={14} /> {locale === 'zh-CN' ? '\u624b\u52a8\u5237\u65b0' : 'Refresh local state'}</button>{task.provider_task_id && <button className="secondary-button compact" onClick={() => void refreshProvider()}><RefreshCw size={14} /> {locale === 'zh-CN' ? '\u8bfb\u53d6 Provider \u72b6\u6001' : 'Read Provider status'}</button>}{['failed', 'cancelled', 'timed_out'].includes(task.status) && <button className="primary-button compact" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>{locale === 'zh-CN' ? '\u4f7f\u7528\u5f53\u524d\u8bbe\u7f6e\u91cd\u65b0\u751f\u6210' : 'Regenerate with same settings'}</button>}<button className="danger-button compact" onClick={() => void removeTask()}><Trash2 size={14} /> {locale === 'zh-CN' ? '\u5220\u9664\u4efb\u52a1\u8bb0\u5f55' : 'Delete task record'}</button></div>{message && <p className="toast-line">{message}</p>}{error && <InlineError message={error} />}</section>
}

function VisualTransformationPanel({ project }: { project: Project }) {
  const { locale } = useI18n()
  const episodeId = queryEpisodeId() || project.active_episode_id
  const [state, setState] = useState<VisualTransformationState | null>(null)
  const [start, setStart] = useState(0)
  const [end, setEnd] = useState(10)
  const [style, setStyle] = useState(project.settings.style_id || 'cinematic-realism')
  const [extra, setExtra] = useState('')
  const [estimate, setEstimate] = useState<{ estimated_credits?: number; estimated_cost_usd?: number; duration_seconds?: number; note?: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const zh = locale === 'zh-CN'
  const refresh = () => { if (episodeId) api<VisualTransformationState>(`/episodes/${episodeId}/visual-transformation`).then((next) => { setState(next); const duration = next.task?.input?.duration || next.task?.duration || 10; setEnd(Math.min(10, Math.max(4, duration))) }).catch((e) => setError((e as Error).message)) }
  useEffect(refresh, [episodeId])
  useEffect(() => { if (!state?.task || !['queued', 'preparing', 'submitting', 'processing', 'downloading'].includes(state.task.status)) return; const timer = window.setInterval(refresh, 4000); return () => window.clearInterval(timer) }, [state?.task?.id, state?.task?.status])
  if (!episodeId) return null
  const task = state?.task
  const running = Boolean(task && ['queued', 'preparing', 'submitting', 'processing', 'downloading'].includes(task.status))
  const styles = [['western-fantasy', zh ? '西部奇幻' : 'Western Fantasy'], ['western-live-action', zh ? '欧美仿真人' : 'Western Live Action'], ['japanese-anime', zh ? '日漫' : 'Japanese Anime'], ['korean-webtoon', zh ? '韩漫' : 'Korean Webtoon'], ['american-comic', zh ? '美漫' : 'American Comic'], ['3d-animation', zh ? '3D动画' : '3D Animation'], ['cinematic-realism', zh ? '电影写实' : 'Cinematic Realism']]
  async function prepare() { setLoading(true); setError(''); try { const next = await api<{ requires_fee_confirmation?: boolean; estimate?: typeof estimate }>(`/episodes/${episodeId}/visual-transformation/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ start_time: start, end_time: end, target_style: style, visual_scope: 'full-scene', extra_description: extra, confirm_fee: false }) }); setEstimate(next.estimate || null) } catch (e) { setError((e as Error).message) } finally { setLoading(false) } }
  async function submit() { setLoading(true); setError(''); try { const next = await api<VisualTransformationTask>(`/episodes/${episodeId}/visual-transformation/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ start_time: start, end_time: end, target_style: style, visual_scope: 'full-scene', extra_description: extra, confirm_fee: true }) }); setEstimate(null); setState((current) => current ? { ...current, task: next } : current) } catch (e) { setError((e as Error).message) } finally { setLoading(false) } }
  async function cancel() { if (!task) return; try { await api(`/visual-transformation-tasks/${task.id}/cancel`, { method: 'POST' }); refresh() } catch (e) { setError((e as Error).message) } }
  return <section className="b1-panel"><div className="b1-heading"><div><span className="section-label">B1 / REAL VISUAL TRANSFORMATION</span><h2>{zh ? '真实转绘 PoC' : 'Real visual transformation PoC'}</h2><p>{zh ? '只处理当前单集的4–10秒全场景片段，不会提交整集视频。' : 'Only the current episode 4–10 second full-scene clip is submitted; the full episode is never sent.'}</p></div><span className={`b1-provider ${state?.provider.configured ? 'ready' : 'disabled'}`}>{state?.provider.configured ? 'Runway' : (zh ? '未配置' : 'Disabled')}</span></div>{error && <InlineError message={error} />} {!state?.provider.configured && <div className="b1-warning">{zh ? 'Runway 未配置。请在项目根目录 .env 填写 RUNWAY_API_KEY；不会使用 Mock 回退。' : 'Runway is not configured. Add RUNWAY_API_KEY to the project-root .env. There is no Mock fallback.'}</div>}<div className="b1-grid"><label className="field-label">{zh ? '开始时间（秒）' : 'Start (seconds)'}<input type="number" min="0" step="0.1" value={start} onChange={(e) => setStart(Number(e.target.value))} disabled={running} /></label><label className="field-label">{zh ? '结束时间（秒）' : 'End (seconds)'}<input type="number" min="4" max="10" step="0.1" value={end} onChange={(e) => setEnd(Number(e.target.value))} disabled={running} /></label><label className="field-label">{zh ? '目标风格' : 'Target style'}<select value={style} onChange={(e) => setStyle(e.target.value)} disabled={running}>{styles.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label className="field-label">{zh ? '转绘范围' : 'Visual scope'}<select value="full-scene" disabled><option value="full-scene">{zh ? '全场景（当前唯一可用）' : 'Full scene (only available scope)'}</option></select></label></div><label className="field-label b1-extra">{zh ? '补充描述（可选）' : 'Extra direction (optional)'}<textarea value={extra} onChange={(e) => setExtra(e.target.value)} placeholder={zh ? '保持镜头、动作和构图，仅改变视觉风格' : 'Preserve camera, action and composition; only change visual style.'} disabled={running} /></label><div className="b1-meta"><span>{zh ? '模型' : 'Model'}: <strong>{state?.provider.model || 'seedance2_fast'}</strong></span><span>{zh ? '当前集' : 'Episode'}: <strong>{String(project.episodes?.find((item) => item.id === episodeId)?.episode_number || '').padStart(2, '0')}</strong></span><span>{zh ? '锁定角色' : 'Locked characters'}: <strong>{state?.locked_characters.length || 0}</strong>{project.processing_mode === 'quick-transformation' && <small> · {zh ? 'Quick 不强制角色库' : 'Quick does not require a library'}</small>}</span></div>{project.processing_mode === 'character-consistency' && <div className="b1-info">{zh ? '角色一致性模式：将读取项目级 Locked Characters 的主参考图；未锁定角色不宣称跨集一致。' : 'Character Consistency mode: primary references from project Locked Characters are used; unlocked roles are not claimed to be consistent across episodes.'}</div>}{task && <div className={`b1-task b1-task-${task.status}`}><div><strong>{zh ? '任务状态' : 'Task status'}: {task.status}</strong>{task.provider_task_id && <span>{zh ? 'Provider任务' : 'Provider task'}: {task.provider_task_id}</span>}</div>{task.error_message && <InlineError message={`${task.error_code || 'RUNWAY'}: ${task.error_message}`} />}{task.status === 'completed' && task.output?.local_output_url && <video className="b1-output-video" controls src={assetUrl(task.output.local_output_url)} />}{task.status === 'completed' && task.output?.local_output_url && <a className="text-button" href={assetUrl(task.output.local_output_url)} target="_blank" rel="noreferrer">{zh ? '打开本地输出' : 'Open local output'}</a>} {running && <button className="quiet-button" onClick={cancel}><X size={14} /> {zh ? '取消任务' : 'Cancel task'}</button>}</div>}{estimate && <div className="b1-fee"><strong>{zh ? '提交前费用确认' : 'Confirm before paid submission'}</strong><span>{zh ? '时长' : 'Duration'} {estimate.duration_seconds ?? (end - start).toFixed(1)}s · {zh ? '预计Credits' : 'Estimated credits'} {estimate.estimated_credits ?? '—'} · {zh ? '预计费用' : 'Estimated cost'} {estimate.estimated_cost_usd == null ? (zh ? '以Provider账单为准' : 'Provider console') : `$${estimate.estimated_cost_usd.toFixed(2)}`}</span><small>{estimate.note}</small><button className="primary-button" onClick={submit} disabled={loading || running}>{loading ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />} {zh ? '确认并提交唯一一次真实任务' : 'Confirm and submit one real task'}</button></div>}{!task && !estimate && <div className="b1-actions"><button className="secondary-button" onClick={prepare} disabled={loading || running || !state?.provider.configured}>{loading ? <LoaderCircle className="spin" size={15} /> : <Gauge size={15} />} {zh ? '读取片段和费用' : 'Prepare clip and estimate'}</button><span>{zh ? '建议4–10秒稳定单人片段；Provider限制会在提交前校验。' : 'Recommended: a stable 4–10 second single-person clip; provider limits are validated before submission.'}</span></div>}</section>
}

function GenerateStep({ project, onChange }: { project: Project; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const { t } = useI18n(); const episodeId = queryEpisodeId() || project.active_episode_id; const storageKey = `drama-task-${project.id}-${episodeId || 'project'}`; const [task, setTask] = useState<Task | null>(null); const [expanded, setExpanded] = useState(false); const [simulateFailure, setSimulateFailure] = useState(false); const [starting, setStarting] = useState(false); const [error, setError] = useState('')
  useEffect(() => { const saved = localStorage.getItem(storageKey); if (saved) api<Task>(`/tasks/${saved}`).then(setTask).catch(() => localStorage.removeItem(storageKey)) }, [storageKey])
  useEffect(() => { if (!task || task.status !== 'running') return; const timer = window.setInterval(() => api<Task>(`/tasks/${task.id}`).then((next) => { setTask(next); if (next.status === 'completed') { onChange({ ...project, status: 'Completed' }); navigate(flowRoute(project.id, 'result', episodeId)) } if (next.status === 'failed') onChange({ ...project, status: 'Failed' }) }).catch((e) => setError((e as Error).message)), 700); return () => window.clearInterval(timer) }, [task?.id, task?.status, episodeId])
  const requiresLibrary = project.processing_mode === 'character-consistency'
  async function start() { if (requiresLibrary && !project.characters.some((item) => item.confirmed && item.priority_level === 'Locked')) { setError(t('generate.confirmFirst')); return }; setStarting(true); setError(''); try { const next = await api<Task>(`/projects/${project.id}/generate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ simulate_failure: simulateFailure }) }); localStorage.setItem(storageKey, next.id); setTask(next); onChange({ ...project, status: 'Generating' }) } catch (e) { setError((e as Error).message) } finally { setStarting(false) } }
  async function cancel() { if (!task) return; const next = await api<Task>(`/tasks/${task.id}/cancel`, { method: 'POST' }); setTask(next); onChange({ ...project, status: 'Ready' }) }
  async function retry() { if (!task) return; const next = await api<Task>(`/tasks/${task.id}/retry`, { method: 'POST' }); localStorage.setItem(storageKey, next.id); setTask(next); onChange({ ...project, status: 'Generating' }) }
  const complete = task?.status === 'completed'; const failed = task?.status === 'failed'; const running = task?.status === 'running'; const confirmed = project.characters.filter((item) => item.confirmed).length
  return <div className="step-content"><DemoNotice>{t('generate.demoNotice')}</DemoNotice><div className="generate-hero"><div className="generate-orbit"><div className="orbit-inner"><Sparkles size={32} /></div></div><div><p className="eyebrow">{t('generate.heroEyebrow')}</p><h2>{complete ? t('generate.completeTitle') : failed ? t('generate.failed') : running ? t('generate.running') : t('generate.ready')}</h2><p>{complete ? t('generate.completeCopy') : t('generate.copy')}</p></div></div>{error && <InlineError message={error} />}{!task && <div className="generation-preflight"><Preflight icon={<Check size={16} />} title={t('generate.sourceValidated')} copy={project.video_filename || t('generate.uploadFirst')} done={Boolean(project.video_filename)} /><Preflight icon={<Users size={16} />} title={t('generate.characterAssets')} copy={requiresLibrary ? t('generate.confirmedOf', { confirmed, total: project.characters.length }) : (project.characters.length ? t('generate.confirmedOf', { confirmed, total: project.characters.length }) : (project.processing_mode === 'quick-transformation' ? 'Optional for Quick Transformation' : t('generate.confirmedOf', { confirmed, total: project.characters.length })))} done={requiresLibrary ? confirmed > 0 : true} /><Preflight icon={<WandSparkles size={16} />} title={t('generate.transformationBrief')} copy={catalogLabel(project.settings.style_id, 'en')} done={Boolean(project.settings.style_id)} /><label className="simulate-toggle"><input type="checkbox" checked={simulateFailure} onChange={(e) => setSimulateFailure(e.target.checked)} /><span className="toggle-switch" /><span>{t('generate.simulateFailure')}</span></label></div>}{task && <LocalizedTaskConsole task={task} expanded={expanded} onToggle={() => setExpanded(!expanded)} />}{task && running && <div className="task-actions"><button className="quiet-button" onClick={cancel}><X size={16} /> {t('generate.cancelTask')}</button></div>}{task && failed && <div className="task-actions"><button className="secondary-button" onClick={retry}><RotateCcw size={16} /> {t('generate.retryPipeline')}</button></div>}{complete && <div className="task-actions"><button className="primary-button" onClick={() => navigate(`/project/${project.id}/result`)}>{t('generate.openResult')} <ArrowRight size={16} /></button></div>}{!task && <div className="generate-cta"><button className="primary-button generate-button" disabled={starting || !project.video_filename || (requiresLibrary && confirmed < 1)} onClick={start}>{starting ? <LoaderCircle className="spin" size={18} /> : <Sparkles size={18} />} {starting ? t('generate.preparing') : t('generate.start')}</button><span>{t('generate.mockOnly')}</span></div>}</div>
}

function LocalizedTaskConsole({ task, expanded, onToggle }: { task: Task; expanded: boolean; onToggle: () => void }) {
  const { t } = useI18n(); const completed = task.steps.filter((step) => step.status === 'completed').length; const title = task.status === 'failed' ? t('generate.failed') : task.status === 'completed' ? t('generate.completeTitle') : task.current_step
  return <section className="task-console"><div className="task-console-top"><div><span className="section-label">{t('generate.heroEyebrow')} · {task.id}</span><h3>{title}</h3></div><span className={`task-status task-${task.status}`}>{localizedStatus(task.status, t)}</span></div><div className="progress-row"><div className="progress-track"><span style={{ width: `${task.progress}%` }} /></div><strong>{task.progress}%</strong></div><div className="task-meta"><span>{completed} / {task.steps.length} {t('generate.demoSimulated')}</span><span>{task.error_message || t('generate.demoNotice')}</span></div><button className="step-expander" onClick={onToggle}>{expanded ? t('common.close') : t('common.details')} {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</button>{expanded && <div className="task-steps">{task.steps.map((step) => <div className="task-step-row" key={step.step_index}><span className={`step-state state-${step.status}`}>{step.status === 'completed' ? <Check size={12} /> : step.status === 'processing' ? <LoaderCircle className="spin" size={12} /> : step.status === 'failed' ? <X size={12} /> : <span />}</span><span className="task-step-name">{String(step.step_index + 1).padStart(2, '0')} {step.name}</span><span className={`step-word word-${step.status}`}>{localizedStatus(step.status, t)}</span></div>)}</div>}</section>
}

function LegacyGenerateStep({ project, onChange }: { project: Project; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const storageKey = `drama-task-${project.id}`; const [task, setTask] = useState<Task | null>(null); const [expanded, setExpanded] = useState(false); const [simulateFailure, setSimulateFailure] = useState(false); const [starting, setStarting] = useState(false); const [error, setError] = useState('')
  useEffect(() => { const saved = localStorage.getItem(storageKey); if (saved) { api<Task>(`/tasks/${saved}`).then(setTask).catch(() => localStorage.removeItem(storageKey)) } }, [storageKey])
  useEffect(() => { if (!task || !['running'].includes(task.status)) return; const timer = window.setInterval(() => api<Task>(`/tasks/${task.id}`).then((next) => { setTask(next); if (next.status === 'completed') { onChange({ ...project, status: 'Completed' }); navigate(`/project/${project.id}/result`) } if (next.status === 'failed') onChange({ ...project, status: 'Failed' }) }).catch((e) => setError(e.message)), 700); return () => window.clearInterval(timer) }, [task?.id, task?.status])
  async function start() { setStarting(true); setError(''); try { const next = await api<Task>(`/projects/${project.id}/generate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ simulate_failure: simulateFailure }) }); localStorage.setItem(storageKey, next.id); setTask(next); onChange({ ...project, status: 'Generating' }) } catch (e) { setError((e as Error).message) } finally { setStarting(false) } }
  async function cancel() { if (!task) return; const next = await api<Task>(`/tasks/${task.id}/cancel`, { method: 'POST' }); setTask(next); onChange({ ...project, status: 'Ready' }) }
  async function retry() { if (!task) return; const next = await api<Task>(`/tasks/${task.id}/retry`, { method: 'POST' }); localStorage.setItem(storageKey, next.id); setTask(next); onChange({ ...project, status: 'Generating' }) }
  const complete = task?.status === 'completed'; const failed = task?.status === 'failed'; const running = task?.status === 'running';
  return <div className="step-content"><div className="generate-hero"><div className="generate-orbit"><div className="orbit-inner"><Sparkles size={32} /></div></div><div><p className="eyebrow">ONE-CLICK TRANSFORMATION</p><h2>{complete ? 'Your new world is ready.' : failed ? 'The mock pipeline needs a retry.' : running ? 'The studio is rebuilding your drama.' : 'Ready to transform your drama?'}</h2><p>{complete ? 'The demo result is now available for side-by-side review.' : 'We will preserve the story, movement, expression, and timing while transforming the visible world around it.'}</p></div></div>{error && <InlineError message={error} />}{!task && <div className="generation-preflight"><Preflight icon={<Check size={16} />} title="Source validated" copy={project.video_filename || 'Upload a source video first'} done={Boolean(project.video_filename)} /><Preflight icon={<Users size={16} />} title="Character assets" copy={`${project.characters.filter((item) => item.confirmed).length} confirmed of ${project.characters.length}`} done={project.characters.length > 0} /><Preflight icon={<WandSparkles size={16} />} title="Transformation brief" copy={styleName(project.settings.style_id, [])} done={Boolean(project.settings.style_id)} /><label className="simulate-toggle"><input type="checkbox" checked={simulateFailure} onChange={(e) => setSimulateFailure(e.target.checked)} /><span className="toggle-switch" /><span>Simulate a failure at Character Transformation</span></label></div>}{task && <TaskConsole task={task} expanded={expanded} onToggle={() => setExpanded(!expanded)} />}{task && running && <div className="task-actions"><button className="quiet-button" onClick={cancel}><X size={16} /> Cancel task</button></div>}{task && failed && <div className="task-actions"><button className="secondary-button" onClick={retry}><RotateCcw size={16} /> Retry pipeline</button></div>}{complete && <div className="task-actions"><button className="primary-button" onClick={() => navigate(`/project/${project.id}/result`)}>Open demo result <ArrowRight size={16} /></button></div>}{!task && <div className="generate-cta"><button className="primary-button generate-button" disabled={starting || !project.video_filename || !project.characters.length} onClick={start}>{starting ? <LoaderCircle className="spin" size={18} /> : <Sparkles size={18} />} {starting ? 'Preparing pipeline...' : 'Start transformation'}</button><span>Mock only · no external AI credits used</span></div>}</div>
}

function LegacyBrokenResultStep({ project, styles, languages, voices, onChange }: { project: Project; styles: Style[]; languages: Language[]; voices: Voice[]; onChange: (p: Project) => void }) { /*
  const navigate = useNavigate(); const { t, formatDuration, formatDate, locale } = useI18n(); const [result, setResult] = useState<Result | null>(null); const [mode, setMode] = useState('side-by-side'); const [slider, setSlider] = useState(50); const [error, setError] = useState(''); const comparisonModes: Array<[string, string]> = [['side-by-side', t('result.sideBySide')], ['slider', t('result.beforeAfter')], ['original', t('result.originalOnly')], ['generated', t('result.generatedOnly')]]
  useEffect(() => { api<Result>(`/projects/${project.id}/result`).then(setResult).catch((e) => setError((e as Error).message)) }, [project.id])
  if (error) return <div className="step-content"><InlineError message={error} /><button className="primary-button" onClick={() => navigate(`/project/${project.id}/generate`)}>{t('result.backToGeneration')}</button></div>
  if (!result) return <div className="loading-line"><LoaderCircle className="spin" size={17} /> {t('common.preparingResult')}</div>
  const source = assetUrl(result.original_video_url); const generatedBase = assetUrl(result.generated_video_url); const generated = generatedBase ? `${generatedBase}${generatedBase.includes('?') ? '&' : '?'}demo=1` : ''
  return <div className="step-content result-content"><DemoNotice>{t('result.demoNotice')}</DemoNotice><div className="result-banner"><div><span className="demo-pill"><Sparkles size={13} /> {t('result.demoTitle')}</span><h2>{project.name} · {t('result.transformed')}</h2><p>{t('result.copy')}</p></div><div className="result-actions"><button className="secondary-button" onClick={() => navigate(`/project/${project.id}/generate`)}><WandSparkles size={15} /> {t('result.generateAgain')}</button><button className="primary-button" onClick={() => navigate('/')}><FolderOpen size={15} /> {t('common.dashboard')}</button></div></div><section className="comparison-panel"><div className="comparison-toolbar"><div className="comparison-tabs">{[['side-by-side', t('result.sideBySide')], ['slider', t('result.beforeAfter')], ['original', t('result.originalOnly')], ['generated', t('result.generatedOnly')].map(([value, label]) => <button key={value} className={mode === value ? 'active' : ''} onClick={() => setMode(value)}>{label}</button>)}</div><button className="icon-button" title={t('result.download')} onClick={() => source && window.open(source, '_blank')}><Download size={16} /></button></div><div className={`comparison-view mode-${mode}`}>{mode === 'side-by-side' && <><MediaTileLocalized label={t('result.originalSource')} src={source} project={project} onChange={onChange} /><div className="comparison-divider"><ArrowRight size={15} /></div><MediaTileLocalized label={t('result.transformedDemo')} src={generated} project={project} onChange={onChange} transformed /></>}{mode === 'slider' && <div className="slider-stage"><MediaTileLocalized label={t('result.originalSource')} src={source} project={project} onChange={onChange} /><div className="slider-overlay" style={{ width: `${slider}%` }}><div className="slider-overlay-content" style={{ width: `${slider ? 10000 / slider : 10000}%` }}><MediaTileLocalized label={t('result.transformedDemo')} src={generated} project={project} onChange={onChange} transformed /></div></div><input className="compare-range" type="range" min="0" max="100" value={slider} onChange={(e) => setSlider(Number(e.target.value))} /></div>}{mode === 'original' && <MediaTileLocalized label={t('result.originalSource')} src={source} project={project} onChange={onChange} />}{mode === 'generated' && <MediaTileLocalized label={t('result.transformedDemo')} src={generated} project={project} onChange={onChange} transformed />}</div></section><div className="result-details"><section className="detail-panel"><div className="section-label">{t('result.transformationBrief')}</div><h3>{catalogLabel(project.settings.style_id, locale)}</h3><div className="detail-chips"><span>{catalogLabel(project.settings.source_language, locale)} → {catalogLabel(project.settings.target_language, locale)}</span><span>{project.settings.character_consistency}</span><span>{project.settings.transformation_strength}</span></div></section><section className="detail-panel"><div className="section-label">{t('result.audioOutput')}</div><div className="output-status"><DemoOutputLine icon={<Mic2 size={15} />} label={t('result.aiVoice')} value={t('generate.demoSimulated')} /><DemoOutputLine icon={<Languages size={15} />} label={t('result.subtitles')} value={t('generate.demoSimulated')} /><DemoOutputLine icon={<Gauge size={15} />} label={t('result.lipSync')} value={t('generate.demoSimulated')} /></div></section></div><div className="result-footer"><span><Check size={15} /> {t('result.generated', { date: formatDate(result.generated_at) })} · {t('common.demo')} · {formatDuration(project.video_duration)}</span><div><button className="text-button" onClick={() => navigate(`/project/${project.id}/characters`)}><Users size={15} /> {t('result.editCharacters')}</button><button className="text-button" onClick={() => navigate(`/project/${project.id}/settings`)}><Settings2 size={15} /> {t('result.editSettings')}</button></div></div></div>
}

*/ }

function ResultStep({ project, styles, languages, voices, onChange }: { project: Project; styles: Style[]; languages: Language[]; voices: Voice[]; onChange: (p: Project) => void }) {
  const navigate = useNavigate()
  const { t, formatDuration, formatDate, locale } = useI18n()
  const [result, setResult] = useState<Result | null>(null)
  const [mode, setMode] = useState('side-by-side')
  const [slider, setSlider] = useState(50)
  const [error, setError] = useState('')
  const episodeId = queryEpisodeId() || project.active_episode_id
  const [xmaxResult, setXmaxResultState] = useState<XmaxRealtimeResult | undefined>(() => getXmaxResult(xmaxResultKey(project.id, episodeId)))
  const [xmaxBatch, setXmaxBatch] = useState<XmaxBatch | null | undefined>(undefined)
  const [overseasTask, setOverseasTask] = useState<OverseasEpisodeTask | null | undefined>(undefined)
  const [overseasLoading, setOverseasLoading] = useState(true)
  const [formalWorkspace, setFormalWorkspace] = useState<FormalClosureWorkspace | null>(null)
  const [formalWorkspaceLoading, setFormalWorkspaceLoading] = useState(true)
  const [xmaxThirtySecondTask, setXmaxThirtySecondTask] = useState<XmaxThirtySecondTask | null | undefined>(undefined)
  const [xmaxThirtySecondLoading, setXmaxThirtySecondLoading] = useState(true)
  const [xmaxThirtySecondTaskError, setXmaxThirtySecondTaskError] = useState('')
  const requestedXmaxThirtySecondTaskId = new URLSearchParams(window.location.search).get('task_id')
  const loadResult = () => { setError(''); api<Result>(`/projects/${project.id}/result${episodeId ? `?episode_id=${encodeURIComponent(episodeId)}` : ''}`).then(setResult).catch((e) => setError((e as Error).message)) }
  useEffect(loadResult, [project.id, episodeId])
  useEffect(() => { setXmaxResultState(getXmaxResult(xmaxResultKey(project.id, episodeId))) }, [project.id, episodeId])
  useEffect(() => { if (!episodeId) { setXmaxBatch(null); return }; api<XmaxBatch | null>(`/episodes/${episodeId}/xmax-batches/latest`).then(setXmaxBatch).catch(() => setXmaxBatch(null)) }, [episodeId])
  useEffect(() => {
    if (!episodeId) { setOverseasTask(null); setOverseasLoading(false); return }
    let active = true
    setOverseasLoading(true)
    void getLatestOverseasEpisodeTask(episodeId)
      .then((next) => { if (active) setOverseasTask(next?.error_code === 'SHOT_MAPPING_REQUIRED' ? null : next) })
      .catch(() => { if (active) setOverseasTask(null) })
      .finally(() => { if (active) setOverseasLoading(false) })
    return () => { active = false }
  }, [episodeId])
  useEffect(() => {
    if (overseasLoading) return
    if (!episodeId) { setFormalWorkspace(null); setFormalWorkspaceLoading(false); return }
    let active = true
    setFormalWorkspaceLoading(true)
    void getFormalClosureWorkspace(episodeId, overseasTask?.visual_task_id || undefined)
      .then((next) => { if (active) setFormalWorkspace(next) })
      .catch(() => { if (active) setFormalWorkspace(null) })
      .finally(() => { if (active) setFormalWorkspaceLoading(false) })
    return () => { active = false }
  }, [episodeId, overseasLoading, overseasTask?.visual_task_id])
  useEffect(() => {
    if (!episodeId || !requestedXmaxThirtySecondTaskId) { setXmaxThirtySecondTask(null); setXmaxThirtySecondLoading(false); return }
    let active = true
    setXmaxThirtySecondLoading(true)
    setXmaxThirtySecondTaskError('')
    void (async () => {
      const exact = await getXmaxThirtySecondTask(requestedXmaxThirtySecondTaskId, episodeId)
      if (active) { setXmaxThirtySecondTask(exact); setXmaxThirtySecondLoading(false) }
    })().catch((reason) => { if (active) { setXmaxThirtySecondTask(null); setXmaxThirtySecondTaskError(reason instanceof Error ? reason.message : String(reason)); setXmaxThirtySecondLoading(false) } })
    return () => { active = false }
  }, [episodeId, requestedXmaxThirtySecondTaskId])
  const latestFormalFinal = formalWorkspace?.latest_final_output
    || formalWorkspace?.final_outputs.find((item) => item.status === 'ready' && Boolean(item.output_url))
  if (overseasLoading || formalWorkspaceLoading || xmaxThirtySecondLoading) return <div className="loading-line"><LoaderCircle className="spin" size={17} /> {t('common.preparingResult')}</div>
  if (!requestedXmaxThirtySecondTaskId && latestFormalFinal) return <FormalFinalOutputResultPage project={project} episodeId={episodeId} output={latestFormalFinal} task={overseasTask || undefined} />
  if (!requestedXmaxThirtySecondTaskId && overseasTask) return <OverseasEpisodeTaskStatusPage project={project} episodeId={episodeId} task={overseasTask} />
  if (!requestedXmaxThirtySecondTaskId) return <div className="step-content"><div className="empty-state"><div className="empty-icon"><Film size={24} /></div><h3>{locale === 'zh-CN' ? '尚未生成海外版' : 'No overseas version yet'}</h3><p>{locale === 'zh-CN' ? '完成素材和角色准备后，在生成海外版页面点击一次即可开始。' : 'Prepare the source and target character, then start the one-click overseas workflow.'}</p><button className="primary-button" onClick={() => navigate(flowRoute(project.id, 'generate', episodeId))}><WandSparkles size={15} /> {locale === 'zh-CN' ? '打开生成海外版' : 'Open Generate'}</button></div></div>
  if (requestedXmaxThirtySecondTaskId && xmaxThirtySecondTaskError) return <div className="step-content"><div className="empty-state"><div className="empty-icon"><Film size={24} /></div><h3>Xmax task not found</h3><p>The requested task does not belong to this Episode or is no longer available.</p><InlineError message={xmaxThirtySecondTaskError} /><button className="primary-button" onClick={() => navigate(flowRoute(project.id, 'generate', episodeId))}><WandSparkles size={15} /> Open Generate</button></div></div>
  if (xmaxThirtySecondTask && xmaxThirtySecondTask.status === 'completed' && xmaxThirtySecondTask.final_output_url && xmaxThirtySecondTask.result_valid) return <XmaxThirtySecondResultPage project={project} episodeId={episodeId} task={xmaxThirtySecondTask} />
  if (xmaxThirtySecondTask && (xmaxThirtySecondTask.status !== 'completed' || !xmaxThirtySecondTask.final_output_url)) return <XmaxThirtySecondTaskStatusPage project={project} episodeId={episodeId} task={xmaxThirtySecondTask} />
  if (xmaxResult) return <XmaxResultPage project={project} episodeId={episodeId} result={xmaxResult} />
  if (xmaxBatch?.status === 'completed' && xmaxBatch.output_url) return <XmaxBatchResultPage project={project} episodeId={episodeId} batch={xmaxBatch} />
  if (error) return <div className="step-content"><div className="empty-state"><div className="empty-icon"><Film size={24} /></div><h3>{locale === 'zh-CN' ? '当前单集还没有可显示的 Mock 结果' : 'No Mock result is available for this episode'}</h3><p>{locale === 'zh-CN' ? '请返回开始重绘页创建或查看 Mock 任务。技术错误可在下方展开。' : 'Return to Generate to create or inspect the Mock task.'}</p><InlineError message={error} /><div className="result-actions"><button className="secondary-button" onClick={loadResult}><RotateCcw size={15} /> {locale === 'zh-CN' ? '重试读取' : 'Retry'}</button><button className="primary-button" onClick={() => navigate(flowRoute(project.id, 'generate', episodeId))}>{t('result.backToGeneration')}</button></div></div></div>
  if (!result) return <div className="loading-line"><LoaderCircle className="spin" size={17} /> {t('common.preparingResult')}</div>
  const source = assetUrl(result.original_video_url); const generatedBase = assetUrl(result.generated_video_url); const generated = generatedBase ? `${generatedBase}${generatedBase.includes('?') ? '&' : '?'}demo=1` : ''
  const comparisonModes: Array<[string, string]> = [['side-by-side', t('result.sideBySide')], ['slider', t('result.beforeAfter')], ['original', t('result.originalOnly')], ['generated', t('result.generatedOnly')]]
  return <div className="step-content result-content"><RealVisualResult project={project} />
    <DemoNotice>{locale === 'zh-CN' ? `Mock 结果记录 ${result.id || ''}：复用当前单集原视频，仅验证任务、状态和结果页闭环。` : `Mock result ${result.id || ''}: the source episode video is reused to verify the workflow.`}</DemoNotice>
    <div className="result-banner"><div><span className="demo-pill"><Sparkles size={13} /> {t('result.demoTitle')}</span><h2>{project.name} · {t('result.transformed')}</h2><p>{t('result.copy')}</p></div><div className="result-actions"><button className="secondary-button" onClick={() => navigate(`/project/${project.id}/generate`)}><WandSparkles size={15} /> {t('result.generateAgain')}</button><button className="primary-button" onClick={() => navigate('/')}><FolderOpen size={15} /> {t('common.dashboard')}</button></div></div>
    <section className="comparison-panel"><div className="comparison-toolbar"><div className="comparison-tabs">{comparisonModes.map(([value, label]) => <button key={value} className={mode === value ? 'active' : ''} onClick={() => setMode(value)}>{label}</button>)}</div><button className="icon-button" title={t('result.download')} onClick={() => source && window.open(source, '_blank')}><Download size={16} /></button></div><div className={`comparison-view mode-${mode}`}>{mode === 'side-by-side' && <><MediaTileLocalized label={t('result.originalSource')} src={source} project={project} onChange={onChange} /><div className="comparison-divider"><ArrowRight size={15} /></div><MediaTileLocalized label={t('result.transformedDemo')} src={generated} project={project} onChange={onChange} transformed /></>}{mode === 'slider' && <div className="slider-stage"><MediaTileLocalized label={t('result.originalSource')} src={source} project={project} onChange={onChange} /><div className="slider-overlay" style={{ width: `${slider}%` }}><div className="slider-overlay-content" style={{ width: `${slider ? 10000 / slider : 10000}%` }}><MediaTileLocalized label={t('result.transformedDemo')} src={generated} project={project} onChange={onChange} transformed /></div></div><input className="compare-range" type="range" min="0" max="100" value={slider} onChange={(e) => setSlider(Number(e.target.value))} /></div>}{mode === 'original' && <MediaTileLocalized label={t('result.originalSource')} src={source} project={project} onChange={onChange} />}{mode === 'generated' && <MediaTileLocalized label={t('result.transformedDemo')} src={generated} project={project} onChange={onChange} transformed />}</div></section>
    <div className="result-details"><section className="detail-panel"><div className="section-label">{t('result.transformationBrief')}</div><h3>{catalogLabel(project.settings.style_id, locale)}</h3><div className="detail-chips"><span>{catalogLabel(project.settings.source_language, locale)} → {catalogLabel(project.settings.target_language, locale)}</span><span>{project.settings.character_consistency}</span><span>{project.settings.transformation_strength}</span></div></section><section className="detail-panel"><div className="section-label">{t('result.audioOutput')}</div><div className="output-status"><DemoOutputLine icon={<Mic2 size={15} />} label={t('result.aiVoice')} value={t('generate.demoSimulated')} /><DemoOutputLine icon={<Languages size={15} />} label={t('result.subtitles')} value={t('generate.demoSimulated')} /><DemoOutputLine icon={<Gauge size={15} />} label={t('result.lipSync')} value={t('generate.demoSimulated')} /></div></section></div>
    <div className="result-footer"><span><Check size={15} /> {t('result.generated', { date: formatDate(result.generated_at) })} · {t('common.demo')} · {formatDuration(project.video_duration)}</span><div><button className="text-button" onClick={() => navigate(`/project/${project.id}/characters`)}><Users size={15} /> {t('result.editCharacters')}</button><button className="text-button" onClick={() => navigate(`/project/${project.id}/settings`)}><Settings2 size={15} /> {t('result.editSettings')}</button></div></div>
  </div>
}

function XmaxResultPage({ project, episodeId, result }: { project: Project; episodeId?: string; result: XmaxRealtimeResult }) {
  const navigate = useNavigate()
  const { locale } = useI18n()
  const episode = project.episodes?.find((item) => item.id === episodeId)
  const source = episode ? episodeVideoUrl(episode) : videoUrl(project)
  const zh = locale === 'zh-CN'
  return <div className="step-content result-content"><DemoNotice>{zh ? '这是浏览器实时转绘的真实输出，当前文件以 WebM 保存。' : 'This is the real browser-captured realtime transformation output, saved as WebM.'}</DemoNotice><div className="result-banner"><div><span className="demo-pill"><Sparkles size={13} /> {zh ? '海外版生成结果' : 'OVERSEAS VERSION RESULT'}</span><h2>{project.name} · {zh ? '转绘结果' : 'Transformation result'}</h2><p>{zh ? '保留原视频作为对照，并显示当前已捕获的实时输出。' : 'The original video is retained for comparison with the captured realtime output.'}</p></div><div className="result-actions"><button className="secondary-button" onClick={() => navigate(flowRoute(project.id, 'generate', episodeId))}><WandSparkles size={15} /> {zh ? '重新生成' : 'Generate again'}</button><button className="primary-button" onClick={() => navigate('/')}><FolderOpen size={15} /> {zh ? '项目首页' : 'Dashboard'}</button></div></div><section className="xmax-result-panel"><div className="xmax-result-grid"><div className="xmax-media-card"><span className="media-label">{zh ? '原视频' : 'Original video'}</span>{source ? <video controls playsInline preload="metadata" src={source} /> : <div className="empty-video"><Film size={20} /> {zh ? '无原视频' : 'No source video'}</div>}</div><div className="xmax-media-card"><span className="media-label">{zh ? '真实转绘输出' : 'Captured transformation output'}</span><video controls playsInline src={result.objectUrl} /></div></div><div className="xmax-summary"><div><span>{zh ? '已处理时长' : 'Processed'}</span><strong>{result.processedSeconds.toFixed(1)}s</strong></div><div><span>{zh ? '预计 credits' : 'Estimated credits'}</span><strong>{result.estimatedCredits.toFixed(1)}</strong></div><div><span>{zh ? '目标画风' : 'Target style'}</span><strong>{result.style}</strong></div><div><span>{zh ? '目标语言' : 'Target language'}</span><strong>{result.targetLanguage}</strong></div></div><div className="xmax-actions"><a className="primary-button" href={result.objectUrl} download="overseas-version.webm"><Download size={16} /> {zh ? '下载结果' : 'Download result'}</a><span className="xmax-disclaimer">{zh ? '输出来自浏览器实时流；本轮未增加后端 MP4 转码。' : 'Output was captured from the browser realtime stream; no backend MP4 transcode was added.'}</span></div></section></div>
}

function XmaxBatchResultPage({ project, episodeId, batch }: { project: Project; episodeId?: string; batch: XmaxBatch }) {
  const navigate = useNavigate()
  const { locale } = useI18n()
  const episode = project.episodes?.find((item) => item.id === episodeId)
  const source = episode ? episodeVideoUrl(episode) : videoUrl(project)
  const output = assetUrl(batch.output_url)
  const zh = locale === 'zh-CN'
  return <div className="step-content result-content"><DemoNotice>{zh ? '真实 Xmax 镜头结果：片段已按原顺序合并，并恢复原视频音频。' : 'Real Xmax shot result: the captured shots were merged in order with the original audio restored.'}</DemoNotice><div className="result-banner"><div><span className="demo-pill"><Sparkles size={13} /> {zh ? '镜头级转绘结果' : 'SHOT TRANSFORMATION RESULT'}</span><h2>{project.name} · {zh ? '完整视频' : 'Complete video'}</h2><p>{zh ? '每个片段独立关闭 Session，最终文件由本地后端合并。' : 'Each shot session was closed independently; the final file was merged locally.'}</p></div><div className="result-actions"><button className="secondary-button" onClick={() => navigate(flowRoute(project.id, 'generate', episodeId))}><WandSparkles size={15} /> {zh ? '重新生成' : 'Generate again'}</button><button className="primary-button" onClick={() => navigate('/')}><FolderOpen size={15} /> {zh ? '项目首页' : 'Dashboard'}</button></div></div><section className="xmax-result-panel"><div className="xmax-result-grid"><div className="xmax-media-card"><span className="media-label">{zh ? '原始视频' : 'Original video'}</span>{source ? <video controls playsInline src={source} /> : <div className="empty-video"><Film size={20} /> {zh ? '无原始视频' : 'No source video'}</div>}</div><div className="xmax-media-card"><span className="media-label">{zh ? '完整转绘结果' : 'Complete transformation'}</span><video controls playsInline src={output} /></div></div><div className="xmax-summary"><div><span>{zh ? '镜头数' : 'Shots'}</span><strong>{batch.total_shots}</strong></div><div><span>{zh ? '已处理秒数' : 'Processed'}</span><strong>{batch.processed_seconds.toFixed(1)}s</strong></div><div><span>{zh ? '预计 credits' : 'Estimated credits'}</span><strong>{batch.estimated_credits.toFixed(1)}</strong></div><div><span>{zh ? '实际 credits' : 'Actual credits'}</span><strong>{batch.actual_credits == null ? (zh ? 'Provider 未返回' : 'Not exposed by Provider') : batch.actual_credits.toFixed(1)}</strong></div></div><div className="xmax-actions"><a className="primary-button" href={output} download><Download size={16} /> {zh ? '下载完整视频' : 'Download complete video'}</a></div></section></div>
}

function FormalFinalOutputResultPage({ project, episodeId, output, task }: { project: Project; episodeId?: string; output: FormalFinalOutput; task?: OverseasEpisodeTask }) {
  const navigate = useNavigate()
  const mediaUrl = assetUrl(output.output_url || undefined)
  const downloadUrl = formalFinalOutputDownloadUrl(output.id)
  return <div className="step-content result-content">
    <div className="result-banner"><div><span className="demo-pill"><Sparkles size={13} /> OVERSEAS VERSION</span><h2>{project.name} · 最终海外版</h2><p>海外版画面、目标语言配音和字幕已合成为最终交付 MP4。</p></div><div className="result-actions"><button className="secondary-button" onClick={() => navigate(`${flowRoute(project.id, 'generate', episodeId)}${episodeId ? '&' : '?'}advanced=1`)}><Settings2 size={15} /> 返回高级调整</button><button className="primary-button" onClick={() => navigate('/')}><FolderOpen size={15} /> 项目首页</button></div></div>
    <section className="xmax-result-panel overseas-final-result">
      <span className="media-label">最终海外版 MP4</span>
      <video controls playsInline preload="metadata" src={mediaUrl} />
      <div className="xmax-summary"><div><span>完成状态</span><strong>海外版已完成</strong></div><div><span>转绘镜头</span><strong>{task ? `${task.transform_completed}/${task.transform_total}` : output.unit_selection_snapshot.length}</strong></div><div><span>分辨率</span><strong>{output.width && output.height ? `${output.width}×${output.height}` : 'MP4'}</strong></div><div><span>完成时间</span><strong>{output.completed_at || output.created_at}</strong></div></div>
      <div className="xmax-actions"><a className="primary-button" href={downloadUrl} download={`overseas-${output.episode_id}.mp4`}><Download size={16} /> 下载最终 MP4</a></div>
      <details className="xmax-thirty-second-diagnostics"><summary>技术详情</summary><code>{JSON.stringify({ finalOutputId: output.id, formalTaskId: output.task_id, outputSha256: output.output_sha256, audioRevision: output.audio_revision, subtitleRevision: output.subtitle_revision }, null, 2)}</code></details>
    </section>
  </div>
}

function OverseasEpisodeTaskStatusPage({ project, episodeId, task }: { project: Project; episodeId?: string; task: OverseasEpisodeTask }) {
  const navigate = useNavigate()
  const label = {
    preparing: '准备素材',
    analyzing_shots: '分析镜头',
    transforming_shots: '转绘镜头',
    merging_visual: '合并画面',
    separating_audio: '分离对白',
    adapting_dialogue: '适配翻译与配音',
    rendering_subtitles: '渲染英文字幕',
    muxing_final: '输出最终视频',
    completed: '已完成',
    failed: '失败',
  }[task.stage]
  return <div className="step-content result-content"><div className="result-banner"><div><span className="demo-pill"><Sparkles size={13} /> OVERSEAS VERSION</span><h2>{label}</h2><p>{task.error_message || task.progress_message || '任务状态已持久化；返回 Generate 可继续查看。'}</p></div><div className="result-actions"><button className="primary-button" onClick={() => navigate(flowRoute(project.id, 'generate', episodeId))}><WandSparkles size={15} /> 打开生成页</button></div></div><section className="xmax-summary"><div><span>进度</span><strong>{task.progress}%</strong></div><div><span>转绘</span><strong>{task.transform_completed}/{task.transform_total}</strong></div><div><span>状态</span><strong>{task.status}</strong></div></section></div>
}

function XmaxThirtySecondResultPage({ project, episodeId, task }: { project: Project; episodeId?: string; task: XmaxThirtySecondTask }) {
  const navigate = useNavigate()
  const episode = project.episodes?.find((item) => item.id === episodeId)
  const source = episode ? episodeVideoUrl(episode) : videoUrl(project)
  const output = assetUrl(task.final_output_url || '')
  return <div className="step-content result-content"><DemoNotice>Formal Xmax multi-shot result. Saved shots were aligned locally, merged in timeline order, and the original Episode audio was restored.</DemoNotice><div className="result-banner"><div><span className="demo-pill"><Sparkles size={13} /> XMAX MULTI-SHOT RESULT</span><h2>{project.name} · Complete video</h2><p>Provider generation is complete; this page reads the persisted final MP4.</p></div><div className="result-actions"><button className="secondary-button" onClick={() => navigate(flowRoute(project.id, 'generate', episodeId))}><WandSparkles size={15} /> Generate again</button><button className="primary-button" onClick={() => navigate('/')}><FolderOpen size={15} /> Dashboard</button></div></div><section className="xmax-result-panel"><div className="xmax-result-grid"><div className="xmax-media-card"><span className="media-label">Original video</span>{source ? <video controls playsInline preload="metadata" src={source} /> : <div className="empty-video"><Film size={20} /> No source video</div>}</div><div className="xmax-media-card"><span className="media-label">Final MP4</span><video controls playsInline preload="metadata" src={output} /></div></div><div className="xmax-summary"><div><span>Shots</span><strong>{task.segments.length}</strong></div><div><span>Duration</span><strong>{task.final_duration?.toFixed(3) || 'n/a'}s</strong></div><div><span>Resolution</span><strong>{task.final_width || 'n/a'}×{task.final_height || 'n/a'}</strong></div><div><span>Audio</span><strong>{task.source_audio_restored ? 'Original audio restored' : 'No source audio'}</strong></div></div><div className="xmax-actions"><a className="primary-button" href={output} download={`xmax-30-second-${task.task_id}.mp4`}><Download size={16} /> Download final MP4</a><span className="xmax-disclaimer">Saved at {task.merged_at || task.completed_at || 'local merge time'} · {task.file_size || 0} bytes</span></div></section><AudioLocalizationPanel episodeId={episodeId} sourceTaskId={task.task_id} /></div>
}

function XmaxThirtySecondTaskStatusPage({ project, episodeId, task }: { project: Project; episodeId?: string; task: XmaxThirtySecondTask }) {
  const navigate = useNavigate()
  const status = task.status === 'merge_failed' ? 'Merge failed' : task.status === 'merging' ? 'Merging' : task.status === 'merge_pending' ? 'Merge pending' : task.status === 'failed' ? 'Failed' : task.status === 'interrupted' ? 'Interrupted' : task.status === 'cancelled' ? 'Cancelled' : task.status
  return <div className="step-content result-content"><DemoNotice>Formal Xmax multi-shot task status: {status}. No new Provider call is started from the Result page.</DemoNotice><div className="result-banner"><div><span className="demo-pill"><Sparkles size={13} /> XMAX MULTI-SHOT</span><h2>Final local merge not available</h2><p>{task.error_message || 'Return to Generate to finish the local merge.'}</p></div><div className="result-actions"><button className="primary-button" onClick={() => navigate(flowRoute(project.id, 'generate', episodeId))}><WandSparkles size={15} /> Open Generate</button></div></div><section className="xmax-summary"><div><span>Shots saved</span><strong>{task.segments.filter((segment) => segment.status === 'completed').length}/{task.segments.length}</strong></div><div><span>Status</span><strong>{status}</strong></div></section></div>
}

function RealVisualResult({ project }: { project: Project }) {
  const { locale } = useI18n(); const episodeId = queryEpisodeId() || project.active_episode_id; const [state, setState] = useState<VisualTransformationState | null>(null)
  useEffect(() => { if (episodeId) api<VisualTransformationState>(`/episodes/${episodeId}/visual-transformation`).then(setState).catch(() => setState(null)) }, [episodeId])
  const task = state?.task; if (!task || task.status !== 'completed' || !task.output?.local_output_url) return null
  const source = task.input?.clip_url ? assetUrl(task.input.clip_url) : ''
  return <section className="b1-result"><div className="b1-heading"><div><span className="section-label">B1 / REAL AI OUTPUT</span><h2>{locale === 'zh-CN' ? '真实转绘结果' : 'Real transformation output'}</h2><p>{locale === 'zh-CN' ? 'Provider 输出已下载到本地；以下不是原有 Mock 结果。' : 'The Provider output was downloaded locally; this is separate from the existing Mock result.'}</p></div><span className="b1-provider ready">Runway · {task.model}</span></div><div className="b1-result-grid">{source && <div><span className="media-label">{locale === 'zh-CN' ? '原始片段' : 'Input clip'}</span><video controls src={source} /></div>}<div><span className="media-label">{locale === 'zh-CN' ? '真实 AI 输出' : 'Real AI output'}</span><video controls src={assetUrl(task.output.local_output_url)} /></div></div><div className="b1-meta"><span>{locale === 'zh-CN' ? '任务' : 'Task'}: <strong>{task.id}</strong></span><span>{locale === 'zh-CN' ? 'Provider任务' : 'Provider task'}: <strong>{task.provider_task_id || '—'}</strong></span><span>{locale === 'zh-CN' ? '时长' : 'Duration'}: <strong>{task.duration.toFixed(2)}s</strong></span><span>{locale === 'zh-CN' ? '参考角色' : 'References'}: <strong>{task.reference_character_ids.length}</strong></span></div></section>
}

function MediaTileLocalized({ label, src, project, onChange, transformed = false }: { label: string; src: string; project: Project; onChange: (project: Project) => void; transformed?: boolean }) { const { t } = useI18n(); return <div className={`media-tile ${transformed ? 'transformed' : ''}`}><div className="media-label">{label}</div>{src ? <ResponsiveVideo src={src} project={project} onChange={onChange} /> : <MockFrame label={t('common.unknown')} variant={transformed ? 'result' : 'source'} />}{transformed && <span className="demo-corner">{t('result.demo')}</span>}</div> }
function DemoOutputLine({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) { return <div className="output-line demo-output-line"><span>{icon}</span><div><strong>{label}</strong><small>{value}</small></div><span className="demo-output-state">{value}</span></div> }

function LegacyResultStep({ project, styles, languages, voices, onChange }: { project: Project; styles: Style[]; languages: Language[]; voices: Voice[]; onChange: (p: Project) => void }) {
  const navigate = useNavigate(); const [result, setResult] = useState<Result | null>(null); const [mode, setMode] = useState('side-by-side'); const [slider, setSlider] = useState(50); const [error, setError] = useState('')
  useEffect(() => { api<Result>(`/projects/${project.id}/result`).then(setResult).catch((e) => setError(e.message)) }, [project.id])
  if (error) return <div className="step-content"><InlineError message={error} /><button className="primary-button" onClick={() => navigate(`/project/${project.id}/generate`)}>Back to generation</button></div>
  if (!result) return <div className="loading-line"><LoaderCircle className="spin" size={17} /> Preparing result...</div>
  const source = assetUrl(result.original_video_url); const generatedBase = assetUrl(result.generated_video_url); const generated = generatedBase ? `${generatedBase}${generatedBase.includes('?') ? '&' : '?'}demo=1` : ''
  return <div className="step-content result-content"><div className="result-banner"><div><span className="demo-pill"><Sparkles size={13} /> DEMO GENERATED RESULT</span><h2>{project.name} · transformed</h2><p>Review the local mock export and compare it against the original source.</p></div><div className="result-actions"><button className="secondary-button" onClick={() => navigate(`/project/${project.id}/generate`)}><WandSparkles size={15} /> Generate again</button><button className="primary-button" onClick={() => navigate('/')}><FolderOpen size={15} /> Dashboard</button></div></div><section className="comparison-panel"><div className="comparison-toolbar"><div className="comparison-tabs">{[['side-by-side', 'Side by side'], ['slider', 'Before / after'], ['original', 'Original only'], ['generated', 'Generated only']].map(([value, label]) => <button key={value} className={mode === value ? 'active' : ''} onClick={() => setMode(value)}>{label}</button>)}</div><button className="icon-button" title="Download demo result" onClick={() => source && window.open(source, '_blank')}><Download size={16} /></button></div><div className={`comparison-view mode-${mode}`}>{mode === 'side-by-side' && <><MediaTile label="ORIGINAL SOURCE" src={source} project={project} onChange={onChange} /><div className="comparison-divider"><ArrowRight size={15} /></div><MediaTile label="TRANSFORMED / DEMO" src={generated} project={project} onChange={onChange} transformed /></>}{mode === 'slider' && <div className="slider-stage"><MediaTile label="ORIGINAL" src={source} project={project} onChange={onChange} /><div className="slider-overlay" style={{ width: `${slider}%` }}><div className="slider-overlay-content" style={{ width: `${slider ? 10000 / slider : 10000}%` }}><MediaTile label="TRANSFORMED / DEMO" src={generated} project={project} onChange={onChange} transformed /></div></div><input className="compare-range" type="range" min="0" max="100" value={slider} onChange={(e) => setSlider(Number(e.target.value))} /></div>}{mode === 'original' && <MediaTile label="ORIGINAL SOURCE" src={source} project={project} onChange={onChange} />}{mode === 'generated' && <MediaTile label="TRANSFORMED / DEMO" src={generated} project={project} onChange={onChange} transformed />}</div></section><div className="result-details"><section className="detail-panel"><div className="section-label">TRANSFORMATION BRIEF</div><h3>{styleName(project.settings.style_id, styles)}</h3><div className="detail-chips"><span>{languageName(project.settings.source_language, languages)} → {languageName(project.settings.target_language, languages)}</span><span>{project.settings.character_consistency} consistency</span><span>{project.settings.transformation_strength} strength</span></div></section><section className="detail-panel"><div className="section-label">AUDIO & OUTPUT</div><div className="output-status"><OutputLine icon={<Mic2 size={15} />} label="AI voice" value={`${project.characters.length} character voices`} /><OutputLine icon={<Languages size={15} />} label="Subtitles" value={project.settings.subtitle_enabled ? 'Enabled' : 'Off'} /><OutputLine icon={<Gauge size={15} />} label="Lip sync" value={project.settings.lip_sync_enabled ? 'Enabled' : 'Off'} /></div></section></div><div className="result-footer"><span><Check size={15} /> Generated {formatDate(result.generated_at)} · {result.comparison_note} · {formatDuration(project.video_duration)}</span><div><button className="text-button" onClick={() => navigate(`/project/${project.id}/characters`)}><Users size={15} /> Edit characters</button><button className="text-button" onClick={() => navigate(`/project/${project.id}/settings`)}><Settings2 size={15} /> Edit style & voice</button></div></div></div>
}

function TaskConsole({ task, expanded, onToggle }: { task: Task; expanded: boolean; onToggle: () => void }) { const completed = task.steps.filter((step) => step.status === 'completed').length; return <section className="task-console"><div className="task-console-top"><div><span className="section-label">GENERATION TASK · {task.id}</span><h3>{task.status === 'failed' ? 'Pipeline paused' : task.status === 'completed' ? 'Pipeline complete' : task.current_step}</h3></div><span className={`task-status task-${task.status}`}>{task.status}</span></div><div className="progress-row"><div className="progress-track"><span style={{ width: `${task.progress}%` }} /></div><strong>{task.progress}%</strong></div><div className="task-meta"><span>{completed} / {task.steps.length} steps complete</span><span>{task.error_message || 'Processing locally with Mock AI services'}</span></div><button className="step-expander" onClick={onToggle}>{expanded ? 'Hide' : 'Show'} detailed steps {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</button>{expanded && <div className="task-steps">{task.steps.map((step) => <TaskStepRow key={step.step_index} step={step} />)}</div>}</section> }
function TaskStepRow({ step }: { step: TaskStep }) { return <div className="task-step-row"><span className={`step-state state-${step.status}`}>{step.status === 'completed' ? <Check size={12} /> : step.status === 'processing' ? <LoaderCircle className="spin" size={12} /> : step.status === 'failed' ? <X size={12} /> : <span />}</span><span className="task-step-name">{String(step.step_index + 1).padStart(2, '0')} {step.name}</span><span className={`step-word word-${step.status}`}>{step.status}</span></div> }

function MediaTile({ label, src, project, onChange, transformed = false }: { label: string; src: string; project: Project; onChange: (project: Project) => void; transformed?: boolean }) { return <div className={`media-tile ${transformed ? 'transformed' : ''}`}><div className="media-label">{label}</div>{src ? <ResponsiveVideo src={src} project={project} onChange={onChange} /> : <MockFrame label="NO VIDEO" variant={transformed ? 'result' : 'source'} />}{transformed && <span className="demo-corner">DEMO</span>}</div> }
function Preflight({ icon, title, copy, done }: { icon: React.ReactNode; title: string; copy: string; done: boolean }) { return <div className={`preflight ${done ? 'done' : ''}`}><span className="preflight-icon">{icon}</span><div><strong>{title}</strong><span>{copy}</span></div>{done && <Check className="green-check" size={16} />}</div> }
function FlowItem({ number, title, copy }: { number: string; title: string; copy: string }) { return <div className="flow-item"><span>{number}</span><div><strong>{title}</strong><small>{copy}</small></div></div> }
function VoiceRange({ label, value, min, max, step, display, onChange }: { label: string; value: number; min: number; max: number; step: number; display: string; onChange: (value: number) => void }) { return <label className="range-field"><span>{label}<strong>{display}</strong></span><input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} /></label> }
function RangeControl(props: Parameters<typeof VoiceRange>[0]) { return <VoiceRange {...props} /> }
function SettingSelect({ label, value, options, onChange }: { label: string; value: string; options: string[][]; onChange: (value: string) => void }) { return <label className="field-label">{label}<select value={value} onChange={(e) => onChange(e.target.value)}>{options.map(([key, text]) => <option key={key} value={key}>{text}</option>)}</select></label> }
function Toggle({ label, copy, checked, disabled = false, onChange }: { label: string; copy: string; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void }) { return <label className={`toggle-card ${disabled ? 'disabled' : ''}`}><input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} /><span className="toggle-switch" /><span><strong>{label}</strong><small>{copy}</small></span></label> }
function OutputLine({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) { return <div className="output-line"><span>{icon}</span><div><strong>{label}</strong><small>{value}</small></div><Check size={14} /></div> }
function MockAvatar({ index }: { index: number }) { return <div className={`mock-avatar avatar-${index % 3}`}><span /></div> }
function MockFrame({ label, variant }: { label: string; variant: string }) { return <div className={`mock-frame frame-${variant}`}><div className="frame-light" /><div className="frame-person"><span className="frame-head" /><span className="frame-body" /></div><span className="frame-label">{label}</span></div> }
function localizedStatus(status: string, t: ReturnType<typeof useI18n>['t']) { const compact = status.replaceAll(' ', ''); const normalized = status === 'running' ? 'processing' : compact === 'CharacterReview' ? 'characterReview' : status.toLowerCase(); const key = `status.${normalized}` as Parameters<typeof t>[0]; return t(key) }
function StatusBadge({ status }: { status: string }) { const { t } = useI18n(); return <span className={`status-badge status-${status.toLowerCase().replaceAll(' ', '-')}`}><span />{localizedStatus(status, t)}</span> }
function InlineError({ message }: { message: string }) { const { friendlyError, t } = useI18n(); const error = friendlyError(message); return <div className="inline-error"><X size={15} /><div><strong>{error.summary}</strong><details><summary>{t('error.details')}</summary><code>{error.details}</code></details></div></div> }
function Stat({ label, value }: { label: string; value: string }) { return <div><span>{label}</span><strong>{value}</strong></div> }
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) { return <div className="modal-backdrop" onMouseDown={onClose}><div className="modal" onMouseDown={(e) => e.stopPropagation()}><div className="modal-header"><h3>{title}</h3><button className="icon-button" onClick={onClose}><X size={17} /></button></div>{children}</div></div> }
function formatDate(value: string) { if (!value) return '—'; const date = new Date(value); return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }

function ToolNavLink({ to, active, icon, label }: { to: string; active: boolean; icon: React.ReactNode; label: string }) { const { projectId } = useParams(); const location = useLocation(); return <Link className={`step-link tool-link ${active ? 'active' : ''}`} to={`/project/${projectId}/${to}${location.search}`}>{icon}<span>{label}</span>{active && <ChevronRight className="step-chevron" size={15} />}</Link> }

function ProcessingModeControl({ project, onChange }: { project: Project; onChange: (project: Project) => void }) {
  const { locale } = useI18n(); const [saving, setSaving] = useState(false)
  const mode = (project.processing_mode || 'quick-transformation') as ProcessingMode
  async function change(next: ProcessingMode) { if (next === mode || saving) return; setSaving(true); try { const updated = await api<Project>(`/projects/${project.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ processing_mode: next, use_character_library: next === 'character-consistency' }) }); onChange(updated) } finally { setSaving(false) } }
  return <label className="mode-control"><span>{roundTwoText(locale, 'modeTitle')}</span><select value={mode} disabled={saving} onChange={(e) => change(e.target.value as ProcessingMode)}><option value="quick-transformation">{roundTwoText(locale, 'quick')}</option><option value="character-consistency">{roundTwoText(locale, 'consistency')}</option><option value="batch-long-episode" disabled>{roundTwoText(locale, 'batch')}</option></select></label>
}

function episodePayload(episode: Episode): Record<string, unknown> { return { title: episode.title, status: episode.status, character_handling: episode.character_handling, source_language: episode.source_language, settings_override: episode.settings_override } }
function characterPayload(character: Character, patch: Record<string, unknown> = {}): Record<string, unknown> { return { name: character.name, gender: character.gender, estimated_age: character.estimated_age, role: character.role, appearance_description: character.appearance_description, hair_description: character.hair_description, headwear_description: character.headwear_description, clothing_description: character.clothing_description, original_reference_image: character.original_reference_image, source_crop_path: character.source_crop_path || character.source_reference_image || character.original_reference_image || null, target_reference_path: character.target_reference_path || character.main_reference_image || null, front_view_image: character.front_view_image, angle_view_image: character.angle_view_image, side_view_image: character.side_view_image, full_body_image: character.full_body_image, voice_id: character.voice_id, voice_settings: character.voice_settings, confirmed: character.confirmed, character_code: character.character_code, character_type: character.character_type || 'Human', primary_reference_type: character.primary_reference_type || null, priority_level: character.priority_level || 'Temporary', identity_description: character.identity_description || character.appearance_description, face_description: character.face_description || '', body_description: character.body_description || '', default_clothing_description: character.default_clothing_description || character.clothing_description, voice_profile_id: character.voice_profile_id || null, source: character.source || 'manual', created_from_episode_id: character.created_from_episode_id || null, ...patch } }

function ToolPageHeader({ title, copy, icon }: { title: string; copy: string; icon: React.ReactNode }) { return <div className="tool-page-header"><div className="tool-page-icon">{icon}</div><div><p className="eyebrow">PROJECT TOOL</p><h2>{title}</h2><p className="muted-copy">{copy}</p></div></div> }

function EpisodePoster({ episode, onSaved }: { episode: Episode; onSaved: () => void }) {
  const [poster, setPoster] = useState(episode.thumbnail_path ? assetUrl(`/uploads/${episode.thumbnail_path.split(/[\\/]/).pop()}`) : ''); const [failed, setFailed] = useState(false)
  useEffect(() => { if (episode.thumbnail_path) { setPoster(assetUrl(`/uploads/${episode.thumbnail_path.split(/[\\/]/).pop()}`)); return }; if (!episode.video_filename) return; const video = document.createElement('video'); video.crossOrigin = 'anonymous'; video.preload = 'auto'; video.src = episodeVideoUrl(episode); const capture = () => { try { const canvas = document.createElement('canvas'); canvas.width = video.videoWidth; canvas.height = video.videoHeight; canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height); const data = canvas.toDataURL('image/jpeg', 0.78); setPoster(data); void api<Episode>(`/episodes/${episode.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ thumbnail_data: data }) }).then(() => onSaved()).catch(() => undefined) } catch { setFailed(true) } }; video.onloadeddata = () => { try { video.currentTime = Math.min(0.1, Math.max(0, video.duration / 2)) } catch { setFailed(true) } }; video.onseeked = capture; video.onerror = () => setFailed(true); return () => { video.onloadeddata = null; video.onseeked = null; video.onerror = null } }, [episode.id, episode.thumbnail_path, episode.video_filename, onSaved])
  if (poster && !failed) return <img className="episode-thumbnail" src={poster} alt={`${episode.title} thumbnail`} onError={() => { setPoster(''); setFailed(true) }} />
  if (failed) return <MockFrame label={`EP ${String(episode.episode_number).padStart(2, '0')}`} variant="source" />
  return <video muted preload="metadata" src={episodeVideoUrl(episode)} />
}

type LocalBatchFile = { item: EpisodeBatchItem; file: File; status: EpisodeBatchItem['status']; progress: number; error?: string }

function episodeDisplayStatus(episode: Episode, locale: 'zh-CN' | 'en') {
  if (episode.upload_status === 'failed' || episode.analysis_status === 'failed' || episode.status === 'Failed') return locale === 'zh-CN' ? '失败' : 'Failed'
  if (!episode.video_filename) return locale === 'zh-CN' ? '未上传' : 'Not uploaded'
  if (episode.status === 'Generating') return locale === 'zh-CN' ? '生成中' : 'Generating'
  if (episode.status === 'Completed') return locale === 'zh-CN' ? '已完成' : 'Completed'
  if (episode.analysis_status === 'not_started') return locale === 'zh-CN' ? '待分析' : 'Needs analysis'
  if ((episode.pending_character_count || 0) > 0 || episode.status === 'Character Review') return locale === 'zh-CN' ? '角色待确认' : 'Character review'
  return locale === 'zh-CN' ? '可生成' : 'Ready'
}

function EpisodesPage({ project, onChange }: { project: Project; onChange: (project: Project) => void }) {
  const navigate = useNavigate(); const location = useLocation(); const { locale, formatDate, formatDuration } = useI18n()
  const [episodes, setEpisodes] = useState<Episode[]>(project.episodes || []); const [error, setError] = useState(''); const [message, setMessage] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set()); const [queue, setQueue] = useState<LocalBatchFile[]>([]); const [batchJob, setBatchJob] = useState<EpisodeBatchJob | null>(null)
  const [renameTarget, setRenameTarget] = useState<Episode | null>(null); const [renameTitle, setRenameTitle] = useState(''); const [deleteTargets, setDeleteTargets] = useState<Episode[]>([]); const [dragging, setDragging] = useState(false)
  useEffect(() => setEpisodes(project.episodes || []), [project.episodes])
  useEffect(() => { api<EpisodeBatchJob[]>(`/projects/${project.id}/episode-batches`).then((jobs) => setBatchJob(jobs[0] || null)).catch(() => undefined) }, [project.id])
  async function refresh() { const next = await api<Episode[]>(`/projects/${project.id}/episodes`); setEpisodes(next); onChange({ ...project, episodes: next }) }
  async function markItem(jobId: string, itemId: string, status: EpisodeBatchItem['status'], progress: number, errorMessage?: string) { return api<EpisodeBatchJob>(`/episode-batches/${jobId}/items/${itemId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status, progress, error_message: errorMessage || null }) }) }
  function updateLocal(itemId: string, patch: Partial<LocalBatchFile>) { setQueue((items) => items.map((entry) => entry.item.id === itemId ? { ...entry, ...patch } : entry)) }
  async function uploadBatchItem(job: EpisodeBatchJob, entry: LocalBatchFile) {
    if (entry.status === 'cancelled') return
    updateLocal(entry.item.id, { status: 'processing', progress: 10, error: '' }); await markItem(job.id, entry.item.id, 'processing', 10)
    try {
      const metadata = await readEpisodeVideoMetadata(entry.file)
      if (metadata.duration > 180) throw new Error(locale === 'zh-CN' ? '当前MVP单集支持3分钟以内，请分集上传。' : 'The current MVP supports episodes up to 3 minutes. Please split the video.')
      updateLocal(entry.item.id, { status: 'uploading', progress: 45 }); await markItem(job.id, entry.item.id, 'uploading', 45)
      const form = new FormData(); form.append('file', entry.file)
      Object.entries(metadata).forEach(([key, value]) => { if (key !== 'thumbnail_data') form.append(key, String(value)) }); if (metadata.thumbnail_data) form.append('thumbnail_data', metadata.thumbnail_data)
      await api(`/episodes/${entry.item.episode_id}/upload`, { method: 'POST', body: form })
      updateLocal(entry.item.id, { status: 'completed', progress: 100 }); setBatchJob(await markItem(job.id, entry.item.id, 'completed', 100))
    } catch (reason) {
      const detail = (reason as Error).message; updateLocal(entry.item.id, { status: 'failed', progress: 100, error: detail }); setBatchJob(await markItem(job.id, entry.item.id, 'failed', 100, detail))
    }
  }
  async function startBatch(filesValue: FileList | File[]) {
    const files = Array.from(filesValue).filter((file) => /\.(mp4|mov)$/i.test(file.name)).sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }))
    if (!files.length) { setError(locale === 'zh-CN' ? '请选择 MP4 或 MOV 文件。' : 'Choose MP4 or MOV files.'); return }
    setError(''); const job = await api<EpisodeBatchJob>(`/projects/${project.id}/episode-batches`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filenames: files.map((file) => file.name), job_type: 'upload', strategy: 'natural-order' }) }); setBatchJob(job)
    const entries = job.items.map((item, index) => ({ item, file: files[index], status: item.status, progress: item.progress } as LocalBatchFile)); setQueue(entries); await refresh()
    for (const entry of entries) await uploadBatchItem(job, entry)
    await refresh()
  }
  async function retry(entry: LocalBatchFile) { if (!batchJob) return; await uploadBatchItem(batchJob, { ...entry, status: 'queued' }); await refresh() }
  async function cancelQueued() { if (!batchJob) return; const waiting = queue.filter((entry) => entry.status === 'queued'); for (const entry of waiting) { updateLocal(entry.item.id, { status: 'cancelled', progress: 0 }); await markItem(batchJob.id, entry.item.id, 'cancelled', 0) } }
  async function addEpisode() { try { await api<Episode>(`/projects/${project.id}/episodes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); await refresh() } catch (reason) { setError((reason as Error).message) } }
  async function saveRename() { if (!renameTarget || !renameTitle.trim()) return; try { await api(`/episodes/${renameTarget.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: renameTitle.trim() }) }); setRenameTarget(null); await refresh() } catch (reason) { setError((reason as Error).message) } }
  async function confirmDelete() { try { for (const episode of deleteTargets) await api(`/episodes/${episode.id}`, { method: 'DELETE' }); if (deleteTargets.some((episode) => new URLSearchParams(location.search).get('episode_id') === episode.id)) navigate(`/project/${project.id}/episodes`); setDeleteTargets([]); setSelected(new Set()); await refresh() } catch (reason) { setError((reason as Error).message) } }
  async function enterEpisode(episode: Episode) { try { const updated = await api<Project>(`/projects/${project.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ active_episode_id: episode.id }) }); onChange(updated); navigate(flowRoute(project.id, 'upload', episode.id)) } catch (reason) { setError((reason as Error).message) } }
  async function uploadSingle(episode: Episode, file?: File) { if (!file) return; try { const metadata = await readEpisodeVideoMetadata(file); if (metadata.duration > 180) throw new Error(locale === 'zh-CN' ? '当前MVP单集支持3分钟以内，请分集上传。' : 'The current MVP supports episodes up to 3 minutes.'); const form = new FormData(); form.append('file', file); Object.entries(metadata).forEach(([key, value]) => { if (key !== 'thumbnail_data') form.append(key, String(value)) }); if (metadata.thumbnail_data) form.append('thumbnail_data', metadata.thumbnail_data); await api(`/episodes/${episode.id}/upload`, { method: 'POST', body: form }); await refresh() } catch (reason) { setError((reason as Error).message) } }
  async function runAnalysis(targets: Episode[], strategy = 'selected') { const analyzable = targets.filter((episode) => episode.video_filename); if (!analyzable.length) return; const job = await api<EpisodeBatchJob>(`/projects/${project.id}/episode-batches`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_type: 'analysis', strategy, episode_ids: analyzable.map((episode) => episode.id) }) }); setBatchJob(job); for (const item of job.items) { await markItem(job.id, item.id, 'processing', 20); try { await api(`/episodes/${item.episode_id}/analyze`, { method: 'POST' }); setBatchJob(await markItem(job.id, item.id, 'completed', 100)) } catch (reason) { setBatchJob(await markItem(job.id, item.id, 'failed', 100, (reason as Error).message)) } } await refresh(); setMessage(locale === 'zh-CN' ? '批量 Mock 分析已完成。' : 'Batch mock analysis completed.') }
  async function moveEpisode(episode: Episode, delta: number) { const index = episodes.findIndex((item) => item.id === episode.id); const other = episodes[index + delta]; if (!other) return; await Promise.all([api(`/episodes/${episode.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sort_order: other.sort_order ?? other.episode_number }) }), api(`/episodes/${other.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sort_order: episode.sort_order ?? episode.episode_number }) })]); await refresh() }
  const selectedEpisodes = episodes.filter((episode) => selected.has(episode.id)); const locked = project.characters.filter((character) => character.priority_level === 'Locked').length; const pending = project.characters.filter((character) => character.priority_level === 'Temporary').length; const conflicts = episodes.reduce((sum, episode) => sum + (episode.conflict_count || 0), 0)
  return <div className="tool-page"><ToolPageHeader title={roundTwoText(locale, 'episodes')} copy={locale === 'zh-CN' ? '项目级管理全部剧集。推荐先批量上传，再按单集进入处理。' : 'Manage every episode at project level. Batch upload first, then enter an episode for processing.'} icon={<ListVideo size={22} />} />
    <DemoNotice>{project.processing_mode === 'character-consistency' ? (locale === 'zh-CN' ? '先上传全部剧集；建议分析 Episode 01 或前 3 集建立初始角色库，后续仅处理新增和低置信度角色。' : 'Upload all episodes first. Analyze Episode 01 or the first three to seed the library, then review only new or low-confidence entities.') : (locale === 'zh-CN' ? 'Quick 模式默认单视频，不提供跨集角色一致性；仍可手动增加剧集。' : 'Quick mode is a single-video flow and does not claim cross-episode consistency.')}</DemoNotice>
    {error && <InlineError message={error} />}{message && <div className="toast-line"><Check size={15} /> {message}</div>}
    <div className="library-progress"><span>{locale === 'zh-CN' ? '已锁定角色' : 'Locked'} <strong>{locked}</strong></span><span>{locale === 'zh-CN' ? '待确认' : 'Pending'} <strong>{pending}</strong></span><span>{locale === 'zh-CN' ? '角色冲突' : 'Conflicts'} <strong>{conflicts}</strong></span></div>
    <div className={`episode-batch-drop ${dragging ? 'dragging' : ''}`} onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); void startBatch(event.dataTransfer.files) }}><UploadCloud size={22} /><div><strong>{locale === 'zh-CN' ? '批量上传剧集' : 'Batch upload episodes'}</strong><span>{locale === 'zh-CN' ? '拖拽或一次选择 60 个以上 MP4 / MOV，按文件名自然排序；单个失败不影响其他文件。' : 'Drop or select 60+ MP4/MOV files. Natural filename order; one failure does not stop the rest.'}</span></div><label className="primary-button compact"><Upload size={14} /> {locale === 'zh-CN' ? '选择多个文件' : 'Choose files'}<input hidden multiple type="file" accept="video/mp4,.mp4,.mov" onChange={(event) => event.target.files && void startBatch(event.target.files)} /></label></div>
    {batchJob && <section className="batch-queue"><div className="batch-summary"><strong>{batchJob.job_type === 'upload' ? (locale === 'zh-CN' ? '上传队列' : 'Upload queue') : (locale === 'zh-CN' ? '分析队列' : 'Analysis queue')}</strong><span>{batchJob.completed_count + batchJob.failed_count} / {batchJob.total_count} · {batchJob.failed_count} {locale === 'zh-CN' ? '失败' : 'failed'}</span><button className="text-button" onClick={cancelQueued}>{locale === 'zh-CN' ? '取消未开始项' : 'Cancel queued'}</button></div>{queue.length ? queue.map((entry) => <div className="batch-row" key={entry.item.id}><span>{entry.file.name}</span><div className="batch-progress"><i style={{ width: `${entry.progress}%` }} /></div><small>{entry.status}</small>{entry.status === 'failed' && <button className="text-button" onClick={() => retry(entry)}><RotateCcw size={12} /> {locale === 'zh-CN' ? '重试' : 'Retry'}</button>}{entry.error && <span className="batch-error" title={entry.error}>{entry.error}</span>}</div>) : batchJob.items.map((item) => <div className="batch-row" key={item.id}><span>{item.filename || item.episode_id}</span><div className="batch-progress"><i style={{ width: `${item.progress}%` }} /></div><small>{item.status}</small>{item.error_message && <span className="batch-error" title={item.error_message}>{item.error_message}</span>}</div>)}</section>}
    <div className="tool-page-actions"><button className="secondary-button" onClick={addEpisode}><Plus size={15} /> {locale === 'zh-CN' ? '添加单集' : 'Add one episode'}</button><button className="secondary-button" disabled={!selectedEpisodes.length} onClick={() => runAnalysis(selectedEpisodes)}><Gauge size={15} /> {locale === 'zh-CN' ? '分析所选' : 'Analyze selected'}</button><button className="secondary-button" onClick={() => runAnalysis(episodes.filter((episode) => episode.video_filename && episode.analysis_status !== 'completed'), 'all-unanalyzed')}><Gauge size={15} /> {locale === 'zh-CN' ? '分析全部未分析' : 'Analyze all pending'}</button><button className="danger-button" disabled={!selectedEpisodes.length} onClick={() => setDeleteTargets(selectedEpisodes)}><Trash2 size={15} /> {locale === 'zh-CN' ? '批量删除' : 'Delete selected'}</button></div>
    {!episodes.length ? <div className="empty-result">{locale === 'zh-CN' ? '暂无剧集。可批量上传或添加单集。' : 'No episodes. Batch upload or add one episode.'}</div> : <div className="episode-grid">{episodes.map((episode, index) => <article className="episode-card" key={episode.id}><div className="episode-select"><input type="checkbox" checked={selected.has(episode.id)} onChange={(event) => setSelected((current) => { const next = new Set(current); event.target.checked ? next.add(episode.id) : next.delete(episode.id); return next })} /></div><div className="episode-poster">{episode.video_filename ? <EpisodePoster episode={episode} onSaved={refresh} /> : <MockFrame label={`EP ${String(episode.episode_number).padStart(2, '0')}`} variant="source" />}</div><div className="episode-body"><div className="card-row"><div><span className="card-id">EPISODE {String(episode.episode_number).padStart(2, '0')}</span><h3>{episode.title}</h3></div><span className="priority-chip">{episodeDisplayStatus(episode, locale)}</span></div><div className="episode-meta"><span>{locale === 'zh-CN' ? '时长' : 'Duration'}: {episode.duration != null ? formatDuration(episode.duration) : (locale === 'zh-CN' ? '未上传' : 'Not uploaded')}</span><span>{locale === 'zh-CN' ? '匹配' : 'Matched'}: {episode.analysis_status === 'not_started' ? (locale === 'zh-CN' ? '未分析' : 'Not analyzed') : episode.matched_count || 0}</span><span>{locale === 'zh-CN' ? '新增' : 'New'}: {episode.new_character_count || 0}</span><span>{locale === 'zh-CN' ? '待确认' : 'Pending'}: {episode.pending_character_count || 0}</span><span>{formatDate(episode.updated_at)}</span></div><div className="episode-actions">{episode.video_filename ? <><button className="secondary-button compact" onClick={() => runAnalysis([episode])}><Gauge size={14} /> {locale === 'zh-CN' ? '分析本集' : 'Analyze'}</button><button className="primary-button compact" onClick={() => enterEpisode(episode)}>{locale === 'zh-CN' ? '进入本集处理' : 'Process this episode'} <ArrowRight size={14} /></button></> : <label className="primary-button compact"><Upload size={14} /> {locale === 'zh-CN' ? '上传本集' : 'Upload'}<input type="file" accept="video/mp4,.mp4,.mov" onChange={(event) => uploadSingle(episode, event.target.files?.[0])} /></label>}</div><div className="episode-menu"><button className="icon-button" disabled={index === 0} title={locale === 'zh-CN' ? '上移' : 'Move up'} onClick={() => moveEpisode(episode, -1)}><ArrowLeft size={14} /></button><button className="icon-button" disabled={index === episodes.length - 1} title={locale === 'zh-CN' ? '下移' : 'Move down'} onClick={() => moveEpisode(episode, 1)}><ArrowRight size={14} /></button><button className="icon-button" title={locale === 'zh-CN' ? '重命名' : 'Rename'} onClick={() => { setRenameTarget(episode); setRenameTitle(episode.title) }}><Save size={14} /></button><button className="icon-button danger-icon" title={locale === 'zh-CN' ? '删除剧集' : 'Delete episode'} onClick={() => setDeleteTargets([episode])}><Trash2 size={14} /></button></div></div></article>)}</div>}
    {renameTarget && <Modal title={locale === 'zh-CN' ? '重命名剧集' : 'Rename episode'} onClose={() => setRenameTarget(null)}><label className="field-label">{locale === 'zh-CN' ? '剧集名称' : 'Episode title'}<input autoFocus value={renameTitle} onChange={(event) => setRenameTitle(event.target.value)} onKeyDown={(event) => event.key === 'Enter' && void saveRename()} /></label><div className="modal-actions"><button className="quiet-button" onClick={() => setRenameTarget(null)}>{locale === 'zh-CN' ? '取消' : 'Cancel'}</button><button className="primary-button" onClick={saveRename}><Save size={15} /> {locale === 'zh-CN' ? '保存' : 'Save'}</button></div></Modal>}
    {!!deleteTargets.length && <Modal title={locale === 'zh-CN' ? '确认删除剧集' : 'Delete episodes'} onClose={() => setDeleteTargets([])}><p className="modal-copy">{locale === 'zh-CN' ? `将删除 ${deleteTargets.length} 集及其视频、分析、匹配、任务和结果文件。此操作不可撤销。` : `This deletes ${deleteTargets.length} episode(s) and related videos, analysis, matches, tasks, and results. This cannot be undone.`}</p><div className="modal-actions"><button className="quiet-button" onClick={() => setDeleteTargets([])}>{locale === 'zh-CN' ? '取消' : 'Cancel'}</button><button className="danger-button" onClick={confirmDelete}><Trash2 size={15} /> {locale === 'zh-CN' ? '确认删除' : 'Delete'}</button></div></Modal>}
  </div>
}

function LegacyEpisodesPage({ project, onChange }: { project: Project; onChange: (project: Project) => void }) {
  const navigate = useNavigate(); const location = useLocation(); const { locale, formatDate } = useI18n(); const [episodes, setEpisodes] = useState<Episode[]>(project.episodes || []); const [error, setError] = useState(''); const [message, setMessage] = useState('')
  useEffect(() => setEpisodes(project.episodes || []), [project.episodes])
  async function refresh() { const next = await api<Episode[]>(`/projects/${project.id}/episodes`); setEpisodes(next); onChange({ ...project, episodes: next }) }
  async function addEpisode() { setError(''); try { await api<Episode>(`/projects/${project.id}/episodes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) }); await refresh() } catch (e) { setError((e as Error).message) } }
  async function renameEpisode(episode: Episode) { const title = window.prompt(roundTwoText(locale, 'rename'), episode.title); if (!title?.trim()) return; try { await api(`/episodes/${episode.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: title.trim() }) }); await refresh() } catch (e) { setError((e as Error).message) } }
  async function duplicateSettings(episode: Episode) { try { const next = await api<Episode>(`/projects/${project.id}/episodes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) }); await api(`/episodes/${next.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ settings_override: episode.settings_override, character_handling: episode.character_handling }) }); await refresh(); setMessage(roundTwoText(locale, 'duplicate')) } catch (e) { setError((e as Error).message) } }
  async function removeEpisode(episode: Episode) { if (!window.confirm(`${roundTwoText(locale, 'deleteEpisode')}?`)) return; try { await api(`/episodes/${episode.id}`, { method: 'DELETE' }); if (new URLSearchParams(location.search).get('episode_id') === episode.id) navigate(`/project/${project.id}/episodes`); await refresh() } catch (e) { setError((e as Error).message) } }
  async function uploadEpisode(episode: Episode, file?: File) { if (!file) return; const form = new FormData(); form.append('file', file); try { const metadata = await readEpisodeVideoMetadata(file); if (metadata.duration > 180) throw new Error('当前MVP单集支持3分钟以内，请分集上传。'); Object.entries(metadata).forEach(([key, value]) => { if (key !== 'thumbnail_data') form.append(key, String(value)) }); if (metadata.thumbnail_data) form.append('thumbnail_data', metadata.thumbnail_data); await api(`/episodes/${episode.id}/upload`, { method: 'POST', body: form }); await refresh() } catch (e) { setError((e as Error).message) } }
  async function analyzeEpisode(episode: Episode) { try { const response = await api<{ project: Project }>(`/episodes/${episode.id}/analyze`, { method: 'POST' }); onChange(response.project); setEpisodes(response.project.episodes || []); setMessage(roundTwoText(locale, 'mockMatch')) } catch (e) { setError((e as Error).message) } }
  return <div className="tool-page"><ToolPageHeader title={roundTwoText(locale, 'episodes')} copy={roundTwoText(locale, 'episodesCopy')} icon={<ListVideo size={22} />} />{project.processing_mode === 'character-consistency' ? <DemoNotice>{locale === 'zh-CN' ? '当前项目已启用角色一致性模式，后续剧集将优先复用已确认的角色资产。' : 'Character Consistency is enabled. Later episodes will prioritize confirmed project character assets.'}</DemoNotice> : <DemoNotice>{locale === 'zh-CN' ? 'Quick 模式不提供跨集角色一致性，可手动增加剧集。' : 'Quick mode does not provide cross-episode character consistency. You can still add episodes manually.'}</DemoNotice>}{error && <InlineError message={error} />}{message && <div className="toast-line"><Check size={15} /> {message}</div>}<div className="tool-page-actions"><button className="primary-button" onClick={addEpisode}><Plus size={16} /> {roundTwoText(locale, 'addEpisode')}</button></div><div className="episode-grid">{episodes.map((episode) => <article className="episode-card" key={episode.id}><div className="episode-poster">{episode.video_filename ? <EpisodePoster episode={episode} onSaved={refresh} /> : <MockFrame label={`EP ${String(episode.episode_number).padStart(2, '0')}`} variant="source" />}</div><div className="episode-body"><div className="card-row"><div><span className="card-id">EPISODE {String(episode.episode_number).padStart(2, '0')}</span><h3>{episode.title}</h3></div><StatusBadge status={episode.status} /></div><div className="episode-meta"><span>{roundTwoText(locale, 'duration')}: {episode.duration ? formatDuration(episode.duration) : (locale === 'zh-CN' ? '未读取' : 'Not read')}</span><span>{roundTwoText(locale, 'match')}: {episode.analysis_status === 'not_started' ? (locale === 'zh-CN' ? '未分析' : 'Not analyzed') : episode.matched_count ?? episode.matches?.length ?? 0}</span>{Boolean(episode.new_character_count) && <span>{locale === 'zh-CN' ? `新增临时角色: ${episode.new_character_count}` : `New temporary: ${episode.new_character_count}`}</span>}<span>{roundTwoText(locale, 'updated', { date: useI18n().formatDate(episode.updated_at) })}</span></div>{episode.video_filename ? <div className="episode-actions"><button className="secondary-button compact" onClick={() => analyzeEpisode(episode)}><Gauge size={14} /> {roundTwoText(locale, 'analyzeEpisode')}</button><button className="text-button" onClick={() => navigate(flowRoute(project.id, 'upload', episode.id))}>{roundTwoText(locale, 'open')} <ArrowRight size={14} /></button></div> : <label className="primary-button compact"><Upload size={14} /> {roundTwoText(locale, 'uploadEpisode')}<input type="file" accept="video/mp4,.mp4,.mov" onChange={(e) => uploadEpisode(episode, e.target.files?.[0])} /></label>}<div className="episode-menu"><button className="icon-button" title={roundTwoText(locale, 'rename')} onClick={() => renameEpisode(episode)}><Save size={14} /></button><button className="icon-button" title={roundTwoText(locale, 'duplicate')} onClick={() => duplicateSettings(episode)}><MoreHorizontal size={14} /></button><button className="icon-button danger-icon" title={roundTwoText(locale, 'deleteEpisode')} onClick={() => removeEpisode(episode)}><Trash2 size={14} /></button></div></div></article>)}</div></div>
}

function CharacterLibraryPage({ project, onChange }: { project: Project; onChange: (project: Project) => void }) {
  const { locale } = useI18n(); const [library, setLibrary] = useState(project.character_library); const [error, setError] = useState(''); const all = library?.characters || [...(library?.locked || []), ...(library?.temporary || []), ...(library?.background || [])]; const voices = project.voice_profiles || []
  useEffect(() => setLibrary(project.character_library), [project.character_library])
  async function refresh() { const next = await api<{ locked: Character[]; temporary: Character[]; background: Character[]; characters: Character[] }>(`/projects/${project.id}/character-library`); setLibrary(next); onChange({ ...project, character_library: next, characters: next.characters }) }
  async function action(character: Character, path: string, patch: Record<string, unknown> = {}) { try { if (!path) { await api(`/characters/${character.id}`, { method: 'DELETE' }); await refresh(); return } const updated = await api<Character>(`/characters/${character.id}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) }); await refresh(); return updated } catch (e) { setError((e as Error).message) } }
  async function update(character: Character, patch: Record<string, unknown>) { try { await api<Character>(`/characters/${character.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(characterPayload(character, patch)) }); await refresh() } catch (e) { setError((e as Error).message) } }
  async function addCharacter() { try { await api<Character>(`/projects/${project.id}/characters`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: locale === 'zh-CN' ? '\u65b0\u89d2\u8272' : 'New Character', priority_level: 'Temporary', identity_description: locale === 'zh-CN' ? '\u624b\u52a8\u6dfb\u52a0\u7684\u6f14\u793a\u89d2\u8272' : 'Manually added demo character' }) }); await refresh() } catch (e) { setError((e as Error).message) } }
  async function addLook(character: Character) { const name = window.prompt(roundTwoText(locale, 'addLook'), locale === 'zh-CN' ? '\u65b0\u9020\u578b' : 'New Look'); if (!name) return; try { await api<CharacterLook>(`/characters/${character.id}/looks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, description: name, clothing_description: name, is_default: !(character.looks || []).length }) }); await refresh() } catch (e) { setError((e as Error).message) } }
  function section(title: string, characters: Character[], tone: string) { return <section className={`library-section ${tone}`}><div className="section-heading-inline"><div><span className="section-label">{title}</span><h2>{characters.length}</h2></div></div>{characters.length ? <div className="library-grid">{characters.map((character) => <article className="library-card" key={character.id}><div className="library-avatar"><MockAvatar index={all.findIndex((item) => item.id === character.id)} /></div><div className="library-card-main"><div className="card-row"><div><span className="card-id">{character.character_code || 'C--'} · {character.character_type || 'Human'}</span><h3>{character.name}</h3></div><span className={`priority-chip priority-${(character.priority_level || 'Temporary').toLowerCase()}`}>{character.priority_level || 'Temporary'}</span></div><p>{character.identity_description || character.appearance_description}</p><div className="library-meta"><span>{roundTwoText(locale, 'role')}: {character.role}</span><span>{roundTwoText(locale, 'assets')}: {(character.reference_assets || []).length}</span></div><label className="field-label">{roundTwoText(locale, 'voice')}<select value={character.voice_profile_id || ''} onChange={(e) => update(character, { voice_profile_id: e.target.value || null })}><option value="">{roundTwoText(locale, 'unbound')}</option>{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}</select></label><div className="reference-strip">{(character.reference_assets || []).map((asset) => <span key={asset.asset_type}>{asset.asset_type}</span>)}</div><div className="look-strip"><strong>{roundTwoText(locale, 'looks')}</strong>{(character.looks || []).map((look) => <span key={look.id}>{look.name}</span>)}<button className="text-button" onClick={() => addLook(character)}><Plus size={13} /> {roundTwoText(locale, 'addLook')}</button></div><div className="library-actions">{character.priority_level !== 'Locked' && <button className="secondary-button compact" onClick={() => action(character, '/upgrade')}><ShieldIcon /> {roundTwoText(locale, 'upgrade')}</button>}{character.priority_level === 'Locked' && <button className="secondary-button compact" onClick={() => update(character, { priority_level: 'Temporary', confirmed: false })}>{roundTwoText(locale, 'downgrade')}</button>}{!character.confirmed && <button className="primary-button compact" onClick={() => action(character, '/confirm')}>{roundTwoText(locale, 'confirm')}</button>}<button className="icon-button danger-icon" title={roundTwoText(locale, 'deleteCharacter')} onClick={() => { if (window.confirm(roundTwoText(locale, 'deleteCharacter'))) void action(character, '', {}) }}>{/* delete is handled below */}<Trash2 size={14} /></button></div></div></article>)}</div> : <div className="empty-result">{roundTwoText(locale, 'emptyLibrary')}</div>}</section> }
  async function removeViaDelete(character: Character) { try { await api(`/characters/${character.id}`, { method: 'DELETE' }); await refresh() } catch (e) { setError((e as Error).message) } }
  return <div className="tool-page"><ToolPageHeader title={roundTwoText(locale, 'library')} copy={project.processing_mode === 'character-consistency' ? (locale === 'zh-CN' ? '项目级核心角色库：后续剧集默认复用已锁定角色，只增量加入新的长期角色。' : 'Project core-character library: later episodes reuse locked roles and only add new long-term roles incrementally.') : (locale === 'zh-CN' ? 'Quick 模式不启用跨集角色库；这里的资产仅供当前项目中的单集处理使用。' : 'Quick mode does not enable cross-episode reuse; assets here are optional for individual episode work.')} icon={<Library size={22} />} /><DemoNotice>{project.processing_mode === 'character-consistency' ? (locale === 'zh-CN' ? `已锁定 ${library?.locked?.length || 0} / 5 个核心角色。普通路人和群演默认不入库。` : `${library?.locked?.length || 0} / 5 core roles locked. Background people and extras stay outside the library.`) : (locale === 'zh-CN' ? '未启用项目角色复用。' : 'Project character reuse is not enabled.')}</DemoNotice>{error && <InlineError message={error} />}<div className="tool-page-actions"><button className="primary-button" onClick={addCharacter}><Plus size={16} /> {tText(locale, 'characters.add')}</button></div><div className="library-sections">{section(roundTwoText(locale, 'locked'), library?.locked || [], 'locked')}{section(roundTwoText(locale, 'temporary'), library?.temporary || [], 'temporary')}</div><div className="demo-notice"><Sparkles size={15} /><span>{locale === 'zh-CN' ? '锁定只要求有效名称和一张原视频主参考图；备注与多视角图片均为可选。' : 'Locking requires a valid name and one primary source frame; notes and multi-view images are optional.'}</span></div></div>
}

function VoiceLibraryPage({ project, onChange }: { project: Project; onChange: (project: Project) => void }) {
  const { locale } = useI18n(); const [profiles, setProfiles] = useState<VoiceProfile[]>(project.voice_profiles || []); const [error, setError] = useState('')
  useEffect(() => {
    setProfiles(project.voice_profiles || [])
    const grid = document.querySelector<HTMLElement>('.voice-library-grid')
    if (!grid || grid.parentElement?.querySelector('.voice-expand-button')) return
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'secondary-button voice-expand-button'
    button.textContent = locale === 'zh-CN' ? '展开全部声音' : 'Show all voices'
    button.addEventListener('click', () => {
      const expanded = grid.classList.toggle('voice-library-expanded')
      button.textContent = expanded ? (locale === 'zh-CN' ? '收起声音' : 'Collapse voices') : (locale === 'zh-CN' ? '展开全部声音' : 'Show all voices')
    })
    grid.parentElement?.insertBefore(button, grid)
    return () => button.remove()
  }, [project.voice_profiles, locale])
  async function refresh() { const next = await api<VoiceProfile[]>(`/projects/${project.id}/voice-profiles`); setProfiles(next); onChange({ ...project, voice_profiles: next }) }
  async function addProfile(sourceType = 'system-preset') { try { await api<VoiceProfile>(`/projects/${project.id}/voice-profiles`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: sourceType === 'system-preset' ? 'Atlas' : sourceType === 'upload-sample' ? 'Uploaded Sample' : 'External Voice Demo', source_type: sourceType, status: sourceType === 'voice-clone' ? 'Demo / Coming Soon' : 'Mock Only', base_voice: 'Neutral', voice_age: 'Adult', voice_style: 'Calm' }) }); await refresh() } catch (e) { setError((e as Error).message) } }
  async function uploadSample(profile: VoiceProfile, file?: File) { if (!file) return; const form = new FormData(); form.append('file', file); try { await api(`/voice-profiles/${profile.id}/upload-sample`, { method: 'POST', body: form }); await refresh() } catch (e) { setError((e as Error).message) } }
  async function preview(profile: VoiceProfile) { try { await api(`/voice-profiles/${profile.id}/preview`); window.alert(locale === 'zh-CN' ? '\u5f53\u524d\u4e3a Mock \u8bd5\u542c\uff0c\u672a\u751f\u6210\u771f\u5b9e\u8bed\u97f3\u3002' : 'This is a local Mock preview. No real voice was generated.') } catch (e) { setError((e as Error).message) } }
  async function remove(profile: VoiceProfile) { if (!window.confirm(`${roundTwoText(locale, 'deleteVoice')}?`)) return; try { await api(`/voice-profiles/${profile.id}`, { method: 'DELETE' }); await refresh() } catch (e) { setError((e as Error).message) } }
  return <div className="tool-page"><ToolPageHeader title={roundTwoText(locale, 'voices')} copy={roundTwoText(locale, 'voiceCopy')} icon={<Volume2 size={22} />} />{error && <InlineError message={error} />}<DemoNotice>{roundTwoText(locale, 'sampleNotice')}</DemoNotice><div className="voice-source-row"><button className="secondary-button" onClick={() => addProfile('system-preset')}><Plus size={15} /> {roundTwoText(locale, 'system')}</button><button className="secondary-button" onClick={() => addProfile('external-tts')}><Plus size={15} /> {roundTwoText(locale, 'external')}</button><button className="secondary-button" onClick={() => addProfile('upload-sample')}><Upload size={15} /> {roundTwoText(locale, 'upload')}</button><button className="secondary-button" onClick={() => addProfile('voice-clone')}><Sparkles size={15} /> {roundTwoText(locale, 'clone')} · {roundTwoText(locale, 'cloneSoon')}</button></div><div className="voice-library-grid">{profiles.length ? profiles.map((profile) => <article className="voice-library-card" key={profile.id}><div className="card-row"><div><span className="card-id">{profile.source_type}</span><h3>{profile.name}</h3></div><span className="priority-chip priority-temporary">{profile.status}</span></div><div className="voice-profile-fields"><span>{roundTwoText(locale, 'provider')}: {profile.provider_id || roundTwoText(locale, 'noProvider')}</span><span>{roundTwoText(locale, 'model')}: {profile.model_id || '—'}</span><span>{roundTwoText(locale, 'voiceId')}: {profile.external_voice_id || profile.id}</span></div>{profile.uploaded_audio_path && <audio controls src={assetUrl(`/uploads/${profile.uploaded_audio_path.split(/[\\/]/).pop()}`)} />}{profile.source_type === 'upload-sample' && <label className="secondary-button compact"><Upload size={14} /> {roundTwoText(locale, 'uploadSample')}<input type="file" accept="audio/mpeg,audio/wav,audio/x-m4a,.mp3,.wav,.m4a" onChange={(e) => uploadSample(profile, e.target.files?.[0])} /></label>}<div className="voice-library-actions"><button className="secondary-button compact" onClick={() => preview(profile)}><Play size={14} /> {roundTwoText(locale, 'preview')}</button><button className="icon-button danger-icon" title={roundTwoText(locale, 'deleteVoice')} onClick={() => remove(profile)}><Trash2 size={14} /></button></div></article>) : <div className="empty-state">{roundTwoText(locale, 'emptyVoice')}</div>}</div></div>
}

function ProjectDefaultsPage({ project, onChange }: { project: Project; onChange: (project: Project) => void }) {
  const { locale } = useI18n(); const [defaults, setDefaults] = useState<ProjectDefaults>(project.defaults || { project_id: project.id, processing_mode: project.processing_mode || 'quick-transformation', style_id: project.settings.style_id, source_language: project.settings.source_language, target_language: project.settings.target_language, language_mode: project.settings.language_mode, subtitle_enabled: project.settings.subtitle_enabled, lip_sync_enabled: project.settings.lip_sync_enabled, character_consistency: project.settings.character_consistency, transformation_strength: project.settings.transformation_strength, scene_processing_mode: project.settings.scene_processing_mode, visual_scope: 'all-elements', character_appearance_policy: 'follow-original-outfit', use_character_library: Boolean(project.use_character_library) }); const [saving, setSaving] = useState(false); const [message, setMessage] = useState('')
  useEffect(() => { if (project.defaults) setDefaults(project.defaults) }, [project.defaults])
  async function save() { setSaving(true); setMessage(''); try { const updated = await api<Project>(`/projects/${project.id}/defaults`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...defaults, lip_sync_enabled: false }) }); setDefaults((current) => ({ ...current, lip_sync_enabled: false })); onChange(updated); setMessage(locale === 'zh-CN' ? '\u9879\u76ee\u9ed8\u8ba4\u503c\u5df2\u4fdd\u5b58' : 'Project defaults saved locally') } catch (e) { setMessage((e as Error).message) } finally { setSaving(false) } }
  const set = (key: keyof ProjectDefaults, value: string | boolean) => setDefaults((old) => ({ ...old, [key]: value }))
  return <div className="tool-page"><ToolPageHeader title={roundTwoText(locale, 'defaults')} copy={locale === 'zh-CN' ? '\u89d2\u8272\u4e00\u81f4\u6027\u9879\u76ee\u4f1a\u5c06\u8fd9\u4e9b\u8bbe\u7f6e\u4f5c\u4e3a\u540e\u7eed\u5267\u96c6\u7684\u9ed8\u8ba4\u503c\u3002' : 'Character Consistency projects inherit these defaults for later episodes.'} icon={<SlidersHorizontal size={22} />} />{message && <div className="toast-line"><Check size={15} /> {message}</div>}<section className="settings-section"><div className="control-grid"><label className="field-label">Style<select value={defaults.style_id} onChange={(e) => set('style_id', e.target.value)}>{['cinematic-realism', 'western-live-action', 'japanese-anime', 'korean-webtoon', 'american-comic', '3d-animation'].map((item) => <option key={item} value={item}>{catalogLabel(item, locale)}</option>)}</select></label><label className="field-label">Source language<select value={defaults.source_language} onChange={(e) => set('source_language', e.target.value)}><option value="auto">{catalogLabel('auto', locale)}</option><option value="mandarin">{catalogLabel('mandarin', locale)}</option><option value="english">{catalogLabel('english', locale)}</option></select></label><label className="field-label">Target language<select value={defaults.target_language} onChange={(e) => set('target_language', e.target.value)}><option value="english">{catalogLabel('english', locale)}</option><option value="mandarin">{catalogLabel('mandarin', locale)}</option><option value="japanese">{catalogLabel('japanese', locale)}</option></select></label></div><div className="control-grid"><SettingSelect label={locale === 'zh-CN' ? '\u89d2\u8272\u4e00\u81f4\u6027' : 'Character consistency'} value={defaults.character_consistency} options={[['standard', locale === 'zh-CN' ? '\u6807\u51c6' : 'Standard'], ['strong', locale === 'zh-CN' ? '\u5f3a' : 'Strong'], ['maximum', locale === 'zh-CN' ? '\u6700\u9ad8' : 'Maximum']]} onChange={(v) => set('character_consistency', v)} /><SettingSelect label={locale === 'zh-CN' ? '\u8f6c\u7ed8\u5f3a\u5ea6' : 'Transformation strength'} value={defaults.transformation_strength} options={[['low', locale === 'zh-CN' ? '\u4f4e' : 'Low'], ['medium', locale === 'zh-CN' ? '\u4e2d' : 'Medium'], ['high', locale === 'zh-CN' ? '\u9ad8' : 'High']]} onChange={(v) => set('transformation_strength', v)} /><SettingSelect label={roundTwoText(locale, 'appearance')} value={defaults.character_appearance_policy} options={[['face-hair', roundTwoText(locale, 'faceHair')], ['face-hair-outfit', roundTwoText(locale, 'faceHairOutfit')], ['follow-original-outfit', roundTwoText(locale, 'originalOutfit')]]} onChange={(v) => set('character_appearance_policy', v)} /></div><div className="toggle-row"><Toggle label={locale === 'zh-CN' ? '\u5b57\u5e55' : 'Subtitles'} copy={locale === 'zh-CN' ? '\u9ed8\u8ba4\u542f\u7528' : 'Enabled by default'} checked={defaults.subtitle_enabled} onChange={(v) => set('subtitle_enabled', v)} /><Toggle label={roundTwoText(locale, 'lipSync')} copy={locale === 'zh-CN' ? '\u9ed8\u8ba4\u542f\u7528' : 'Enabled by default'} checked={defaults.lip_sync_enabled} onChange={(v) => set('lip_sync_enabled', v)} /></div><div className="step-footer"><span>{roundTwoText(locale, 'modeNotice')}</span><button className="primary-button" onClick={save} disabled={saving}>{saving ? tText(locale, 'saving') : tText(locale, 'save')} <Save size={15} /></button></div></section></div>
}

function ShieldIcon() { return <Check size={14} /> }
function tText(locale: 'zh-CN' | 'en', key: string) { if (key === 'saving') return locale === 'zh-CN' ? '\u4fdd\u5b58\u4e2d...' : 'Saving...'; if (key === 'characters.add') return locale === 'zh-CN' ? '\u6dfb\u52a0\u89d2\u8272' : 'Add character'; return locale === 'zh-CN' ? '\u4fdd\u5b58' : 'Save' }

export default App
