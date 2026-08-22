import { useEffect, useState } from 'react'
import { RefreshCw, Search, ShieldCheck, ShieldOff } from 'lucide-react'
import {
  inactivateAdminXmaxTemporaryKey,
  listAdminXmaxTemporaryKeys,
  reconcileAdminXmaxTemporaryKey,
  refreshAdminXmaxTemporaryKey,
  type XmaxTemporaryKeyRecord,
} from './api'

const PAGE_SIZE = 20
const messageOf = (reason: unknown) => (reason instanceof Error ? reason.message : String(reason)).split('\n')[0]
const shortSession = (value?: string | null) => value ? `${value.slice(0, 12)}...${value.slice(-6)}` : '未绑定'

export function XmaxTemporaryKeyAdminPanel() {
  const [records, setRecords] = useState<XmaxTemporaryKeyRecord[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [username, setUsername] = useState('')
  const [status, setStatus] = useState('')
  const [createdFrom, setCreatedFrom] = useState('')
  const [createdTo, setCreatedTo] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  async function load(nextPage = 1, append = false) {
    const query = new URLSearchParams({ page: String(nextPage), page_size: String(PAGE_SIZE) })
    if (username.trim()) query.set('username', username.trim())
    if (status) query.set('status', status)
    if (createdFrom) query.set('created_from', `${createdFrom}T00:00:00`)
    if (createdTo) query.set('created_to', `${createdTo}T23:59:59`)
    const result = await listAdminXmaxTemporaryKeys(`?${query}`)
    setRecords((current) => append ? [...current, ...result.items] : result.items)
    setTotal(result.total)
    setPage(nextPage)
  }

  useEffect(() => { void load().catch((reason) => setError(messageOf(reason))) }, [])

  async function run(record: XmaxTemporaryKeyRecord, action: 'refresh' | 'inactivate' | 'reconcile') {
    setBusy(`${action}:${record.id}`); setError('')
    try {
      if (action === 'refresh') await refreshAdminXmaxTemporaryKey(record.id)
      if (action === 'inactivate') await inactivateAdminXmaxTemporaryKey(record.id)
      if (action === 'reconcile') await reconcileAdminXmaxTemporaryKey(record.id)
      await load()
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy('')
    }
  }

  return <section className="admin-section xmax-temp-key-admin">
    <div className="section-heading-inline">
      <div><span className="section-label">XMAX SETTLEMENT</span><h3>Xmax 临时授权与积分结算</h3></div>
      <ShieldCheck size={18} />
    </div>
    <div className="admin-query-toolbar">
      <label>用户名<input value={username} onChange={(event) => setUsername(event.target.value)} /></label>
      <label>结算状态<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部</option><option value="pending_settlement">待结算</option><option value="settled">已结算</option><option value="settlement_conflict">结算冲突</option><option value="active">进行中</option><option value="failed">失败</option><option value="expired">已过期</option><option value="cancelled">已取消</option></select></label>
      <label>创建日期从<input type="date" value={createdFrom} onChange={(event) => setCreatedFrom(event.target.value)} /></label>
      <label>到<input type="date" value={createdTo} onChange={(event) => setCreatedTo(event.target.value)} /></label>
      <button className="secondary-button" onClick={() => void load()}><Search size={15} />查询</button>
    </div>
    {error && <div className="auth-error">{error}</div>}
    <div className="admin-ledger-list">
      {records.map((record) => <article key={record.id}>
        <div>
          <strong>{record.username || record.user_id} · {record.local_status}</strong>
          <span>{new Date(record.created_at).toLocaleString()} · Episode {record.episode_id} · Unit {record.unit_id}</span>
          <span>冻结 {record.points_limit} · 消费 {record.consumed_points} · 释放 {record.released_points ?? 0}</span>
          <small>Session <span title={record.session_uid || ''}>{shortSession(record.session_uid)}</span> · 临时 Key ····{record.key_last4 || '----'}</small>
        </div>
        <div>
          <button className="text-button" disabled={Boolean(busy)} onClick={() => void run(record, 'refresh')} title="查询官方状态"><RefreshCw size={15} /></button>
          <button className="text-button" disabled={Boolean(busy) || record.local_status === 'settled'} onClick={() => void run(record, 'reconcile')}>重新核对</button>
          <button className="text-button danger" disabled={Boolean(busy) || record.local_status === 'settled'} onClick={() => void run(record, 'inactivate')}><ShieldOff size={15} />失效并结算</button>
        </div>
      </article>)}
      {!records.length && <p className="billing-note">暂无符合条件的结算记录。</p>}
    </div>
    {records.length < total && <button className="text-button admin-load-more" onClick={() => void load(page + 1, true)}>查看更多</button>}
  </section>
}
