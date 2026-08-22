import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Check, LoaderCircle, RefreshCw } from 'lucide-react'
import {
  assetUrl,
  getXmaxThirtySecondScenePlan,
  getXmaxThirtySecondTask,
  getXmaxThirtySecondTasks,
  type XmaxThirtySecondScenePlan,
  type XmaxThirtySecondTask,
} from './api'
import { XMAX_SCENE_MAPPINGS_SAVED_EVENT } from './lib/providers/xmax/XmaxSceneReview'

export default function TransformUnitPlanSummary({ episodeId }: { episodeId?: string }) {
  const [plan, setPlan] = useState<XmaxThirtySecondScenePlan | null>(null)
  const [task, setTask] = useState<XmaxThirtySecondTask | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function load() {
    if (!episodeId) return
    setLoading(true)
    setError('')
    try {
      const [nextPlan, index] = await Promise.all([
        getXmaxThirtySecondScenePlan(episodeId, 0.35, true),
        getXmaxThirtySecondTasks(episodeId),
      ])
      const summary = index.active_candidates[0] || index.completed_results[0] || index.historical_tasks[0]
      const nextTask = summary ? await getXmaxThirtySecondTask(summary.task_id, episodeId) : null
      setPlan(nextPlan)
      setTask(nextTask)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setPlan(null)
    setTask(null)
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeId])

  useEffect(() => {
    const refreshAfterMappingSave = (event: Event) => {
      const detail = (event as CustomEvent<{ episodeId?: string }>).detail
      if (detail?.episodeId === episodeId) void load()
    }
    window.addEventListener(XMAX_SCENE_MAPPINGS_SAVED_EVENT, refreshAfterMappingSave)
    return () => window.removeEventListener(XMAX_SCENE_MAPPINGS_SAVED_EVENT, refreshAfterMappingSave)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeId])

  const mappingByUnit = useMemo(
    () => new Map((task?.segments || []).map((segment) => [segment.transform_unit_id, segment])),
    [task?.segments],
  )
  const effectivePlan = useMemo(() => {
    if (plan?.persisted) return { value: plan, source: 'scene-plan JSON' }
    if (task?.scene_plan?.base_shots?.length && task.scene_plan.transform_units?.length) {
      return { value: task.scene_plan, source: `任务 ${task.task_id}` }
    }
    return null
  }, [plan, task])

  if (!episodeId) return null
  return <section className="transform-unit-plan-summary">
    <div className="section-heading-inline">
      <div>
        <span className="section-label">SHOT PLAN</span>
        <h3>原镜头 → 细分单元 → 角色映射</h3>
      </div>
      <button className="icon-button" title="刷新镜头计划" disabled={loading} onClick={() => void load()}>
        {loading ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}
      </button>
    </div>
    {effectivePlan ? <>
      <div className="transform-unit-plan-counts">
        <div><span>原镜头数</span><strong>{effectivePlan.value.base_shot_count ?? effectivePlan.value.base_shots?.length}</strong></div>
        <div><span>细分单元数</span><strong>{effectivePlan.value.transform_unit_count ?? effectivePlan.value.transform_units?.length}</strong></div>
        <div><span>保存来源</span><strong><Check size={15} /> {effectivePlan.source}</strong></div>
      </div>
      <div className="transform-unit-plan-list">
        {(effectivePlan.value.transform_units || []).map((unit) => {
          const mapping = mappingByUnit.get(unit.transform_unit_id)
          return <div key={unit.transform_unit_id || unit.sequence}>
            <span>{unit.parent_shot_id || `base-shot-${unit.parent_sequence}`} → {unit.transform_unit_id}</span>
            <span>{unit.source_start.toFixed(2)}–{unit.source_end.toFixed(2)}s</span>
            <strong>{mapping
              ? mapping.preserve_original || mapping.use_source_for_merge
                ? '保留原画'
                : mapping.selected_character_id || mapping.shot_type
              : '尚未创建任务'}</strong>
            <span>{mapping
              ? mapping.mapping_status === 'manual_confirmed' && !mapping.output_url && (mapping.execution_status || mapping.status) === 'pending'
                ? '等待用户操作'
                : `${mapping.execution_status || mapping.status} · ${mapping.quality_status || '未审核'}`
              : '未执行'}</span>
            <span>{mapping?.output_url
              ? <a href={assetUrl(mapping.output_url)} target="_blank" rel="noreferrer">查看输出</a>
              : '无输出'}</span>
          </div>
        })}
      </div>
    </> : !loading && !error ? <p>尚未生成。当前Episode尚无可持久化的真实镜头分析结果。</p> : null}
    {error && <div className="inline-error"><AlertTriangle size={16} /><span>{error}</span></div>}
  </section>
}
